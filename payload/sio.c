/* sio.c - driver SIO del payload: il trasporto che in Fase 5 sostituisce il
 * socket Lua di mGBA.
 *
 * COSA CAMBIA E COSA NO
 * ---------------------
 * La mailbox NON si tocca. E' gia' il confine fra il payload e il mondo, il
 * payload non ha nessuna dipendenza dal Lua, e cambia solo CHI la drena: prima
 * lo script dell'emulatore, adesso questo file. Chi legge main.c non deve
 * accorgersi della differenza.
 *
 * MODALITA': MULTI-PLAYER A 16 BIT, NON NORMAL 32
 * ----------------------------------------------
 * CLAUDE.md diceva "Normal mode 32-bit, adattatore master" ed era sbagliato: il
 * firmware Celio implementa SOLO la modalita' Multi-Player (pio_master_mode.pio:
 * frame UART start + 16 bit + stop a 115200 su una sola linea SD, SC come
 * bus-busy e non come clock). L'adattatore fa da MASTER, il GBA da child.
 *
 * PERCHE' SI PUO' PRENDERE IL SIO SENZA LITIGARE COL GIOCO
 * -------------------------------------------------------
 * Verificato nella decomp, ed e' il punto su cui poggia tutto il file:
 *
 *   - `InitIntrHandlers` (main.c:294-312) chiude con
 *     `EnableInterrupts(INTR_FLAG_VBLANK)`: in overworld IE ha SOLO il bit
 *     VBlank. Il bit seriale e' spento.
 *   - Il gioco un handler seriale ce l'ha (`gIntrTable[1] = SerialIntr`,
 *     main.c:331), ma non lo esegue mai finche' quel bit resta giu'.
 *   - Il nostro hook gira PRIMA dell'handler del gioco (hook.S: payload_frame()
 *     e poi tail jump all'originale). Quindi possiamo gestire l'IRQ seriale e
 *     fare l'ACK del bit 7 di REG_IF per conto nostro: quando IntrMain legge
 *     `IF & IE`, quel bit e' gia' pulito e `SerialIntr` non viene chiamato.
 *
 * Il gioco si riprende il SIO quando entra davvero in una sessione link (Cable
 * Club, scambi). Li' il payload dorme gia' per conto suo, ma per sicurezza
 * SioShutdown() rimette i registri come li ha trovati.
 *
 * STATO (aggiornato il 2026-08-23; il paragrafo precedente diceva "niente di
 * questo e' mai girato" ed era fermo al 2026-07-31)
 * ---------------------------------------------------------------------------
 *   1. l'integrazione in main.c C'E': SioInit/SioShutdown/SioYield seguono il
 *      possesso della porta in payload_frame, SioTick/SioGuard girano al
 *      VBlank, SioOnSerialIrq dall'handler quando IF ha il bit 7,
 *      payload_drain dal tail jump di hook.S (tutto sotto PAYLOAD_WITH_SIO);
 *   2. build.ps1 -WithSio compila questo file (a -Os -fno-jump-tables);
 *   3. F-1 e F-2 sono nel firmware del Pico dal 2026-08-02 (F-3, F-4 dopo).
 * Questo driver ha parlato sul GBA fisico con la cartuccia vera dal
 * 2026-08-02 (226 parole/s) ed e' il trasporto delle sessioni via internet.
 * SIO_ZERO_IS_LOST resta come contatore: a F-2 fatta DEVE restare 0.
 */

/* Solo i tipi: NON game_types.h, che tirerebbe dentro game_syms.h e con esso i
 * 42 indirizzi del gioco. Qui non serve chiamare niente di Smeraldo, e cosi'
 * questo file compila identico anche nel banco di prova multiboot, dove un
 * gioco non c'e' proprio. */
#include "types.h"

/* --- registri SIO (GBATEK 4.0) ------------------------------------------- */
#define REG_SIOCNT      (*(volatile u16 *)0x04000128)
#define REG_SIOMLT_SEND (*(volatile u16 *)0x0400012A)
#define REG_SIOMULTI0   (*(volatile u16 *)0x04000120)
#define REG_RCNT        (*(volatile u16 *)0x04000134)
#define REG_IE          (*(volatile u16 *)0x04000200)
#define REG_IF          (*(volatile u16 *)0x04000202)

#define RCNT_SIO_MODE   0x0000  /* R/W mode spento: comanda SIOCNT           */

#define SIOCNT_BAUD_115200  0x0003
#define SIOCNT_MULTI        0x2000  /* bit 13-12 = 01: Multi-Player          */
#define SIOCNT_IRQ          0x4000
#define SIOCNT_START        0x0080  /* solo il master lo alza                */
#define SIOCNT_ID_MASK      0x0030  /* bit 5-4: chi siamo nella sessione     */
#define SIOCNT_ERROR        0x0040

#define IRQ_VBLANK      0x0001  /* bit 0 di IE/IF                            */
#define IRQ_SERIAL      0x0080  /* bit 7 di IE/IF                            */

/* --- framing -------------------------------------------------------------
 * Il canale trasporta parole da 16 bit senza confini di messaggio, quindi il
 * framing lo mettiamo noi. Un NetEvent e' 12 byte = 6 parole.
 *
 *   [SYNC][HEAD][... dati ...][CKSUM]
 *
 *   SYNC  = 0xA55A, scelto perche' non e' ne' 0x0000 (che il firmware scarta)
 *           ne' 0x7FFF (la parola di idle) ne' 0xFFFF (linea a riposo).
 *   HEAD  = tipo << 8 | numero di parole dati
 *   CKSUM = somma a 16 bit di HEAD e dei dati, negata: sommando tutto viene 0.
 *
 * Il ricevitore non si fida del SYNC da solo: riaggancia solo se anche la
 * lunghezza e' plausibile e il checksum torna. Su un canale che parte a meta'
 * (accendiamo l'adattatore quando vuole) e' l'unico modo di ritrovarsi.        */
#define SIO_SYNC        0xA55A
#define SIO_IDLE        0x7FFF

#define SIO_T_EVENT     0x01  /* un NetEvent: 6 parole                       */
#define SIO_T_STATE     0x02  /* blocco di PayloadState (diagnostica)        */
#define SIO_T_PING      0x03

/* Da 32 a 16 il 2026-08-02, per far entrare il driver nella coda di EWRAM.
 *
 * Non e' una limatura al buio: il frame piu' grande che il protocollo manda
 * davvero e' SIO_T_EVENT, cioe' un NetEvent = 6 parole, che sul cavo diventano
 * 9 (SYNC + intestazione + 6 + checksum). 8 lascia due parole di scorta sul
 * frame piu' grande reale; l'ipotetico SIO_T_STATE di diagnostica da 32 byte
 * (mai implementato) non ci starebbe piu' — se un giorno si fa, questo numero
 * va rialzato E il margine ricontrollato, la guardia di build.ps1 lo dira'.
 *
 * Da 16 a 8 il 2026-08-02, sera: 32 byte di .bss per fare posto al RAMMENDO
 * dei passi persi in main.c. Il de-framer di usb_link.py accetta fino a 32
 * parole, quindi il taglio qui non cambia il contratto sul filo. */
#define SIO_MAX_WORDS   8

/* Il firmware, cosi' com'e' oggi, SCARTA le parole 0x0000 in TX
 * (rawRelay.cpp). Finche' F-2 non e' fatto, ogni parola nulla che spediamo
 * sparisce e il frame arriva corrotto. Non lo aggiriamo con un escape, che
 * complicherebbe entrambi i lati: lo CONTIAMO, cosi' il log dice esattamente
 * quanto costa non aver fatto F-2 invece di lasciare un difetto muto. */
#define SIO_ZERO_IS_LOST 1

/* --- stato --------------------------------------------------------------- */
enum { RX_WAIT_SYNC, RX_WAIT_HEAD, RX_DATA, RX_CKSUM };

struct SioState {
    u8  active;
    u8  rxPhase;
    u8  rxType;
    u8  rxCount;      /* parole dati attese                                  */
    u8  rxGot;
    u16 rxSum;
    u16 rxBuf[SIO_MAX_WORDS];

    u16 txBuf[SIO_MAX_WORDS + 3];
    u8  txLen;
    u8  txPos;
};

static struct SioState sSio;

static u16 SioPeekWord(void);


/* Contatori. Vale la regola del progetto: un contatore che resta a zero e' un
 * difetto da indagare, non un dettaglio. Li legge il log alla riga [sio ]. */
struct SioCounters {
    u32 irqs;         /* IRQ seriali gestiti                                 */
    u32 wordsTx;
    u32 wordsRx;
    u32 framesTx;
    u32 framesRx;
    u32 frameErr;     /* checksum sbagliato o lunghezza assurda              */
    u32 resync;       /* quante volte si e' perso e ritrovato il SYNC        */
    u32 txFull;       /* frame non spedito perche' il buffer era occupato    */
    u32 rxFull;       /* frame ricevuto e buttato perche' la mailbox e' piena*/
    u32 zeroWords;    /* parole 0x0000 spedite: se sale, manca F-2           */
    u32 errFlag;      /* SIOCNT segnalava errore                             */
    u32 repairs;      /* la guardia ha trovato i registri cambiati sotto     */
    u32 drained;      /* IRQ seriali raccolti prima del tail jump (la falla) */
    u32 ieScrubs;     /* uscite dalla porta con bit ALTRUI accesi in IE:
                       * sale a ogni entrata in lotta (li' il gioco arma
                       * VCount e HBlank). Se resta a ZERO vuol dire che
                       * SioShutdown non gira piu' dove deve, e il difetto
                       * dell'audio del 2026-08-30 puo' tornare.            */
};

struct SioCounters g_sio;

/* --- ciclo di vita ------------------------------------------------------- */

/* Da chiamare una volta sola, quando il payload si aggancia. Salva lo stato dei
 * registri per poterlo rimettere: se il giocatore entra nel Cable Club il SIO
 * torna al gioco esattamente come l'aveva lasciato. */
/* NOTA sull'azzeramento del .bss, perche' c'e' stato e non c'e' piu'.
 *
 * Quando questo stato viveva in IWRAM (fuori dalla regione che il caricatore
 * azzera) SioInit se lo azzerava da solo, per non dipendere in silenzio da chi
 * lo carica. Ora che e' tornato in EWRAM dentro il payload, quel codice e'
 * ridondante per costruzione: OGNI caricatore che abbiamo azzera l'intera
 * regione riservata prima di scrivere il payload - zeroRegion() in
 * mgba/inject_body.lua e il zero32() in hw/mbstub/main.c. Costava 28 byte su un
 * margine che serve tutto. */
/* L'UNICO POSTO CHE ARMA LA SERIALE. Ci arrivano in quattro - l'avvio, il
 * watchdog del silenzio, la guardia dei registri e il rilascio del freno
 * anti-raffica - e prima ognuno aveva la sua copia della stessa sequenza:
 * quattro occasioni di divergere alla prossima correzione, oltre che byte
 * sprecati in una coda di EWRAM dove si contano. */
static __attribute__((noinline)) void SioArm(void)
{
    REG_RCNT   = RCNT_SIO_MODE;
    REG_SIOCNT = SIOCNT_MULTI | SIOCNT_BAUD_115200 | SIOCNT_IRQ;
    REG_IE    |= IRQ_SERIAL;
    sSio.rxPhase = RX_WAIT_SYNC;
    REG_SIOMLT_SEND = SioPeekWord();
}

void SioInit(void)
{
    /* Niente SIOCNT_START: lo alza il master, e il master e' l'adattatore. */
    sSio.txLen = 0;
    sSio.txPos = 0;
    sSio.active = 1;
    SioArm();
}

/* SI SPEGNE QUELLO CHE ABBIAMO ACCESO NOI, NON SI "RIMETTE COM'ERA"
 * (2026-08-30, il difetto dell'audio in lotta).
 *
 * Qui c'era un ripristino da fotografia: SioInit salvava REG_IE / REG_SIOCNT /
 * REG_RCNT e SioShutdown li riscriveva INTERI. Sembra prudente e invece e' la
 * cosa piu' pericolosa che si possa fare a REG_IE, per tre ragioni che si
 * sommano:
 *
 *  1. REG_IE non e' la fonte di verita' del gioco. Lo e' `sRegIE` in
 *     gpu_regs.c, e REG_IE viene risincronizzato SOLO dentro
 *     EnableInterrupts/DisableInterrupts. Una nostra scrittura grezza resta
 *     quindi in piedi finche' il gioco non ricapita per caso in quelle due
 *     funzioni: in lotta puo' voler dire per tutta la lotta.
 *  2. Il bit che ci giochiamo e' quello del VCount, e sul VCount gira
 *     m4aSoundVSync (main.c:388), che riavvia i DMA del suono. Spento quello,
 *     il FIFO continua a rileggere il buffer vecchio: musica sfasciata
 *     dall'inizio alla fine della lotta. Con l'HBlank spento si congelano
 *     invece gli effetti a scanline dell'animazione d'ingresso.
 *  3. La fotografia era sbagliata FIN DAL PRIMO SCATTO e si auto-manteneva.
 *     Col multiboot il primo SioInit cade prima di EnableVCountIntrAtLine150
 *     (AgbMain), quando REG_IE vale 0x0001: solo VBlank. E il SioInit
 *     successivo rileggeva... quello che avevamo appena scritto noi. Da qui
 *     il "dopo un po' si rompe e poi resta rotto" riferito dal campo, anche
 *     senza mai passare dal Cable Club.
 *
 * La regola nuova e' quella che vale per chiunque condivida dei registri: si
 * tocca un bit solo, il proprio, e si lascia stare tutto il resto.
 *
 * E SIOCNT esce SENZA il bit di IRQ. Prima "ripristinavamo" una seriale che
 * poteva essere armata (EnableSerial del gioco scrive la NOSTRA identica
 * configurazione, link.c:1868, e nemmeno SioGuard sa distinguerle): con
 * l'IRQ acceso e il payload addormentato, ogni parola battuta dall'adattatore
 * - 226 al secondo - entrava in SerialIntr, chiamava SerialCB e svegliava la
 * macchina di link del gioco, che arma il Timer3 a 1331 Hz. Quello era il
 * "rallentamento" che accompagnava la musica rotta. */
void SioShutdown(void)
{
    if (!sSio.active)
        return;
    /* La contro-prova che questa funzione serve: se in IE c'e' qualcosa che
     * non e' nostro, il vecchio ripristino l'avrebbe cancellato. */
    if (REG_IE & (u16)~(IRQ_VBLANK | IRQ_SERIAL))
        g_sio.ieScrubs++;
    REG_IE    &= (u16)~IRQ_SERIAL;
    REG_SIOCNT = SIOCNT_MULTI | SIOCNT_BAUD_115200;   /* senza SIOCNT_IRQ */
    REG_RCNT   = RCNT_SIO_MODE;
    sSio.active = 0;
}

/* La RESA al link del gioco (Consegna C, 2026-08-09). Diversa da SioShutdown
 * per una ragione di ORDINE: OpenLink (link.c:367) prima riconfigura la
 * seriale con ResetSerial e POI alza gLinkCallback - cioe' quando il payload
 * si accorge che il gioco vuole il cavo, i registri sono GIA' del gioco.
 * Il restore di SioShutdown ci scriverebbe sopra la nostra configurazione
 * stantia, rompendo l'handshake del Cable Club. Qui si molla e basta:
 * sSio.active a zero fa uscire subito SioOnSerialIrq, l'IRQ seriale torna a
 * SerialIntr del gioco, e i registri restano come il gioco li ha messi. */
void SioYield(void)
{
    sSio.active = 0;
}

/* --- trasmissione -------------------------------------------------------- */

/* La parola che uscirebbe adesso, SENZA consumarla. Serve alla guardia: chi
 * ripara i registri deve poter ricaricare SIOMLT_SEND senza rubare un pezzo
 * del frame che sta partendo. */
static __attribute__((noinline)) u16 SioPeekWord(void)
{
    if (sSio.txPos < sSio.txLen)
        return sSio.txBuf[sSio.txPos];
    return SIO_IDLE;
}

static u16 SioNextWord(void)
{
    u16 w;
    if (sSio.txPos < sSio.txLen) {
        w = sSio.txBuf[sSio.txPos++];
        if (sSio.txPos >= sSio.txLen) {
            sSio.txLen = 0;
            sSio.txPos = 0;
            g_sio.framesTx++;
        }
        g_sio.wordsTx++;
#if SIO_ZERO_IS_LOST
        if (w == 0)
            g_sio.zeroWords++;
#endif
        return w;
    }
    return SIO_IDLE;
}

/* Prepara un frame. Ritorna 0 se il buffer e' ancora occupato: chi chiama
 * decide se riprovare al giro dopo o perdere l'evento (e contarlo). */
int SioSendFrame(u8 type, const u16 *words, u8 count)
{
    u8 i;
    u16 head, sum;

    if (!sSio.active || count > SIO_MAX_WORDS)
        return 0;
    if (sSio.txLen != 0) {
        g_sio.txFull++;
        return 0;
    }

    head = (u16)((type << 8) | count);
    sum = head;

    sSio.txBuf[0] = SIO_SYNC;
    sSio.txBuf[1] = head;
    for (i = 0; i < count; i++) {
        sSio.txBuf[2 + i] = words[i];
        sum = (u16)(sum + words[i]);
    }
    sSio.txBuf[2 + count] = (u16)(-sum);   /* somma totale = 0 */
    sSio.txLen = (u8)(count + 3);
    sSio.txPos = 0;
    return 1;
}

/* --- ricezione ----------------------------------------------------------- */

/* Chi consuma un frame completo. Definita in main.c, che sa dov'e' la mailbox:
 * qui non si vuole sapere niente della struttura degli eventi. Ritorna 0 se non
 * c'e' posto (e allora si conta rxFull). */
extern int SioDeliverFrame(u8 type, const u16 *words, u8 count);

static void SioRxWord(u16 w)
{
    g_sio.wordsRx++;

    switch (sSio.rxPhase) {
    case RX_WAIT_SYNC:
        if (w == SIO_SYNC)
            sSio.rxPhase = RX_WAIT_HEAD;
        /* Idle e rumore si ignorano in silenzio: la linea passa piu' tempo a
         * riposo che a parlare, contarli sarebbe rumore anche nel log. */
        break;

    case RX_WAIT_HEAD: {
        u8 type = (u8)(w >> 8);
        u8 count = (u8)(w & 0xFF);
        if (count > SIO_MAX_WORDS || type == 0) {
            /* Il SYNC era un caso: si ricomincia a cercarlo. */
            g_sio.frameErr++;
            g_sio.resync++;
            sSio.rxPhase = RX_WAIT_SYNC;
            break;
        }
        sSio.rxType = type;
        sSio.rxCount = count;
        sSio.rxGot = 0;
        sSio.rxSum = w;
        sSio.rxPhase = count ? RX_DATA : RX_CKSUM;
        break;
    }

    case RX_DATA:
        sSio.rxBuf[sSio.rxGot++] = w;
        sSio.rxSum = (u16)(sSio.rxSum + w);
        if (sSio.rxGot >= sSio.rxCount)
            sSio.rxPhase = RX_CKSUM;
        break;

    case RX_CKSUM:
        if ((u16)(sSio.rxSum + w) != 0) {
            g_sio.frameErr++;
            g_sio.resync++;
        } else {
            g_sio.framesRx++;
            if (!SioDeliverFrame(sSio.rxType, sSio.rxBuf, sSio.rxCount))
                g_sio.rxFull++;
        }
        sSio.rxPhase = RX_WAIT_SYNC;
        break;
    }
}

/* --- l'IRQ --------------------------------------------------------------- */

/* Da chiamare dall'handler del payload quando REG_IF ha il bit 7.
 *
 * ATTENZIONE, ed e' il motivo per cui questa funzione esiste invece di lasciar
 * fare al gioco: l'ACK del bit seriale lo facciamo QUI. Il nostro hook gira
 * prima di IntrMain, quindi quando il gioco legge `IF & IE` quel bit e' gia'
 * pulito e `SerialIntr` (gIntrTable[1]) non viene mai chiamato. Se si togliesse
 * questa riga, il gioco si troverebbe a gestire un IRQ seriale che non ha
 * chiesto, con `gMain.serialCallback` a NULL. */
void SioOnSerialIrq(void)
{
    u16 cnt;

    if (!sSio.active)
        return;

    g_sio.irqs++;

    cnt = REG_SIOCNT;
    if (cnt & SIOCNT_ERROR)
        g_sio.errFlag++;

    /* In Multi-Player il master finisce nel proprio slot e i child a seguire.
     * L'adattatore e' il master, quindi la sua parola sta in SIOMULTI0. */
    SioRxWord(REG_SIOMULTI0);

    /* Carichiamo subito la prossima parola: il master puo' ripartire appena
     * vuole, e trovare il registro gia' pronto evita di perdere uno scambio. */
    REG_SIOMLT_SEND = SioNextWord();

    REG_IF = IRQ_SERIAL;
}

/* --- rete di sicurezza --------------------------------------------------- */

/* Da chiamare dal VBlank. Non serve a trasmettere - lo fa l'IRQ - ma a
 * accorgersi se l'IRQ ha smesso di arrivare, che su hardware e' il modo piu'
 * probabile di rompersi: cavo staccato, adattatore riavviato, master fermo.
 * Senza questo il payload resterebbe muto senza dirlo a nessuno. */
void SioTick(void)
{
    static u32 lastIrqs;
    static u16 idleFrames;

    if (!sSio.active)
        return;

    if (g_sio.irqs != lastIrqs) {
        lastIrqs = g_sio.irqs;
        idleFrames = 0;
        return;
    }

    if (idleFrames < 0xFFFF)
        idleFrames++;

    /* Due secondi di silenzio: riarmo COMPLETO, non piu' solo la parola di
     * uscita (2026-08-15). Il gioco puo' spegnere la seriale sotto di noi
     * senza che il payload lo veda: uscire da un Centro Pokemon chiama
     * CloseLink() -> DisableSerial() (overworld.c:1758, link.c:1857), che
     * azzera il bit IE seriale e riconfigura SIOCNT. Con la porta tenuta
     * anche nelle transizioni, questa e' la rete che ricuce QUALUNQUE
     * calpestio: il master batte a 226 parole/s, quindi 2 s di silenzio con
     * sSio.active alzato possono voler dire solo che i registri non sono
     * piu' i nostri. Si riprova ogni 2 s, non una volta sola: il contatore
     * resync sale a ogni riarmo, e la sonda lo porta fuori (w4, bit 12-15). */
    if (idleFrames >= 120) {
        idleFrames = 0;
        /* Durante tregua o freno il silenzio e' VOLUTO: riarmare qui
         * riaprirebbe la guerra che quei due meccanismi hanno appena chiuso. */
        SioArm();
        g_sio.resync++;
    }
}


/* LA GUARDIA (2026-08-16). Il watchdog qui sopra reagisce al SILENZIO, e per
 * accorgersene deve aspettare 2 secondi: entrando in un edificio il canale
 * moriva, tornava per una manciata di millisecondi al riarmo e rimoriva,
 * ciclicamente. Contro un guastatore che scrive i registri di continuo, il
 * riarmo lento perde sempre.
 *
 * Questa invece guarda i REGISTRI, non il traffico: se la configurazione non
 * e' piu' la nostra la si rimette SUBITO, nello stesso frame. Si chiama due
 * volte per frame - dal VBlank (prima che giri il gioco) e dal main loop
 * (dopo) - proprio perche' non sappiamo in quale dei due momenti il gioco
 * scriva: qualunque sia l'ordine, una delle due passate arriva dopo di lui.
 *
 * Si confrontano solo i bit di CONFIGURAZIONE: in Multi-Player i bit 0-5 (id
 * della sessione), 6 (errore) e 7 (busy) li muove l'hardware a ogni scambio,
 * e confrontarli farebbe scattare la guardia sessanta volte al secondo su un
 * canale sanissimo. Il contatore `repairs` e' la misura del guastatore: se
 * sale di ~60/s dentro le case, e' qualcosa che gira a ogni frame del gioco;
 * se sale di 1 a ogni ingresso, e' una scrittura una tantum del caricamento
 * mappa. La differenza dice dove guardare, e la porta fuori la sonda. */
#define SIOCNT_CFG_MASK  0x7003u   /* modo (13-12), IRQ (14), baud (1-0)     */

/* LA GUARDIA. Rimette la configurazione seriale se qualcuno l'ha cambiata
 * sotto di noi. Con la falla dell'hook chiusa (vedi payload_drain) questa
 * dovrebbe scattare RARAMENTE: se il contatore `repairs` continua a salire di
 * qualche unita' al secondo, vuol dire che c'e' ancora un guastatore, e non e'
 * la macchina di link del gioco.
 *
 * La TREGUA che stava qui (mollare la presa dopo 20 riparazioni di fila) e'
 * stata TOLTA il 2026-08-16: nel log del campo `TREGUE` e' rimasto 0 mentre
 * le riparazioni salivano a ~2,6/s, cioe' non scattava mai perche' le
 * riparazioni erano sparse e non consecutive. Era la cura di una diagnosi
 * sbagliata - "il gioco reclama la porta nei Centri" - e la diagnosi vera si
 * e' rivelata un'altra: eravamo NOI a svegliargli la macchina di link. */


void SioGuard(void)
{
    u16 want;

    if (!sSio.active)
        return;

    want = SIOCNT_MULTI | SIOCNT_BAUD_115200 | SIOCNT_IRQ;

    if ((REG_SIOCNT & SIOCNT_CFG_MASK) == (want & SIOCNT_CFG_MASK)
        && (REG_RCNT & 0xC000u) == (RCNT_SIO_MODE & 0xC000u)
        && (REG_IE & IRQ_SERIAL) != 0)
        return;

    SioArm();
    /* La parola di uscita va ricaricata: chi ci ha riconfigurato la seriale
     * quasi certamente ha azzerato anche SIOMLT_SEND (lo fa DisableSerial,
     * link.c:1857). Si SBIRCIA e non si consuma: SioNextWord() avanza il
     * puntatore di trasmissione, quindi la versione precedente si mangiava
     * una parola del frame in uscita a ogni riparazione - il frame arrivava
     * monco e il de-framer lo buttava. */
    g_sio.repairs++;
}

u32 SioRepairCount(void)
{
    return g_sio.repairs;
}

u32 SioScrubCount(void)
{
    return g_sio.ieScrubs;
}

/* Raccoglie l'IRQ seriale caduto mentre il payload lavorava. La chiama
 * hook.S subito PRIMA del tail jump all'handler del gioco, e il perche' e'
 * spiegato la' per esteso: se un bit seriale resta in sospeso, IntrMain lo
 * vede, chiama SerialCB, e la macchina di link del gioco si sveglia con dati
 * che non sono suoi.
 *
 * Un `while` e non un `if`: se il nostro giro e' stato lungo puo' essercene
 * piu' d'uno, e basta lasciarne UNO perche' il guaio succeda. Il tetto a 4
 * giri e' contro un IF incastrato, e tiene anche limitato per costruzione il
 * tempo che sottraiamo al gioco dentro un IRQ. */
void payload_drain(void)
{
    u32 giri = 0;
    while (sSio.active && (REG_IF & IRQ_SERIAL) && giri++ < 4)
    {
        g_sio.drained++;
        SioOnSerialIrq();
    }
}

/* Chiamata dal VBlank prima del cancello anti-raffica. Oggi non fa piu'
 * niente: il FRENO che stava qui (spegnere il nostro IRQ se ne arrivavano
 * piu' di 32 fra due VBlank) e' stato tolto il 2026-08-16 perche' nel log del
 * campo non e' MAI scattato e la misura diceva 3,1 IRQ per frame, cioe' il
 * ritmo normale del canale: non c'era nessuna tempesta da frenare. La classe
 * di guasto che copriva - noi che affamiamo la CPU del gioco - resta chiusa
 * per costruzione dal tetto di 4 giri dentro payload_drain(). */

/* SioFillDiag (i registri veri per la pagina 2 della sonda) e' stata tolta
 * il 2026-08-25 insieme alla pagina 2 in main.c: bilancio EWRAM dei 4
 * giocatori. Storia e motivazione nel commento al posto della pagina 2. */
