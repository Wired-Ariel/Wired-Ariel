/* =============================================================================
 * main.c - corpo del payload
 * =============================================================================
 *
 * FASE 1 (dimostrata): l'hook sul vettore IRQ regge ai cambi di scena, ai menu
 *                      e alle lotte.
 * FASE 2 (dimostrata): un secondo allenatore spawnato e animato dal motore, che
 *                      il gioco tratta come un NPC qualsiasi.
 * FASE 3 (dimostrata): lettura dello stato del giocatore locale e coda di eventi
 *                      verso l'esterno.
 * FASE 4 (questa versione): il remoto smette di camminare in quadrato e obbedisce
 *                      agli eventi che arrivano dall'esterno. La mailbox diventa
 *                      bidirezionale: TX quello che faccio io, RX quello che fa
 *                      l'amico.
 *
 * Il payload non reimplementa niente: chiama le routine originali del gioco
 * (indirizzi in game_syms.h, generati dal .map) e riusa uno sprite gia' in ROM.
 *
 * Le due regole architetturali che valgono anche qui (vedi CLAUDE.md):
 *   - si accodano MOVEMENT ACTION, non si scrivono le coordinate: cosi' il
 *     motore regala animazione del passo, interpolazione sub-pixel e timing;
 *   - si sincronizzano EVENTI, non stati: "passo iniziato, direzione D", piu'
 *     una posizione assoluta ogni secondo come correzione.
 *
 * IL CAVEAT CHE C'ERA QUI SI E' AVVERATO, ED E' CHIUSO (2026-08-21).
 * Per mesi il corpo del payload e' girato in modo IRQ, con la nota "se il
 * frame e' lungo e il main loop non ha ancora raggiunto VBlankIntrWait, la
 * corsa esiste". E' esistita: il Surf dell'amico scriveva gFieldEffectArguments
 * dall'IRQ in mezzo al Surf del giocatore, e il gioco leggeva gPlayerParty[x]
 * fuori squadra (vedi CreateRemoteSurfBlob). L'audit del salvataggio ha poi
 * contato tutto cio' che l'IRQ toccava del gioco: CreateSprite, AllocSpriteTiles,
 * LoadPalette, gObjectEvents, gSprites - nessuno scrive nel save, ma ognuno e'
 * la stessa corsa in potenza (sprite doppi, tile altrui, NPC che cambiano
 * aspetto).
 *
 * REGOLA DA ALLORA: dall'IRQ (payload_frame) si leggono contatori, si fa il
 * SIO, si campiona lo stato e si tiene lo scudo del salvataggio. TUTTO cio'
 * che crea, muove o distrugge object event e sprite gira in OverworldTick,
 * chiamata da payload_cb1 dal main loop del gioco, PRIMA del callback1 del
 * gioco: in sequenza con esso, quindi senza nessuna corsa possibile.
 */

#include "game_types.h"

#define REG_IF      (*(volatile u16 *)0x04000202)
/* REG_IE si LEGGE soltanto, e solo mentre il payload e' in resa al link del
 * gioco: il bit serial spento e' la prova che CloseLink -> DisableSerial e'
 * passato di li' (link.c:1857). Vedi il latch del linkBusy. */
#define REG_IE      (*(volatile u16 *)0x04000200)
/* REG_IME: solo per la sezione critica di EmitEvent/EmitCardChunk, che dal
 * 2026-08-21 hanno due produttori (il corpo dal main loop e il VBlank per
 * STATO fuori dall'overworld e CLUB). */
#define REG_IME     (*(volatile u16 *)0x04000208)
#define IRQ_SERIAL_BIT  0x0080u
#define IRQ_VBLANK  (1 << 0)

#define STATE_IDLE     0u   /* nessun remoto sulla mappa            */
#define STATE_SPAWNED  1u   /* remoto vivo                          */

/* Quanti VBlank aspettare dopo essere entrati nell'overworld prima di spawnare.
 * Serve a far assestare la mappa: appena cambiata, gli object event della mappa
 * non sono ancora tutti popolati e prenderemmo uno slot che serve a loro. */
#define SPAWN_DELAY_FRAMES  90u

/* Attesa fra due tentativi di spawn falliti. */
#define SPAWN_RETRY_FRAMES  60u

/* Scostamento oltre il quale una correzione assoluta non si assorbe camminando
 * ma si applica di forza (respawn nel punto giusto). */
#define DRIFT_TOLERANCE     3

/* Quanti SYNC di fila con lo scostamento ancora li' prima di riallineare
 * ANCHE se l'amico non si ferma mai. Il SYNC arriva ~1/s, quindi sono ~3
 * secondi: abbastanza da non confondere la latenza di una camminata (dove il
 * remoto e' normalmente indietro di un tile) con un passo perso che nessuno
 * ricuce. Piu' basso di cosi' si torna a teleportare durante le camminate
 * normali, che e' proprio l'effetto-cheat da evitare. */
#define DRIFT_RUN_MAX       3u

/* Quanti eventi in coda fanno scattare la marcia di recupero. */
#define HURRY_THRESHOLD     4u

/* Fin dove il rammendo insegue un PASSO arrivato lontano (in tile). Oltre non
 * e' un passo perso: e' un warp o una disconnessione, e la risposta giusta
 * resta il riposizionamento. Stesso valore di HEAL_MAX in client.py. */
#define MEND_MAX            3

/* Ingresso in una mappa nuova: quante posizioni assolute mandare oltre alla
 * prima, e ogni quanti VBlank. Su UDP un SYNC perso costa un secondo intero di
 * posizione sbagliata, e il cambio mappa e' il punto in cui costa piu' caro:
 * e' esattamente il momento in cui l'amico non sa piu' dove siamo. */
#define ENTRY_SYNC_REPEATS  3u
#define ENTRY_SYNC_SPACING  5u

/* --- porte: stati della FSM ------------------------------------------------
 * Non esiste nessun precedente nel gioco per "un giocatore remoto che entra in
 * una porta": i link player della Union Room lampeggiano e basta. La sequenza
 * pero' e' quella del giocatore locale, letta da Task_DoDoorWarp
 * (field_screen_effect.c:677-728) e da Task_ExitDoor (:317-362). */
#define DOOR_ST_NONE        0u
#define DOOR_ST_OPENING     1u  /* ingresso: si apre, il PASSO aspetta in coda */
#define DOOR_ST_WALKING     2u  /* ingresso: il remoto sale sulla soglia       */
#define DOOR_ST_CLOSING     3u  /* ingresso: remoto sparito, porta che chiude  */
#define DOOR_ST_HOLD        4u  /* ingresso: nessun respawn davanti alla porta */
#define DOOR_ST_EXIT_OPEN   5u  /* uscita: si apre, lo spawn aspetta           */
#define DOOR_ST_EXIT_STEP   6u  /* uscita: il remoto scende, poi si chiude     */

/* Un'animazione di porta dura una trentina di VBlank. Oltre questo si smette di
 * aspettarla e si va avanti lo stesso: meglio un'animazione saltata che una
 * macchina a stati appesa. */
#define DOOR_WAIT_FRAMES    60u
/* Oltre questo si molla tutto e si chiude la porta: e' la rete di sicurezza per
 * i casi che non abbiamo previsto. Ogni scatto alza `doorTimeouts`, che e' il
 * contatore da guardare se qualcosa non torna. */
#define DOOR_GIVEUP_FRAMES  150u
/* Dopo che l'amico e' entrato, per quanto si rifiuta di rispawnarlo aspettando
 * che arrivi il suo cambio mappa. Senza, lo si vedrebbe riapparire davanti alla
 * porta appena chiusa per la frazione di secondo che la rete ci mette. */
#define DOOR_HOLD_FRAMES    180u

/* --- assestamento del cambio mappa (Blocco A) -------------------------------
 *
 * LA FINESTRA DI TRANSIZIONE, MISURATA.
 *
 * CameraUpdate (field_camera.c:416-417) fa, in quest'ordine e nella stessa
 * funzione:
 *
 *     CameraMove(deltaX, deltaY);                   <- rebasa pos, CARICA LA MAPPA
 *     UpdateObjectEventsForCameraUpdate(dx, dy);    <- traduce gli object event
 *
 * Dentro CameraMove (fieldmap.c:649) c'e' SetPositionFromConnection, che sposta
 * gSaveBlock1Ptr->pos nello spazio NUOVO, e subito dopo
 * LoadMapFromCameraTransition (overworld.c:784), che aggiorna `location`
 * (ApplyCurrentWarp), poi gMapHeader (LoadCurrentMapData), poi tileset in VRAM,
 * palette, script, popup del nome: e' pesante e dura piu' di un VBlank.
 *
 * Quindi il nostro hook cade IN MEZZO, e la sessione del 2026-07-30 lo ha
 * misurato: `disallineamenti al bordo` 35 su 35 attraversamenti, sempre con
 * scarto (0,+-100). Non e' una gara rara, e' deterministico. Per due o tre
 * frame `pos` e `location` sono nello spazio nuovo mentre le coordinate degli
 * object event - quelle del GIOCATORE comprese - sono ancora in quello vecchio.
 *
 * IL PREDICATO DI COERENZA. Il giocatore e' l'oggetto tracciato dalla camera, e
 * il gioco stesso definisce le due cose come la stessa cosa: GetCameraFocusCoords
 * (fieldmap.c:800) ritorna pos + MAP_OFFSET, e InitObjectEventsLocal
 * (overworld.c:2171) ci spawna sopra il giocatore. Quindi in condizioni normali
 *
 *     oe_currentX(player) == sb1_posX + MAP_OFFSET
 *
 * con al massimo un tile di scarto mentre il passo e' in corso. Nella finestra
 * lo scarto vale invece la larghezza o l'altezza della mappa attraversata
 * (misurato 100): un tile di soglia non puo' dare falsi positivi ne' falsi
 * negativi.
 *
 * Finche' non torna coerente si CONGELA TUTTO: niente TX, niente RX, niente
 * spawn, niente despawn. E' il momento in cui qualunque cosa si legga e' un
 * miscuglio di due spazi. */
#define SETTLE_GIVEUP_FRAMES  60u

/* --- indicatori di stato (Blocco B) -----------------------------------------
 * Cosa sta facendo l'amico quando non sta camminando. Il campo `dir` dell'evento
 * viene riusato come stato: non c'e' nessuna direzione da trasportare.
 *
 * Dal quarto blocco lo stato MENU e' sdoppiato per sezione. La sezione si
 * riconosce dal callback che il menu START installa con SetMainCallback2
 * (start_menu.c:647-692): quel valore resta in gMain.callback2 per almeno un
 * frame prima che la schermata passi al proprio run loop, e il nostro VBlank
 * campiona una volta a frame, quindi non lo puo' perdere. Si fa LATCH finche'
 * non si rientra nell'overworld. ST_MENU resta il ripiego per le schermate i
 * cui callback sono static e non stanno nel .map (Trainer Card, Opzioni, PC):
 * la regola del progetto vieta gli indirizzi inventati. Chi riceve tratta
 * qualunque valore sconosciuto come ST_MENU: un peer piu' vecchio mostra
 * l'icona generica invece di sbagliare. */
#define ST_OVERWORLD    0u
#define ST_BATTLE       1u
#define ST_DIALOG       2u
#define ST_MENU         3u
#define ST_MENU_BAG     4u
#define ST_MENU_PARTY   5u
#define ST_MENU_DEX     6u
#define ST_MENU_NAV     7u

/* Stessa disciplina degli entry sync: su UDP un cambio di stato perso lascia
 * l'icona sbagliata sulla testa dell'amico finche' non cambia di nuovo. */
#define STATUS_REPEATS      3u
#define STATUS_SPACING      5u
/* Battendo il tamburo mentre lo stato NON e' overworld si copre anche chi si e'
 * collegato dopo. In overworld non serve: l'assenza di icona e' il default. */
#define STATUS_HEARTBEAT    120u
/* Uscire dall'overworld non vuol dire "menu": vuol dire anche warp, porta,
 * transizione di lotta. Un secondo di attesa distingue una pausa vera da un
 * passaggio - ed e' anche il criterio di prova ("menu lampo, nessuna icona"). */
#define MENU_DEBOUNCE_FRAMES 60u

/* --- protocollo -------------------------------------------------------------
 * 12 byte per evento. Due tipi: PASSO all'avvio di ogni passo, SYNC come
 * posizione assoluta periodica.
 */

#define EVENT_STEP      1u
#define EVENT_SYNC      2u
/* Girarsi sul posto non muove le coordinate, quindi non produce nessun PASSO:
 * senza un evento suo, il remoto resterebbe rivolto dove stava. */
#define EVENT_TURN      3u
/* L'amico ha lasciato la stanza (cambio mappa o timeout). Non lo emette il
 * gioco: lo sintetizza il bridge quando il relay manda un T_BYE. Senza, il suo
 * avatar resterebbe piantato qui ad aspettare il culling. */
#define EVENT_LEAVE     4u
/* Lo stato dell'amico fuori dall'overworld (lotta, dialogo, menu). Non porta
 * nessuna informazione di posizione: si consuma da se', come EVENT_LEAVE, e non
 * tocca ne' remoteTargetX/Y ne' remoteMapKey. Il relay e il bridge lo inoltrano
 * opachi - per loro i 12 byte sono e restano un blocco senza struttura. */
#define EVENT_STATUS    5u
/* La scheda allenatore dell'amico, a pezzi da 7 byte (Consegna B). dir e'
 * l'indice del chunk (0..13); i byte utili stanno in speed, x, y, gender,
 * avatarState. mapGroup/mapNum restano VERI anche qui: il client li legge
 * (extract_map_key) per ricordare su che mappa sta l'amico. Si consuma da
 * se', come lo STATO: niente di posizionale, nessun resync, nessun dedup
 * (le copie del doppio invio sono scritture idempotenti). */
#define EVENT_CARD      6u

/* IL MITTENTE STA NEL NIBBLE ALTO DEL TIPO (2026-08-25, fino a 4 giocatori).
 *
 * I 12 byte non hanno un campo mittente e non possono crescerne uno (9 parole
 * sul filo, contratto con usb_link e col Lua). Ma i tipi sono 1..7: il nibble
 * alto e' sempre stato zero. Il CLIENT del ricevente - l'unico che conosce i
 * peer - assegna a ogni amico uno SLOT 0..2 e lo timbra li' prima di scrivere
 * l'evento verso il gioco. Un client vecchio non timbra: nibble 0 = slot 0,
 * cioe' la compatibilita' col protocollo a due giocatori e' automatica.
 * In TX il payload emette il tipo nudo: il mittente non sa ne' deve sapere
 * quale slot occupa presso ciascun ricevente. */
#define EVENT_KIND(t)   ((u8)((t) & 0x0Fu))
#define EVENT_SLOT(t)   ((u8)(((t) >> 4) & 0x03u))

/* La coda per-slot in cui il corpo smista la RX. Serve contro il BLOCCO IN
 * TESTA: con una coda sola condivisa, il passo dell'amico A ancora in
 * animazione congelava anche i passi di B e C dietro di lui (ognuno dei tre
 * cammina col SUO ritmo, e il ritmo lo da' proprio "il passo prima e' finito").
 * STATO, SCHEDA si consumano al volo e non entrano; le copie del doppio invio
 * si scartano all'INGRESSO (seq contro l'ultimo accodato dello stesso slot),
 * quindi la profondita' conta solo eventi veri: 6 bastano per il congelamento
 * di una porta (~5 passi a 8 eventi/s in 40 frame). */
#define RQ_SLOTS        6u
#define RQ_WRAP(i)      (((i) + 1u < RQ_SLOTS) ? ((i) + 1u) : 0u)
/* Il congelamento della coda durante una porta, ora per-slot (era 10 sulla
 * coda condivisa: qui la coda e' 6, il tetto scende con lei). */
#define DOOR_FREEZE_MAX_PENDING_SLOT  5u

/* "Il mio GBA sta entrando al Cable Club" (Consegna D). Emesso TRE volte sul
 * fronte di salita di linkBusy, PRIMA della resa della porta: e' l'avviso con
 * cui client.py cambia modo al Pico da solo (passthrough -> onlineLink).
 * Nessun campo utile oltre al tipo; le copie sono ridondanza (il canale perde
 * ~8% per direzione e questo evento vale una sessione di scambi). */
#define EVENT_CLUB      7u
/* Frame di grazia fra il fronte di linkBusy e la resa effettiva del SIO: il
 * tempo di far USCIRE i tre EVENT_CLUB (27 parole a ~3,8 parole/frame ~ 8
 * frame) prima di smettere di pompare. Il gioco in quel momento e' child in
 * attesa: non tocca il cavo. */
#define LINK_GRACE_FRAMES 15u

#define CARD_SIZE        96u     /* la PARTE LINK della scheda (0x60): e' quanto
                                  * riempie TrainerCard_GenerateCardForLinkPlayer
                                  * (memset 0x60, trainer_card.c:779) ed e' cio'
                                  * che viaggia nei chunk                      */
#define CARD_ENTRY_SIZE  0x64u   /* sizeof(struct TrainerCard) VERO: dopo 0x60
                                  * ci sono hasAllFrontierSymbols (0x60) e
                                  * frontierBP (0x62). E' il passo dell'array
                                  * gTrainerCards[] - il viewer legge la voce 1
                                  * a base+0x64, NON a base+0x60. Confuso il
                                  * 2026-08-09: la scheda usciva con tutti i
                                  * valori slittati di 4 byte ("sballatissimi",
                                  * riportato da Lain). Conferma dal .map: il
                                  * simbolo dopo gTrainerCards sta a +0x194 =
                                  * 4 voci da 0x64 piu' align                  */
#define CARD_CHUNK_BYTES 7u
#define CARD_CHUNKS      14u     /* 13 pieni + 1 da 5 byte                    */
#define CARD_ALL_CHUNKS  0x3FFFu
#define CARD_TX_PERIOD   24u     /* frame fra due chunk: ~2,5 eventi/s, scheda
                                  * completa ogni ~5,6 s, e si ricomincia     */
#define CARD_FADE_FRAMES 40u     /* attesa del fade prima del viewer: il fade
                                  * a delay 0 dura ~16-32 frame, e leggere
                                  * gPaletteFade.active vorrebbe dire fidarsi
                                  * del layout dei bitfield di agbcc          */
#define LANGUAGE_ITALIAN 4u      /* constants/global.h                        */
#define FADE_TO_BLACK    1u      /* constants/field_weather.h, modo di
                                  * FadeScreen                                */

/* TX e RX a 6 dal 2026-08-25 (erano 8 e 12: la storia dei tagli precedenti
 * sta in NOTES, 2026-08-02). Il TX si svuota a un frame ogni ~40 ms (226
 * parole/s) e il payload emette al massimo ~5-10 eventi/s: la coda non ha
 * mai superato pochi slot, e txFull/eventsDropped lo direbbero. La RX con lo
 * smistamento per-slot e' solo un PIANEROTTOLO, drenato per intero a ogni
 * giro del corpo (60 Hz) contro i ~25 eventi/s del SIO: il buffering vero -
 * compreso quello che il rammendo trattiene mentre il remoto recupera - sta
 * nelle code per-slot (RQ_SLOTS, in g_rq). */
#define TX_SLOTS        6u
#define RX_SLOTS        6u
/* Avanzamento circolare SENZA `%`: con 16 slot il modulo compilava in un AND,
 * con 12 diventerebbe una chiamata a __aeabi_uidivmod di libgcc, che il
 * payload non linka (il primo build con 12 e' morto proprio cosi'). */
#define WRAP(i, n)      ((i) + 1u >= (n) ? 0u : (i) + 1u)
#define SYNC_PERIOD     60u  /* VBlank fra due correzioni assolute */

struct NetEvent          /* 12 byte */
{
    u8  type;
    u8  dir;             /* DIR_*                                        */
    u8  speed;           /* SPEED_*, decide la movement action           */
    u8  seq;
    u8  mapGroup;
    u8  mapNum;
    s16 x;
    s16 y;
    u8  gender;          /* MALE / FEMALE, decide lo sprite              */
    u8  avatarState;     /* PLAYER_AVATAR_STATE_*, decide lo sprite      */
};

/* Casella postale fra payload e mondo esterno, a PAYLOAD_BASE + 0x100.
 * Chi la drena e la riempie oggi e' l'harness Lua; in Fase 5 sara' il firmware
 * Celio. Le regole sono le solite dei ring buffer a produttore/consumatore
 * singolo: chi scrive muove solo la propria testa, chi legge solo la propria
 * coda, e nessuno dei due ha bisogno di lock. */
struct Mailbox
{
    u32 magic;           /* +0x00  'MBOX'                                  */
    u32 txHead;          /* +0x04  scritto dal payload                     */
    u32 txTail;          /* +0x08  scritto da chi drena                    */
    u32 txSlots;         /* +0x0C                                          */
    u32 rxHead;          /* +0x10  scritto da chi consegna                 */
    u32 rxTail;          /* +0x14  scritto dal payload                     */
    u32 rxSlots;         /* +0x18                                          */
    u32 rxApplied;       /* +0x1C  eventi remoti effettivamente applicati  */
    struct NetEvent tx[TX_SLOTS];   /* +0x20 */
    struct NetEvent rx[RX_SLOTS];   /* +0x20 + txSlots*12: chi legge lo CALCOLI
                                     * da txSlots, mai un offset cablato       */
};

__attribute__((section(".payload_mailbox"), used))
volatile struct Mailbox g_mailbox = {
    0x584F424Du, 0, 0, TX_SLOTS, 0, 0, RX_SLOTS, 0, { { 0 } }, { { 0 } }
};

#ifdef PAYLOAD_WITH_SIO
/* =============================================================================
 * Cablaggio del driver SIO (payload\sio.c)
 * =============================================================================
 *
 * PERCHE' DIETRO A UNA MACRO. In emulatore chi riempie la coda RX e drena la TX
 * e' l'harness Lua. Se il payload accendesse il SIO anche li', drenerebbe la TX
 * da solo e l'harness resterebbe a bocca asciutta: due consumatori sulla stessa
 * coda a produttore singolo. Quindi la build con il cavo e quella con Lua sono
 * distinte, e lo sono per costruzione invece che per disciplina.
 *
 * IL PUNTO DI GIUNZIONE E' LA MAILBOX, E NON CAMBIA. Il resto del payload non
 * sa - e non deve sapere - se un evento e' arrivato dal cavo o da Lua: legge la
 * stessa coda con le stesse regole. E' il motivo per cui questo cablaggio e'
 * corto.
 */
#define IRQ_SERIAL      0x0080   /* bit 7 di IE/IF, come in sio.c            */
#define SIO_T_EVENT     0x01     /* un NetEvent: 6 parole, come in sio.c     */
#define SIO_T_STATE     0x02     /* la sonda: 8 parole di contatori (v. sotto) */
#define SIO_T_DIAG      0x04     /* la sonda, pagina 2: i registri veri       */

void SioInit(void);
void SioShutdown(void);
void SioYield(void);
void SioTick(void);
void SioOnSerialIrq(void);
int  SioSendFrame(u8 type, const u16 *words, u8 count);
void SioGuard(void);
u32  SioRepairCount(void);
u32  SioScrubCount(void);

static u8 sSioReady;

/* Il ritmo della sonda: un frame al secondo, pagine alternate. Il timer
 * NON si azzera se il buffer era occupato: si riprova ogni frame finche'
 * non parte. Stanno qui in alto perche' SetLocalStatus li forza: al cambio
 * di stato la sonda parte SUBITO, pagina 1 (vedi la'). */
static u32 sStateTimer;

/* Consegna di un frame ricevuto: lo mette nella coda RX, cioe' esattamente dove
 * lo metterebbe l'harness Lua.
 *
 * Il valore di ritorno e' "consumato", non "applicato": un tipo che non e' il
 * nostro va dichiarato consumato, altrimenti il driver lo conterebbe come
 * rxFull e il contatore accuserebbe una coda piena che non c'entra niente. */
int SioDeliverFrame(u8 type, const u16 *words, u8 count)
{
    u32 head, next, i;
    const u8 *src;
    volatile u8 *dst;

    if (type != SIO_T_EVENT || count != (u8)(sizeof(struct NetEvent) / 2))
        return 1;

    head = g_mailbox.rxHead;
    next = WRAP(head, RX_SLOTS);
    if (next == g_mailbox.rxTail)
        return 0;            /* piena davvero: il driver conta rxFull */

    src = (const u8 *)words;
    dst = (volatile u8 *)&g_mailbox.rx[head];
    for (i = 0; i < sizeof(struct NetEvent); i++)
        dst[i] = src[i];

    g_mailbox.rxHead = next;
    return 1;
}

/* Svuota la coda TX sul cavo.
 *
 * `txTail` avanza SOLO se il frame e' partito davvero. Avanzarlo comunque
 * perderebbe l'evento in silenzio, che in un protocollo a eventi e' il difetto
 * peggiore: il remoto resta indietro di un passo per sempre e nessun contatore
 * lo dice. Se il buffer e' occupato si esce e si riprova al VBlank dopo - e il
 * driver conta txFull da solo. */
static void SioPumpTx(void)
{
    while (g_mailbox.txTail != g_mailbox.txHead)
    {
        u32 tail = g_mailbox.txTail;

        if (!SioSendFrame(SIO_T_EVENT, (const u16 *)&g_mailbox.tx[tail],
                          (u8)(sizeof(struct NetEvent) / 2)))
            break;

        g_mailbox.txTail = WRAP(tail, TX_SLOTS);
    }
}
#endif /* PAYLOAD_WITH_SIO */

/* Stato a offset fisso PAYLOAD_BASE + 0x10, letto dall'harness Lua.
 * Sull'hardware siamo ciechi e in emulatore non vedo lo schermo: questa
 * struttura e' l'unico modo che ho di sapere cosa sta succedendo. */
struct PayloadState
{
    u32 magic;          /* +0x00  'STAT'                                    */
    u32 irqCount;       /* +0x04                                            */
    u32 vblankCount;    /* +0x08                                            */
    u32 lastIf;         /* +0x0C                                            */
    u32 inOverworld;    /* +0x10                                            */
    u32 state;          /* +0x14  STATE_*                                   */
    u32 objectId;       /* +0x18  slot dell'object event remoto             */
    u32 spawnAttempts;  /* +0x1C                                            */
    u32 spawnFailures;  /* +0x20                                            */
    u32 mapKey;         /* +0x24  mappa corrente                            */
    s32 playerX;        /* +0x28                                            */
    s32 playerY;        /* +0x2C                                            */
    s32 remoteX;        /* +0x30  dove il gioco disegna il remoto           */
    s32 remoteY;        /* +0x34                                            */
    u32 pixelFixes;     /* +0x38  offset di disegno azzerati sullo sprite del
                         * remoto: oggi solo il residuo del dondolio del surf
                         * (vedi ClearRemoteSpriteOffset). Sale a ogni smontata
                         * dell'amico dall'acqua e basta: se resta 0 e nessuno
                         * ha surfato, e' NORMALE.
                         * Sta a +0x38 perche' era il posto di `moveIndex`, un
                         * campo morto dalla Fase 4: cosi' non si spostano gli
                         * offset degli altri e non costa un byte di EWRAM  */
    u32 movesQueued;    /* +0x3C                                            */
    u32 despawns;       /* +0x40                                            */
    u32 playerDir;      /* +0x44                                            */
    u32 playerSpeed;    /* +0x48                                            */
    u32 eventsEmitted;  /* +0x4C                                            */
    u32 eventsDropped;  /* +0x50                                            */
    u32 movesRejected;  /* +0x54                                            */
    u32 spawnMapKey;    /* +0x58                                            */
    u32 adoptions;      /* +0x5C                                            */
    u32 remoteKnown;    /* +0x60  abbiamo notizie dell'amico?               */
    u32 remoteMapKey;   /* +0x64  su che mappa dice di essere               */
    s32 remoteTargetX;  /* +0x68  dove dice di essere                       */
    s32 remoteTargetY;  /* +0x6C                                            */
    u32 rxSteps;        /* +0x70  PASSO remoti applicati                    */
    u32 rxSyncs;        /* +0x74  SYNC remoti applicati                     */
    u32 rxCorrections;  /* +0x78  correzioni forzate per deriva             */
    u32 remoteAway;     /* +0x7C  l'amico e' su un'altra mappa              */
    u32 movesHurried;   /* +0x80  passi eseguiti in marcia di recupero      */
    u32 rxPending;      /* +0x84  eventi remoti in attesa                   */
    u32 remoteGender;   /* +0x88  sesso dell'amico                          */
    u32 remoteState;    /* +0x8C  PLAYER_AVATAR_STATE_* dell'amico          */
    u32 gfxChanges;     /* +0x90  cambi di sprite del remoto                */
    u32 remoteGfxId;    /* +0x94  graphicsId effettivo dell'object event    */
    u32 remoteAnimNum;  /* +0x98  animNum dello sprite: se supera 19 con uno
                         *        sprite in bici e' il bug dei glitch       */
    u32 spawnSkipped;   /* +0x9C  spawn non tentati: amico fuori finestra   */
    u32 gfxResizes;     /* +0xA0  sprite ricreati per cambio di dimensione  */
    u32 rxTurns;        /* +0xA4  GIRA remoti applicati                     */
    u32 blobsCreated;   /* +0xA8  blob del surf creati                      */
    u32 blobsDestroyed; /* +0xAC  blob del surf distrutti                   */
    u32 settles;        /* +0xB0  volte che il remoto e' tornato in posa    */
    u32 idleFixes;      /* +0xB4  riallineamenti da fermo (deriva piccola)  */
    /* +0xB8  callback1 originale del gioco. E' ESPOSTO APPOSTA: l'iniettore lo
     * legge per disinnescare l'hook prima di azzerare la regione, altrimenti
     * ricaricare lo script farebbe saltare il gioco su memoria a zero. */
    u32 origCallback1;
    u32 cb1Installs;    /* +0xBC  quante volte abbiamo (ri)messo il trampolino */
    u32 aBlocked;       /* +0xC0  pressioni di A soppresse davanti al remoto   */
    u32 remotesRemoved; /* +0xC4  object event del remoto distrutti da noi     */
    u32 entryResyncs;   /* +0xC8  riposizionamenti forzati dopo un cambio mappa */
    u32 rxOtherMap;     /* +0xCC  eventi scartati perche' di un'altra mappa    */
    u32 rxImplausible;  /* +0xD0  PASSO piu' lunghi di un tile, degradati a SYNC */
    u32 entrySyncs;     /* +0xD4  SYNC emessi entrando in una mappa nuova      */
    u32 rxLeaves;       /* +0xD8  VIA ricevuti (l'amico ha lasciato la stanza) */
    u32 rxDrained;      /* +0xDC  eventi buttati uscendo dall'overworld        */
    u32 prevMapKey;     /* +0xE0  mappa da cui veniamo                         */
    u32 mapChanges;     /* +0xE4  cambi di mappa nostri                        */
    /* --- Fase 7: route adiacenti ------------------------------------------ */
    s32 remoteLocalX;   /* +0xE8  dove l'amico dice di essere, TRADOTTO nello   */
    s32 remoteLocalY;   /* +0xEC  spazio della NOSTRA mappa                     */
    u32 remoteVia;      /* +0xF0  1 = lo stiamo disegnando oltre il bordo       */
    u32 rxTranslated;   /* +0xF4  eventi applicati con traduzione               */
    u32 stripRejects;   /* +0xF8  volte che e' uscito dalla striscia caricata    */
    u32 borderCarries;  /* +0xFC  remoti sopravvissuti a un NOSTRO cambio route  */
    /* --- Fase 7: porte ---------------------------------------------------- */
    u32 doorState;      /* +0x100 DOOR_ST_*                                     */
    u32 doorEnters;     /* +0x104 ingressi animati completati                   */
    u32 doorExits;      /* +0x108 uscite animate completate                     */
    u32 doorBusy;       /* +0x10C ripieghi: una porta era gia' in animazione    */
    u32 doorTimeouts;   /* +0x110 attese scadute: la FSM si e' sbloccata da se' */
    /* --- Fase 7: prova che la traduzione al bordo e' quella del gioco ------
     * Deve restare 0. Senza questo contatore l'assunto su cui poggia F4 (le
     * due traduzioni danno lo stesso tile fisico, quindi non c'e' niente da
     * risincronizzare) sarebbe INVERIFICABILE: se divergessero, la correzione
     * della deriva rimetterebbe tutto a posto entro un secondo, l'allarme
     * [deriva] non scatterebbe mai (pretende scostamento per tre secondi di
     * fila) e resterebbe solo un +1 su idleFixes, che sale gia' per motivi
     * legittimi. La rete di sicurezza nasconderebbe l'errore. */
    u32 borderMismatch; /* +0x114 volte che dopo un bordo il tile non tornava   */
    s32 borderDriftX;   /* +0x118 ultimo scarto misurato, con segno             */
    s32 borderDriftY;   /* +0x11C                                              */
    /* --- Blocco A: assestamento del cambio mappa -------------------------- */
    u32 settleEntries;  /* +0x120 quante volte siamo entrati in assestamento.
                         * DEVE salire di 1 a ogni nostro cambio mappa: se
                         * resta 0 il gate non e' mai stato attraversato e
                         * borderMismatch tornerebbe a salire                  */
    u32 settleTimeouts; /* +0x124 assestamenti chiusi dal watchdog invece che
                         * dalla coerenza. DEVE restare 0                      */
    u32 settleFramesLast; /* +0x128 durata dell'ultimo assestamento, in giri del
                         * corpo. Quando il corpo girava nel VBlank era atteso
                         * 1-4 (l'IRQ cadeva DENTRO il caricamento mappa); dal
                         * main loop (2026-08-21) e' atteso 1 SEMPRE: il corpo
                         * non puo' piu' vedere la finestra di transizione, e
                         * il gate resta come rete di sicurezza              */
    u32 borderWalks;    /* +0x12C attraversamenti dell'AMICO trasformati in un
                         * passo camminato invece che in un teletrasporto      */
    u32 borderHolds;    /* +0x130 attraversamenti dell'amico gia' allineati: il
                         * gioco lo aveva gia' messo dove serviva              */
    /* --- Blocco B: indicatori di stato ------------------------------------ */
    u32 statusSent;     /* +0x134 eventi STATO emessi da noi                   */
    u32 statusRx;       /* +0x138 eventi STATO ricevuti                        */
    u32 localStatus;    /* +0x13C ST_* nostro                                  */
    u32 remoteStatus;   /* +0x140 ST_* dell'amico                              */
    u32 indicatorShown; /* +0x144 icone create sopra la testa del remoto        */
    u32 indicatorLoadFails; /* +0x148 grafica dell'icona non caricabile.
                         * DEVE restare 0                                      */
    u32 passThroughs;   /* +0x14C volte che ci siamo tolti di mezzo per non
                         * farci bloccare dal remoto (a TRANSIZIONE, non per
                         * frame). Se resta 0 il pass-through e' codice morto  */
    /* --- Fase 7, quarto blocco: sezioni del menu, tasto A nei menu --------- */
    u32 menuLatches;    /* +0x150 sezioni di menu riconosciute dal latch su
                         * gMain.callback2 (a transizione). Se apri lo Zaino e
                         * resta 0, MenuSectionFor non sta riconoscendo niente
                         * e l'icona degrada sempre a quella generica          */
    u32 aFreedInMenu;   /* +0x154 pressioni di A col remoto davanti lasciate
                         * passare perche' i controlli di campo erano bloccati
                         * (menu START, dialogo). E' ESATTAMENTE il caso che
                         * prima veniva mangiato: se martellando A dentro il
                         * menu START col remoto davanti resta 0, la correzione
                         * e' codice morto                                     */
    /* --- Possesso della porta seriale (solo build -WithSio) ---------------
     * La correzione del 2026-08-02 (SioShutdown fuori dall'overworld) non era
     * mai stata ESEGUITA da nessuna parte: la guardia dell'iniettore rifiutava
     * la build -WithSio in emulatore, quindi la prima esecuzione sarebbe stata
     * sul GBA fisico, senza log. Questi due contatori sono la prova che il
     * prendi/molla morde. Nelle build senza -WithSio restano 0 per costruzione
     * e il rapporto Lua non li stampa. */
    u32 sioAcquires;    /* +0x158 SioInit() chiamate: 1 all'ingresso in overworld
                         * e +1 a ogni rientro. Se in una build -WithSio resta 0,
                         * il cablaggio del driver e' codice morto              */
    u32 sioReleases;    /* +0x15C SioShutdown() chiamate: +1 a ogni uscita
                         * dall'overworld (menu, lotta, warp). L'invariante e':
                         * acquires - releases = 1 dentro l'overworld, 0 fuori.
                         * Se dopo una lotta la differenza resta 1, la porta non
                         * e' stata restituita ed e' il difetto della Torre     */
    u32 vbSkips;        /* +0x160 ingressi col bit VBlank alzato SCARTATI dal
                         * cancello anti-raffica (il contatore del gioco non
                         * era avanzato). Nelle fasi con HBlank armato DEVE
                         * correre a migliaia al secondo - se resta 0 mentre
                         * [hook] irq esplode, il cancello e' codice morto     */
    u32 rxDupes;        /* +0x164 copie scartate dal dedup (vedi DOPPIO INVIO
                         * in ConsumeRemoteEvents). Con il client che manda
                         * ogni PASSO due volte questo DEVE valere circa
                         * quanto i passi ricevuti: se resta 0 le copie non
                         * stanno arrivando e la ridondanza non c'e'; se fosse
                         * molto piu' basso, meta' delle copie si perde per
                         * strada e il canale sta messo peggio del previsto  */
    u32 remotesSwept;   /* +0x168 fantasmi (object event col NOSTRO localId)
                         * distrutti dallo spazzino. Il
                         * salvataggio serializza TUTTI i gObjectEvents dentro
                         * SaveBlock1 (load_save.c:180-186) e il continue li
                         * ripristina: salvando col remoto sullo schermo, alla
                         * partita dopo rinasce un remoto di nessuno - SOLIDO,
                         * perche' il pass-through vive solo finche' il payload
                         * lo possiede. Se dopo un continue con fantasma nel
                         * save resta 0, lo spazzino e' codice morto           */
    u32 stagingScrubs;  /* +0x16C scudo del salvataggio: voci col nostro localId
                         * RIPULITE (come ClearObjectEvent)
                         * nella COPIA D'APPOGGIO (gSaveBlock1Ptr->objectEvents,
                         * quella che la flash serializza - vedi
                         * ScrubSaveStaging). +1 a ogni salvataggio fatto con
                         * il remoto a schermo, e +1 al primo frame se il save
                         * caricato era gia' sporco. Se salvi con l'amico a
                         * schermo e resta 0, lo scudo e' codice morto e il
                         * save e' sporco                                      */
    /* --- Consegna B: la scheda allenatore ---------------------------------- */
    u32 cardChunksTx;   /* +0x170 chunk della NOSTRA scheda emessi. Con un
                         * amico in stanza DEVE correre (~2,5/s): se resta 0
                         * la trasmissione della scheda e' codice morto        */
    u32 cardChunksRx;   /* +0x174 chunk della scheda dell'amico ricevuti      */
    u32 cardRxBitmap;   /* +0x178 quali chunk abbiamo (bit 0..13). La scheda
                         * e' completa - e A la apre - quando vale 0x3FFF     */
    u32 cardShows;      /* +0x17C volte che la scheda dell'amico e' stata
                         * aperta con A. Se premi A davanti al remoto a scheda
                         * completa e resta 0, l'apertura e' codice morto      */
    u32 linkYields;     /* +0x180 rese al link del gioco (a TRANSIZIONE):
                         * gLinkCallback o gReceivedRemoteLinkPlayers vivi ->
                         * il payload molla la porta senza restore e dorme.
                         * Se parli con la signorina del Cable Club, accetti,
                         * e resta 0, la resa e' codice morto e il club si
                         * pianta come la Torre Lotta del 2026-08-02          */
    u32 linkWakes;      /* +0x184 risvegli dal latch del link: il gioco ha
                         * riattaccato (REG_IE serial spento, nessun remoto)
                         * e il payload ha ripreso il cavo azzerando il
                         * gLinkCallback stantio che CloseLink dimentica.
                         * Dopo un linkup ANNULLATO deve valere 1: se resta 0
                         * il payload e' rimasto addormentato per sempre - e'
                         * il congelamento del 2026-08-15                     */
    /* --- Audit del salvataggio (2026-08-21): il corpo gira dal main loop --- */
    u32 bodyTicks;      /* +0x188 giri di OverworldTick dal main loop (payload_cb1).
                         * DEVE correre a ~60/s in overworld: se resta fermo con
                         * inOverworld = 1 il corpo non gira piu' (hook su
                         * callback1 perso o mai preso) e il remoto e' morto   */
    u32 aUnscripted;    /* +0x18C pressioni di A soppresse davanti a un object
                         * event SENZA template sulla mappa corrente - compreso
                         * il nostro OLTRE UN BANCONE (vedi FrontObjectClass).
                         * Se premi A verso il remoto dietro un bancone e resta
                         * 0, la regola generale e' codice morto              */
    /* --- fino a 4 giocatori (2026-08-25) ----------------------------------
     * I campi remote* qui sopra restano e valgono per lo slot PRIMARIO (il
     * primo con notizie): e' il contratto con la tabella S del Lua, che cosi'
     * non cambia di una riga. Lo stato pieno per-slot sta in g_remotes[]. */
    u32 rxQueueDrops;   /* +0x190 eventi buttati da una coda per-slot piena.
                         * DEVE restare ~0: se sale, un amico produce piu' di
                         * quanto il suo remoto consuma e va capito perche'   */
    u32 slotsKnown;     /* +0x194 bitmask: di quali slot abbiamo notizie      */
    u32 slotsSpawned;   /* +0x198 bitmask: quali slot hanno l'avatar a schermo */
    u32 blobScrubbed;   /* +0x19C id di bolla del surf STANTII trovati e
                         * azzerati: senza questo la bolla non rinasceva piu'
                         * dopo un cambio mappa o una lotta                  */
    u32 resizeRespawns; /* +0x1A0 respawn immediati dopo un cambio di
                         * dimensione dello sprite (bici, surf). Deve seguire
                         * gfxResizes: se gfxResizes sale e questo no,
                         * l'amico e' sparito invece di cambiare avatar      */
    u32 fxArgsKept;     /* +0x1A4 frame in cui il GIOCO, muovendo l'avatar
                         * dell'amico, ha scritto gFieldEffectArguments (erba
                         * alta, orme, pozzanghere, salti) e RemoteSpriteCb
                         * ha rimesso la casella com'era. Se l'amico cammina
                         * nell'erba alta e resta 0, la guardia non morde e
                         * la MN esce di nuovo col Pokemon sbagliato        */
};

__attribute__((section(".payload_state"), used))
volatile struct PayloadState g_state = {
    .magic = 0x53544154,
    .objectId = OBJECT_EVENTS_COUNT,   /* 16 = nessuno slot */
};

/* UN SOLO INCREMENTO PER TUTTI I CONTATORI, e vale 332 byte di EWRAM.
 *
 * PayloadState e' fatto di 103 campi u32 e il payload la incrementa da 76
 * posti diversi. Ogni `g_state.qualcosa++` scritto in chiaro costa, in Thumb-1
 * su memoria volatile: materializzare l'indirizzo (una cella di literal pool
 * da 4 byte, spesso NON condivisa fra funzioni diverse), la ldr, la add, la
 * str. Fra le 6 e le 14 byte a botta, 76 volte.
 *
 * Passando dall'indice - i campi sono tutti u32 e contigui, quindi il campo N
 * sta a `((u32 *)&g_state)[N]` - ogni sito diventa `movs r0, #N; bl Bump`:
 * quattro byte, e nessuna cella di literal pool. Il costo dell'indirizzo si
 * paga UNA volta, qui dentro.
 *
 * MISURATO, non stimato (2026-08-28): main.o 9680 -> 9348 byte.
 *
 * `noinline` e' obbligatorio, non un suggerimento: se GCC la inlinea torna
 * esattamente il codice di prima, moltiplicato per 76. La guardia in build.ps1
 * non se ne accorgerebbe (il margine calerebbe e basta), quindi se un giorno
 * il margine crolla di ~330 byte senza motivo, la prima cosa da guardare e'
 * se questo attributo e' ancora qui.
 *
 * NIENTE SEMANTICA NUOVA: l'offset di ogni campo lo calcola il compilatore da
 * __builtin_offsetof, quindi il contratto con la tabella S di inject_body.lua
 * e con client.py resta identico byte per byte - e la guardia `contratto`
 * della build continua a verificarlo. Il tipo volatile e' conservato: la
 * scrittura avviene comunque, nell'ordine in cui e' scritta.
 *
 * ATTENZIONE al percorso IRQ: Bump viene chiamata anche da payload_frame, ed
 * e' quindi dentro il grafo che la guardia dello stack di build.ps1 somma. Ha
 * frame zero e non chiama nessuno, ma se un giorno crescesse, la guardia lo
 * direbbe. */
static void __attribute__((noinline)) Bump(u32 idx)
{
    ((volatile u32 *)&g_state)[idx]++;
}

#define BUMPF(f) Bump(__builtin_offsetof(struct PayloadState, f) / 4u)

/* L'INDIRIZZO DI UN OBJECT EVENT E DI UNO SPRITE, CALCOLATO IN UN POSTO SOLO:
 * altri 228 byte di EWRAM, con la stessa idea di Bump qui sopra.
 *
 * `ObjectEvent(i)` e `Sprite(i)` sono macro (game_types.h): ogni uso espande a
 * `base + i * 36` e `base + i * 68`. In Thumb-1 ognuno costa la costante della
 * moltiplicazione, la muls, la cella di literal pool con la base e la add:
 * una decina di byte, moltiplicata per 24 e 11 usi. Da qui in giu' le due
 * macro chiamano invece una funzione, e ogni sito diventa una `bl`.
 *
 * Le due funzioni si definiscono PRIMA della ridefinizione, cosi' il loro
 * corpo usa ancora la macro vera: invertire l'ordine le renderebbe ricorsive
 * (e il compilatore non se ne accorgerebbe, perche' e' una ricorsione
 * legittima - si pianterebbe il GBA, non la build).
 *
 * MISURATO (2026-08-28): main.o 9348 -> 9120 byte. Il codice sorgente non
 * cambia di una riga: continua a leggersi `ObjectEvent(i)` ovunque.
 *
 * Costo in tempo: una chiamata invece di una moltiplicazione. Sta tutto nel
 * corpo (payload_cb1), non nel percorso caldo dell'IRQ. */
static u32 __attribute__((noinline)) OeAt(u32 i) { return ObjectEvent(i); }
static u32 __attribute__((noinline)) SprAt(u32 i) { return Sprite(i); }
#undef ObjectEvent
#define ObjectEvent(i) OeAt(i)
#undef Sprite
#define Sprite(i) SprAt(i)

static u32 sFramesInOverworld;
static s16 sLastPlayerX, sLastPlayerY;
static u8  sLastPlayerDir;
/* Seq dell'ultimo PASSO/GIRA accodato per uno slot, per scartare la copia del
 * doppio invio (vedi il blocco DOPPIO INVIO in DrainMailbox). Si conserva come
 * SEQ_TAG(seq), cioe' con il bit 8 acceso: il seq sul filo e' un byte, quindi
 * ogni valore 0..255 e' possibile e uno stato iniziale di "nessuno" servirebbe
 * comunque. Con il tag, lo zero di partenza non e' un seq valido e il primo
 * evento della sessione non puo' essere scambiato per una copia. */
#define SEQ_TAG(s)  ((u16)(0x100u | (u8)(s)))
static u32 sHavePlayerPos;
static u32 sFramesSinceSync;
static u8  sSeq;
/* Mappa dell'ultimo campione: il cambio e' un EVENTO del protocollo, non un
 * caso limite. Vedi TrackPlayer. */
static u32 sLastMapKey;
static u32 sEntrySyncsLeft;
static u32 sEntrySyncTimer;
/* FSM delle porte. UNA SOLA per tutto il payload, come nel gioco: il motore
 * anima una porta alla volta (StartDoorAnimationTask rifiuta la seconda), e
 * la FSM ha un PROPRIETARIO - lo slot del remoto che sta entrando o uscendo.
 * Gli altri slot, porta occupata, ripiegano sulla sparizione secca (doorBusy),
 * che era gia' il comportamento della coda condivisa. Il timer conta in SU e
 * si azzera a ogni transizione: cosi' ogni stato ha la sua soglia e non ci si
 * porta dietro il tempo di quello precedente. */
static u32 sDoorState;
static u32 sDoorTimer;
static s16 sDoorX, sDoorY;
static u32 sDoorOwner;

/* --- LO STATO PER-SLOT (2026-08-25, fino a 4 giocatori) --------------------
 *
 * Tutto cio' che prima era "IL remoto" - statiche sparse e campi di g_state -
 * e ora e' "il remoto dello slot s". I campi restano u32/s32 per la lezione
 * Thumb-1 del 2026-08-23 (i tipi stretti costano istruzioni); i campi di
 * g_state con lo stesso nome restano vivi come SPECCHIO dello slot primario,
 * per non toccare il contratto con la tabella S del Lua.
 *
 * ATTENZIONE ALL'AZZERAMENTO: la .bss parte a zero, ma per objectId e
 * indSpriteId zero significherebbe "slot 0 del gioco" e "sprite 0", che sono
 * di qualcun altro. L'init vero lo fa RemotesInitOnce, dal primo VBlank,
 * PRIMA che il trampolino su callback1 esista: nessuno legge prima. */
struct Remote
{
    u32 state;          /* STATE_IDLE / STATE_SPAWNED                       */
    u32 objectId;       /* slot in gObjectEvents, OBJECT_EVENTS_COUNT = no  */
    u32 known;          /* abbiamo notizie di questo amico?                 */
    u32 mapKey;         /* mappa che DICHIARA                               */
    s32 targetX;        /* dove dice di essere (spazio suo)                 */
    s32 targetY;
    u32 gender;
    u32 avatarState;    /* PLAYER_AVATAR_STATE_*                            */
    u32 status;         /* ST_*: l'icona sopra la testa                     */
    u32 spawnMapKey;    /* su che mappa NOSTRA l'avatar e' stato messo      */
    u32 settled;        /* rimesso in posa da fermo dopo l'ultimo movimento */
    u32 driftRun;       /* SYNC di fila con lo stesso scostamento mentre
                         * cammina: a DRIFT_RUN_MAX si riallinea comunque    */
    u32 lastEnqSeq;     /* SEQ_TAG dell'ultimo PASSO/GIRA accodato (dedup)  */
    u32 forceResync;    /* il primo evento applicabile riposiziona di forza */
    u32 outOfView;      /* latch "fuori dalla finestra di culling"          */
    u32 stripOut;       /* latch "oltre la striscia della route accanto"    */
    u32 carryCheck;     /* verifica del bordo armata (vedi sCarryCheck di un
                         * tempo: il commento vive su SettleCommit)         */
    u32 spawnCooldown;
    u32 cardRxBitmap;   /* chunk della sua scheda gia' ricevuti             */
    /* icona sopra la testa: ognuno ha la sua                               */
    u32 indSpriteId;    /* MAX_SPRITES = nessuna                            */
    u32 indIcon;
    u32 indTileStart;
    u32 indFailed;      /* latch del "grafica non caricabile"               */
    /* il proprio indice, scritto da RemotesInitOnce: ricavarlo dal puntatore
     * sarebbe una divisione per sizeof(struct Remote) - non potenza di due -
     * a ogni chiamata, cioe' sei istruzioni Thumb ognuna (lezione EWRAM) */
    u32 slot;
    /* teste della coda degli eventi di QUESTO amico; gli eventi stanno in
     * g_rq (regione .lateclear, vedi sotto)                                */
    u32 qHead;
    u32 qTail;
    /* la callback originale dello sprite dell'avatar, avvolta da
     * RemoteSpriteCb (vedi GuardRemoteSprite); 0 = non ancora vista       */
    void (*sprCb)(void *);
};

static struct Remote g_remotes[N_REMOTES];

/* Le code per-slot vivono nella regione di handoff (.lateclear, payload.ld):
 * sotto HANDOFF_BASE i 4 giocatori non ci stavano, e quella regione dopo il
 * boot e' codice morto. NON e' azzerata dal caricatore (mbstub ci copia
 * handoff.S DOPO l'azzeramento): la azzera RemotesInitOnce al primo VBlank,
 * prima che chiunque le legga. */
static struct NetEvent g_rq[N_REMOTES][RQ_SLOTS]
    __attribute__((section(".lateclear")));

static u32 RemoteSlotOf(struct Remote *r)
{
    return r->slot;
}

static u32 RqLen(struct Remote *r)
{
    u32 h = r->qHead, t = r->qTail;
    return (h >= t) ? (h - t) : (RQ_SLOTS - t + h);
}

/* L'init che la .bss a zero non puo' dare (vedi il commento sulla struct).
 * Chiamata dal blocco "completamento del reset" di payload_frame: gira una
 * volta, al primo VBlank, prima che qualunque altra cosa legga g_remotes. */
static void RemotesInitOnce(void)
{
    u32 i;
    u8 *rq = (u8 *)g_rq;

    for (i = 0; i < N_REMOTES; i++)
    {
        g_remotes[i].slot = i;
        g_remotes[i].objectId = OBJECT_EVENTS_COUNT;
        g_remotes[i].indSpriteId = MAX_SPRITES;
    }

    /* L'azzeramento tardivo di .lateclear: vedi la dichiarazione di g_rq. */
    for (i = 0; i < sizeof(g_rq); i++)
        rq[i] = 0;
}

/* --- assestamento del cambio mappa (Blocco A) ----------------------------- */
/* La mappa dell'ultimo frame VISTA DA payload_frame. E' separata da sLastMapKey
 * (che vive nel lato TX e si riallinea solo al commit) apposta: sono due
 * domande diverse, "la mappa e' cambiata adesso?" e "da quale mappa e' partito
 * l'ultimo evento che ho emesso?". */
static u32 sSettleMapKey;
static u32 sHaveSettleKey;
static u32 sMapSettling;
static u32 sSettleTimer;

/* --- stato locale e indicatori (Blocco B) --------------------------------- */
static u32 sLocalStatus;
/* La sezione del menu vista passare in gMain.callback2 (latch, vedi ST_MENU_*).
 * 0 = nessuna riconosciuta: dopo il debounce si dichiara ST_MENU generico. */
static u32 sMenuSection;
static u32 sStatusRepeatsLeft;
static u32 sStatusTimer;
static u32 sStatusHeartbeat;
static u32 sOutFieldFrames;
/* Il link del gioco era vivo al frame scorso (latch per contare le RESE a
 * transizione, non a frame). */
static u32 sLinkBusyLatch;
/* Frame di grazia rimasti prima della resa effettiva (vedi LINK_GRACE_FRAMES). */
static u32 sLinkGrace;
/* --- la scheda allenatore (Consegna B) ------------------------------------ */
/* Trasmissione round-robin: quale chunk tocca, e il timer fra due chunk. */
static u32 sCardTxIdx;
static u32 sCardTxTimer;
/* La rigenerazione della scheda e' un passaggio di consegne fra contesti:
 * il VBlank (payload_frame) la CHIEDE alzando sCardGenPending, il main loop
 * (payload_cb1) la ESEGUE - TrainerCard_GenerateCardForLinkPlayer e' codice
 * del gioco e dall'IRQ non si chiama - e conferma alzando sCardReady. */
static u32 sCardGenPending;
static u32 sCardReady;
/* Frame che mancano fra il FadeScreen e l'apertura del viewer. 0 = fermo. */
static u32 sCardShowWait;
/* Di QUALE amico si sta aprendo la scheda (deciso alla pressione di A, usato
 * allo scadere del fade: fra i due momenti passa mezzo secondo). */
static u32 sCardShowSlot;
/* Icone sopra la testa: sprite id, icona e tile start stanno in g_remotes[]
 * (ognuno ha la sua). Qui resta solo cio' che e' davvero condiviso. */

/* --- pass-through (Blocco B4) --------------------------------------------- */
/* I remoti disattivati in payload_cb1 da riattivare al VBlank, come BITMASK di
 * slot. Prima di riattivare si riverifica l'identita' (localId): fra i due
 * momenti il gioco puo' aver riciclato l'object event, e riattivare quello
 * sbagliato vorrebbe dire resuscitare un NPC che il gioco aveva tolto. */
static u32 sPassMask;
static u32 sPassThroughWasOn;

/* Delta di direzione, indicizzato con DIR_*: NONE, SOUTH, NORTH, WEST, EAST.
 * Sta qui in cima e non accanto a payload_cb1 perche' lo usano tre posti
 * distanti fra loro: la continuita' al bordo, la soppressione del tasto A e il
 * pass-through. */
static const signed char sDirDeltaX[] = { 0,  0,  0, -1,  1 };
static const signed char sDirDeltaY[] = { 0,  1, -1,  0,  0 };

/* ========================================================================== */
/* Lato TX: leggere il giocatore locale e mettere in coda cosa fa             */
/* ========================================================================== */

static u32 CurrentMapKey(void)
{
    u32 sb1 = SaveBlock1();
    if (sb1 == 0)
        return 0xFFFFFFFFu;
    return ((u32)sb1_mapGroup(sb1) << 8) | (u32)sb1_mapNum(sb1);
}

/* Le coordinate del giocatore e la mappa dicono la stessa cosa?
 *
 * E' il predicato del gate di assestamento: vedi il commento esteso su
 * SETTLE_GIVEUP_FRAMES per perche' funziona e perche' un tile di soglia basta.
 * Ritorna 0 anche se il salvataggio non e' leggibile: in quel caso non si sa
 * niente, e non sapere niente non e' "assestato". */
static int PlayerCoordsSettled(u32 playerOe)
{
    u32 sb1 = SaveBlock1();
    s32 dx, dy;

    if (sb1 == 0)
        return 0;

    dx = (s32)oe_currentX(playerOe) - ((s32)sb1_posX(sb1) + MAP_OFFSET);
    dy = (s32)oe_currentY(playerOe) - ((s32)sb1_posY(sb1) + MAP_OFFSET);

    if (dx < 0) dx = -dx;
    if (dy < 0) dy = -dy;

    return dx <= 1 && dy <= 1;
}

static u32 PlayerSpeedClass(void)
{
    u8 flags = gPlayerAvatar_flags;

    if (flags & (PLAYER_AVATAR_FLAG_SURFING | PLAYER_AVATAR_FLAG_UNDERWATER))
        return SPEED_SURF;
    if (flags & (PLAYER_AVATAR_FLAG_MACH_BIKE | PLAYER_AVATAR_FLAG_ACRO_BIKE))
        return SPEED_BIKE;
    if (flags & PLAYER_AVATAR_FLAG_DASH)
        return SPEED_RUN;
    return SPEED_WALK;
}

/* Lo stato dell'avatar e' cosa il giocatore E', non quanto va veloce: e' lui a
 * decidere lo sprite. Correre resta stato NORMAL, perche' la corsa e'
 * un'animazione dello stesso sprite a piedi. */
static u8 PlayerAvatarState(void)
{
    u8 flags = gPlayerAvatar_flags;

    if (flags & PLAYER_AVATAR_FLAG_UNDERWATER)
        return PLAYER_AVATAR_STATE_UNDERWATER;
    if (flags & PLAYER_AVATAR_FLAG_SURFING)
        return PLAYER_AVATAR_STATE_SURFING;
    if (flags & PLAYER_AVATAR_FLAG_MACH_BIKE)
        return PLAYER_AVATAR_STATE_MACH_BIKE;
    if (flags & PLAYER_AVATAR_FLAG_ACRO_BIKE)
        return PLAYER_AVATAR_STATE_ACRO_BIKE;
    return PLAYER_AVATAR_STATE_NORMAL;
}

static u8 PlayerGender(void)
{
    u32 sb2 = SaveBlock2();
    return sb2 ? sb2_playerGender(sb2) : 0;
}

/* La grafica giusta per un dato stato e sesso: quella del RIVALE, non quella
 * del giocatore.
 *
 * Sono gli stessi disegni - il rivale e' Brendan/May - e quindi arrivano gratis
 * le animazioni di camminata, corsa, pedalata e surf. La differenza che conta e'
 * il `paletteSlot`: le grafiche del giocatore hanno PALSLOT_PLAYER (0), che e'
 * la slot di palette OBJ dell'avatar LOCALE. Usarle per il remoto significa che
 * i due si sovrascrivono la palette a vicenda: con un amico di sesso diverso uno
 * dei due prende i colori dell'altro (osservato il 2026-07-30, era la previsione
 * 0-quater di NOTES). Le grafiche del rivale hanno invece PALSLOT_NPC_SPECIAL,
 * una slot tutta loro, e il paletteTag giusto per sesso.
 *
 * Non e' un'invenzione nostra: e' esattamente cio' che fa il gioco per i
 * giocatori remoti del Cable Club e della Union Room, in CreateLinkPlayerSprite
 * (overworld.c), che chiama GetRivalAvatarGraphicsIdByStateIdAndGender. */
static u8 GfxForRemote(u8 avatarState, u8 gender)
{
    return GetRivalAvatarGraphicsIdByStateIdAndGender(avatarState, gender);
}

/* Quanti byte di grafica vuole questo sprite. Serve perche' le grafiche del
 * rivale NON hanno tutte la stessa dimensione (256 a piedi, 512 in bici e in
 * surf) e ObjectEventSetGraphicsId non rialloca i tile: vedi SyncRemoteGraphics. */
static u16 GfxSize(u8 gfxId)
{
    return gfxinfo_size(GetObjectEventGraphicsInfo(gfxId));
}

/* I 7 byte utili di un chunk di scheda, per OFFSET dentro NetEvent: speed(2),
 * x(6,7), y(8,9), gender(10), avatarState(11). E' il contratto fra
 * EmitCardChunk (che impacchetta) e DrainMailbox (che spacchetta): una
 * tabella sola, cosi' i due non possono divergere. */
static const u8 kCardOff[CARD_CHUNK_BYTES] = { 2, 6, 7, 8, 9, 10, 11 };

/* DUE PRODUTTORI SULLA CODA TX (2026-08-21). Il corpo emette dal main loop
 * (PASSO, SYNC, GIRA, scheda), il VBlank emette STATO fuori dall'overworld e
 * CLUB alla resa del link. Non girano mai nello stesso frame per costruzione
 * (inOverworld li separa), ma un VBlank caduto a meta' frame nel momento
 * esatto della resa potrebbe entrare qui mentre il corpo ha gia' letto `head`:
 * l'IRQ scriverebbe lo stesso slot e il corpo riporterebbe txHead indietro.
 * Una sezione critica (IME giu') lo rende impossibile; dal VBlank stesso e'
 * un no-op (gli IRQ sono gia' spenti a livello di CPU). Anche sSeq si tocca
 * SOLO qui dentro, per lo stesso motivo.
 *
 * L'evento arriva gia' costruito tranne seq, mapGroup e mapNum, che sono
 * uguali per tutti e si scrivono qui: e' il tratto comune di EmitEvent e
 * EmitCardChunk, tenuto in un posto solo (2026-08-25, per il bilancio EWRAM
 * dei 4 giocatori). Ritorna 0 a coda piena: meglio perdere l'evento nuovo
 * che sovrascrivere roba che il lettore non ha ancora visto - e si conta. */
static int EmitRaw(struct NetEvent *ev)
{
    u16 ime = REG_IME;
    u32 head, next, k;
    u32 sb1 = SaveBlock1();
    const u8 *src;
    volatile u8 *dst;

    ev->mapGroup = sb1 ? sb1_mapGroup(sb1) : 0;
    ev->mapNum   = sb1 ? sb1_mapNum(sb1) : 0;

    REG_IME = 0;
    head = g_mailbox.txHead;
    next = WRAP(head, TX_SLOTS);

    if (next == g_mailbox.txTail)
    {
        BUMPF(eventsDropped);
        REG_IME = ime;
        return 0;
    }

    ev->seq = sSeq++;
    src = (const u8 *)ev;
    dst = (volatile u8 *)&g_mailbox.tx[head];
    for (k = 0; k < sizeof(struct NetEvent); k++)
        dst[k] = src[k];

    g_mailbox.txHead = next;
    BUMPF(eventsEmitted);
    REG_IME = ime;
    return 1;
}

static void EmitEvent(u8 type, u8 dir, u8 speed, s16 x, s16 y)
{
    struct NetEvent ev;

    ev.type  = type;
    ev.dir   = dir;
    ev.speed = speed;
    ev.x     = x;
    ev.y     = y;
    ev.gender      = PlayerGender();
    ev.avatarState = PlayerAvatarState();
    EmitRaw(&ev);
}

/* Entrare in una mappa nuova: si riparte da una posizione assoluta e la si
 * ripete, invece di raccontare un passo che non c'e' stato. */
static void BeginEntrySyncs(u8 dir, u8 speed, s16 x, s16 y)
{
    EmitEvent(EVENT_SYNC, dir, speed, x, y);
    sFramesSinceSync = 0;
    sEntrySyncsLeft = ENTRY_SYNC_REPEATS;
    sEntrySyncTimer = ENTRY_SYNC_SPACING;
    BUMPF(entrySyncs);
}

/* Lo stato di chi non sta camminando: lotta, dialogo, menu.
 *
 * Si manda su TRANSIZIONE, ripetuto, piu' un battito finche' lo stato non e'
 * OVERWORLD. Le coordinate sono l'ultima posizione nota: l'evento non le usa
 * per muovere niente (chi lo riceve non tocca remoteTargetX/Y), ma mapGroup e
 * mapNum servono comunque al bridge, che li legge per sapere su quale mappa
 * stava l'amico se poi il relay manda un T_BYE. */
static void EmitStatus(void)
{
    EmitEvent(EVENT_STATUS, (u8)sLocalStatus, 0, sLastPlayerX, sLastPlayerY);
    BUMPF(statusSent);
}

/* Un chunk della NOSTRA scheda (gTrainerCards[0], riempita dal main loop -
 * vedi sCardGenPending). I byte grezzi della scheda finiscono nei campi di
 * NetEvent secondo kCardOff; mapGroup e mapNum restano quelli veri, per il
 * client (vedi EVENT_CARD). */
static void EmitCardChunk(u8 idx)
{
    struct NetEvent ev;
    u32 off = (u32)idx * CARD_CHUNK_BYTES;
    const volatile u8 *src = (const volatile u8 *)ADDR_gTrainerCards;
    u8 *d = (u8 *)&ev;
    u32 k;

    ev.type = EVENT_CARD;
    ev.dir  = idx;
    for (k = 0; k < CARD_CHUNK_BYTES; k++)
        d[kCardOff[k]] = (off + k < CARD_SIZE) ? src[off + k] : 0;

    if (EmitRaw(&ev))
        BUMPF(cardChunksTx);
}

/* Il giro della scheda: un chunk ogni CARD_TX_PERIOD frame, solo quando c'e'
 * qualcuno in stanza. A inizio giro la scheda si rigenera (ore di gioco,
 * medaglie e soldi cambiano), ma la genera il main loop: qui si chiede e si
 * aspetta il frame dopo. Ripetere il giro per sempre e' la ritrasmissione:
 * nessun ACK, nessuno stato condiviso, e un chunk perso torna da solo. */
static void CardTxTick(void)
{
    u32 i, n = 0;

    for (i = 0; i < N_REMOTES; i++)
        if (g_remotes[i].known)
            n++;
    if (n == 0)
        return;

    if (sCardTxTimer)
    {
        sCardTxTimer--;
        return;
    }
    if (sCardTxIdx == 0 && !sCardReady)
    {
        sCardGenPending = 1;
        return;               /* si riprova al frame dopo, a scheda pronta */
    }

    /* IL RITMO SCALA CON LA STANZA (2026-08-25). Ogni mittente rallenta il
     * proprio giro di scheda di un fattore pari agli amici che conosce: cosi'
     * chi RICEVE da n amici continua a vedere ~2,5 chunk/s in totale, non
     * 2,5*n, e il budget del filo (25 eventi/s sul SIO) non se lo mangia la
     * scheda. La scheda completa passa da ~5,6 s a ~17 s con tre amici:
     * accettato, e' un dato che cambia lentamente. */
    sCardTxTimer = CARD_TX_PERIOD * n;
    EmitCardChunk((u8)sCardTxIdx);
    sCardTxIdx++;
    if (sCardTxIdx >= CARD_CHUNKS)
    {
        sCardTxIdx = 0;
        sCardReady = 0;
    }
}

/* Quale sezione del menu e' questo callback2? 0 = nessuna riconosciuta.
 *
 * Gli indirizzi sono GREZZI (bit Thumb gia' tolto dal chiamante) e non vengono
 * MAI chiamati: sono solo etichette da confrontare, come ADDR_CB2_Overworld.
 * Sono i callback che il menu START installa (start_menu.c:647-692); il run
 * loop dello Zaino (CB2_BagMenuRun) e' l'unico esportato, e copre anche lo
 * Zaino aperto da dentro la Squadra ("dai uno strumento"). */
static u32 MenuSectionFor(u32 cb2)
{
    if (cb2 == ADDR_CB2_BagMenuFromStartMenu || cb2 == ADDR_CB2_BagMenuRun)
        return ST_MENU_BAG;
    if (cb2 == ADDR_CB2_PartyMenuFromStartMenu)
        return ST_MENU_PARTY;
    if (cb2 == ADDR_CB2_OpenPokedex)
        return ST_MENU_DEX;
    if (cb2 == ADDR_CB2_InitPokeNav)
        return ST_MENU_NAV;
    return 0;
}

static void SetLocalStatus(u32 status)
{
    if (status == sLocalStatus)
        return;

    sLocalStatus = status;
    g_state.localStatus = status;
    sStatusRepeatsLeft = STATUS_REPEATS;
    sStatusTimer = STATUS_SPACING;
    sStatusHeartbeat = STATUS_HEARTBEAT;
    EmitStatus();
#ifdef PAYLOAD_WITH_SIO
    /* LA SONDA PARTE SUBITO (2026-08-21): al prossimo VBlank esce la pagina
     * 1, che porta il nuovo stato (w4 bit 8-11) e le sezioni riconosciute
     * (bit 12-15). Sul fisico e' la riga che distingue i tre guasti
     * possibili della "scheda bianca": il payload NON cambia stato (qui
     * resta menu), cambia stato ma gli EVENT_STATUS non escono (qui zaino,
     * nel log niente `io -> zaino`), oppure escono e si perdono dopo. */
    sStateTimer = 60;
#endif
}

/* Le ripetizioni e il battito. Si chiama a ogni frame, dentro e fuori
 * dall'overworld: e' l'unico pezzo del lato TX che deve continuare a girare
 * proprio mentre il resto dorme, perche' e' li' che lo stato e' interessante. */
static void StatusTick(void)
{
    if (sStatusRepeatsLeft)
    {
        if (sStatusTimer)
        {
            sStatusTimer--;
        }
        else
        {
            sStatusRepeatsLeft--;
            sStatusTimer = STATUS_SPACING;
            EmitStatus();
        }
        return;
    }

    /* In overworld l'assenza di icona e' gia' il default di chi riceve: non c'e'
     * niente da ribadire, e un battito ogni due secondi sarebbe traffico buttato
     * per il 99% del tempo di gioco. */
    if (sLocalStatus == ST_OVERWORLD)
        return;

    if (sStatusHeartbeat)
    {
        sStatusHeartbeat--;
        return;
    }

    sStatusHeartbeat = STATUS_HEARTBEAT;
    EmitStatus();
}

/* L'inizio del passo si riconosce dal cambio di currentCoords: in Gen 3 passa
 * al tile di destinazione all'AVVIO del movimento, e lo sprite ci arriva
 * interpolando.
 *
 * IL CAMBIO MAPPA NON SI TRATTA PIU' QUI. Fino al 2026-07-30 il ramo
 * `mapKey != sLastMapKey` stava in questa funzione, ed era il difetto: al primo
 * VBlank dopo il cambio emetteva i SYNC d'ingresso leggendo oe_currentX/Y del
 * giocatore, che in quell'istante sono ANCORA NELLO SPAZIO VECCHIO (vedi
 * SETTLE_GIVEUP_FRAMES). Ne usciva un SYNC avvelenato - mappa nuova, coordinate
 * vecchie - che dall'altra parte diventava un passo di cento tile, un despawn
 * "non rappresentabile" e un TeleportRemote al rientro. Adesso il cambio mappa
 * lo possiede il gate di assestamento (SettleCommit), che e' l'unico posto in
 * cui la domanda "dove sono?" ha una risposta sola. */
static void TrackPlayer(u32 playerOe)
{
    s16 x = oe_currentX(playerOe);
    s16 y = oe_currentY(playerOe);
    /* facingDirection, non movementDirection: e' il verso in cui il giocatore
     * GUARDA, ed e' l'unico che cambia quando ci si gira sul posto. Durante il
     * cammino i due coincidono. */
    u8  dir = oe_facingDirection(playerOe);
    u32 speed = PlayerSpeedClass();
    u32 mapKey = g_state.mapKey;

    g_state.playerX = x;
    g_state.playerY = y;
    g_state.playerDir = dir;
    g_state.playerSpeed = speed;

    if (!sHavePlayerPos)
    {
        u32 i;

        sLastPlayerX = x;
        sLastPlayerY = y;
        sLastPlayerDir = dir;
        sLastMapKey = mapKey;
        sHavePlayerPos = 1;
        /* Il primo campione dopo un rientro: ogni amico riparte da una
         * posizione assoluta, non da un passo. */
        for (i = 0; i < N_REMOTES; i++)
            g_remotes[i].forceResync = 1;
        BeginEntrySyncs(dir, (u8)speed, x, y);
        return;
    }

    if (x != sLastPlayerX || y != sLastPlayerY)
    {
        sLastPlayerX = x;
        sLastPlayerY = y;
        EmitEvent(EVENT_STEP, dir, (u8)speed, x, y);
    }
    else if (dir != sLastPlayerDir)
    {
        /* Fermo ma girato: nessun PASSO lo racconterebbe. */
        EmitEvent(EVENT_TURN, dir, (u8)speed, x, y);
    }
    sLastPlayerDir = dir;

    /* Le ripetizioni dell'ingresso: un solo SYNC perso costerebbe un secondo di
     * posizione sbagliata proprio nel momento peggiore. */
    if (sEntrySyncsLeft)
    {
        if (sEntrySyncTimer)
        {
            sEntrySyncTimer--;
        }
        else
        {
            sEntrySyncsLeft--;
            sEntrySyncTimer = ENTRY_SYNC_SPACING;
            sFramesSinceSync = 0;
            EmitEvent(EVENT_SYNC, dir, (u8)speed, x, y);
            BUMPF(entrySyncs);
        }
        return;
    }

    if (++sFramesSinceSync >= SYNC_PERIOD)
    {
        sFramesSinceSync = 0;
        EmitEvent(EVENT_SYNC, dir, (u8)speed, x, y);
    }
}

/* ========================================================================== */
/* Lato RX: far muovere il remoto come dice l'amico                           */
/* ========================================================================== */

/* Ogni famiglia di movement action e' ordinata DOWN, UP, LEFT, RIGHT, cioe'
 * DIR_SOUTH, DIR_NORTH, DIR_WEST, DIR_EAST. Cambiare andatura = cambiare base. */
static u8 ActionFor(u8 dir, u8 speed, int hurry)
{
    u8 base;

    if (dir < DIR_SOUTH || dir > DIR_EAST)
        return MOVEMENT_ACTION_FACE_DOWN;

    /* Marcia di recupero: quando la coda si accumula, il remoto va piu' veloce
     * dell'andatura dichiarata finche' non ha riassorbito il ritardo. Serve
     * perche' correndo o in bici il giocatore produce piu' passi al secondo di
     * quanti l'animazione normale ne possa consumare, e senza questo il ritardo
     * cresce per tutta la volata. */
    if (hurry)
        base = MOVEMENT_BASE_FASTER;
    else
    {
        switch (speed)
        {
        case SPEED_RUN:  base = MOVEMENT_BASE_RUN;  break;
        case SPEED_BIKE: base = MOVEMENT_BASE_FAST; break;
        case SPEED_SURF: base = MOVEMENT_BASE_SURF; break;
        default:         base = MOVEMENT_BASE_WALK; break;
        }
    }

    return (u8)(base + (dir - DIR_SOUTH));
}

/* Girarsi sul posto: stessa famiglia ordinata DOWN, UP, LEFT, RIGHT, base 0x00
 * (MOVEMENT_ACTION_FACE_*). */
static u8 FaceActionFor(u8 dir)
{
    if (dir < DIR_SOUTH || dir > DIR_EAST)
        return MOVEMENT_ACTION_FACE_DOWN;

    return (u8)(MOVEMENT_BASE_FACE + (dir - DIR_SOUTH));
}

/* Quanti eventi giacciono nella mailbox condivisa (non ancora smistati). */
static u32 MailboxPending(void)
{
    u32 head = g_mailbox.rxHead;
    u32 tail = g_mailbox.rxTail;

    if (head >= tail)
        return head - tail;
    return RX_SLOTS - tail + head;
}

/* Quanti eventi remoti sono in attesa di essere applicati, in tutto: mailbox
 * piu' le code per-slot. E' il numero del log (rxPending); la marcia di
 * recupero invece guarda la coda del SINGOLO slot (RqLen), perche' il ritardo
 * da riassorbire e' suo e non della stanza. */
static u32 RxPending(void)
{
    u32 n = MailboxPending();
    u32 i;

    for (i = 0; i < N_REMOTES; i++)
        n += RqLen(&g_remotes[i]);
    return n;
}

/* Cerca un remoto gia' presente fra gli object event.
 *
 * Serve perche' il payload puo' essere reiniettato su una partita gia' in corso
 * (ricarica dello script in mGBA): il nostro stato riparte da zero ma il remoto
 * di prima e' ancora vivo nel gioco. E `GetAvailableObjectEventId` rifiuta lo
 * spawn se esiste gia' un object event con lo stesso localId sulla stessa mappa
 * (event_object_movement.c:1358, "if the object is already loaded, returns TRUE"),
 * quindi senza questo controllo si resta bloccati a fallire per sempre. */
static u8 FindExistingRemote(u8 lid)
{
    u32 sb1 = SaveBlock1();
    u8 mapNum, mapGroup;
    u8 i;

    if (sb1 == 0)
        return OBJECT_EVENTS_COUNT;

    mapNum = sb1_mapNum(sb1);
    mapGroup = sb1_mapGroup(sb1);

    for (i = 0; i < OBJECT_EVENTS_COUNT; i++)
    {
        u32 oe = ObjectEvent(i);
        /* La mappa fa parte dell'identita'. Senza questo filtro, un remoto
         * sopravvissuto a un cambio di route (LoadMapFromCameraTransition NON
         * chiama ResetObjectEvents: lo fanno solo le InitObjectEvents*,
         * overworld.c:2158/2170) verrebbe riadottato nella mappa nuova con lo
         * scostamento che si portava dietro. */
        if (oe_active(oe) && oe_localId(oe) == lid
            && oe_mapNum(oe) == mapNum && oe_mapGroup(oe) == mapGroup)
            return i;
    }
    return OBJECT_EVENTS_COUNT;
}

static int RemoteStillAlive(struct Remote *r)
{
    u32 oe;

    if (r->objectId >= OBJECT_EVENTS_COUNT)
        return 0;

    oe = ObjectEvent(r->objectId);
    return oe_active(oe)
        && oe_localId(oe) == REMOTE_LID(RemoteSlotOf(r));
}

static void DestroyRemoteSurfBlob(u32 oe);
/* L'icona sopra la testa del remoto muore insieme al remoto. IndicatorTick la
 * ripulirebbe comunque al frame dopo, ma "al frame dopo" e' esattamente la
 * finestra in cui il gioco puo' aver gia' riciclato lo sprite. */
static void IndicatorDestroy(struct Remote *r);
/* Se la FSM della porta appartiene a questo slot, si molla tutto (e si chiude
 * la porta). Degli altri slot non si tocca niente. */
static void DoorAbortIfOwner(u32 slot);

/* Immagine fittizia usata per la rimozione.
 *
 * DestroySprite libera i tile OAM contando `sprite->images->size / TILE_SIZE_4BPP`
 * a partire da `oam.tileNum` (sprite.c:625). RemoveObjectEventInternal non si
 * fida di cosa `images` stia puntando in quel momento e lo sostituisce con un
 * descrittore che porta `GetObjectEventGraphicsInfo(graphicsId)->size`, cioe' la
 * dimensione della grafica ATTUALE dell'object event. Per noi non e' un
 * dettaglio: lo scambio a caldo dello sprite (ObjectEventSetGraphicsId) cambia
 * `images` senza riallocare i tile, quindi e' esattamente il caso in cui i due
 * valori possono divergere - liberare la quantita' sbagliata vorrebbe dire
 * lasciare tile occupati per sempre, o liberare tile di altri sprite.
 * Il gioco usa una locale di stack (penzolante appena la funzione ritorna, e
 * funziona solo perche' DestroySprite la consuma subito); noi una statica. */
static struct { const void *data; u16 size; } sRemoveImage;

/* MAI PIU' RemoveObjectEventByLocalIdAndMap. Quella fa
 * FlagSet(GetObjectEventFlagIdByObjectEventId(id)) (event_object_movement.c:1394),
 * e per il nostro localId (REMOTE_LOCAL_ID) non esiste nessun template:
 * GetObjectEventTemplateByLocalIdAndMap ritorna NULL e la lettura di `obj->flagId`
 * finisce all'indirizzo 0x00000014, dentro il BIOS, restituendo spazzatura.
 * FlagSet con un id qualunque sotto SPECIAL_FLAGS_START scrive in
 * gSaveBlock1Ptr->flags[id / 8] con id/8 fino a 2048: SCRITTURA FUORI DALL'ARRAY
 * DEI FLAG, dentro SaveBlock1 - cioe' dentro il salvataggio. Sopra quella soglia
 * scrive fuori da sSpecialFlags in EWRAM (event_data.c:196).
 * Non e' teorico: il ramo "cambio di dimensione dello sprite" la chiamava, e nei
 * log del 2026-07-30 salire e scendere dalla bici in tre secondi ha prodotto tre
 * ricreazioni, cioe' tre scritture fuori array.
 *
 * Questa e' la stessa cosa che fa RemoveObjectEvent + RemoveObjectEventInternal
 * (event_object_movement.c:1383/1399), meno il FlagSet, che per un object event
 * senza template non ha comunque nessun senso. */
static void RemoveRemoteObjectEvent(u32 oe)
{
    u32 sprite;

    DestroyRemoteSurfBlob(oe);

    /* Il gioco non controlla lo spriteId qui, ma il gioco arriva sempre da un
     * object event che ha creato lui. Noi possiamo arrivarci da uno stato
     * qualunque, e scrivere `images` a un indice fuori range vorrebbe dire
     * scrivere un puntatore in mezzo ai dati del gioco. */
    if (oe_spriteId(oe) < MAX_SPRITES)
    {
        sprite = Sprite(oe_spriteId(oe));
        sRemoveImage.data = 0;
        sRemoveImage.size = gfxinfo_size(GetObjectEventGraphicsInfo(oe_graphicsId(oe)));
        sprite_images(sprite) = (u32)&sRemoveImage;
        DestroySprite((void *)sprite);
    }

    oe_flags0(oe) = (u8)(oe_flags0(oe) & ~0x01u);
    BUMPF(remotesRemoved);
}

static void ForgetRemote(struct Remote *r)
{
    /* Si DISTRUGGE, non si lascia andare. Dimenticare e basta lasciava in giro
     * un avatar che FindExistingRemote riadottava alla prima occasione - anche
     * dopo un cambio di route, portandosi dietro lo scostamento. E il blob del
     * surf e' uno sprite separato dall'object event: se il gioco ci elimina il
     * remoto perche' uscito dalla vista, il blob resterebbe a galleggiare da
     * solo. Il localId sopravvive alla rimozione, quindi si puo' ancora
     * distinguere il nostro slot da uno gia' riciclato per un altro NPC. */
    if (r->objectId < OBJECT_EVENTS_COUNT)
    {
        u32 oe = ObjectEvent(r->objectId);
        if (oe_localId(oe) == REMOTE_LID(RemoteSlotOf(r)))
        {
            if (oe_active(oe))
                RemoveRemoteObjectEvent(oe);
            else
                DestroyRemoteSurfBlob(oe);
        }
    }

    IndicatorDestroy(r);

    r->state = STATE_IDLE;
    r->objectId = OBJECT_EVENTS_COUNT;
    BUMPF(despawns);
    /* Il controllo del bordo era in attesa di un object event che non c'e' piu':
     * lasciarlo armato significherebbe misurarlo su uno spawn successivo e
     * contare un disallineamento che non c'entra niente. Un contatore che deve
     * restare 0 non si permette falsi positivi. */
    r->carryCheck = 0;
}

/* Lo SPAZZINO dei fantasmi (2026-08-09).
 *
 * Il salvataggio serializza TUTTI i gObjectEvents dentro SaveBlock1
 * (load_save.c:180-186, SaveObjectEvents) e il continue li ripristina
 * (LoadObjectEvents): se si salva col remoto sullo schermo, alla partita dopo
 * rinasce un object event col nostro localId che nessuno possiede. E' SOLIDO - il
 * pass-through vive solo finche' il payload lo tiene - e permanente finche' un
 * cambio mappa non lo scarta (i respawn da template lo ignorano).
 *
 * Si chiama SOLO da STATE_IDLE, e solo nei frame in cui l'adozione non puo'
 * avvenire (nessuna notizia dell'amico, oppure amico noto ma non
 * rappresentabile qui): un remoto vivo in quel momento non e' di nessuno.
 * Quando l'amico c'e' ed e' rappresentabile, l'adozione resta la prima
 * scelta e lo spazzino non viene nemmeno interpellato.
 *
 * Niente filtro sulla mappa, al contrario di FindExistingRemote: un fantasma
 * trascinato oltre un bordo di route e' lo stesso fantasma. E niente lavoro
 * durante una sequenza di porta: li' l'avatar lo possiede la FSM.
 *
 * Effetto collaterale accettato: dopo una REINIEZIONE del payload a sessione
 * viva, il primo frame IDLE puo' spazzare l'avatar che l'adozione avrebbe
 * riusato - lo spawn lo ricrea appena arrivano gli eventi. Un lampo, solo in
 * sviluppo; il caso del giocatore (continue dal save) e' quello che conta. */
static void SweepStaleRemotes(u32 slot)
{
    u32 i;
    u8 lid = REMOTE_LID(slot);

    /* Si spazza SOLO il localId di QUESTO slot: un 0xE1 vivo mentre lo slot 1
     * e' in piena camminata non e' un fantasma, e' l'amico dello slot 1. */
    if (sDoorState != DOOR_ST_NONE && sDoorOwner == slot)
        return;

    for (i = 0; i < OBJECT_EVENTS_COUNT; i++)
    {
        u32 oe = ObjectEvent(i);
        if (oe_active(oe) && oe_localId(oe) == lid)
        {
            RemoveRemoteObjectEvent(oe);
            BUMPF(remotesSwept);
        }
    }
}

/* Lo SCUDO DEL SALVATAGGIO (2026-08-09, terza stesura - quella giusta).
 *
 * Requisito di Lain, per intero: il remoto si vede e si muove SEMPRE,
 * normale, icone comprese - e nel save non deve finire NIENTE del payload,
 * senza rischi di nessun tipo. (Prima stesura: despawn nel menu - bocciata.
 * Seconda: congelamento nel menu - bocciata. Questa non tocca lo schermo.)
 *
 * COME: il salvataggio prima copia gObjectEvents nella COPIA D'APPOGGIO
 * gSaveBlock1Ptr->objectEvents (SaveObjectEvents, load_save.c:184) e solo
 * dopo scrive la flash. Questa funzione gira a OGNI VBlank e annulla le voci
 * col nostro localId nella copia d'appoggio: quello che arriva su flash e' pulito, e la
 * partita a schermo non se ne accorge.
 *
 * PERCHE' VINCE SEMPRE LA CORSA (aritmetica, non fortuna; RILETTO il
 * 2026-08-21 nell'audit del salvataggio). HandleWriteSector (save.c:176) fa,
 * per OGNI settore e in quest'ordine: azzera il buffer temporaneo (4096
 * byte), COPIA i dati del settore in quel buffer, calcola il checksum, poi
 * programma e verifica la flash DAL BUFFER. Quello che finisce su flash e' la
 * copia fatta in quel momento: dopo la copia, SB1 non conta piu' per quel
 * settore. L'ordine dei settori e' fisso: chunk 0 (SaveBlock2) PRIMA del
 * chunk 1 (SaveBlock1 0x0000-0x0F80, objectEvents a +0xA30). Scrivere il
 * chunk 0 costa erase + 4096 program byte per byte con polling (agb_flash:
 * StartFlashTimer/StopFlashTimer per ogni byte): decine di ms sul fisico, e
 * piu' di un frame anche in emulatore, dove e' lavoro di CPU. E durante la
 * programmazione gli IRQ restano ABILITATI (StartFlashTimer AGGIUNGE il bit
 * del timer a IE, non maschera gli altri: agb_flash.c:88): il VBlank arriva,
 * questa funzione gira, e quando il gioco copia il chunk 1 nel buffer la
 * copia d'appoggio e' gia' pulita. Vale identico per il salvataggio di link
 * (LinkFullSave_*: un settore per frame) e per la Sala d'Onore.
 * (La versione precedente di questo commento diceva che la verifica della
 * flash "fallirebbe e riscriverebbe coi dati puliti": falso, la verifica
 * confronta col buffer temporaneo. La garanzia vera e' il tempo del chunk 0.)
 *
 * Bonus sul GBA fisico: il payload gira dal titolo in poi (multiboot), quindi
 * un fantasma lasciato nella copia d'appoggio da un save GIA' sporco viene
 * annullato PRIMA che il continue lo ricarichi (LoadObjectEvents legge
 * proprio da qui): il fantasma non tocca nemmeno gObjectEvents. In emulatore
 * lo script si carica a partita avviata, e li' resta lo spazzino.
 *
 * Si annulla anche il localId, non solo il bit active: una voce morta ma
 * riconoscibile tornerebbe a contare come fantasma a ogni frame e il
 * contatore diventerebbe rumore - deve contare le PULIZIE, non i frame.
 *
 * PERIMETRO, per il requisito "niente del payload nel save": l'unica cosa
 * nostra che il flusso di salvataggio puo' toccare e' l'object event del
 * remoto via questa copia. Il payload non scrive MAI altrove dentro
 * SaveBlock1/2 (niente FlagSet - vedi RemoveRemoteObjectEvent - niente vars,
 * niente stats); sprite, porte animate e mailbox vivono fuori dal save. */
static void ScrubSaveStaging(void)
{
    u32 sb1 = SaveBlock1();
    u32 i, k;

    if (sb1 == 0)
        return;

    for (i = 0; i < OBJECT_EVENTS_COUNT; i++)
    {
        u32 entry = sb1_objEvent(sb1, i);
        if (IS_REMOTE_LID(oe_localId(entry)))
        {
            /* ESATTAMENTE ClearObjectEvent (event_object_movement.c:1182):
             * tutto a zero, localId = LOCALID_PLAYER (0xFF), mapNum e
             * mapGroup = MAP_UNDEFINED (0xFF), movementActionId =
             * MOVEMENT_ACTION_NONE (0xFF). Cosi' lo slot e' indistinguibile
             * da uno MAI USATO: nessuna impronta (2026-08-21; prima si
             * azzeravano solo active e localId, e restavano 34 byte nostri -
             * grafica del rivale, coordinate, spriteId - in un salvataggio
             * che doveva essere legit). */
            for (k = 0; k < OBJECT_EVENT_SIZE; k++)
                GAME_U8(entry + k) = 0;
            oe_localId(entry) = 0xFF;
            oe_mapNum(entry) = 0xFF;
            oe_mapGroup(entry) = 0xFF;
            oe_movementActionId(entry) = 0xFF;
            BUMPF(stagingScrubs);
        }
    }
}


/* ========================================================================== */
/* Route adiacenti: disegnare l'amico che sta nella mappa accanto             */
/* ========================================================================== */

/* PERCHE' SI PUO' FARE.
 *
 * Non e' un trucco: e' un caso che il motore gestisce gia' da solo. Quando il
 * giocatore attraversa un bordo, gli NPC della mappa vecchia restano attivi con
 * coordinate traslate FUORI dai bordi della mappa nuova, e vengono disegnati
 * normalmente. Le coordinate fuori mappa sono sicure perche' GetMapGridBlockAt
 * ha il fallback sul border block (fieldmap.c:51-64) e il culling e' relativo a
 * gSaveBlock1Ptr->pos, non ai bordi della mappa.
 *
 * IL LIMITE E' FISICO, NON NOSTRO. InitBackupMapLayoutConnections
 * (fieldmap.c:133-343) copia in memoria solo una STRISCIA della mappa connessa:
 * 7 tile, 8 verso EST. Oltre quella striscia il terreno non esiste proprio, e
 * l'amico non e' rappresentabile. Non e' un difetto da correggere: e' il punto
 * in cui il motore finisce. */

/* La connessione verso una data mappa, o 0. Si scorre la tabella a mano invece
 * di usare GetMapConnection (0x08084FC0) perche' quella dereferenzia
 * connections->count PRIMA del controllo NULL, e la maggior parte delle mappe
 * (tutti gli interni) non ha nessuna connessione. */
static u32 FindConnectionTo(u32 mapKey)
{
    u32 conns = gMapHeader_connections;
    u32 c;
    s32 count, i;

    if (conns == 0)
        return 0;

    count = mapconns_count(conns);
    c = mapconns_list(conns);
    if (c == 0 || count <= 0)
        return 0;

    for (i = 0; i < count; i++, c += MAP_CONNECTION_SIZE)
    {
        u8 dir = mapconn_direction(c);

        /* DIVE ed EMERGE (5 e 6) non sono contiguita' visiva: sono l'altra
         * faccia della stessa acqua, e sono fuori scopo. */
        if (dir < CONNECTION_SOUTH || dir > CONNECTION_EAST)
            continue;

        if ((((u32)mapconn_group(c) << 8) | (u32)mapconn_num(c)) == mapKey)
            return c;
    }
    return 0;
}

/* Traduce una posizione della mappa connessa B nello spazio della nostra A.
 * In ingresso e in uscita le coordinate sono di GRIGLIA (con MAP_OFFSET), come
 * tutto il resto del protocollo; la formula pero' vive sulle locali, quindi si
 * toglie e si rimette l'offset.
 *
 * La tabella e' l'inversa esatta di quello che fa FillConnection: B viene
 * copiata nella griglia a partire da (offset + MAP_OFFSET) sull'asse parallelo
 * al bordo e dal bordo stesso sull'altro.
 *
 *   SUD    ax = bx + offset      ay = by + altezzaA
 *   NORD   ax = bx + offset      ay = by - altezzaB
 *   OVEST  ax = bx - larghezzaB  ay = by + offset
 *   EST    ax = bx + larghezzaA  ay = by + offset
 *
 * Ritorna 0 se la posizione cade fuori dalla striscia caricata. */
static int TranslateFromConnection(u32 conn, s16 *x, s16 *y)
{
    u32 layoutA = gMapHeader_mapLayout;
    u32 hdrB, layoutB;
    s32 offset = mapconn_offset(conn);
    s32 bx, by, ax, ay, gx, gy;

    if (layoutA == 0)
        return 0;

    hdrB = GetMapHeaderFromConnection(conn);
    if (hdrB == 0)
        return 0;
    layoutB = maphdr_mapLayout(hdrB);
    if (layoutB == 0)
        return 0;

    bx = (s32)*x - MAP_OFFSET;
    by = (s32)*y - MAP_OFFSET;

    switch (mapconn_direction(conn))
    {
    case CONNECTION_SOUTH:
        ax = bx + offset;
        ay = by + maplayout_height(layoutA);
        break;
    case CONNECTION_NORTH:
        ax = bx + offset;
        ay = by - maplayout_height(layoutB);
        break;
    case CONNECTION_WEST:
        ax = bx - maplayout_width(layoutB);
        ay = by + offset;
        break;
    case CONNECTION_EAST:
        ax = bx + maplayout_width(layoutA);
        ay = by + offset;
        break;
    default:
        return 0;
    }

    gx = ax + MAP_OFFSET;
    gy = ay + MAP_OFFSET;

    /* Dentro la griglia in memoria? E' il limite dell'asse PERPENDICOLARE al
     * bordo (la profondita' della striscia) e insieme il ritaglio che
     * FillConnection applica sull'altro asse. */
    if (gx < 0 || gx >= maplayout_width(layoutA) + MAP_OFFSET_W)
        return 0;
    if (gy < 0 || gy >= maplayout_height(layoutA) + MAP_OFFSET_H)
        return 0;

    /* E dentro QUESTA connessione? Lo decide il gioco: GetMapConnectionAtPos
     * ripete la stessa aritmetica che ha usato per copiare la striscia. Usarla
     * invece di riscrivere i limiti 7 e 8 a mano significa che i due non possono
     * divergere - ed e' esattamente il tipo di duplicazione che in questo
     * progetto e' gia' costato una sessione. */
    if (GetMapConnectionAtPos((s16)gx, (s16)gy) != conn)
        return 0;

    *x = (s16)gx;
    *y = (s16)gy;
    return 1;
}

/* Dove disegnare l'amico, date la mappa e le coordinate che DICHIARA.
 * Ritorna 0 se qui non e' rappresentabile (mappa non connessa, oppure connessa
 * ma oltre la striscia). */
static int MapRemoteToLocal(struct Remote *r, u32 mapKey, s16 dx, s16 dy,
                            s16 *ox, s16 *oy, u32 *via)
{
    u32 conn;

    *ox = dx;
    *oy = dy;
    *via = 0;

    if (mapKey == g_state.mapKey)
    {
        r->stripOut = 0;
        return 1;
    }

    conn = FindConnectionTo(mapKey);
    if (conn == 0)
        return 0;

    if (!TranslateFromConnection(conn, ox, oy))
    {
        if (!r->stripOut)
        {
            r->stripOut = 1;
            BUMPF(stripRejects);
        }
        return 0;
    }

    r->stripOut = 0;
    *via = 1;
    return 1;
}

/* La stessa cosa per l'ULTIMA posizione dichiarata dall'amico: e' il predicato
 * "adesso lo posso disegnare", e va valutato sulle coordinate DICHIARATE, non su
 * quelle disegnate. Al cambio mappa le due stanno in spazi diversi per qualche
 * frame, ed e' esattamente il caso in cui serve una risposta giusta. */
static int RemoteViewNow(struct Remote *r, s16 *x, s16 *y, u32 *via)
{
    if (!r->known)
    {
        *x = 0;
        *y = 0;
        *via = 0;
        return 0;
    }

    return MapRemoteToLocal(r, r->mapKey,
                            (s16)r->targetX,
                            (s16)r->targetY,
                            x, y, via);
}

/* La posizione e' dentro la finestra in cui il gioco tiene in vita gli object
 * event? Fuori da qui RemoveObjectEventIfOutsideView cancella, quindi spawnare
 * fuori significa farsi cancellare al frame dopo: un CreateSprite buttato, e con
 * l'amico in bici (che ci resta 8 tile indietro) il ciclo spawn/despawn gira
 * all'infinito. */
static int InsideView(s16 x, s16 y)
{
    u32 sb1 = SaveBlock1();
    s16 px, py;

    if (sb1 == 0)
        return 0;

    px = sb1_posX(sb1);
    py = sb1_posY(sb1);

    return x >= VIEW_LEFT(px) && x <= VIEW_RIGHT(px)
        && y >= VIEW_TOP(py)  && y <= VIEW_BOTTOM(py);
}

static void TrySpawnRemote(struct Remote *r, s16 x, s16 y)
{
    u32 playerOe = ObjectEvent(gPlayerAvatar_objectEventId);
    u8 elevation;
    u8 id;

    if (!oe_active(playerOe))
        return;

    if (!InsideView(x, y))
    {
        /* Si contano le TRANSIZIONI, non i frame: contando ogni frame il numero
         * cresceva di 60 al secondo e nel log sembrava un guasto invece di un
         * "l'amico e' lontano". */
        if (!r->outOfView)
        {
            r->outOfView = 1;
            BUMPF(spawnSkipped);
        }
        return;
    }
    r->outOfView = 0;

    elevation = oe_elevation(playerOe);

    BUMPF(spawnAttempts);

    /* Le coordinate sono quelle di campo: SpawnSpecialObjectEventParameterized
     * sottrae MAP_OFFSET da solo (event_object_movement.c:1514). */
    id = SpawnSpecialObjectEventParameterized(GfxForRemote((u8)r->avatarState,
                                                           (u8)r->gender),
                                              MOVEMENT_TYPE_NONE,
                                              REMOTE_LID(RemoteSlotOf(r)),
                                              x, y, elevation);

    if (id >= OBJECT_EVENTS_COUNT)
    {
        BUMPF(spawnFailures);
        /* Non ritentare a ogni frame: lo spawn e' una chiamata pesante e se
         * fallisce di solito continuera' a fallire (slot pieni, o duplicato). */
        r->spawnCooldown = SPAWN_RETRY_FRAMES;
        return;
    }

    r->objectId = id;
    r->state = STATE_SPAWNED;
    r->spawnMapKey = g_state.mapKey;
    /* LO SPAWN E' GIA' UN RIPOSIZIONAMENTO ASSOLUTO: il remoto e' stato messo
     * esattamente dove l'amico dice di essere. Se sForceResync restasse alzato,
     * il PRIMO PASSO dopo lo spawn verrebbe degradato a SYNC e applicato con
     * TeleportRemote - cioe' un teletrasporto invece di una camminata. E'
     * meta' del difetto "striscia uscendo dalla porta": finche' il remoto non
     * esiste nessun evento e' applicabile, quindi nessuno consuma il resync
     * alzato dal cambio mappa dichiarato dall'amico, e resta li' ad aspettare
     * proprio il passo che dovrebbe animarsi.
     *
     * L'azzeramento va QUI, dopo che lo stato e' SPAWNED: mai in cima alla
     * funzione, o le tre uscite anticipate (giocatore non attivo, fuori vista,
     * spawn fallito) consumerebbero il resync senza aver piazzato niente. */
    r->forceResync = 0;
}

/* Il remoto e' pronto ad accettare un nuovo movimento? */
static int RemoteReadyForMovement(u32 oe)
{
    if (!ObjectEventIsHeldMovementActive((void *)oe))
        return 1;

    if (ObjectEventCheckHeldMovementStatus((void *)oe))
    {
        ObjectEventClearHeldMovement((void *)oe);
        return 1;
    }

    return 0;
}

static void QueueRemoteStep(u32 oe, u8 dir, u8 speed, int hurry)
{
    if (ObjectEventSetHeldMovement((void *)oe, ActionFor(dir, speed, hurry)) == 0)
    {
        BUMPF(movesQueued);
        if (hurry)
            BUMPF(movesHurried);
    }
    else
    {
        BUMPF(movesRejected);
    }
}

static s32 AbsDiff(s32 a, s32 b)
{
    return (a > b) ? (a - b) : (b - a);
}

/* Tiene lo sprite del remoto allineato a quello che l'amico e' adesso.
 *
 * Non e' agganciato al "cambio di stato" ma confronta ogni frame la grafica
 * effettiva dell'object event con quella attesa, e corregge se divergono.
 * Cosi' si ripara da solo in tutti i casi, non solo quando l'amico sale in
 * bici: in particolare copre l'ADOZIONE di un remoto rimasto da una sessione
 * precedente, che altrimenti si porterebbe dietro il vecchio sprite.
 *
 * IL CAMBIO DI SPRITE NON SI FA A META'. ObjectEventSetGraphicsId sostituisce
 * sprite->anims ma NON tocca sprite->animNum. Le ANIM_RUN_* stanno agli indici
 * 20-23 e ESISTONO SOLO nella tabella dello sprite a piedi (24 voci); quella
 * della mach bike ne ha 20. Cambiare sprite mentre il remoto corre lascerebbe
 * animNum a 20-23 su una tabella da 20: si legge fuori dall'array, si ottiene un
 * comando di animazione spazzatura e RequestSpriteFrameImageCopy (sprite.c:802)
 * ne ricava src e SIZE, facendo un DMA di dimensione arbitraria nella OBJ VRAM
 * a partire dai nostri tile. Risultato: il remoto renderizza spazzatura e gli
 * sprite vicini in VRAM si ritrovano tile di altri - cioe' NPC che cambiano
 * aspetto a caso.
 * Per questo si fa esattamente cio' che fa il gioco in PlayerAvatarTransition_*:
 * si cancella il movimento in corso e si chiama ObjectEventTurn, che riporta
 * animNum a una FACE_* (0-3) e riavvolge il frame. */
static void SyncRemoteGraphics(struct Remote *r, u32 oe)
{
    u8 want = GfxForRemote((u8)r->avatarState, (u8)r->gender);
    u8 have = oe_graphicsId(oe);

    if (have == want)
        return;

    /* SECONDA COSA CHE ObjectEventSetGraphicsId NON FA: riallocare i tile.
     * Cambia oam.shape/size e sprite->images, ma lo sprite continua a usare i
     * tile che gli sono stati assegnati alla creazione. Fra le grafiche del
     * GIOCATORE non si vedeva perche' hanno tutte size 512; fra quelle del
     * RIVALE no (256 a piedi, 512 in bici e in surf), e passare da 256 a 512
     * farebbe scrivere la copia dell'immagine oltre la propria allocazione,
     * dentro i tile degli sprite vicini.
     * Quando la dimensione cambia non si scambia a caldo: si ricrea. E' raro
     * (solo quando l'amico sale o scende dalla bici) e il percorso di respawn
     * esiste gia'. */
    if (GfxSize(want) != GfxSize(have))
    {
        /* Le coordinate PRIMA di distruggere: dopo, l'object event non c'e'
         * piu' e non si sa nemmeno dove rimetterlo. */
        s16 rx = oe_currentX(oe);
        s16 ry = oe_currentY(oe);

        BUMPF(gfxResizes);
        /* ForgetRemote distrugge da se', con RemoveRemoteObjectEvent: la vecchia
         * RemoveObjectEventByLocalIdAndMap scriveva fuori dall'array dei flag,
         * dentro il salvataggio. Vedi il commento su RemoveRemoteObjectEvent. */
        ForgetRemote(r);

        /* E SI RIMETTE SUBITO, NELLO STESSO FRAME (2026-08-30). Prima si
         * distruggeva e basta, lasciando che il respawn arrivasse dal giro
         * normale di OverworldTick - che pero' ha QUATTRO cancelli in fila:
         * spawnCooldown, SPAWN_DELAY_FRAMES sui frame passati in overworld,
         * DoorGateSpawn e InsideView. Con l'amico in bici, che produce passi
         * molto piu' in fretta, bastava che uno solo di quei cancelli restasse
         * chiuso e l'avatar spariva invece di cambiare: e' il difetto riferito
         * dal campo, «le bici non si vedono mai».
         *
         * Qui i cancelli non servono per costruzione: il tile e' quello in cui
         * il remoto era gia' disegnato un istante fa, quindi e' a schermo e la
         * porta non c'entra. Siamo in payload_cb1 (main loop), quindi chiamare
         * il gioco e' lecito. Se lo spawn fallisce davvero - slot tutti pieni -
         * TrySpawnRemote alza spawnFailures e mette il suo cooldown, e il giro
         * normale ritenta: la rete di sicurezza resta quella di sempre. */
        TrySpawnRemote(r, rx, ry);
        if (r->state == STATE_SPAWNED)
            BUMPF(resizeRespawns);
        return;
    }

    /* Un'azione di corsa in corso continuerebbe a spingere l'animNum fuori
     * range per tutta la sua durata, anche dopo il turn. */
    ObjectEventClearHeldMovementIfActive((void *)oe);
    ObjectEventSetGraphicsId((void *)oe, want);
    /* movementDirection, non facingDirection: e' l'argomento che passano
     * PlayerAvatarTransition_Normal / _MachBike / _Surfing
     * (field_player_avatar.c:852, 859, 878). */
    ObjectEventTurn((void *)oe, oe_movementDirection(oe));
    BUMPF(gfxChanges);
}

/* --- blob del surf --------------------------------------------------------
 *
 * In Gen 3 il Pokemon su cui si surfa NON fa parte dello sprite del giocatore:
 * e' un field effect separato, creato a parte da PlayerAvatarTransition_Surfing
 * (field_player_avatar.c:875). Il nostro remoto aveva la grafica giusta e nessun
 * blob, quindi sembrava galleggiare sull'acqua.
 *
 * Il blob insegue l'object event indicato da gFieldEffectArguments[2]
 * (field_effect_helpers.c:1010): funziona per un object event QUALUNQUE, non
 * solo per quello del giocatore. */

/* «HA GIA' LA BOLLA?» E' UNA DOMANDA A CUI NON BASTA UN ID (2026-08-30).
 *
 * Prima qui si guardava solo se `fieldEffectSpriteId` fosse un numero
 * plausibile. Il guaio e' che quel byte vive DENTRO l'object event e nessuno
 * lo azzera: ForgetRemote, quando trova l'object event gia' morto
 * (`!oe_active`), lo lascia com'e', e il gioco che resetta gli sprite
 * uscendo da una lotta o cambiando mappa non ne sa niente. Al ritorno
 * nell'overworld restava li' un id VECCHIO, che punta a uno slot ormai
 * libero o riciclato da un altro sprite qualunque.
 *
 * Da quell'id stantio nascevano i due difetti riferiti dal campo, opposti fra
 * loro: SyncRemoteSurfBlob credeva che la bolla ci fosse gia' e non la
 * ricreava mai piu' - «l'amico e' in surf senza animazione di surf» - e
 * DestroyRemoteSurfBlob, nel caso simmetrico, avrebbe distrutto lo sprite di
 * qualcun ALTRO.
 *
 * Adesso la domanda si fa allo sprite, non all'id: dev'essere in uso E deve
 * dichiarare di seguire proprio questo object event (data[2] = sPlayerObjId,
 * quello che FldEff_SurfBlob gli scrive dentro alla nascita). Se non torna,
 * l'id e' spazzatura e si azzera subito: cosi' SyncRemoteSurfBlob ricrea nello
 * stesso giro invece di restare bloccato per sempre. */
static int RemoteHasBlob(u32 oe)
{
    u8 id = oe_fieldEffectSpriteId(oe);
    u32 sp;

    if (id == 0 || id >= MAX_SPRITES)
        return 0;

    sp = Sprite(id);
    /* Il confronto si fa MOLTIPLICANDO l'indice della bolla, non dividendo
     * l'indirizzo dell'object event: un object event e' lungo 0x24 byte, che
     * non e' una potenza di due, e la divisione farebbe chiamare
     * __aeabi_uidiv - che in una build freestanding come questa non esiste
     * (il link fallisce). Moltiplicare e' anche piu' economico. */
    if ((sprite_flags3E(sp) & SPRITE_FLAG_IN_USE)
        && ObjectEvent((u32)(u16)sprite_data2(sp)) == oe)
        return 1;

    oe_fieldEffectSpriteId(oe) = 0;
    BUMPF(blobScrubbed);
    return 0;
}

/* SOLO DAL MAIN LOOP (payload_cb1), MAI DALL'IRQ - 2026-08-21, «il Pokemon
 * del Surf esce sbagliato: negativo, uovo, punto di domanda».
 *
 * gFieldEffectArguments e' la casella con cui il GIOCO si passa i parametri
 * dei field effect, e la usa in due tempi nello stesso frame: Surf scrive
 * [0] = indice di squadra, poi FldEff_FieldMoveShowMonInit lo rilegge, lo
 * trasforma in specie/OT/personalita' e FldEff_FieldMoveShowMon li consuma
 * (field_effect.c:1927, 2587-2592, 2578). Finche' questa funzione girava nel
 * VBlank, un IRQ caduto in mezzo a quei passi (il frame in cui parte Surf e'
 * pesante e sfora) ci metteva dentro la NOSTRA x: il gioco leggeva
 * gPlayerParty[x] con x >= 7, cioe' FUORI dalla squadra, il checksum non
 * tornava e GetBoxMonData marcava quella memoria come UOVO CATTIVO (tre bit
 * scritti a gPlayerParty + x*100: e' l'uovo visto sul fisico). Per x fra 14 e
 * 27 quei byte stanno in SaveBlock2, fra 28 e 212 in SaveBlock1: un difetto da
 * chiudere per sempre, non da mitigare.
 *
 * Dal main loop non c'e' nessuna corsa (il nostro cb1 gira PRIMA del callback1
 * del gioco, in sequenza), e il ripristino degli argomenti lascia la casella
 * esattamente come l'abbiamo trovata: impronta zero anche sui parametri. */
static void CreateRemoteSurfBlob(struct Remote *r, u32 oe)
{
    u8 spriteId;
    u32 a0, a1, a2;

    if (RemoteHasBlob(oe))
        return;

    a0 = gFieldEffectArguments(0);
    a1 = gFieldEffectArguments(1);
    a2 = gFieldEffectArguments(2);
    gFieldEffectArguments(0) = (u32)(s32)oe_currentX(oe);
    gFieldEffectArguments(1) = (u32)(s32)oe_currentY(oe);
    gFieldEffectArguments(2) = r->objectId;

    spriteId = FieldEffectStart(FLDEFF_SURF_BLOB);

    gFieldEffectArguments(0) = a0;
    gFieldEffectArguments(1) = a1;
    gFieldEffectArguments(2) = a2;

    if (spriteId >= MAX_SPRITES)
        return;

    oe_fieldEffectSpriteId(oe) = spriteId;
    SetSurfBlob_BobState(spriteId, BOB_PLAYER_AND_MON);
    BUMPF(blobsCreated);
}

/* Gli offset di disegno dello sprite, azzerati.
 *
 * `x2/y2` sono lo scostamento in pixel che il gioco somma alla posizione dello
 * sprite per i salti, il levitare e IL DONDOLIO DEL SURF: UpdateBobbingEffect
 * (field_effect_helpers.c:1128) scrive `y2` del CAVALIERE a ogni frame,
 * mentre la bolla galleggia. Il gioco lo riporta a zero perche' si smonta con
 * un salto (le azioni di salto finiscono con `sprite->y2 = 0`), ma il nostro
 * amico remoto smonta cambiando stato: la bolla sparisce e il residuo resta.
 * Risultato: un allenatore sollevato o affondato di qualche pixel per il
 * resto della partita, che nessun riposizionamento correggeva -
 * MoveObjectEventToMapCoords tocca `x/y`, non `x2/y2`. */
__attribute__((noinline)) static void ClearRemoteSpriteOffset(u32 sp)
{
    if (sprite_x2(sp) | sprite_y2(sp))
    {
        sprite_x2(sp) = 0;
        sprite_y2(sp) = 0;
        BUMPF(pixelFixes);
    }
}

/* Da chiamare anche quando il remoto viene rimosso o ricreato: un blob orfano
 * resterebbe a galleggiare da solo sull'acqua. */
static void DestroyRemoteSurfBlob(u32 oe)
{
    if (!RemoteHasBlob(oe))
        return;

    DestroySprite((void *)Sprite(oe_fieldEffectSpriteId(oe)));
    oe_fieldEffectSpriteId(oe) = 0;
    BUMPF(blobsDestroyed);
    /* Il dondolio scriveva y2 sul CAVALIERE a ogni frame: tolta la bolla, il
     * residuo resterebbe li' per sempre (vedi ClearRemoteSpriteOffset). */
    ClearRemoteSpriteOffset(Sprite(oe_spriteId(oe)));
}

/* Il blob c'e' se e solo se l'amico sta surfando. Come per la grafica, si
 * confronta lo stato voluto con quello effettivo a ogni frame invece di
 * agganciarsi alle transizioni: cosi' si ripara da solo in ogni percorso. */
static void SyncRemoteSurfBlob(struct Remote *r, u32 oe)
{
    if (r->avatarState == PLAYER_AVATAR_STATE_SURFING)
        CreateRemoteSurfBlob(r, oe);
    else
        DestroyRemoteSurfBlob(oe);
}

/* LA STESSA CASELLA, SCRITTA DAL GIOCO PER CONTO NOSTRO (2026-09-26, «ogni
 * tanto la MN fuori lotta mostra un Pokemon buggato: negativo, MissingNo»).
 *
 * CreateRemoteSurfBlob ha chiuso le scritture NOSTRE su gFieldEffectArguments.
 * Restavano quelle che il GIOCO fa muovendo l'avatar dell'amico: a ogni passo
 * UpdateObjectEventCurrentMovement chiama DoGroundEffects_* (erba alta, orme
 * nella sabbia, pozzanghere, acqua bassa, ombra e polvere dei salti), e ognuno
 * scrive [0] = x del tile e FieldEffectStart (event_object_movement.c:7804 e
 * seguenti). Nel gioco vero e' innocuo, perche' durante uno script gli NPC
 * sono congelati. Il nostro no: FreezeObjectEvent salta chi ha un movimento
 * in corso (:8144) e il movimento "tenuto" gira anche da congelati (:4934) -
 * ed e' cosi' che l'amico continua a camminare mentre tu leggi un dialogo.
 *
 * Le MN pero' lasciano l'indice di squadra in [0] e lo rileggono DOPO,
 * a frame di distanza:
 *   - da script (albero, masso, Forza, Surf, Cascata, Sub, Forzasegreta):
 *     `setfieldeffectargument 0, VAR_RESULT`, poi la domanda SI'/NO, poi
 *     `dofieldeffect` (data/scripts/field_move_scripts.inc:7-14, surf.inc:5-10):
 *     la finestra dura quanto ci metti a rispondere;
 *   - dal menu squadra (Taglio, Spaccaroccia, Forza, Flash, Fossa...): la posa
 *     del giocatore dura qualche frame prima di FLDEFF_FIELD_MOVE_SHOW_MON_INIT
 *     (fldeff_rocksmash.c:57-86).
 * Se in quella finestra l'amico fa un passo nell'erba, [0] diventa la sua x e
 * FldEff_FieldMoveShowMonInit legge gPlayerParty[x] FUORI dalla squadra
 * (field_effect.c:2588): specie a caso = MissingNo/punto di domanda, dati a
 * caso = palette sbagliata (il "negativo"), checksum rotto = uovo. Ed e' lo
 * stesso GetBoxMonData che marca "uovo cattivo" scrivendo tre bit in quella
 * memoria: per x oltre 13 e' SaveBlock2/SaveBlock1 (vedi CreateRemoteSurfBlob).
 *
 * LA CURA e' la stessa della bolla, ma nel punto giusto: l'unico codice che
 * scrive la casella per conto dell'amico e' la callback del SUO sprite (tutti
 * i field effect dei passi partono e consumano gli argomenti dentro
 * FieldEffectStart, nello stesso giro), quindi la si avvolge: si fotografano
 * gli 8 argomenti, si chiama l'originale, si rimettono. Il gioco vede la
 * casella come se l'amico non esistesse; l'erba sotto l'amico si muove
 * lo stesso. Nessun simbolo nuovo: l'originale si legge dallo sprite. */
#define FX_ARGS 8u

static void RemoteSpriteCb(void *sprite)
{
    u32 save[FX_ARGS];
    u32 i, changed = 0;
    u32 oeId = (u16)sprite_data0((u32)sprite);   /* sObjEventId */
    void (*cb)(void *) = 0;

    for (i = 0; i < N_REMOTES; i++)
        if (g_remotes[i].objectId == oeId)
            cb = g_remotes[i].sprCb;
    /* Nessun padrone: lo sprite non puo' essere piu' nostro (ForgetRemote e
     * il respawn distruggono prima lo sprite). Non si chiama un puntatore
     * che non sappiamo da dove venga. */
    if (!cb)
        return;

    for (i = 0; i < FX_ARGS; i++)
        save[i] = gFieldEffectArguments(i);
    cb(sprite);
    for (i = 0; i < FX_ARGS; i++)
    {
        changed |= gFieldEffectArguments(i) ^ save[i];
        gFieldEffectArguments(i) = save[i];
    }
    if (changed)
        BUMPF(fxArgsKept);
}

/* Si rifa' a ogni frame, perche' il gioco ricrea lo sprite dal template quando
 * vuole (ritorno da una lotta, respawn): la callback tornata "del gioco" e' il
 * segnale, e l'originale si riprende da li'. Per-slot, non globale: una
 * callback catturata male non deve fermare anche gli altri amici. */
static void GuardRemoteSprite(struct Remote *r, u32 oe)
{
    u32 spr, cur;
    u32 mine = (u32)&RemoteSpriteCb | 1u;

    if (oe_spriteId(oe) >= MAX_SPRITES)
        return;
    spr = Sprite(oe_spriteId(oe));
    cur = sprite_callback(spr);
    if (cur == mine || !cur)
        return;
    r->sprCb = (void (*)(void *))cur;
    sprite_callback(spr) = mine;
}

/* Il giro della bolla, dal main loop (vedi CreateRemoteSurfBlob): stessa
 * disciplina di IndicatorTick, stessi controlli d'identita' sullo slot.
 * Dallo stesso giro si avvolge la callback dello sprite (GuardRemoteSprite):
 * payload_cb1 gira prima di callback2, cioe' prima di AnimateSprites. */
static void SurfBlobTick(void)
{
    u32 i;

    if (!g_state.inOverworld)
        return;
    for (i = 0; i < N_REMOTES; i++)
    {
        struct Remote *r = &g_remotes[i];
        u32 oe;

        if (r->state != STATE_SPAWNED || r->objectId >= OBJECT_EVENTS_COUNT)
            continue;
        oe = ObjectEvent(r->objectId);
        if (!oe_active(oe) || oe_localId(oe) != REMOTE_LID(i))
            continue;
        GuardRemoteSprite(r, oe);
        SyncRemoteSurfBlob(r, oe);
    }
}

/* Riposiziona il remoto di forza.
 *
 * Si usa solo come CORREZIONE, quando la deriva e' troppo grande per essere
 * riassorbita camminando. Nel funzionamento normale il remoto si muove sempre
 * accodando movement action, mai scrivendo coordinate.
 *
 * MoveObjectEventToMapCoords (event_object_movement.c:2133) e' la routine del
 * gioco per gli object event ORDINARI: imposta le coordinate, riposiziona lo
 * SPRITE con gli offset centerToCornerVec e il +8/+16, azzera i dati di field
 * effect e resetta la camera se l'object e' tracciato.
 *
 * La versione precedente ricopiava InitLinkPlayerObjectEventPos (overworld.c:2958)
 * ed era sbagliata: quella e' scritta per gli object event dei giocatori link,
 * che sono esenti dal culling e il cui sprite viene riposizionato ogni frame da
 * SpriteCB_LinkPlayer. Sul nostro object event ordinario non spostava lo sprite -
 * le coordinate logiche saltavano e il pixel restava indietro, cosi' il passo
 * successivo interpolava dalla posizione sbagliata - e scriveva in initialCoords
 * dei PIXEL, che RemoveObjectEventIfOutsideView confronta invece con una finestra
 * in TILE. */
static void TeleportRemote(u32 oe, s16 x, s16 y)
{
    ObjectEventClearHeldMovementIfActive((void *)oe);
    MoveObjectEventToMapCoords((void *)oe, x, y);
    /* NIENTE COMPENSAZIONE DELLA CAMERA, ed e' una lezione pagata.
     * Il 2026-08-28 sembrava che MoveObjectEventToMapCoords piazzasse lo
     * sprite sbagliato di gFieldCamera (0..15 px) quando la camera e' a meta'
     * tile, e qui c'era la somma per rimediare. Il banco
     * mgba/banco_subpixel.lua ha misurato il contrario, forzando i
     * riposizionamenti proprio a camera in movimento: SENZA la somma i
     * riposizionamenti sono allineati al pixel (5 su 5, confrontati con un
     * NPC fermo), CON la somma finivano fuori di 7-13 px. L'aritmetica sulle
     * due formule del gioco era sbagliata: questa routine piazza bene da
     * sola, e va lasciata in pace. */
    /* Qui NON si azzerano x2/y2: il solo residuo che il nostro remoto puo'
     * accumulare e' il dondolio del surf, e quello lo azzera gia'
     * DestroyRemoteSurfBlob nel momento in cui nasce e muore. Le azioni di
     * salto del motore rimettono y2 a zero da sole in fondo all'animazione. */
    /* MoveObjectEventToMapCoords aggiorna current e previous, NON initialCoords
     * (SetObjectEventCoords, event_object_movement.c:2125). E
     * RemoveObjectEventIfOutsideView tiene in vita l'object event se e' dentro la
     * finestra l'UNA O L'ALTRA coppia (event_object_movement.c:1698): con
     * initialCoords stantie il culling del gioco e la nostra InsideView() non
     * dicono la stessa cosa, e il remoto sopravvive dove non dovrebbe. */
    oe_initialX(oe) = x;
    oe_initialY(oe) = y;
    BUMPF(rxCorrections);
}

/* ========================================================================== */
/* Porte: l'amico che entra e che esce da un edificio                        */
/* ========================================================================== */

/* COME SI RICONOSCE UN INGRESSO, E PERCHE' NON SI ASPETTA IL CAMBIO MAPPA.
 *
 * Il warp di una porta animata scatta quando il giocatore sta SOTTO la porta e
 * tiene premuto su (TryDoorWarp, field_control_avatar.c:833: pretende
 * direction == DIR_NORTH e MetatileBehavior_IsWarpDoor sul tile davanti). Poi
 * Task_DoDoorWarp lo fa camminare DENTRO la porta con un
 * MOVEMENT_ACTION_WALK_NORMAL_UP vero e proprio - quindi le sue currentCoords
 * cambiano, e il mittente EMETTE quel passo come qualunque altro.
 *
 * Ne segue che il segnale d'ingresso ci arriva prima del cambio mappa, ed e'
 * inequivocabile: un PASSO verso NORD la cui destinazione e' un
 * MB_ANIMATED_DOOR. Quel tile e' impassabile per chiunque non stia usando il
 * warp, quindi non esistono falsi positivi.
 *
 * Aspettare il cambio mappa, come sembrerebbe naturale, sarebbe invece
 * sbagliato: a quel punto il remoto e' GIA' salito sulla soglia e la porta non
 * sta piu' un tile piu' su. */

/* L'ASIMMETRIA CON DoorGateStep E' VOLUTA, NON UN DIFETTO DA ALLINEARE.
 *
 * Questo predicato (usato in USCITA, da DoorGateSpawn) accetta MB_ANIMATED_DOOR
 * e MB_PETALBURG_GYM_DOOR; quello in INGRESSO, dentro DoorGateStep, pretende
 * == MB_ANIMATED_DOOR e basta. Il motivo sta nel gioco:
 *
 *  - si ENTRA camminando verso nord solo dove MetatileBehavior_IsWarpDoor dice
 *    di si' (metatile_behavior.c:220-226), e quella accetta SOLO
 *    MB_ANIMATED_DOOR. Le porte della palestra di Petalburg si warpano via
 *    script, quindi in ingresso non possono comparire;
 *  - si ESCE anche da quelle, e li' il gioco stesso usa
 *    MetatileBehavior_IsDoor, che le accetta entrambe.
 *
 * Allineare i due predicati significherebbe o perdere l'uscita dalla palestra,
 * o aprire porte per passi che non sono warp. */
static int IsAnimatedDoorAt(s16 x, s16 y)
{
    u32 b = MapGridGetMetatileBehaviorAt((s32)x, (s32)y);

    return b == MB_ANIMATED_DOOR || b == MB_PETALBURG_GYM_DOOR;
}

static void DoorSetState(u32 state)
{
    sDoorState = state;
    sDoorTimer = 0;
    g_state.doorState = state;
}

/* Si molla tutto. Una porta lasciata aperta ci resta fino al ricaricamento della
 * mappa, quindi chiudere e' sempre l'ultima cosa da fare - anche quando la
 * sequenza non e' andata come doveva. */
static void DoorAbort(void)
{
    if (sDoorState == DOOR_ST_OPENING || sDoorState == DOOR_ST_WALKING
        || sDoorState == DOOR_ST_EXIT_OPEN || sDoorState == DOOR_ST_EXIT_STEP)
        FieldAnimateDoorClose((u32)sDoorX, (u32)sDoorY);

    DoorSetState(DOOR_ST_NONE);
}

static void DoorAbortIfOwner(u32 slot)
{
    if (sDoorState != DOOR_ST_NONE && sDoorOwner == slot)
        DoorAbort();
}

/* Il tratto comune dei due gate: prova ad aprire la porta a (x,y) e ad armare
 * la FSM per questo slot. Ritorna 0 - con doorBusy contato - se un'altra
 * animazione e' in corso (UNA SOLA PORTA ANIMATA ALLA VOLTA IN TUTTO IL
 * GIOCO: StartDoorAnimationTask ritorna -1 se un Task_AnimateDoor e' gia'
 * attivo, field_door.c:437) o se il gioco rifiuta: il chiamante ripiega
 * sulla sparizione secca. Il suono c'e' solo in INGRESSO: ScrCmd_opendoor
 * (scrcmd.c:2050) e' PlaySE + FieldAnimateDoorOpen, mentre Task_ExitDoor
 * in uscita non suona (field_screen_effect.c:317) - e si tiene uguale. */
static int DoorTryOpen(u32 slot, s16 x, s16 y, u32 st, int sound)
{
    if (FieldIsDoorAnimationRunning())
    {
        BUMPF(doorBusy);
        return 0;
    }

    if (sound)
        PlaySE((u16)GetDoorSoundEffect((u32)x, (u32)y));

    if (FieldAnimateDoorOpen((u32)x, (u32)y) < 0)
    {
        BUMPF(doorBusy);
        return 0;
    }

    sDoorX = x;
    sDoorY = y;
    sDoorOwner = slot;
    DoorSetState(st);
    return 1;
}

/* Fa avanzare gli stati che NON dipendono dalla coda RX. Quelli che dipendono
 * (l'attesa dell'apertura, prima del passo e prima dello spawn) li fanno
 * avanzare DoorGateStep e DoorGateSpawn, che sono gli unici punti in cui si sa
 * se c'e' ancora qualcosa da applicare. */
static void DoorTick(void)
{
    struct Remote *r = &g_remotes[sDoorOwner];

    if (sDoorState == DOOR_ST_NONE)
        return;

    sDoorTimer++;

    switch (sDoorState)
    {
    case DOOR_ST_OPENING:
    case DOOR_ST_EXIT_OPEN:
        /* L'avanzamento normale lo fanno i due gate. Qui c'e' solo la rete di
         * sicurezza: se nessuno li chiama piu' (il remoto e' stato tolto, la
         * coda si e' svuotata) la FSM resterebbe appesa e la porta aperta. */
        if (sDoorTimer >= DOOR_GIVEUP_FRAMES)
        {
            BUMPF(doorTimeouts);
            DoorAbort();
        }
        break;

    case DOOR_ST_WALKING:
        if (r->state != STATE_SPAWNED)
        {
            DoorAbort();
            break;
        }
        {
            u32 oe = ObjectEvent(r->objectId);
            int late = (sDoorTimer >= DOOR_GIVEUP_FRAMES);

            if (RemoteReadyForMovement(oe) || late)
            {
                if (late)
                    BUMPF(doorTimeouts);
                /* Il gioco fa SetPlayerVisibility(FALSE) esattamente qui, un
                 * istante prima di far partire la chiusura (Task_DoDoorWarp,
                 * caso 2). Distruggere e' indistinguibile a schermo e ci evita
                 * di dover scrivere - e poi rimettere - il bit `invisible`. */
                ForgetRemote(r);
                FieldAnimateDoorClose((u32)sDoorX, (u32)sDoorY);
                BUMPF(doorEnters);
                DoorSetState(DOOR_ST_CLOSING);
            }
        }
        break;

    case DOOR_ST_CLOSING:
        if (!FieldIsDoorAnimationRunning() || sDoorTimer >= DOOR_WAIT_FRAMES)
            DoorSetState(DOOR_ST_HOLD);
        break;

    case DOOR_ST_HOLD:
        {
            s16 vx, vy;
            u32 via;

            /* Si esce appena l'amico dichiara una mappa che qui non si puo'
             * disegnare: e' il suo cambio mappa che arriva. Il timeout copre il
             * caso in cui non arrivi affatto. */
            if (sDoorTimer >= DOOR_HOLD_FRAMES || !RemoteViewNow(r, &vx, &vy, &via))
                DoorSetState(DOOR_ST_NONE);
        }
        break;

    case DOOR_ST_EXIT_STEP:
        if (r->state != STATE_SPAWNED)
        {
            DoorAbort();
            break;
        }
        {
            u32 oe = ObjectEvent(r->objectId);
            int off = (oe_currentX(oe) != sDoorX || oe_currentY(oe) != sDoorY);
            int late = (sDoorTimer >= DOOR_GIVEUP_FRAMES);

            /* Come Task_ExitDoor caso 2: si chiude quando il passo verso il
             * basso e' FINITO, non quando e' cominciato. Il passo glielo manda
             * l'amico dalla rete, non lo accodiamo noi. */
            if ((off && RemoteReadyForMovement(oe)) || late)
            {
                if (late)
                    BUMPF(doorTimeouts);
                FieldAnimateDoorClose((u32)sDoorX, (u32)sDoorY);
                DoorSetState(DOOR_ST_NONE);
            }
        }
        break;
    }
}

/* Il PASSO in arrivo si puo' applicare adesso? Ritorna 0 se prima la porta deve
 * finire di aprirsi: in quel caso l'evento RESTA IN CODA e si riprova al frame
 * dopo, esattamente come quando il remoto e' ancora in movimento. */
static int DoorGateStep(u32 slot, u8 dir, u32 via, s16 tx, s16 ty)
{
    if (sDoorState == DOOR_ST_OPENING && sDoorOwner == slot)
    {
        if (FieldIsDoorAnimationRunning() && sDoorTimer < DOOR_WAIT_FRAMES)
            return 0;

        if (sDoorTimer >= DOOR_WAIT_FRAMES)
            BUMPF(doorTimeouts);

        DoorSetState(DOOR_ST_WALKING);
        return 1;
    }

    /* La FSM e' occupata (da noi in un altro stato, o da un ALTRO slot): si
     * ripiega sulla sparizione secca, come quando la porta e' del giocatore. */
    if (sDoorState != DOOR_ST_NONE)
        return 1;

    if (dir != DIR_NORTH)
        return 1;

    /* Niente animazioni oltre il bordo: li' il tile esiste in memoria ma il
     * disegno della porta passa da CurrentMapDrawMetatileAt, che ragiona sulla
     * finestra della mappa CORRENTE. L'amico che entra in una casa della route
     * accanto sparisce e basta - come oggi. */
    if (via != 0)
        return 1;

    /* SOLO MB_ANIMATED_DOOR, non IsAnimatedDoorAt: in ingresso e' l'unico
     * comportamento che MetatileBehavior_IsWarpDoor accetta
     * (metatile_behavior.c:220-226), quindi camminando verso nord non si puo'
     * entrare in nient'altro. L'asimmetria con l'uscita e' spiegata sopra
     * IsAnimatedDoorAt. */
    if (MapGridGetMetatileBehaviorAt((s32)tx, (s32)ty) != MB_ANIMATED_DOOR)
        return 1;

    return DoorTryOpen(slot, tx, ty, DOOR_ST_OPENING, 1) ? 0 : 1;
}

/* Lo spawn si puo' fare adesso? Ritorna 0 se prima la porta deve aprirsi.
 * L'amico che esce da un edificio ricompare ESATTAMENTE sul tile della porta:
 * e' li' che Task_ExitDoor mette il giocatore prima di farlo scendere. */
static int DoorGateSpawn(u32 slot, u32 via, s16 x, s16 y)
{
    if (sDoorState == DOOR_ST_EXIT_OPEN && sDoorOwner == slot)
    {
        if (FieldIsDoorAnimationRunning() && sDoorTimer < DOOR_WAIT_FRAMES)
            return 0;

        if (sDoorTimer >= DOOR_WAIT_FRAMES)
            BUMPF(doorTimeouts);

        return 1;
    }

    if (sDoorState != DOOR_ST_NONE)
        return 1;

    if (via != 0 || !IsAnimatedDoorAt(x, y))
        return 1;

    /* Fuori dalla finestra di culling TrySpawnRemote non spawnerebbe comunque:
     * aprire una porta che nessuno vede sarebbe solo un modo per occupare
     * l'unico slot di animazione disponibile. */
    if (!InsideView(x, y))
        return 1;

    return DoorTryOpen(slot, x, y, DOOR_ST_EXIT_OPEN, 0) ? 0 : 1;
}

/* Consuma la coda RX, in DUE FASI (2026-08-25, fino a 4 giocatori).
 *
 * FASE 1 - DrainMailbox: la mailbox condivisa si svuota sempre, per intero.
 * STATO e SCHEDA si consumano al volo (non sono posizionali e non hanno
 * bisogno d'ordine rispetto ai passi); le copie del doppio invio si scartano
 * QUI, sul seq dell'ultimo evento ACCODATO dello stesso slot; tutto il resto
 * finisce nella coda del SUO slot (il mittente sta nel nibble alto del tipo,
 * vedi EVENT_SLOT).
 *
 * FASE 2 - ConsumeSlotEvents, per ogni slot: la stessa macchina a stati di
 * prima, ma su una coda che contiene solo eventi di QUEL remoto. E' la cura
 * del BLOCCO IN TESTA: con la coda condivisa, il passo di A ancora in
 * animazione (RemoteReadyForMovement falso -> return) congelava anche i passi
 * di B e C accodati dietro di lui - ognuno dei tre cammina col SUO ritmo, e
 * il ritmo lo detta proprio "il passo prima e' finito".
 *
 * I SYNC si consumano sempre e subito: sono la verita' sulla posizione.
 * I PASSO si consumano UNO ALLA VOLTA, e solo quando il remoto ha finito il
 * movimento precedente: e' la coda di movement action a fare da buffer, ed e'
 * il motivo per cui la camminata resta fluida anche se i pacchetti arrivano
 * a raffica o in ritardo.
 *
 * IL DOPPIO INVIO, e perche' il dedup sta nella FASE 1.
 * ---------------------------------------------------------------------------
 * L'ultimo tratto verso il gioco perde eventi (SIO, ~8% misurato) e la cura
 * e' la copia (net/client.py, --copie). Le copie viaggiano APPAIATE: il
 * client le emette back-to-back e il canale conserva l'ordine, quindi
 * confrontare il seq con l'ultimo ACCODATO dello stesso slot le prende
 * tutte. Scartarle all'ingresso invece che al consumo ha un vantaggio in
 * piu': non occupano la coda per-slot, che cosi' misura solo eventi veri.
 * QUI e non in SioDeliverFrame perche' il dedup deve valere per ENTRAMBI i
 * trasporti: su mGBA la mailbox la scrive il Lua, e senza un punto solo il
 * banco di prova in emulatore non proverebbe la stessa cosa dell'hardware.
 * Si deduplicano SOLO PASSO e GIRA: sono i soli tipi che il client duplica.
 * Un SYNC ripetuto e' idempotente, e il VIA resta fuori APPOSTA (seq 0
 * fabbricato dal client: scartarlo per collisione di numero lascerebbe
 * l'avatar piantato per sempre). */
static void DrainMailbox(void)
{
    while (g_mailbox.rxTail != g_mailbox.rxHead)
    {
        u32 tail = g_mailbox.rxTail;
        u8  type = g_mailbox.rx[tail].type;
        u8  kind = EVENT_KIND(type);
        u32 slot = EVENT_SLOT(type);
        struct Remote *r;

        if (slot >= N_REMOTES)
            slot = N_REMOTES - 1;   /* il client non lo produce: cintura */
        r = &g_remotes[slot];

        /* LO STATO DELL'AMICO NON E' UNA POSIZIONE: si consuma da se' e non
         * tocca niente di posizionale (ne' resync, ne' mapKey, ne' target).
         * Viene emesso anche da fuori dall'overworld, dove la mappa e'
         * l'ultima nota, e non deve poter alzare un resync. */
        if (kind == EVENT_STATUS)
        {
            r->status = g_mailbox.rx[tail].dir;
            BUMPF(statusRx);
        }
        /* LA SCHEDA DELL'AMICO ARRIVA A PEZZI (Consegna B). Come lo STATO: i
         * byte vanno DIRETTAMENTE in gTrainerCards[1 + slot] - l'array ha 4
         * voci, una per giocatore della stanza, col passo VERO 0x64. Un chunk
         * duplicato riscrive gli stessi byte: idempotente. */
        else if (kind == EVENT_CARD)
        {
            u8 idx = g_mailbox.rx[tail].dir;

            if (idx < CARD_CHUNKS)
            {
                volatile u8 *dst = (volatile u8 *)(ADDR_gTrainerCards
                                                   + CARD_ENTRY_SIZE * (slot + 1u));
                const volatile u8 *src = (const volatile u8 *)&g_mailbox.rx[tail];
                u32 off = (u32)idx * CARD_CHUNK_BYTES;
                u32 k;

                /* kCardOff: l'inverso esatto dell'impacchettamento di
                 * EmitCardChunk, dalla stessa tabella. */
                for (k = 0; k < CARD_CHUNK_BYTES && off + k < CARD_SIZE; k++)
                    dst[off + k] = src[kCardOff[k]];

                r->cardRxBitmap |= (1u << idx);
                BUMPF(cardChunksRx);
            }
        }
        else if ((kind == EVENT_STEP || kind == EVENT_TURN)
                 && SEQ_TAG(g_mailbox.rx[tail].seq) == r->lastEnqSeq)
        {
            BUMPF(rxDupes);
        }
        else if (kind == EVENT_STEP || kind == EVENT_SYNC
                 || kind == EVENT_TURN || kind == EVENT_LEAVE)
        {
            u32 next;

            if (kind == EVENT_STEP || kind == EVENT_TURN)
                r->lastEnqSeq = SEQ_TAG(g_mailbox.rx[tail].seq);

            next = RQ_WRAP(r->qHead);
            if (next == r->qTail)
            {
                /* Coda dello slot piena: si butta il PIU' VECCHIO, non il
                 * nuovo - il nuovo e' piu' vicino alla verita' - e si alza il
                 * resync: il primo evento applicabile riposiziona di forza e
                 * il buco non lascia deriva. */
                r->qTail = RQ_WRAP(r->qTail);
                r->forceResync = 1;
                BUMPF(rxQueueDrops);
            }
            {
                const volatile u8 *src = (const volatile u8 *)&g_mailbox.rx[tail];
                u8 *dst = (u8 *)&g_rq[slot][r->qHead];
                u32 k;

                for (k = 0; k < sizeof(struct NetEvent); k++)
                    dst[k] = src[k];
            }
            r->qHead = next;
        }
        /* Qualunque altro tipo (un CLUB vagante, un tipo di un peer piu'
         * nuovo): si butta. Accodarlo farebbe finire mappa e coordinate a
         * zero dentro lo stato dichiarato dell'amico. */

        g_mailbox.rxTail = WRAP(tail, RX_SLOTS);
        g_mailbox.rxApplied++;
    }
}

static void ConsumeSlotEvents(u32 slot)
{
    struct Remote *r = &g_remotes[slot];

    /* LA CODA SI CONGELA MENTRE UNA PORTA STA ANIMANDO - ma solo quella del
     * PROPRIETARIO della porta: gli altri slot camminano normalmente.
     *
     * DOOR_ST_WALKING: il remoto cammina dentro la porta e un SYNC lo
     * teletrasporterebbe via dalla soglia a meta' animazione.
     *
     * DOOR_ST_EXIT_OPEN: qui lo stato e' ancora STATE_IDLE, quindi
     * `applicable` e' falso e gli eventi verrebbero CONSUMATI E BUTTATI,
     * aggiornando solo target. L'apertura vanilla dura ~20 VBlank
     * (sDoorOpenAnimFrames, field_door.c:135-142): se il PASSO con cui
     * l'amico esce dalla porta cade in quella finestra viene perso, e quando
     * finalmente si spawna lo si mette gia' fuori dalla soglia, senza
     * camminata. E' l'altra meta' del difetto "striscia".
     *
     * Il limite sul pending e' obbligatorio (DOOR_FREEZE_MAX_PENDING_SLOT):
     * DOOR_ST_EXIT_OPEN puo' durare fino a DOOR_GIVEUP_FRAMES se nessuno
     * chiama piu' DoorGateSpawn, e la coda per-slot traboccherebbe.
     *
     * NON si congela durante la chiusura: li' il remoto non esiste piu' e
     * lasciar scorrere gli eventi serve - e' cosi' che DOOR_ST_HOLD si
     * accorge che e' arrivato il cambio mappa dell'amico. */
    if ((sDoorState == DOOR_ST_WALKING || sDoorState == DOOR_ST_EXIT_OPEN)
        && sDoorOwner == slot
        && RqLen(r) < DOOR_FREEZE_MAX_PENDING_SLOT)
        return;

    while (r->qTail != r->qHead)
    {
        u32 tail = r->qTail;
        const struct NetEvent *qe = &g_rq[slot][tail];
        u8  type = EVENT_KIND(qe->type);
        u8  dir = qe->dir;
        u8  speed = qe->speed;
        s16 x = qe->x;
        s16 y = qe->y;
        u32 mapKey = ((u32)qe->mapGroup << 8)
                   | (u32)qe->mapNum;
        /* Le coordinate dell'evento sono nello spazio della mappa DEL
         * MITTENTE. Qui si calcola dove vanno disegnate da noi: stessa mappa
         * -> identiche, route connessa -> tradotte, altrimenti non
         * rappresentabili.
         *
         * La traduzione avviene SOLO al momento di applicare. Tutto cio' che
         * confronta l'evento con quel che l'amico aveva detto prima (la
         * regola "un passo e' un tile") resta nello spazio DELL'AMICO: si
         * paragona dichiarato con dichiarato, mai dichiarato con tradotto. */
        s16 lx, ly;
        u32 via;
        int representable = MapRemoteToLocal(r, mapKey, x, y, &lx, &ly, &via);
        int applicable = (r->state == STATE_SPAWNED && representable);
        /* I CONTATORI DEL BORDO SI ALZANO SOLO QUANDO L'EVENTO VIENE
         * CONSUMATO: un PASSO che resta in coda viene rivalutato al frame
         * dopo, e il calcolo e' idempotente - ripeterlo non fa danno,
         * contarlo si'. */
        int borderWalk = 0;
        int borderHold = 0;

        /* L'AMICO HA CAMBIATO MAPPA.
         *
         * Il passo che manca non e' un mistero: e' il mittente a inghiottirlo
         * (attraversando il bordo entra nel suo gate di assestamento e non
         * emette nessun PASSO, solo la posizione assoluta d'ingresso - che
         * pero' e' a un tile esatto da dove il remoto sta gia' disegnato, nel
         * verso in cui l'amico guarda). Quel passo lo ricostruiamo qui, e si
         * vede camminare. Il confronto e' fra la posizione TRADOTTA e le
         * currentCoords: entrambe nel NOSTRO spazio, quindi confrontabili.
         * Fuori dai due casi (gia' allineato / passo contiguo) si torna al
         * resync. */
        if (!r->known)
        {
            r->forceResync = 1;
        }
        else if (r->mapKey != mapKey)
        {
            int continued = 0;

            if (applicable)
            {
                u32 oe = ObjectEvent(r->objectId);
                s32 ddx = (s32)lx - (s32)oe_currentX(oe);
                s32 ddy = (s32)ly - (s32)oe_currentY(oe);

                if (ddx == 0 && ddy == 0)
                {
                    /* Il gioco lo aveva gia' messo dove serve: succede quando
                     * siamo NOI ad aver attraversato e il remoto e' stato
                     * portato oltre il bordo (ramo borderCarries), oppure
                     * quando il passo di attraversamento e' gia' arrivato. */
                    continued = 1;
                    borderHold = 1;
                }
                else if (dir >= DIR_SOUTH && dir <= DIR_EAST
                         && ddx == (s32)sDirDeltaX[dir]
                         && ddy == (s32)sDirDeltaY[dir])
                {
                    continued = 1;
                    borderWalk = 1;
                    type = EVENT_STEP;   /* il passo inghiottito dal mittente */
                }
            }

            if (!continued)
                r->forceResync = 1;
        }

        /* L'amico se n'e' andato dalla stanza: il suo avatar sparisce subito
         * invece di restare piantato qui finche' il culling non se ne
         * accorge. Non porta nessuna informazione di posizione, quindi non
         * aggiorna niente e si consuma da solo. */
        if (type == EVENT_LEAVE)
        {
            DoorAbortIfOwner(slot);
            if (r->state == STATE_SPAWNED)
                ForgetRemote(r);
            r->known = 0;
            /* Anche lo stato se n'e' andato con lui: senza questo, un amico
             * che si disconnette in lotta e si ricollega si ritroverebbe la
             * palla sulla testa fino al primo battito di stato. */
            r->status = ST_OVERWORLD;
            BUMPF(rxLeaves);
            r->forceResync = 1;
            r->qTail = RQ_WRAP(tail);
            continue;
        }

        /* UN PASSO E' UN TILE, PER DEFINIZIONE.
         *
         * Se un PASSO porta coordinate a piu' di un tile dall'ultima
         * posizione nota dell'amico, non e' un passo: e' una lettura
         * incoerente o un buco del canale. Si degrada a riposizionamento -
         * oppure, per i buchi corti, RAMMENDO (2026-08-02): un buco di 2-3
         * tile e' un PASSO perso; qui si accoda UN passo verso il traguardo
         * e si lascia l'evento in coda - al giro dopo il buco e' piu' corto,
         * e quando arriva a un tile il passo vero si anima da solo. hurry=1:
         * e' una marcia di recupero, e movesHurried la conta.
         *
         * SOLO IN LINEA RETTA (mdx o mdy nullo), ed e' sicurezza: i passi
         * del remoto non controllano le collisioni, e un rammendo a L
         * taglierebbe l'angolo ATTRAVERSO i muri che il giocatore vero ha
         * aggirato (visto in M-1). Un buco dritto e' calpestabile per
         * costruzione: il giocatore vero ci ha camminato sopra. */
        if (type == EVENT_STEP && r->known && r->mapKey == mapKey)
        {
            s32 mdx = (s32)x - r->targetX;
            s32 mdy = (s32)y - r->targetY;
            s32 mgap = AbsDiff(mdx, 0) + AbsDiff(mdy, 0);

            if (mgap > 1 && mgap <= MEND_MAX && applicable
                && (mdx == 0 || mdy == 0))
            {
                u32 oe = ObjectEvent(r->objectId);
                u8 mdir;

                if (!RemoteReadyForMovement(oe))
                    return;   /* ancora in movimento: si riprende da qui */

                if (mdx != 0)
                {
                    mdir = (mdx > 0) ? DIR_EAST : DIR_WEST;
                    r->targetX += (mdx > 0) ? 1 : -1;
                }
                else
                {
                    mdir = (mdy > 0) ? DIR_SOUTH : DIR_NORTH;
                    r->targetY += (mdy > 0) ? 1 : -1;
                }
                QueueRemoteStep(oe, mdir, speed, 1);
                r->settled = 0;
                return;   /* l'evento NON e' consumato: qTail non avanza */
            }
            if (mgap > 1)
            {
                type = EVENT_SYNC;
                BUMPF(rxImplausible);
            }
        }

        /* Dopo un cambio mappa non si insegue: si riparte da dove l'amico
         * dice di essere. Qualunque evento applicabile vale come posizione
         * assoluta. */
        if (r->forceResync && (type == EVENT_STEP || type == EVENT_TURN))
            type = EVENT_SYNC;

        if (!representable)
            BUMPF(rxOtherMap);

        if ((type == EVENT_STEP || type == EVENT_TURN) && applicable)
        {
            u32 oe = ObjectEvent(r->objectId);

            /* Si aspetta che il movimento precedente sia finito, altrimenti
             * lo si troncherebbe a meta': l'evento resta in coda. */
            if (!RemoteReadyForMovement(oe))
                return;

            /* Questo passo entra in una porta? Se si', prima la si apre e
             * l'evento resta in coda: al frame in cui l'animazione e' finita
             * viene ripreso da qui e il passo diventa la camminata sulla
             * soglia, come nel gioco. */
            if (type == EVENT_STEP && !DoorGateStep(slot, dir, via, lx, ly))
                return;

            /* Lo sprite si allinea PRIMA di accodare il movimento, con lo
             * stato dichiarato da QUESTO evento: andatura e sprite devono
             * essere consistenti per costruzione (vedi SyncRemoteGraphics). */
            r->gender = qe->gender;
            r->avatarState = qe->avatarState;
            SyncRemoteGraphics(r, oe);

            /* SyncRemoteGraphics puo' aver deciso di ricreare il remoto
             * (cambio di dimensione dello sprite): in quel caso lo slot non
             * e' piu' buono e il movimento lo si perde, non lo si applica a
             * un object event morto. */
            if (r->state != STATE_SPAWNED)
                return;
            /* E DAL 2026-08-30 PUO' ANCHE AVERLO RICREATO SUBITO, nello stesso
             * frame: `state` torna SPAWNED e la guardia qui sopra non scatta
             * piu', ma `oe` e' l'indirizzo di PRIMA - uno slot ormai libero, o
             * peggio riassegnato a un altro object event. Va riletto.
             * E' la trappola classica di questo file: una correzione (il
             * respawn immediato per le bici) che rende cieca una guardia
             * scritta per un'altra ragione. */
            oe = ObjectEvent(r->objectId);

            if (type == EVENT_STEP)
            {
                /* NIENTE MARCIA DI RECUPERO SCENDENDO DA UNA PORTA
                 * (l'animazione a 4 frame/tile si legge come "striscia", ed
                 * e' l'unico posto in cui il passo DEVE vedersi camminare).
                 * La marcia guarda la coda del SINGOLO slot: il ritardo da
                 * riassorbire e' suo, non della stanza. */
                QueueRemoteStep(oe, dir, speed,
                                !(sDoorState == DOOR_ST_EXIT_STEP && sDoorOwner == slot)
                                && RqLen(r) >= HURRY_THRESHOLD);
                BUMPF(rxSteps);
                if (via)
                    BUMPF(rxTranslated);
            }
            else if (ObjectEventSetHeldMovement((void *)oe, FaceActionFor(dir)) == 0)
            {
                BUMPF(rxTurns);
                if (via)
                    BUMPF(rxTranslated);
            }
            else
                BUMPF(movesRejected);
            r->settled = 0;
        }
        else if (type == EVENT_SYNC && applicable)
        {
            u32 oe = ObjectEvent(r->objectId);

            s32 drift = AbsDiff(oe_currentX(oe), lx);
            s32 driftY = AbsDiff(oe_currentY(oe), ly);

            if (driftY > drift)
                drift = driftY;

            /* Il criterio non e' quanto e' grande lo scostamento ma se questo
             * e' il momento buono per correggerlo: da FERMO (r->settled, cioe'
             * coda vuota, movimento concluso e posa rimessa) si riallinea per
             * qualunque scostamento; IN MOVIMENTO solo se lo scostamento non
             * e' piu' recuperabile. La storia dei due predicati morti che
             * hanno preceduto `settled` sta nel diario (2026-07-30 e
             * 2026-08-02: heldMovementActive che resta TRUE per sempre, e
             * RxPending() che contava anche l'evento in mano). */
            /* LO SCOSTAMENTO CHE NON PASSA (2026-08-28). Da fermo si corregge
             * subito; in cammino no, ed e' giusto: durante una camminata il
             * remoto e' quasi sempre indietro di un tile o due per via della
             * latenza, e teleportarlo li' sarebbe lo scatto da cheat che il
             * progetto evita da sempre. Ma se l'amico cammina SENZA MAI
             * fermarsi, `settled` non arriva mai e uno scostamento di 1-3
             * tile puo' durare all'infinito: e' il caso che restava scoperto.
             * Il discrimine non e' quanto e' grande, e' se DURA: tre SYNC di
             * fila (~3 s) con lo scostamento ancora li' non sono la latenza,
             * sono un passo perso che nessuno ricuce piu'. Il conto si tiene
             * qui, in una riga che vale anche da azzeramento: appena il
             * remoto e' di nuovo al suo posto, la corsa riparte da zero. */
            r->driftRun = drift ? r->driftRun + 1 : 0;

            if (r->forceResync)
            {
                /* Appena entrati in una mappa non esiste nessuna deriva da
                 * riassorbire: c'e' solo una posizione da credere. */
                TeleportRemote(oe, lx, ly);
                r->forceResync = 0;
                BUMPF(entryResyncs);
            }
            else if (drift > DRIFT_TOLERANCE)
            {
                TeleportRemote(oe, lx, ly);
            }
            else if (drift > 0 && RemoteReadyForMovement(oe)
                     && (r->settled || r->driftRun >= DRIFT_RUN_MAX))
            {
                TeleportRemote(oe, lx, ly);
                BUMPF(idleFixes);
                r->settled = 0;
                r->driftRun = 0;
            }
            BUMPF(rxSyncs);
            if (via)
                BUMPF(rxTranslated);
        }
        else if (type == EVENT_SYNC)
        {
            BUMPF(rxSyncs);
        }

        /* Aggiorna sempre lo stato dichiarato dall'amico, anche quando
         * l'evento non e' applicabile: e' cio' che ci dice dove e su che
         * mappa sta. Lo sprite lo allinea il ramo PASSO qui sopra prima di
         * muovere, e a fine frame SyncRemoteGraphics come rete di sicurezza:
         * e' idempotente. */
        r->known = 1;
        r->mapKey = mapKey;
        r->targetX = x;
        r->targetY = y;
        r->gender = qe->gender;
        r->avatarState = qe->avatarState;

        if (borderWalk)
            BUMPF(borderWalks);
        else if (borderHold)
            BUMPF(borderHolds);

        r->qTail = RQ_WRAP(tail);
    }
}

static void ConsumeRemoteEvents(void)
{
    u32 i;

    DrainMailbox();
    for (i = 0; i < N_REMOTES; i++)
        ConsumeSlotEvents(i);
}

/* ========================================================================== */
/* Indicatore di stato: l'icona sopra la testa del remoto                     */
/* ========================================================================== */

/* PERCHE' UNO SPRITE NOSTRO E NON UN FIELD EFFECT.
 *
 * Il campo ha gia' le icone sopra la testa (FLDEFF_EXCLAMATION_MARK_ICON e
 * compagne), ma sono animazioni ONE-SHOT: rimbalzano e si spengono da sole
 * (SpriteCB_TrainerIcons chiama FieldEffectStop appena animEnded). A noi serve
 * il contrario, un'icona che resti finche' lo stato resta. E i disegni che
 * servono - palla, "...", zaino - non esistono come oggetti di campo.
 *
 * Quindi: grafica incorporata nel payload, sprite creato da noi. E' la stessa
 * infrastruttura che servira' al nametag, che sara' solo un'altra sheet.
 *
 * DOVE GIRA, E PERCHE' NON NELL'IRQ. LoadCompressedSpriteSheet decomprime con
 * SWI 11h e LoadSpriteSheet copia in OBJ VRAM con CpuCopy16, cioe' con una SWI. Chiamare una SWI dall'handler IRQ mentre il
 * gioco sta dentro VBlankIntrWait (che e' a sua volta una SWI) sovrascrive
 * r14_svc: il gioco non tornerebbe piu' da VBlankIntrWait. Tutto cio' che
 * riguarda l'indicatore gira quindi da payload_cb1, che il gioco chiama dal
 * main loop in modo normale. Nel VBlank non si tocca. */

#define INDICATOR_TAG           0x4F57u   /* 'OW', non usato dal gioco */
/* Stesso valore che il gioco usa per le proprie icone sopra la testa
 * (FldEff_ExclamationMarkIcon, field_effect_helpers.c). */
#define INDICATOR_SUBPRIORITY   0x52u

#define ICON_BALL   0u
#define ICON_DOTS   1u
#define ICON_BAG    2u
#define ICON_PARTY  3u
#define ICON_DEX    4u
#define ICON_NAV    5u
#define ICON_MENU   6u

/* 7 icone da 16x16 4bpp = 7 x 128 = 896 byte grezzi, QUI COMPRESSI IN LZ77
 * (formato BIOS, SWI 11h): 380 byte. Li decomprime il GIOCO, in
 * gDecompressionBuffer, dentro LoadCompressedSpriteSheet (decompress.c:22-31),
 * che poi chiama la solita LoadSpriteSheet: zero decoder nostro, ~500 byte di
 * EWRAM recuperati (2026-08-21). L'ordine dei tile e' quello del mapping 1D
 * dello sprite: alto-sinistra, alto-destra, basso-sinistra, basso-destra.
 * L'ORDINE DELLE ICONE e' un contratto con la lista ICONS di gen_icons.py.
 *
 * Generate da tools/gen_icons.py, dove i disegni stanno come mappe di pixel
 * leggibili e dove vive l'encoder. `python tools/gen_icons.py --check`
 * decomprime questi byte e dice se corrispondono ancora: e' l'unico modo che
 * abbiamo di controllare una grafica senza poterla guardare. Allineato a 4:
 * il BIOS legge l'header come word. */
static const u8 sIndicatorTilesLz[380] __attribute__((aligned(4))) = {
    0x10, 0x80, 0x03, 0x00, 0x40, 0x00, 0xA0, 0x00, 0x10, 0x11, 0x00, 0x00, 0x31, 0x33, 0x12, 0x00,
    0x10, 0x33, 0x40, 0x03, 0x11, 0x21, 0x90, 0x1F, 0x11, 0x04, 0x01, 0x00, 0x00, 0x33, 0x13, 0x00,
    0x03, 0x33, 0x01, 0xB1, 0x20, 0x03, 0x12, 0x10, 0x10, 0x10, 0x23, 0x10, 0x22, 0x22, 0x20, 0x03,
    0x3C, 0x00, 0x21, 0x00, 0x03, 0x10, 0x43, 0x70, 0x55, 0x10, 0x23, 0x22, 0x22, 0xB1, 0x40, 0x03,
    0x12, 0x30, 0x43, 0xE0, 0x01, 0x11, 0x11, 0x11, 0x00, 0x42, 0x47, 0x22, 0x20, 0x03, 0x12, 0x21,
    0x12, 0x20, 0x03, 0x40, 0x0F, 0x40, 0x1E, 0x91, 0x00, 0x43, 0x22, 0x01, 0x10, 0x03, 0x21, 0x12,
    0x21, 0x20, 0x03, 0x8D, 0x50, 0x0F, 0x00, 0x11, 0x21, 0x00, 0x55, 0x10, 0x03, 0x10, 0xF0, 0x59,
    0xF0, 0x40, 0x3B, 0xF0, 0x71, 0xF0, 0x81, 0x00, 0xFB, 0x77, 0x00, 0x00, 0x71, 0x41, 0x88, 0x01,
    0x02, 0x11, 0x00, 0x71, 0x77, 0x77, 0x30, 0x03, 0x24, 0x87, 0x88, 0x40, 0x1C, 0x00, 0x77, 0x00,
    0xFB, 0x88, 0x17, 0x82, 0x10, 0xAA, 0x01, 0x00, 0x77, 0x77, 0x17, 0x20, 0x03, 0x88, 0x47, 0x78,
    0x00, 0x10, 0x71, 0x87, 0x99, 0x20, 0x27, 0x50, 0x33, 0xC0, 0x70, 0x7D, 0x99, 0x00, 0x23, 0x10,
    0x27, 0x50, 0x33, 0xF0, 0x8F, 0x60, 0x7F, 0x33, 0x31, 0x7F, 0x7F, 0x10, 0x10, 0xF3, 0xA1, 0x43,
    0x11, 0x50, 0x01, 0x72, 0x21, 0x7F, 0x01, 0x4A, 0xC0, 0x1C, 0x57, 0x11, 0x11, 0xAE, 0x01, 0x00,
    0xB6, 0x01, 0x01, 0x86, 0x01, 0x0F, 0xC0, 0xEF, 0xE2, 0xF0, 0x1F, 0xF1, 0x80, 0x51, 0x64, 0x41,
    0x34, 0x13, 0x00, 0x03, 0x21, 0xF5, 0x40, 0x03, 0x10, 0x0B, 0x00, 0x93, 0x71, 0x47, 0x31, 0x01,
    0xFC, 0x12, 0x40, 0x03, 0xB3, 0x10, 0x0B, 0x33, 0x12, 0x0C, 0x10, 0x23, 0x41, 0x34, 0x10, 0x3B,
    0x10, 0x07, 0xE7, 0x30, 0x03, 0x81, 0x74, 0x10, 0x23, 0x11, 0x31, 0x30, 0x2B, 0x50, 0x03, 0xF1,
    0x93, 0x83, 0x41, 0xE4, 0x91, 0x99, 0x99, 0x00, 0x91, 0x10, 0x07, 0x02, 0x47, 0x14, 0x91, 0x21,
    0x66, 0x00, 0x07, 0x26, 0x91, 0xC7, 0x99, 0x99, 0x51, 0x19, 0x02, 0x2A, 0x19, 0x02, 0x47, 0x19,
    0x00, 0x66, 0x00, 0x03, 0x77, 0x62, 0x00, 0x07, 0x30, 0x2B, 0x50, 0x3B, 0x69, 0x00, 0x3F, 0x00,
    0x43, 0x81, 0xF4, 0xE7, 0x10, 0x2B, 0x10, 0x33, 0x10, 0x3B, 0x96, 0x69, 0x30, 0x07, 0xF2, 0x13,
    0x82, 0x2C, 0x46, 0x21, 0x02, 0xCB, 0x21, 0x66, 0x66, 0x60, 0x07, 0xC2, 0x4B, 0x22, 0xB3, 0x02,
    0xC4, 0x26, 0x02, 0xC8, 0x10, 0x07, 0x66, 0x26, 0x12, 0x7A, 0x90, 0x2F, 0xFC, 0x03, 0x07, 0xC2,
    0x70, 0x20, 0x27, 0x40, 0x2F, 0x10, 0x3B, 0xD2, 0x8F, 0x00, 0x00, 0x00,
};

/* 16 colori RGB555. L'indice 0 e' trasparente per definizione dell'hardware. */
static const u16 sIndicatorPal[16] = {
    0x0000,  /*  0 trasparente        */
    0x0000,  /*  1 nero, contorno     */
    0x7FFF,  /*  2 bianco             */
    0x18DC,  /*  3 rosso              */
    0x0852,  /*  4 rosso scuro        */
    0x5294,  /*  5 grigio             */
    0x294A,  /*  6 grigio scuro       */
    0x15B6,  /*  7 marrone            */
    0x08ED,  /*  8 marrone scuro      */
    0x239F,  /*  9 giallo             */
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
};

/* struct CompressedSpriteSheet (include/sprite.h:19): data, size, tag - lo
 * stesso layout di SpriteSheet, ma `size` e' quella DECOMPRESSA (896) e
 * `data` punta ai byte LZ77. */
static const struct { const void *data; u16 size; u16 tag; } sIndicatorSheet = {
    sIndicatorTilesLz, 128u * 7u, INDICATOR_TAG
};

/* struct SpritePalette (include/sprite.h:36): data, tag. */
static const struct { const u16 *data; u16 tag; } sIndicatorPalette = {
    sIndicatorPal, INDICATOR_TAG
};

/* struct OamData, 8 byte, scritta come i tre attributi dell'hardware invece che
 * come bitfield: e' la stessa scelta di tutto il resto di game_types.h.
 *   attr0 = y | affineMode<<8 | objMode<<10 | mosaic<<12 | bpp<<13 | shape<<14
 *   attr1 = x | matrixNum<<9 | size<<14
 *   attr2 = tileNum | priority<<10 | paletteNum<<12
 * shape 0 + size 1 = 16x16, 4bpp, priority 1: la stessa priorita' che il gioco
 * da' alle proprie icone sopra la testa (SetIconSpriteData). */
static const u16 sIndicatorOam[4] = { 0x0000, 0x4000, 0x0400, 0x0000 };

/* ANIMCMD_END: il primo halfword vale -1, e BeginAnim (sprite.c:919) su quel
 * valore non fa NIENTE - non tocca oam.tileNum e lascia animBeginning alzato.
 * E' esattamente cio' che serve: il tile lo scegliamo noi una volta e nessuno
 * ce lo riscrive. E' la stessa costante di gDummySpriteAnimTable, definita qui
 * per non doverla ritrovare nel .map della ROM italiana in Fase 5. */
static const s16 sIndicatorAnim[2] = { -1, 0 };
static const s16 *const sIndicatorAnimTable[1] = { sIndicatorAnim };

/* AFFINEANIMCMD_END (0x7FFD). Con affineMode 0 il gioco non ci arriva mai, ma
 * un puntatore valido costa quattro halfword e toglie un "e se". */
static const s16 sIndicatorAffine[4] = { 0x7FFD, 0, 0, 0 };
static const s16 *const sIndicatorAffineTable[1] = { sIndicatorAffine };

/* La callback dello sprite e' nostra: il gioco la invoca a ogni frame da
 * AnimateSprites e non deve fare niente. Un simbolo in meno da ritrovare in
 * Fase 5 rispetto a SpriteCallbackDummy, e per giunta uno che non fa nulla.
 * Il seguito dell'icona lo fa payload_cb1, non questa: qui non si sa ancora
 * dove il gioco disegnera' il remoto in questo frame. */
static void PayloadSpriteDummy(void *sprite)
{
    (void)sprite;
}

/* struct SpriteTemplate (include/sprite.h:179). */
static const struct {
    u16 tileTag;
    u16 paletteTag;
    const void *oam;
    const void *anims;
    const void *images;
    const void *affineAnims;
    void (*callback)(void *);
} sIndicatorTemplate = {
    INDICATOR_TAG, INDICATOR_TAG,
    sIndicatorOam, sIndicatorAnimTable, 0, sIndicatorAffineTable,
    PayloadSpriteDummy
};

/* Lo sprite che credo mio e' ancora mio? Stessa disciplina del localId per gli
 * object event: `template` e' un puntatore dentro il payload, e nessuno sprite
 * del gioco puo' averlo. Dopo un warp il gioco resetta tutti gli sprite e lo
 * slot puo' essere gia' di qualcun altro. Con piu' remoti serve anche lo SLOT
 * (sprite data[0], timbrato alla creazione): le tre icone condividono lo
 * stesso template, e senza il timbro dopo un reset degli sprite due slot
 * potrebbero adottare la stessa icona. */
static int IndicatorSpriteValid(struct Remote *r)
{
    u32 sp;

    if (r->indSpriteId >= MAX_SPRITES)
        return 0;

    sp = Sprite(r->indSpriteId);
    if (!(sprite_flags3E(sp) & SPRITE_FLAG_IN_USE))
        return 0;

    return sprite_template(sp) == (u32)&sIndicatorTemplate
        && sprite_data0(sp) == (s16)RemoteSlotOf(r);
}

static void IndicatorDestroy(struct Remote *r)
{
    /* DestroySprite su uno sprite con usingSheet non libera nessun tile
     * (sprite.c:625): azzera lo slot e basta. Niente SWI, quindi si puo'
     * chiamare anche dall'IRQ - ed e' quello che fa ForgetRemote. */
    if (IndicatorSpriteValid(r))
        DestroySprite((void *)Sprite(r->indSpriteId));

    r->indSpriteId = MAX_SPRITES;
}

/* Caricamento pigro. Dopo ogni warp il gioco svuota le tabelle dei tag, quindi
 * il test "il tag c'e'?" e' anche il meccanismo di ricarica: non serve
 * accorgersi del warp, basta chiedere.
 *
 * L'esito NON si legge dal valore di ritorno di LoadSpriteSheet: quella ritorna
 * il tile di partenza, e 0 come codice di errore - ma 0 e' anche un tile di
 * partenza legittimo. Si richiede invece il tag: se c'e', il caricamento e'
 * andato, e in piu' si ottiene il valore che serve davvero. La sheet e la
 * palette sono UNICHE e condivise fra le tre icone: 7 disegni, un tag. */
static int IndicatorEnsureGfx(u16 *start)
{
    u16 tileStart = GetSpriteTileStartByTag(INDICATOR_TAG);

    if (tileStart == 0xFFFFu)
    {
        LoadCompressedSpriteSheet(&sIndicatorSheet);
        tileStart = GetSpriteTileStartByTag(INDICATOR_TAG);
        if (tileStart == 0xFFFFu)
            return 0;
    }

    if (IndexOfSpritePaletteTag(INDICATOR_TAG) == 0xFFu)
    {
        LoadSpritePalette(&sIndicatorPalette);
        if (IndexOfSpritePaletteTag(INDICATOR_TAG) == 0xFFu)
            return 0;
    }

    *start = tileStart;
    return 1;
}

/* Il latch del "grafica non caricabile" e' per-slot: si contano le
 * TRANSIZIONI, non i frame. */
static void IndicatorFail(struct Remote *r)
{
    IndicatorDestroy(r);
    if (!r->indFailed)
    {
        r->indFailed = 1;
        BUMPF(indicatorLoadFails);
    }
}

static void IndicatorShow(struct Remote *r, u8 icon, u32 remoteOe)
{
    u32 sp, remoteSp;
    u16 start;

    /* Si chiede a ogni frame, non solo alla creazione: cosi' la ricarica pigra
     * copre anche lo sprite che e' sopravvissuto a una liberazione della sheet,
     * e `start` e' sempre quello vero. */
    if (!IndicatorEnsureGfx(&start))
    {
        IndicatorFail(r);
        return;
    }

    if (!IndicatorSpriteValid(r))
    {
        u8 id = CreateSprite(&sIndicatorTemplate, 0, 0, INDICATOR_SUBPRIORITY);

        if (id >= MAX_SPRITES)
        {
            IndicatorFail(r);
            return;
        }

        r->indSpriteId = id;
        r->indIcon = 0xFF;             /* forza la scelta del tile qui sotto */
        r->indTileStart = 0xFFFFu;
        /* Senza questo l'icona resterebbe inchiodata allo schermo mentre la
         * mappa scorre: e' il bit che il gioco alza per tutti gli sprite del
         * campo (SetIconSpriteData, field_effect_helpers.c). */
        sprite_flags3E(Sprite(id)) |= SPRITE_FLAG_COORD_OFFSET;
        /* Il timbro dello slot: vedi IndicatorSpriteValid. */
        sprite_data0(Sprite(id)) = (s16)RemoteSlotOf(r);
        BUMPF(indicatorShown);
    }

    r->indFailed = 0;
    sp = Sprite(r->indSpriteId);

    /* Il tile si riscrive quando cambia l'icona OPPURE quando la sheet e' stata
     * ricaricata altrove: il secondo caso da solo non cambia `icon`, e senza
     * questo confronto l'icona continuerebbe a puntare a tile di qualcun altro. */
    if (r->indIcon != icon || r->indTileStart != start)
    {
        sprite_oamAttr2(sp) = (u16)((sprite_oamAttr2(sp) & ~OAM_TILENUM_MASK)
                                    | ((start + icon * 4u) & OAM_TILENUM_MASK));
        r->indIcon = icon;
        r->indTileStart = start;
    }

    /* Lo stesso seguito di SpriteCB_TrainerIcons (trainer_see.c:744): si copia
     * la posizione dello sprite del remoto e si sale di 16 pixel, cioe' di un
     * tile sopra la testa. */
    remoteSp = Sprite(oe_spriteId(remoteOe));
    sprite_x(sp)  = sprite_x(remoteSp);
    sprite_y(sp)  = (s16)(sprite_y(remoteSp) - 16);
    sprite_x2(sp) = sprite_x2(remoteSp);
    sprite_y2(sp) = sprite_y2(remoteSp);
}

static void IndicatorTickSlot(u32 slot)
{
    struct Remote *r = &g_remotes[slot];
    u32 remoteOe;
    u8 icon;

    if (!g_state.inOverworld
        || r->status == ST_OVERWORLD
        || r->state != STATE_SPAWNED
        || r->objectId >= OBJECT_EVENTS_COUNT)
    {
        IndicatorDestroy(r);
        return;
    }

    remoteOe = ObjectEvent(r->objectId);
    if (!oe_active(remoteOe) || oe_localId(remoteOe) != REMOTE_LID(slot)
        || oe_spriteId(remoteOe) >= MAX_SPRITES)
    {
        IndicatorDestroy(r);
        return;
    }

    /* Il default e' l'icona di menu GENERICA, non lo zaino: qualunque stato
     * sconosciuto (un peer piu' nuovo con una sezione in piu') deve degradare
     * a "e' in un menu", mai a "e' nella Borsa". */
    switch (r->status)
    {
    case ST_BATTLE:     icon = ICON_BALL;  break;
    case ST_DIALOG:     icon = ICON_DOTS;  break;
    case ST_MENU_BAG:   icon = ICON_BAG;   break;
    case ST_MENU_PARTY: icon = ICON_PARTY; break;
    case ST_MENU_DEX:   icon = ICON_DEX;   break;
    case ST_MENU_NAV:   icon = ICON_NAV;   break;
    default:            icon = ICON_MENU;  break;
    }

    IndicatorShow(r, icon, remoteOe);
}

static void IndicatorTick(void)
{
    u32 i;

    for (i = 0; i < N_REMOTES; i++)
        IndicatorTickSlot(i);
}

/* ========================================================================== */
/* Pass-through: non farsi bloccare dall'amico                                */
/* ========================================================================== */

/* Le movement action del remoto non controllano le collisioni (verificato in
 * Fase 2: attraversa i muri), quindi il problema e' solo il simmetrico - il
 * GIOCATORE LOCALE bloccato dall'avatar dell'amico. In una porta o in un
 * corridoio significa softlock, ed e' vietato da CLAUDE.md.
 *
 * La collisione la decide DoesObjectCollideWithObjectAt
 * (event_object_movement.c:4724), che scorre gli object event e guarda SOLO
 * quelli con `active` alzato. Quindi basta togliere il bit per il tempo in cui
 * il gioco fa quel controllo.
 *
 * QUANDO SI RIMETTE, E PERCHE' NON AL VBLANK. Il controllo di collisione sta
 * dentro CB1_Overworld -> PlayerStep, cioe' dentro il callback1 del gioco;
 * l'aggiornamento dei movimenti degli object event sta invece in callback2
 * (AnimateSprites) e lo spawn di nuovi object event in CameraUpdate, sempre in
 * callback2. Rimettendo il bit APPENA il callback1 originale ha finito, il
 * remoto non perde nemmeno un frame di animazione e non esiste nessuna
 * finestra in cui il gioco possa riciclare il nostro slot: la sola cosa che
 * gira con `active` a zero e' il codice che deve non vederci. */

static void PassThroughRelease(void)
{
    u32 i;

    if (!sPassMask)
        return;

    for (i = 0; i < N_REMOTES; i++)
    {
        if (sPassMask & (1u << i))
        {
            struct Remote *r = &g_remotes[i];

            if (r->objectId < OBJECT_EVENTS_COUNT)
            {
                /* Identita' prima di scrivere, come sempre in questo payload. */
                u32 oe = ObjectEvent(r->objectId);
                if (oe_localId(oe) == REMOTE_LID(i))
                    oe_flags0(oe) = (u8)(oe_flags0(oe) | 0x01u);
            }
        }
    }
    sPassMask = 0;
}

static void PassThroughTick(void)
{
    u32 playerOe;
    u16 keys;
    s16 tx, ty;
    u8 dir;
    u32 i, hit = 0;

    /* MAI durante l'assestamento: li' il gioco sta traducendo le coordinate di
     * tutti gli object event ATTIVI, e un frame di assenza vorrebbe dire un
     * remoto rimasto nello spazio della mappa vecchia per sempre. */
    if (!g_state.inOverworld || sMapSettling)
    {
        sPassThroughWasOn = 0;
        return;
    }

    keys = gMain_heldKeys;
    if (keys & DPAD_DOWN)       dir = DIR_SOUTH;
    else if (keys & DPAD_UP)    dir = DIR_NORTH;
    else if (keys & DPAD_LEFT)  dir = DIR_WEST;
    else if (keys & DPAD_RIGHT) dir = DIR_EAST;
    else
    {
        sPassThroughWasOn = 0;
        return;
    }

    playerOe = ObjectEvent(gPlayerAvatar_objectEventId);
    if (!oe_active(playerOe))
    {
        sPassThroughWasOn = 0;
        return;
    }

    tx = (s16)(oe_currentX(playerOe) + sDirDeltaX[dir]);
    ty = (s16)(oe_currentY(playerOe) + sDirDeltaY[dir]);

    /* TUTTI i remoti sul tile davanti, non il primo: due amici possono stare
     * sovrapposti (il pass-through vale anche fra loro e i passi non
     * controllano le collisioni), e basterebbe quello non disattivato a
     * bloccare. current OPPURE previous: a meta' passo il remoto occupa due
     * tile agli occhi del motore, e la collisione li guarda entrambi. */
    for (i = 0; i < N_REMOTES; i++)
    {
        struct Remote *r = &g_remotes[i];
        u32 remoteOe;

        if (r->state != STATE_SPAWNED || r->objectId >= OBJECT_EVENTS_COUNT)
            continue;
        remoteOe = ObjectEvent(r->objectId);
        if (!oe_active(remoteOe) || oe_localId(remoteOe) != REMOTE_LID(i))
            continue;

        if ((oe_currentX(remoteOe) == tx && oe_currentY(remoteOe) == ty)
            || (oe_previousX(remoteOe) == tx && oe_previousY(remoteOe) == ty))
        {
            oe_flags0(remoteOe) = (u8)(oe_flags0(remoteOe) & ~0x01u);
            sPassMask |= (1u << i);
            hit = 1;
        }
    }

    if (hit)
    {
        /* A transizione: spingendo contro il remoto per un secondo il contatore
         * salirebbe di sessanta e nel log sembrerebbe un guasto. */
        if (!sPassThroughWasOn)
        {
            sPassThroughWasOn = 1;
            BUMPF(passThroughs);
        }
    }
    else
    {
        sPassThroughWasOn = 0;
    }
}

/* ========================================================================== */
/* Trampolino su gMain.callback1: togliere il tasto A davanti al remoto       */
/* ========================================================================== */

/* PERCHE' ESISTE QUESTO PEZZO.
 *
 * Premendo A rivolti al remoto, il gioco cerca lo script dell'object event dal
 * suo localId. `FindObjectEventTemplateByLocalId` non trova 0xF0 e ritorna NULL,
 * ma `GetObjectEventScriptPointerByLocalIdAndMap` dereferenzia comunque:
 * NULL + 0x10 (offset di `script` in ObjectEventTemplate) legge l'indirizzo
 * 0x00000010, dentro la regione BIOS. Il valore che ne esce e' spazzatura ma non
 * e' NULL, quindi il motore lo ESEGUE come bytecode di script.
 * Misurato il 2026-07-30: dialoghi a caso e un warp a mappa 1.28 (103,103).
 *
 * Non si puo' impedire al gioco di TROVARE il nostro object event senza
 * combattere il motore a ogni frame (l'elevazione la riscrive il codice dei
 * ground effect). Si impedisce invece che l'input ci arrivi.
 *
 * AgbMain fa ReadKeys() e poi CallCallbacks(), che chiama callback1 e poi
 * callback2 (src/main.c). Mettendoci al posto di callback1 giriamo dopo la
 * lettura dei tasti e prima che DoCB1_Overworld li usi. Dall'IRQ sarebbe
 * impossibile: ReadKeys gira DOPO il VBlank e sovrascriverebbe. */

static void (*sOrigCallback1)(void);

/* CHE COSA C'E' SUL TILE DAVANTI, agli occhi del gioco (2026-08-21, audit del
 * salvataggio: un solo predicato al posto dei due di prima).
 * Ritorna 0 = niente da sopprimere, 1 = il NOSTRO remoto sul tile davanti
 * (l'unico caso in cui A puo' aprire la scheda), 2 = la regola generale.
 *
 * La regola generale chiude due buchi del vecchio controllo "remoto sul tile
 * davanti":
 *  1) i BANCONI: GetInteractedObjectEventScript (field_control_avatar.c:
 *     291-300), se davanti c'e' un MB_COUNTER senza nessuno sopra, guarda il
 *     tile OLTRE il bancone. Il remoto attraversa i muri e puo' starci - a
 *     due tile da noi - e il gioco andrebbe a eseguire lo "script" del suo
 *     template NULL;
 *  2) QUALUNQUE object event della mappa corrente senza template (un avatar
 *     lasciato da una versione precedente del payload, uno speciale del
 *     gioco) ha lo stesso percorso: puntatore letto da 0x00000010, bytecode
 *     spazzatura, flag e variabili scritte a caso dentro SaveBlock1
 *     (misurato il 2026-07-30: dialoghi a caso e un warp).
 * Si ripete la ricerca del gioco - primo object event attivo sul tile davanti
 * con elevazione compatibile (GetObjectEventIdByXYZ + ObjectEventDoesElevation
 * Match), altrimenti oltre il bancone - e si risponde 1 o 2 se quello trovato
 * e' nostro, 2 se e' della mappa corrente e nessun template della mappa porta
 * il suo localId. Gli NPC della mappa accanto (mapNum diverso) hanno il
 * template nel loro header e non si toccano. Solo letture, e solo nel frame
 * in cui A viene premuto. */
static int FrontObjectClass(u32 *outSlot)
{
    u32 playerOe, oe, events, sb1;
    s16 tx, ty;
    u8 dir, pz, lid;
    u32 i, count, hop;

    *outSlot = 0;

    playerOe = ObjectEvent(gPlayerAvatar_objectEventId);
    if (!oe_active(playerOe))
        return 0;
    dir = oe_facingDirection(playerOe);
    if (dir < DIR_SOUTH || dir > DIR_EAST)
        return 0;
    sb1 = SaveBlock1();
    if (sb1 == 0)
        return 0;
    pz = oe_elevation(playerOe);

    tx = (s16)(oe_currentX(playerOe) + sDirDeltaX[dir]);
    ty = (s16)(oe_currentY(playerOe) + sDirDeltaY[dir]);

    oe = 0;
    for (hop = 0; hop < 2 && oe == 0; hop++)
    {
        if (hop == 1)
        {
            /* Nessuno davanti: se il tile e' un bancone si guarda oltre,
             * come fa il gioco. Altrimenti non c'e' nessuna interazione. */
            if (MapGridGetMetatileBehaviorAt((s32)tx, (s32)ty) != MB_COUNTER)
                return 0;
            tx = (s16)(tx + sDirDeltaX[dir]);
            ty = (s16)(ty + sDirDeltaY[dir]);
        }
        for (i = 0; i < OBJECT_EVENTS_COUNT; i++)
        {
            u32 cand = ObjectEvent(i);
            u8 cz;
            if (!oe_active(cand) || oe_currentX(cand) != tx || oe_currentY(cand) != ty)
                continue;
            cz = oe_elevation(cand);
            if (cz != 0 && pz != 0 && cz != pz)
                continue;
            /* Il gioco prende il PRIMO: se e' il giocatore stesso, per lui
             * e' come se non ci fosse nessuno (e guarda oltre il bancone). */
            if (oe_localId(cand) != 0xFF)
                oe = cand;
            break;
        }
    }
    if (oe == 0)
        return 0;

    lid = oe_localId(oe);
    if (IS_REMOTE_LID(lid))
    {
        *outSlot = (u32)(u8)(lid - REMOTE_LOCAL_ID);
        return (hop == 1) ? 1 : 2;   /* hop vale 1 se trovato sul tile davanti */
    }
    if (oe_mapNum(oe) != sb1_mapNum(sb1) || oe_mapGroup(oe) != sb1_mapGroup(sb1))
        return 0;     /* NPC della mappa accanto: ha il suo template */

    events = gMapHeader_events;
    count = events ? mapevents_objectEventCount(events) : 0;
    if (count > OBJECT_EVENT_TEMPLATES_COUNT)
        count = OBJECT_EVENT_TEMPLATES_COUNT;
    for (i = 0; i < count; i++)
        if (oet_localId(sb1_objEventTemplate(sb1, i)) == lid)
            return 0; /* ha un template: script vero, si lascia al gioco */
    return 2;
}

/* Il corpo del payload, dal main loop. Definita accanto a payload_frame,
 * dopo SettleCommit: qui serve solo la firma. */
static void OverworldTick(void);

/* Si tocca solo newKeys, non i raw: la combinazione di soft reset
 * (A+B+START+SELECT) la legge AgbMain dai raw e deve continuare a funzionare.
 * E si toglie solo A: correre e la bici usano B, il menu usa START.
 *
 * A SI SOPPRIME SOLO A CONTROLLI DI CAMPO SBLOCCATI. Il percorso pericoloso
 * (TryGetObjectEventScript che dereferenzia il template NULL del nostro localId)
 * passa da ProcessPlayerFieldInput, che gira SOLO quando sLockFieldControls e'
 * basso. Quando e' alto, A appartiene a qualcun altro: al menu START (che gira
 * come task DENTRO l'overworld, quindi questo hook continua a vederlo), a un
 * dialogo, a uno script. Senza questo controllo, con il remoto sul tile davanti
 * non si riusciva a selezionare NIENTE nel menu START: ogni A veniva mangiata
 * qui prima di arrivare al task del menu (riportato da Lain il 2026-07-30).
 * ArePlayerFieldControlsLocked e' una foglia di tre istruzioni, gia' usata da
 * payload_frame: costa niente e non puo' bloccarsi. */
__attribute__((used))
void payload_cb1(void)
{
#ifdef PAYLOAD_WITH_SIO
    /* LA SECONDA PASSATA DELLA GUARDIA. Il VBlank ci da' il controllo PRIMA
     * che il gioco lavori; qui siamo nel main loop, cioe' DOPO. Non sappiamo
     * quando il gioco riconfiguri la seriale entrando in un edificio, quindi
     * si copre in tutti e due i momenti: chiunque scriva, una delle due
     * passate viene dopo di lui e ripara entro il frame. */
    SioGuard();
#endif

    /* IL CORPO (2026-08-21): stato locale, TX, RX, spawn, porte, grafica del
     * remoto. Prima di tutto il resto, perche' il tasto A, l'icona, la bolla e
     * il pass-through qui sotto devono vedere lo stato di QUESTO frame. Fuori
     * dall'overworld non fa niente (inOverworld lo decide il VBlank). */
    OverworldTick();

    /* La rigenerazione della scheda chiesta dal corpo: solo lettura del
     * salvataggio + memset del buffer, niente sprite ne' DMA. */
    if (sCardGenPending)
    {
        TrainerCard_GenerateCardForLinkPlayer((void *)ADDR_gTrainerCards);
        sCardGenPending = 0;
        sCardReady = 1;
    }

    if (gMain_newKeys & A_BUTTON)
    {
        /* 1 = un remoto sul tile davanti (la scheda si apre solo cosi', e
         * `slot` dice QUALE amico); 2 = la regola generale: il remoto oltre
         * un bancone, o un object event senza template (FrontObjectClass). */
        u32 slot;
        int cls = FrontObjectClass(&slot);

        if (cls)
        {
            if (ArePlayerFieldControlsLocked())
            {
                /* A passa: appartiene al menu o al dialogo. Il contatore esiste
                 * perche' senza, questa correzione sarebbe inverificabile dal log. */
                BUMPF(aFreedInMenu);
            }
            else
            {
                /* A non arriva MAI al gioco (il percorso dello script NULL
                 * resta vietato). Ma se la scheda dell'amico e' completa e il
                 * remoto e' proprio davanti, la pressione diventa LA SCHEDA:
                 * fade come fa il menu START per la propria card
                 * (start_menu.c:623), controlli bloccati perche' nel mezzo
                 * secondo di fade il giocatore non possa avviare dialoghi o
                 * warp. Lo sblocco al ritorno lo fa il menu START, che e' il
                 * cb2 di rientro del viewer. */
                gMain_newKeys = (u16)(gMain_newKeys & ~A_BUTTON);
                if (cls == 1 && g_remotes[slot].cardRxBitmap == CARD_ALL_CHUNKS
                    && !sCardShowWait && g_state.inOverworld)
                {
                    LockPlayerFieldControls();
                    FadeScreen(FADE_TO_BLACK, 0);
                    sCardShowWait = CARD_FADE_FRAMES;
                    sCardShowSlot = slot;
                }
                else if (cls == 1)
                {
                    BUMPF(aBlocked);
                }
                else
                {
                    BUMPF(aUnscripted);
                }
            }
        }
    }

    /* Il fade e' partito: allo scadere dell'attesa si cede lo schermo al
     * viewer del gioco. La lingua va scritta in gLinkPlayers[1] PRIMA:
     * ShowTrainerCardInLink la legge da li' per il font, e a 0 la scheda
     * uscirebbe con la resa giapponese. */
    if (sCardShowWait)
    {
        if (!g_state.inOverworld)
        {
            /* Qualcosa ha rubato la scena a meta' fade. Non dovrebbe potere
             * (controlli bloccati), ma i controlli non restano in ostaggio. */
            UnlockPlayerFieldControls();
            sCardShowWait = 0;
        }
        else if (--sCardShowWait == 0)
        {
            /* La voce 1 + slot di gTrainerCards e di gLinkPlayers: e' dove
             * DrainMailbox ha depositato i chunk di QUELL'amico. */
            CleanupOverworldWindowsAndTilemaps();
            GAME_U16(ADDR_gLinkPlayers + 0x1C * (sCardShowSlot + 1u) + 0x1A)
                = LANGUAGE_ITALIAN;
            ShowTrainerCardInLink((u8)(sCardShowSlot + 1u),
                                  (void *)(ADDR_CB2_ReturnToFieldWithOpenMenu | 1u));
            BUMPF(cardShows);
        }
    }

    /* Qui, non nell'IRQ: creare uno sprite e caricarne la grafica passa da
     * CpuCopy16, cioe' da una SWI, e una SWI dentro l'handler IRQ mentre il
     * gioco e' dentro VBlankIntrWait gli sovrascrive r14_svc. Vedi il commento
     * in cima alla sezione dell'indicatore. */
    IndicatorTick();
    SurfBlobTick();

    /* Deve stare PRIMA del callback1 del gioco: e' li' dentro che PlayerStep
     * chiede se il tile davanti e' occupato. */
    PassThroughTick();

    if (sOrigCallback1)
        sOrigCallback1();

    /* E si rimette subito dopo, prima che callback2 aggiorni i movimenti degli
     * object event e ne spawni di nuovi. */
    PassThroughRelease();
}

/* Installazione e watchdog: stesso schema del vettore IRQ. Il gioco riscrive
 * callback1 a ogni cambio di scena, quindi si perde l'hook e lo si rimette al
 * rientro, concatenando l'originale del momento. CB1_Overworld controlla gia'
 * da se' che callback2 sia CB2_Overworld prima di fare qualcosa, quindi
 * concatenarlo e' sicuro in ogni situazione. */
static void EnsureCallback1Hook(void)
{
    u32 mine = (u32)&payload_cb1 | 1u;
    u32 cur = gMain_callback1;

    if (cur == mine)
        return;

    sOrigCallback1 = (void (*)(void))cur;
    g_state.origCallback1 = cur;
    gMain_callback1 = mine;
    BUMPF(cb1Installs);
}

/* ========================================================================== */
/* Il commit del cambio mappa: un punto solo                                  */
/* ========================================================================== */

/* Ci si arriva quando le coordinate del giocatore e la mappa sono tornate a
 * dire la stessa cosa - oppure quando il watchdog si arrende. Qui, e SOLO qui,
 * si riallinea il riferimento del lato TX, si mandano le posizioni assolute
 * d'ingresso, e si decide se il remoto sopravvive al bordo o va distrutto.
 *
 * Prima di questa consegna le stesse tre cose stavano in due posti diversi
 * (TrackPlayer per la parte TX, payload_frame per la parte remota) e giravano
 * al PRIMO frame dopo il cambio, cioe' dentro la finestra in cui le coordinate
 * degli object event sono ancora nello spazio vecchio.
 *
 * Ritorna 0 se il remoto e' stato distrutto: chi chiama deve smettere di
 * lavorarci sopra per questo frame. */
static void SettleCommit(u32 playerOe)
{
    s16 x = oe_currentX(playerOe);
    s16 y = oe_currentY(playerOe);
    u8  dir = oe_facingDirection(playerOe);
    u32 speed = PlayerSpeedClass();
    u32 slot;

    g_state.prevMapKey = sLastMapKey;
    BUMPF(mapChanges);
    sLastMapKey = g_state.mapKey;
    sLastPlayerX = x;
    sLastPlayerY = y;
    sLastPlayerDir = dir;
    /* Se si arriva qui rientrando nell'overworld su una mappa diversa, il ramo
     * "primo campione" di TrackPlayer non deve rifare il lavoro al frame dopo. */
    sHavePlayerPos = 1;

    g_state.playerX = x;
    g_state.playerY = y;
    g_state.playerDir = dir;
    g_state.playerSpeed = speed;

    /* Adesso le coordinate sono quelle giuste. E' il SYNC che prima usciva
     * avvelenato: mappa nuova, coordinate vecchie. */
    BeginEntrySyncs(dir, (u8)speed, x, y);

    /* Il cambio mappa e' NOSTRO, quindi vale per tutti gli amici: ognuno
     * riparte da un resync - tranne chi viene portato oltre il bordo qui
     * sotto, che il resync se lo toglie da solo. */
    for (slot = 0; slot < N_REMOTES; slot++)
    {
        struct Remote *r = &g_remotes[slot];
        s16 vx, vy;
        u32 via;

        r->forceResync = 1;

        if (r->state != STATE_SPAWNED)
            continue;

        if (g_state.mapKey == r->spawnMapKey)
            continue;   /* sta gia' sulla mappa giusta: niente da decidere */

        /* IL REMOTO SOPRAVVIVE AL BORDO?
         *
         * La decisione si prende sulla posizione DICHIARATA, mai su quella
         * disegnata - ma adesso le due sono di nuovo nello stesso spazio, che
         * e' esattamente cosa garantisce il gate di assestamento. Se l'amico
         * e' ancora rappresentabile (stessa mappa, o route connessa a quella
         * nuova) il remoto si tiene: le sue coordinate le ha gia' tradotte il
         * gioco, che a ogni attraversamento di bordo applica gCamera a TUTTI
         * gli object event attivi (UpdateObjectEventCoordsForCameraUpdate,
         * event_object_movement.c:2167). Se una porta e' in corso PER QUESTO
         * slot, la sequenza vince e il remoto va tolto come prima; una porta
         * di un ALTRO slot non c'entra con la sua decisione. */
        if ((sDoorState == DOOR_ST_NONE || sDoorOwner != slot)
            && RemoteViewNow(r, &vx, &vy, &via))
        {
            u32 oe = ObjectEvent(r->objectId);
            u32 sb1 = SaveBlock1();

            r->spawnMapKey = g_state.mapKey;
            /* La mappa fa parte dell'identita' del remoto: FindExistingRemote
             * filtra su mapNum/mapGroup, e un object event che dice di stare
             * nella mappa di prima e' un object event che il frame dopo non
             * ritroviamo piu'. */
            if (sb1)
            {
                oe_mapNum(oe) = sb1_mapNum(sb1);
                oe_mapGroup(oe) = sb1_mapGroup(sb1);
            }
            BUMPF(borderCarries);
            /* NIENTE RESYNC: NON C'E' NIENTE DA RISINCRONIZZARE. Il remoto lo
             * stiamo tenendo e le sue coordinate le ha gia' tradotte il gioco
             * con la stessa aritmetica della nostra formula. Lasciando il flag
             * alzato, il primo PASSO dell'amico dopo il bordo diventerebbe un
             * TeleportRemote: scatto, sempre, deterministico. Il contatore
             * borderMismatch e' li' per dimostrare che l'assunto regge. */
            r->forceResync = 0;
            r->carryCheck = 1;
            continue;
        }

        DoorAbortIfOwner(slot);
        ForgetRemote(r);
        sFramesInOverworld = 0;
    }
}

/* ========================================================================== */

#ifdef PAYLOAD_WITH_SIO
/* LA SONDA (2026-08-15). Il debito di telemetria e' arrivato al conto: tre
 * diagnosi di fila fatte sui log del PC, con PayloadState illeggibile sul
 * fisico. Questo frame porta fuori, una volta al secondo E appena la porta
 * viene ripresa, i contatori che decidono ogni diagnosi: il payload e' vivo
 * (vbl sale)? emette (emessi sale)? la porta gira (prese/rese)? il latch e'
 * scattato (latch/rese/risvegli)? e in quale callback sta il gioco (cb2)?
 * Se la sonda TACE, anche quello e' un verdetto: porta chiusa o payload
 * fermo, e l'ultimo frame arrivato dice com'era il mondo un attimo prima.
 *
 * Layout (8 parole, little endian sul filo):
 *   w0  vblankCount   (16 bit bassi)
 *   w1  eventsEmitted (16 bit bassi)
 *   w2  sioAcquires<<8 | sioReleases   (8 bit bassi ciascuno)
 *   w3  linkYields<<8  | linkWakes
 *   w4  bit0 latch, bit1 inOverworld, bit2 assestamento,
 *       bit4-7 g_state.state, bit8-11 sLocalStatus,
 *       bit12-15 menuLatches (modulo 16: conta le TRANSIZIONI)
 *   w5  cb2 bassi   w6  cb2 alti
 *   w7  settleEntries<<8 | eventsDropped
 * Ritorna il valore di SioSendFrame: 0 = buffer occupato, si riprova. */
static int SioSendSonda(u32 cb2)
{
    u16 w[8];

    w[0] = (u16)g_state.vblankCount;
    w[1] = (u16)g_state.eventsEmitted;
    w[2] = (u16)(((g_state.sioAcquires & 0xFFu) << 8) | (g_state.sioReleases & 0xFFu));
    w[3] = (u16)(((g_state.linkYields  & 0xFFu) << 8) | (g_state.linkWakes   & 0xFFu));
    w[4] = (u16)((sLinkBusyLatch ? 1u : 0u)
               | (g_state.inOverworld ? 2u : 0u)
               | (sMapSettling ? 4u : 0u)
               | ((g_state.state & 0xFu) << 4)
               | ((sLocalStatus & 0xFu) << 8)
               | ((g_state.menuLatches & 0xFu) << 12));
    w[5] = (u16)cb2;
    w[6] = (u16)(cb2 >> 16);
    /* w7 porta DUE misure, un byte l'una, entrambe MODULO 256: il frame e'
     * fermo a 8 parole (SIO_MAX_WORDS) e non ce n'e' una nona. Il modulo non
     * fa danno perche' di questi due numeri interessa il MOTO, non il totale:
     * si guarda se salgono fra una sonda e la successiva.
     *
     * basso  RIPARAZIONI: quante volte la guardia ha trovato i registri
     *        seriali cambiati sotto di noi. Dice se il guastatore scrive una
     *        volta sola (caricamento mappa) o a ogni frame.
     * alto   SCRUB IE: quante volte siamo usciti dalla porta con bit ALTRUI
     *        accesi in IE (2026-08-30). In lotta il gioco arma VCount e
     *        HBlank: se questo NON sale entrando in battaglia, SioShutdown
     *        non sta girando dove deve e il difetto dell'audio puo' tornare. */
    w[7] = (u16)(((SioScrubCount() & 0xFFu) << 8) | (SioRepairCount() & 0xFFu));
    return SioSendFrame(SIO_T_STATE, w, 8);
}

/* LA PAGINA 2 (i registri veri, SIO_T_DIAG) E' STATA TOLTA il 2026-08-25 per
 * il bilancio EWRAM dei 4 giocatori: aveva chiuso la diagnosi del possesso
 * della porta (2026-08-16), quella classe e' stabile da allora, e i suoi
 * numeri restano leggibili in emulatore da PayloadState. Se il guasto dei
 * registri tornasse, si ripesca da payload/attic/sonda-pagina2.c. */

/* (sStateTimer: dichiarato in cima, accanto a sSioReady.) */
#endif

#ifndef PAYLOAD_WITH_SIO
/* hook.S chiama payload_drain() in tutte e due le build, perche' l'assembly
 * e' lo stesso file. Nella build per l'emulatore il driver SIO non c'e' e non
 * c'e' niente da raccogliere: la falla che quella funzione tura esiste solo
 * dove gli IRQ seriali li gestiamo noi. */
void payload_drain(void) {}
#endif

/* ========================================================================== */
/* IL CORPO: dal main loop, non piu' dall'IRQ (2026-08-21)                    */
/* ========================================================================== */

/* Fino al 2026-08-21 tutto questo girava dentro payload_frame, cioe' nel
 * VBlank. Funzionava perche' il main loop del gioco finisce ogni frame in
 * VBlankIntrWait e quando arriva l'IRQ il gioco e' di solito fermo - ma "di
 * solito" non e' "sempre": nei frame lunghi (caricamento mappa, Surf, lotta
 * in arrivo) l'IRQ cade IN MEZZO al lavoro del gioco, e ogni funzione del
 * gioco chiamata da qui diventa una corsa con lui. L'audit del salvataggio
 * ha contato cosa toccavamo dall'IRQ: SpawnSpecialObjectEventParameterized
 * (CreateSprite, AllocSpriteTiles, LoadPalette), ObjectEventSetGraphicsId
 * (palette), MoveObjectEventToMapCoords, ObjectEventSetHeldMovement,
 * DestroySprite, FieldAnimateDoorOpen/Close (CreateTask), PlaySE. Nessuno
 * scrive nel salvataggio; TUTTI possono, nella corsa, lasciare uno sprite
 * doppio, un tile altrui, un NPC con l'aspetto sbagliato. Il Surf e' stato
 * il primo a farsi vedere (gFieldEffectArguments -> gPlayerParty fuori
 * squadra). Questa e' la chiusura della classe intera, non del sintomo.
 *
 * DA QUI gira in payload_cb1, PRIMA del callback1 del gioco: stesso punto
 * logico del frame (il VBlank e' subito prima di ReadKeys, callback1 subito
 * dopo), ma in sequenza con il gioco, mai sovrapposto. Due cose restano al
 * VBlank perche' DEVONO: il SIO (hardware) e lo scudo del salvataggio (deve
 * girare anche quando il main loop non e' nell'overworld: Sala d'Onore,
 * salvataggi di link). Il pass-through, l'icona e la bolla erano gia' qui.
 *
 * Conseguenza visibile nei contatori: settleFramesLast vale 1 sempre (il
 * corpo non puo' piu' cadere dentro il caricamento mappa), e bodyTicks deve
 * correre a ~60/s finche' inOverworld e' 1. */
static void OverworldTick(void)
{
    u32 playerOe;

    if (!g_state.inOverworld)
        return;
    BUMPF(bodyTicks);

    /* Dentro l'overworld lo stato e' "sto camminando" oppure "sono dentro un
     * dialogo o uno script". Il flag e' sLockFieldControls, che e' static e non
     * compare nel .map: si legge chiamandone il getter, che e' tre istruzioni.
     * Il latch della sezione di menu si azzera QUI: il rientro nell'overworld
     * e' l'unico momento in cui si sa che la schermata e' stata chiusa.
     *
     * MA "controlli bloccati" NON vuol dire "dialogo": il menu START gira come
     * TASK dentro CB2_Overworld, a controlli bloccati. Senza distinguerlo,
     * chi sta nel menu veniva annunciato ST_DIALOG e l'amico vedeva il balloon
     * al posto dell'icona di menu (riportato da Lain il 2026-08-09). Il task
     * tiene Task_ShowStartMenu come func per TUTTA la vita del menu, dialogo
     * di salvataggio compreso (start_menu.c:561-577: il run loop passa da
     * gMenuCallback, ma DestroyTask arriva solo alla chiusura). La scansione
     * la fa il gioco (FuncIsActiveTask, 16 confronti) e si paga solo nel caso
     * raro in cui i controlli sono gia' bloccati. Il bit Thumb va rimesso:
     * gTasks[].func e' un puntatore C e ce l'ha acceso.
     * Nei primissimi frame dopo START la func e' ancora StartMenuTask (static,
     * fuori dal .map): resta un lampo di ST_DIALOG di qualche frame prima di
     * ST_MENU. Accettato: e' un'imprecisione da decimi di secondo, non il
     * balloon fisso di prima. */
    sOutFieldFrames = 0;
    sMenuSection = 0;
    if (!ArePlayerFieldControlsLocked())
        SetLocalStatus(ST_OVERWORLD);
    else if (FuncIsActiveTask((void *)(ADDR_Task_ShowStartMenu | 1u)))
        SetLocalStatus(ST_MENU);
    else
        SetLocalStatus(ST_DIALOG);
    StatusTick();

    playerOe = ObjectEvent(gPlayerAvatar_objectEventId);
    if (!oe_active(playerOe))
        return;

    g_state.mapKey = CurrentMapKey();   /* mappa corrente, aggiornata sempre */

    /* PRIMA DI TUTTO: GLI SLOT SONO ANCORA NOSTRI?
     *
     * Il gioco puo' aver riciclato un nostro slot, o averlo rimosso perche'
     * uscito dalla vista (RemoveObjectEventsOutsideView). `state == SPAWNED` e'
     * uno stato NOSTRO e puo' essere stantio: al primo frame di rientro
     * nell'overworld dopo un warp vero, ResetObjectEvents ha gia' riciclato lo
     * slot. Questo controllo sta PRIMA del blocco del cambio mappa, che scrive
     * oe_mapNum/oe_mapGroup - timbrare la mappa su un object event che non e'
     * piu' il nostro e' la classe di difetto n.3 (scrivere in una struttura
     * del gioco senza aver verificato l'identita'), e la cura e' l'ordine.
     * ForgetRemote controlla gia' localId prima di distruggere, quindi non
     * tocca l'object event di nessun altro. */
    {
        u32 slot;

        for (slot = 0; slot < N_REMOTES; slot++)
        {
            struct Remote *r = &g_remotes[slot];

            if (r->state == STATE_SPAWNED && !RemoteStillAlive(r))
            {
                DoorAbortIfOwner(slot);
                ForgetRemote(r);
            }
        }
    }

    /* IL GATE DI ASSESTAMENTO (vedi SETTLE_GIVEUP_FRAMES).
     *
     * La mappa e' cambiata? Allora per qualche frame tutto quello che si legge
     * e' un miscuglio di due spazi, e l'unica cosa sensata da fare e' non fare
     * niente: niente emissioni, niente consumo della coda RX, niente spawn,
     * niente despawn. La coda RX regge (mailbox + code per-slot, ~10 eventi/s
     * per amico, finestra di pochi frame) e la FSM delle porte continua a
     * girare da sola perche' ha i suoi timeout e non dipende dalle coordinate.
     *
     * sSettleMapKey e' separata da sLastMapKey apposta: la prima risponde a
     * "la mappa e' cambiata in questo frame?", la seconda a "da quale mappa e'
     * partito l'ultimo evento che ho emesso?" - e la seconda deve restare
     * ferma fino al commit, o il commit non saprebbe piu' da dove veniamo. */
    if (!sHaveSettleKey)
    {
        sSettleMapKey = g_state.mapKey;
        sHaveSettleKey = 1;
    }
    else if (g_state.mapKey != sSettleMapKey)
    {
        sSettleMapKey = g_state.mapKey;
        if (!sMapSettling)
        {
            sMapSettling = 1;
            BUMPF(settleEntries);
        }
        /* Mappa cambiata di nuovo mentre ci si assestava (due bordi di fila,
         * warp dentro warp): l'attesa riparte da zero. */
        sSettleTimer = 0;
    }

    if (sMapSettling)
    {
        int ready = PlayerCoordsSettled(playerOe);
        int late;

        sSettleTimer++;
        late = (sSettleTimer >= SETTLE_GIVEUP_FRAMES);

        if (!ready && !late)
        {
            DoorTick();
            g_state.rxPending = RxPending();
            return;
        }

        /* Il watchdog e' una rete di sicurezza per un percorso che non sappiamo
         * provocare: se sale, c'e' un caso in cui il predicato di coerenza non
         * torna mai vero e va capito quale. */
        if (!ready)
            BUMPF(settleTimeouts);

        sMapSettling = 0;
        g_state.settleFramesLast = sSettleTimer;

        SettleCommit(playerOe);

        DoorTick();
        g_state.rxPending = RxPending();
        /* Il resto riprende al frame successivo, con il mondo di nuovo coerente
         * e - per i remoti tenuti oltre il bordo - con carryCheck armato. */
        return;
    }

    TrackPlayer(playerOe);
    CardTxTick();

    /* LA PROVA CHE LA TRADUZIONE AL BORDO E' QUELLA DEL GIOCO.
     *
     * Un frame dopo aver portato un remoto oltre un bordo: il gioco ha gia'
     * applicato gCamera a initialCoords/currentCoords/previousCoords di tutti
     * gli object event attivi (UpdateObjectEventCoordsForCameraUpdate,
     * event_object_movement.c:2167-2190, chiamata dalla riga subito dopo
     * CameraMove in field_camera.c:415-416). Il tile disegnato e quello che la
     * nostra formula si aspetta devono coincidere.
     *
     * Il confronto vale anche a passo in corso: il PASSO del protocollo porta
     * gia' le coordinate di DESTINAZIONE, che sono le stesse che currentCoords
     * assume all'avvio del movimento. */
    {
        u32 slot;

        for (slot = 0; slot < N_REMOTES; slot++)
        {
            struct Remote *r = &g_remotes[slot];
            s16 vx, vy;
            u32 via;

            if (!r->carryCheck)
                continue;
            r->carryCheck = 0;

            if (r->state == STATE_SPAWNED && RemoteViewNow(r, &vx, &vy, &via))
            {
                u32 oe = ObjectEvent(r->objectId);
                s32 dx = (s32)oe_currentX(oe) - (s32)vx;
                s32 dy = (s32)oe_currentY(oe) - (s32)vy;

                if (dx != 0 || dy != 0)
                {
                    BUMPF(borderMismatch);
                    g_state.borderDriftX = dx;
                    g_state.borderDriftY = dy;
                }
            }
        }
    }

    DoorTick();
    ConsumeRemoteEvents();
    g_state.rxPending = RxPending();

    /* IL GIRO DEGLI SLOT: despawn, spazzino, adozione, spawn, posa da fermo.
     * E' il vecchio corpo del singolo remoto, una volta per amico. Lo slot
     * PRIMARIO (il primo con notizie) e' quello che i campi remote* di
     * g_state rispecchiano - contratto con la tabella S del Lua, invariato -
     * e lo specchio posizionale si aggiorna QUI, dove rep/via sono gia'
     * stati calcolati, invece di rifare RemoteViewNow a parte. */
    {
        u32 slot, primary;

        for (primary = 0; primary + 1u < N_REMOTES; primary++)
            if (g_remotes[primary].known)
                break;

        for (slot = 0; slot < N_REMOTES; slot++)
        {
            struct Remote *r = &g_remotes[slot];
            s16 vx, vy;
            u32 via;
            int rep = RemoteViewNow(r, &vx, &vy, &via);

            if (slot == primary)
            {
                g_state.remoteVia = via;
                if (rep)
                {
                    g_state.remoteLocalX = vx;
                    g_state.remoteLocalY = vy;
                }
                g_state.remoteAway = (r->known && !rep) ? 1u : 0u;
            }

            /* L'amico non e' piu' disegnabile qui mentre il suo avatar e'
             * ancora sulla nostra mappa. Prima si aspettava che il culling se
             * ne accorgesse. Durante una SUA sequenza di porta non si tocca:
             * e' la FSM a decidere quando l'avatar sparisce, ed e' proprio in
             * quel momento che l'amico risulta gia' altrove. */
            if (r->state == STATE_SPAWNED && r->known && !rep
                && (sDoorState == DOOR_ST_NONE || sDoorOwner != slot))
            {
                ForgetRemote(r);
                continue;
            }

            if (r->state == STATE_IDLE)
            {
                u8 existing;

                /* Nessuna notizia dell'amico: niente da disegnare. E un
                 * localId di questo slot vivo ADESSO non e' di nessuno - e'
                 * il fantasma del continue dal save (o di una reiniezione):
                 * si spazza. */
                if (!r->known)
                {
                    SweepStaleRemotes(slot);
                    continue;
                }

                /* L'amico e' appena entrato in una porta: non lo si fa
                 * riapparire davanti a quella appena chiusa, si aspetta la
                 * sua mappa nuova (o il timeout di DOOR_ST_HOLD). */
                if ((sDoorState == DOOR_ST_CLOSING || sDoorState == DOOR_ST_HOLD)
                    && sDoorOwner == slot)
                    continue;

                /* Non rappresentabile qui: non si spawna. Il caso simmetrico -
                 * avatar ancora qui e amico gia' altrove - lo chiude il
                 * despawn qui sopra. Ma un localId vivo mentre l'amico sta
                 * ALTROVE e' comunque un fantasma: si spazza. */
                if (!rep)
                {
                    SweepStaleRemotes(slot);
                    continue;
                }

                existing = FindExistingRemote(REMOTE_LID(slot));

                /* Un remoto e' gia' li' (tipico dopo una reiniezione del
                 * payload): lo si adotta invece di provare a crearne un altro,
                 * cosa che il gioco rifiuterebbe come duplicato. */
                if (existing < OBJECT_EVENTS_COUNT)
                {
                    u32 oe = ObjectEvent(existing);
                    u32 sb1 = SaveBlock1();

                    /* Adottare salta tutta la sequenza dell'uscita: se una
                     * porta era stata aperta per lui, va richiusa o resterebbe
                     * spalancata. */
                    if (sDoorState == DOOR_ST_EXIT_OPEN && sDoorOwner == slot)
                        DoorAbort();

                    r->objectId = existing;
                    r->state = STATE_SPAWNED;
                    r->spawnMapKey = g_state.mapKey;
                    BUMPF(adoptions);
                    /* Adottare senza riposizionare significa ereditare la
                     * posizione in cui era rimasto, che non ha niente a che
                     * vedere con dove sta l'amico adesso. */
                    TeleportRemote(oe, vx, vy);
                    /* Come per lo spawn (vedi TrySpawnRemote): questo E' gia'
                     * un riposizionamento assoluto, quindi il PASSO successivo
                     * deve essere un passo vero e non un teletrasporto. */
                    r->forceResync = 0;
                    /* E la mappa va riscritta, non solo la posizione: le
                     * lookup del gioco per localId cercano anche mapNum e
                     * mapGroup. */
                    if (sb1)
                    {
                        oe_mapNum(oe) = sb1_mapNum(sb1);
                        oe_mapGroup(oe) = sb1_mapGroup(sb1);
                    }
                    /* LA GRAFICA VA RIMESSA ANCHE QUI (2026-08-30). L'adozione
                     * riusa un object event che esisteva gia', con lo sprite
                     * che aveva: se nel frattempo l'amico e' salito in bici o
                     * in surf, restava disegnato a piedi. E' meta' del difetto
                     * riferito dal campo - «al cambio mappa o dopo la lotta
                     * l'amico e' senza animazione di surf» - perche' il cambio
                     * mappa passa proprio di qui. L'altra meta' e' la bolla,
                     * che sta in RemoteHasBlob. */
                    SyncRemoteGraphics(r, oe);
                    continue;
                }

                if (r->spawnCooldown)
                {
                    r->spawnCooldown--;
                    continue;
                }

                if (sFramesInOverworld < SPAWN_DELAY_FRAMES)
                    continue;

                /* USCITA DA UNA PORTA. Se l'amico ricompare esattamente su un
                 * tile di porta, prima la si apre e poi lo si spawna sulla
                 * soglia: il passo verso il basso arrivera' da lui, dalla
                 * rete, come qualunque altro. */
                if (!DoorGateSpawn(slot, via, vx, vy))
                    continue;

                TrySpawnRemote(r, vx, vy);

                if (sDoorState == DOOR_ST_EXIT_OPEN && sDoorOwner == slot)
                {
                    if (r->state == STATE_SPAWNED)
                    {
                        DoorSetState(DOOR_ST_EXIT_STEP);
                        BUMPF(doorExits);
                    }
                    else
                    {
                        /* Spawn fallito con la porta gia' aperta: si richiude,
                         * o resterebbe spalancata fino al ricaricamento. */
                        DoorAbort();
                    }
                }

                continue;
            }

            {
                u32 oe = ObjectEvent(r->objectId);

                SyncRemoteGraphics(r, oe);
                if (r->state != STATE_SPAWNED)
                    continue;   /* distrutto e non ancora rimesso */
                /* Se e' stato ricreato SUBITO (respawn per cambio dimensione,
                 * 2026-08-30) l'indirizzo di prima non vale piu': lo si
                 * rilegge, o tutto il blocco qui sotto - posa da fermo e
                 * lettura per il log - lavorerebbe su uno slot morto. */
                oe = ObjectEvent(r->objectId);

                /* La bolla del Surf la cura SurfBlobTick, che payload_cb1
                 * chiama DOPO questo corpo (vedi CreateRemoteSurfBlob). */

                /* POSA DA FERMO. Il remoto si muove solo quando arriva un
                 * evento: quando l'amico si ferma non arriva piu' niente,
                 * l'ultima movement action finisce e lo sprite resta
                 * sull'ULTIMO FOTOGRAMMA - dopo una corsa, a meta' falcata.
                 * Il giocatore locale ha l'idle handler che gli rimette la
                 * posa a ogni frame; il remoto quell'handler non ce l'ha,
                 * glielo facciamo noi. Il predicato "niente in attesa" e'
                 * la coda del SUO slot (RqLen), non quella della stanza. */
                if (!r->settled && RqLen(r) == 0 && RemoteReadyForMovement(oe))
                {
                    if (ObjectEventSetHeldMovement((void *)oe,
                                                   FaceActionFor(oe_facingDirection(oe))) == 0)
                        BUMPF(settles);
                    r->settled = 1;
                }

                /* Solo per il log, e solo per il primario: dove il gioco sta
                 * disegnando il remoto, con che grafica e a che indice di
                 * animazione (sopra 19 con lo sprite in bici = il bug dei
                 * glitch, vedi SyncRemoteGraphics). */
                if (slot == primary)
                {
                    g_state.remoteX = oe_currentX(oe);
                    g_state.remoteY = oe_currentY(oe);
                    g_state.remoteGfxId = oe_graphicsId(oe);
                    g_state.remoteAnimNum = sprite_animNum(Sprite(oe_spriteId(oe)));
                }
            }
        }

        /* Il resto dello specchio: gli scalari del primario e le bitmask del
         * quadro intero. */
        {
            struct Remote *pr = &g_remotes[primary];
            u32 i, known = 0, spawned = 0;

            for (i = 0; i < N_REMOTES; i++)
            {
                if (g_remotes[i].known)
                    known |= (1u << i);
                if (g_remotes[i].state == STATE_SPAWNED)
                    spawned |= (1u << i);
            }
            g_state.slotsKnown = known;
            g_state.slotsSpawned = spawned;

            g_state.state = pr->state;
            g_state.objectId = pr->objectId;
            g_state.remoteKnown = pr->known;
            g_state.remoteMapKey = pr->mapKey;
            g_state.remoteTargetX = pr->targetX;
            g_state.remoteTargetY = pr->targetY;
            g_state.remoteGender = pr->gender;
            g_state.remoteState = pr->avatarState;
            g_state.remoteStatus = pr->status;
            g_state.spawnMapKey = pr->spawnMapKey;
            g_state.cardRxBitmap = pr->cardRxBitmap;
        }
    }
}

void payload_frame(void)
{
    u16 flags = REG_IF;   /* l'handler originale non ha ancora fatto l'ack */
    u32 cb2;
    u32 linkBusy;

    BUMPF(irqCount);
    g_state.lastIf = flags;

#ifdef PAYLOAD_WITH_SIO
    /* PRIMA del ritorno anticipato sul VBlank: l'IRQ seriale e' un IRQ diverso,
     * e uscendo prima non lo vedremmo mai. E' il percorso caldo - durante un
     * salvataggio questo hook viene chiamato ~6800 volte al secondo (misurato il
     * 2026-08-02) - quindi qui non ci va nient'altro. */
    if (flags & IRQ_SERIAL)
        SioOnSerialIrq();
#endif

    if (!(flags & IRQ_VBLANK))
        return;

    /* IL CANCELLO ANTI-RAFFICA (misura del 2026-08-02, riga [hook] RAFFICA).
     *
     * In certe fasi - transizione pre-lotta, menu, salvataggio - il gioco arma
     * HBlank/VCount e questo handler entra ~440 volte a frame; l'ack del bit
     * VBlank arriva tardi e il bit resta leggibile per meta' di quegli
     * ingressi. Senza cancello il corpo girava 150-270 volte a frame e il
     * gioco rallentava a vista (lo "scatto" della transizione di lotta).
     *
     * Il cancello: il corpo gira solo se il contatore VBlank del GIOCO e'
     * avanzato dall'ultimo giro. Durante la raffica quel contatore avanza di
     * 60/s esatti (misurato), quindi il corpo torna a ~60 giri/s; al piu'
     * raddoppia nel frame in cui VBlankIntr lo incrementa fra due nostri
     * ingressi, che e' irrilevante. Costo dichiarato: se il gioco si pianta
     * del tutto il corpo si ferma con lui - visibile dal log, [hook] "noi" e
     * "gioco" fermi insieme. */
    {
        static u32 sLastGameVbl;
        u32 gv = gMain_vblankCounter1;
        if (gv == sLastGameVbl) {
            BUMPF(vbSkips);
            return;
        }
        sLastGameVbl = gv;
    }

    BUMPF(vblankCount);

    /* Il SIO si prende e si molla insieme all'overworld: vedi piu' sotto, dopo
     * che cb2 e' stato letto. */

    /* PRIMA DI QUALUNQUE ALTRA COSA NOSTRA: rimettere attivo il remoto se il
     * pass-through lo aveva tolto di mezzo. La riattivazione normale avviene in
     * payload_cb1, appena il callback1 del gioco ha finito di guardare le
     * collisioni; questa e' la rete di sicurezza per il caso in cui payload_cb1
     * non arrivi mai in fondo (hook perso a meta' frame, callback1 riscritto dal
     * gioco). Un object event lasciato inattivo per sempre e' uno slot che il
     * gioco puo' riciclare sotto di noi. */
    PassThroughRelease();

    /* Battaglie, menu, transizioni: qui non si tocca niente. */
    cb2 = gMain_callback2 & ~1u;   /* i puntatori a funzione hanno il bit Thumb */

    /* IL COMPLETAMENTO DEL RESET SALTATO (2026-08-15). Sul percorso mbstub si
     * entra in AgbMain OLTRE il RegisterRamReset (e' cosi' che il payload
     * sopravvive in EWRAM), quindi TUTTA la RAM che il gioco si aspetta
     * azzerata... non lo e'. Il gioco se la cava perche' i suoi puntatori li
     * scrive esplicitamente, ma i testimoni del latch (gLinkCallback,
     * gReceivedRemoteLinkPlayers, IWRAM) vengono scritti SOLO dalle funzioni
     * di link: se all'accensione contengono spazzatura, restano spazzatura
     * per tutta la sessione e il latch scatta al primo frame -- muto per
     * sempre. Qui si completa, una volta sola e PRIMA della prima lettura,
     * la parte del reset che ci riguarda. In emulatore (iniezione a gioco
     * avviato, RAM gia' pulita) e' una scrittura di zero su zero. */
    {
        static u32 sRamCleanDone;
        if (!sRamCleanDone)
        {
            sRamCleanDone = 1;
            GAME_U32(ADDR_gLinkCallback) = 0u;
            GAME_U8(ADDR_gReceivedRemoteLinkPlayers) = 0u;
            /* gMain.inBattle e' un bitfield che SOLO l'ingresso in lotta
             * scrive: sporco all'accensione resterebbe sporco fino alla
             * prima lotta, e ora e' lui a decidere il possesso della porta. */
            GAME_U8(ADDR_gMain + 0x439) &= (u8)~0x02u;
        }
        else if (sRamCleanDone == 1)
        {
            /* L'init NOSTRO (g_remotes e l'azzeramento di .lateclear) al
             * SECONDO VBlank, non al primo. Il primo IRQ dopo l'hook puo'
             * cadere sulle ULTIME istruzioni di handoff.S: la finestra e' di
             * pochi cicli (IME riacceso al passo 7, salto al passo 8) ma un
             * VBlank rimasto PENDENTE mentre IME era spento scatta esattamente
             * li' - e azzerare .lateclear in quel momento significherebbe
             * azzerare il literal pool che quelle istruzioni stanno per
             * leggere: bx verso 0x00000000, un crash al boot ogni qualche
             * migliaio, non diagnosticabile. Un frame dopo handoff e' morto
             * per davvero. Il frame di ritardo e' innocuo: ogni lettura di
             * g_remotes nel primo frame e' zero-safe (stato IDLE, known 0,
             * code vuote), verificato ramo per ramo il 2026-08-25. */
            sRamCleanDone = 2;
            RemotesInitOnce();
        }
    }

    /* SEMPRE, dentro e fuori dall'overworld: i salvataggi partono dal menu
     * START ma anche da script (Torre Lotta) e dalla Sala d'Onore, che
     * dell'overworld non sono. Lo scudo deve coprire tutti. */
    ScrubSaveStaging();

    /* IL GIOCO VUOLE IL CAVO (Consegna C; RISCRITTO il 2026-08-15 dopo la
     * prova sul campo). I testimoni d'INGRESSO sono gLinkCallback (si alza
     * dentro OpenLink) e gReceivedRemoteLinkPlayers (la sessione stabilita).
     * Ma il testimone d'ingresso NON serve da testimone d'uscita, ed e'
     * l'errore che ha congelato la prova del 2026-08-15: su un linkup
     * ANNULLATO il gioco chiama CloseLink, che azzera
     * gReceivedRemoteLinkPlayers e spegne la seriale MA NON gLinkCallback
     * (link.c:400-407 - lo azzerano solo alcuni flussi interni). Risultato:
     * linkBusy restava vero PER SEMPRE, e il payload dormiva per il resto
     * della sessione - "non ci vediamo piu', anche uscendo all'aperto".
     *
     * La macchina giusta e' un LATCH con due segnali diversi:
     *   INGRESSO: fronte di salita dei testimoni. Si butta l'arretrato TX
     *     (il CLUB non deve fare la coda: e' l'avviso che vale la sessione),
     *     si emettono 3 copie di EVENT_CLUB, e per LINK_GRACE_FRAMES si
     *     continua a pompare perche' escano dal cavo. Il gioco e' child in
     *     attesa: in quei frame il cavo non lo tocca.
     *   USCITA: il gioco HA RIATTACCATO. La prova non e' gLinkCallback: e'
     *     REG_IE - CloseLink -> DisableSerial spegne il bit serial
     *     (link.c:1857-1859), e mentre siamo in resa i registri sono suoi.
     *     Bit spento + nessun giocatore remoto = link finito. Al risveglio
     *     si finisce il lavoro che CloseLink ha lasciato a meta': si azzera
     *     gLinkCallback NOI, o il callback stantio ci farebbe rientrare nel
     *     latch al frame dopo, all'infinito. E' una scrittura in RAM del
     *     gioco, ma e' esattamente lo stato in cui un CloseLink completo
     *     l'avrebbe lasciata. */
    if (!sLinkBusyLatch)
    {
        /* Filtro di plausibilita' (cintura oltre alle bretelle del reset
         * completato qui sopra): un gLinkCallback vero e' un puntatore a
         * funzione Thumb in ROM (0x08xxxxxx, bit 0 alzato -- tutti i
         * LinkCB_* stanno li'), e gReceivedRemoteLinkPlayers e' un bool8
         * che il gioco scrive solo come 0 o 1. Qualunque altro valore non
         * e' un club: e' RAM sporca, e non deve addormentare il payload. */
        u32 lcb = GAME_U32(ADDR_gLinkCallback);
        if (((lcb >> 24) == 0x08u && (lcb & 1u))
            || GAME_U8(ADDR_gReceivedRemoteLinkPlayers) == 1u)
        {
            sLinkBusyLatch = 1;
            sLinkGrace = LINK_GRACE_FRAMES;
            BUMPF(linkYields);
            g_mailbox.txTail = g_mailbox.txHead;   /* via l'arretrato */
            EmitEvent(EVENT_CLUB, 0, 0, 0, 0);
            EmitEvent(EVENT_CLUB, 0, 0, 0, 0);
            EmitEvent(EVENT_CLUB, 0, 0, 0, 0);
        }
    }
    else if (sLinkGrace == 0
             && (REG_IE & IRQ_SERIAL_BIT) == 0
             && GAME_U8(ADDR_gReceivedRemoteLinkPlayers) == 0u)
    {
        sLinkBusyLatch = 0;
        GAME_U32(ADDR_gLinkCallback) = 0u;
        BUMPF(linkWakes);
    }

    linkBusy = sLinkBusyLatch;
    if (linkBusy && sLinkGrace)
    {
        sLinkGrace--;
        linkBusy = 0;   /* ancora un frame da passthrough: si pompa il CLUB */
    }

#ifdef PAYLOAD_WITH_SIO
    /* IL SIO SI PRENDE E SI MOLLA INSIEME ALL'OVERWORLD.
     *
     * Prima SioInit() veniva chiamata una volta al primo VBlank e la porta
     * seriale non tornava piu' al gioco. Sembrava innocuo perche' nell'overworld
     * il gioco non la usa - ma `VBlankIntr` chiama `LinkVSync()` a OGNI frame
     * quando gLinkVSyncDisabled e' 0, e ci sono schermate (Cable Club, Union
     * Room, le facility del Fronte Lotta) in cui quella macchina si arma davvero.
     * Il 2026-08-02 la build con il driver dentro ha piantato il gioco alla Torre
     * Lotta, in emulatore e senza partner: il sintomo era amplificato dalla
     * mancanza del cavo, ma la collisione e' la stessa anche col Celio attaccato.
     *
     * Ora il possesso della porta segue esattamente la condizione in cui il
     * payload lavora. Fuori dall'overworld SioShutdown() rimette REG_IE,
     * REG_SIOCNT e REG_RCNT come li aveva trovati, e SioOnSerialIrq() esce
     * subito su `!sSio.active`: da quel momento l'IRQ seriale non viene piu'
     * consumato da noi e `SerialIntr` del gioco lo vede di nuovo. */
    /* LA PORTA E' NOSTRA SEMPRE, TRANNE LOTTA E LINK (2026-08-15, sera).
     * La storia di questa condizione e' la storia dei muti: "solo overworld"
     * accumulava le icone dei menu e le sputava alla chiusura; "overworld +
     * menu noti" e' morta sul fisico al primo warp - la sonda ha fotografato
     * `porta 1 prese / 0 rese, latch 0, cb2 = CB2_Overworld` e poi il buio:
     * il giro molla/riprendi attraversa il caricamento mappa, dove il gioco
     * sposta perfino i SaveBlock (`MoveSaveBlocks_ResetHeap`, overworld.c
     * ~2074) e spegne la seriale uscendo dai Centri (CloseLink,
     * overworld.c:1758). Qualcosa in quel campo minato uccideva la ripresa,
     * e OGNI condizione basata su cb2 obbliga a riattraversarlo a ogni
     * porta di casa.
     *
     * La politica nuova rovescia il criterio: si molla SOLO quando si sa
     * che la seriale serve ad altri - il link del gioco (linkBusy, resa
     * senza restore) e la lotta (prudenza: le lotte link la usano davvero).
     * Nei warp, nei menu, nei dialoghi, sul titolo la porta resta nostra e
     * la sonda continua a battere. Se il gioco ci calpesta i registri di
     * nascosto (il CloseLink dei Centri lo fa di sicuro), NON ce ne
     * accorgiamo da qui: se ne accorge il watchdog di SioTick, che dopo 2 s
     * senza IRQ riarma la configurazione intera - vedi sio.c. Il rischio
     * residuo dichiarato: schermate che armano la seriale SENZA alzare
     * gLinkCallback (la Torre Lotta del crash 2026-08-02?); se esistono, la
     * sonda ora vive abbastanza da mostrarcele. */
    if (!linkBusy && !gMain_inBattle) {
        if (!sSioReady) {
            SioInit();
            sSioReady = 1;
            BUMPF(sioAcquires);
            /* La ripresa si annuncia da sola: se dopo un warp questo frame
             * non arriva mai al PC, il verdetto e' "porta mai ripresa". */
            SioSendSonda(cb2);
            sStateTimer = 0;
        }
        /* Prima la guardia, poi tutto il resto: se i registri non sono piu'
         * nostri, rimetterli PRIMA di provare a trasmettere e' la differenza
         * fra un frame perso e due secondi di buio. */
        SioGuard();
        SioTick();
        if (++sStateTimer >= 60)
        {
            if (SioSendSonda(cb2))
                sStateTimer = 0;
        }
        SioPumpTx();
    } else if (sSioReady) {
        /* Due modi di restituire la porta, e la differenza e' l'ORDINE degli
         * eventi. Uscita normale dall'overworld: i registri li abbiamo
         * configurati noi e si RIPRISTINANO (SioShutdown). Resa al link del
         * gioco: OpenLink ha GIA' riconfigurato la seriale prima che noi si
         * potesse vedere gLinkCallback, quindi il ripristino scriverebbe la
         * nostra configurazione stantia sopra la sua - si molla e basta
         * (SioYield), i registri restano del gioco. */
        if (linkBusy)
            SioYield();
        else
            SioShutdown();
        sSioReady = 0;
        BUMPF(sioReleases);
    }
#endif

    if (cb2 != ADDR_CB2_Overworld || linkBusy)
    {
        g_state.inOverworld = 0;
        sFramesInOverworld = 0;
        sHavePlayerPos = 0;   /* al rientro si riparte con un sync assoluto */


        /* CHE COSA STA FACENDO CHI NON E' NELL'OVERWORLD.
         *
         * gMain.inBattle si alza al primo frame fuori (CB2_InitBattleInternal),
         * perche' la transizione di battaglia gira ancora DENTRO l'overworld:
         * quindi la palla non e' mai preceduta dallo zaino.
         *
         * Tutto il resto passa da un secondo di attesa. Uscire dall'overworld
         * non vuol dire "menu": vuol dire anche warp, porta, transizione. Senza
         * il debounce l'amico si prenderebbe uno zaino sulla testa ogni volta
         * che entra in una casa. */
        if (gMain_inBattle)
        {
            sOutFieldFrames = 0;
            sMenuSection = 0;
            SetLocalStatus(ST_BATTLE);
        }
        else
        {
            /* La sezione si campiona a OGNI frame, non solo dopo il debounce:
             * il callback di lancio passa in gMain.callback2 nei primissimi
             * frame della transizione, molto prima del secondo di attesa. E il
             * latch si AGGIORNA se ne passa un altro (Squadra -> "dai uno
             * strumento" -> Zaino): l'icona segue la schermata vera. */
            u32 section = MenuSectionFor(cb2);

            if (section != 0 && section != sMenuSection)
            {
                sMenuSection = section;
                BUMPF(menuLatches);
            }

            if (sOutFieldFrames < MENU_DEBOUNCE_FRAMES)
                sOutFieldFrames++;
            else
                SetLocalStatus(sMenuSection ? sMenuSection : ST_MENU);
        }
        StatusTick();

        /* LA CODA RX NON SI ACCUMULA MENTRE DORMIAMO. Uscendo prima di drenare,
         * il bridge trovava la coda piena e scartava: nel log `RX scartati`
         * saliva 1, 2, 3 ... 10 stando in un menu. E al rientro si sarebbero
         * applicati fino a 16 eventi vecchi di secondi, cioe' una camminata
         * fantasma verso una posizione che l'amico ha gia' lasciato.
         * Si butta tutto e si alza il resync: al rientro l'unica cosa che conta
         * e' la prima posizione assoluta. */
        if (RxPending() != 0)
        {
            u32 i;

            /* MA LO STATO DELL'AMICO NON E' UN PASSO (2026-08-21): non
             * invecchia e non sposta niente. Se ne tiene l'ULTIMO, PER OGNI
             * SLOT, prima di buttare la coda: al rientro dal MIO menu
             * l'icona sopra la testa di ciascuno e' gia' quella giusta,
             * invece di restare quella vecchia fino al battito successivo
             * (fino a 2 s). */
            u32 t = g_mailbox.rxTail;
            while (t != g_mailbox.rxHead)
            {
                if (EVENT_KIND(g_mailbox.rx[t].type) == EVENT_STATUS)
                {
                    u32 sl = EVENT_SLOT(g_mailbox.rx[t].type);
                    if (sl >= N_REMOTES)
                        sl = N_REMOTES - 1;
                    g_remotes[sl].status = g_mailbox.rx[t].dir;
                    BUMPF(statusRx);
                }
                t = WRAP(t, RX_SLOTS);
            }
            g_state.rxDrained += RxPending();
            g_mailbox.rxTail = g_mailbox.rxHead;
            /* Anche le code per-slot: il corpo e' fermo e gli eventi la'
             * dentro invecchiano uguale. Il consumatore non gira (fuori
             * dall'overworld payload_cb1 non lavora), quindi toccarle da
             * qui non e' una corsa. */
            for (i = 0; i < N_REMOTES; i++)
            {
                g_remotes[i].qTail = g_remotes[i].qHead;
                g_remotes[i].forceResync = 1;
            }
        }

        /* La FSM delle porte si dimentica, ma NON si chiude niente: qui fuori
         * non c'e' piu' una mappa su cui disegnare. Una porta rimasta aperta la
         * ripara da sola la DrawWholeMapView del rientro, perche' l'animazione
         * tocca la vista e non i metatile. */
        if (sDoorState != DOOR_ST_NONE)
            DoorSetState(DOOR_ST_NONE);
        return;
    }

    g_state.inOverworld = 1;
    if (sFramesInOverworld < 0xFFFFFFFFu)
        sFramesInOverworld++;

    /* Da qui in poi, in overworld, l'IRQ NON tocca piu' niente del gioco: il
     * corpo gira in OverworldTick dal main loop (payload_cb1), che questo
     * hook installa e tiene installato. Vedi il commento sopra OverworldTick
     * e in testa al file (2026-08-21). */
    EnsureCallback1Hook();
}
