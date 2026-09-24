/* main.c - fase 1 dello stub multiboot: aspetta la cartuccia, semina il payload
 *          nella coda di EWRAM, poi passa la mano a handoff.S.
 *
 * LA SEQUENZA COMPLETA, per non doverla ricostruire ogni volta
 * ------------------------------------------------------------
 *   cartuccia FUORI -> accendi -> il BIOS entra in multiboot slave -> il PC
 *   manda questa immagine -> lo stub gira in EWRAM e diventa ROSSO -> inserisci
 *   la cartuccia -> lo stub la riconosce (GIALLO), semina il payload (VERDE) e
 *   salta in handoff.S, che azzera la EWRAM tranne la coda, aggancia l'IRQ e
 *   entra dentro AgbMain.
 *
 * Da quel momento Smeraldo gira normalmente con il nostro codice gia' dentro, e
 * il SALVATAGGIO NON E' STATO TOCCATO. E' l'intero punto del blocco W'.
 *
 * COLORI, che sono l'unica diagnostica che Lain ha davanti
 * -------------------------------------------------------
 *   rosso    aspetto la cartuccia (o la cartuccia non e' quella profilata)
 *   giallo   cartuccia riconosciuta, sto seminando
 *   verde    seminato e verificato, passo la mano
 *   magenta  RIFIUTO: il payload non ci sta sotto HANDOFF_BASE. Non salta.
 *   ciano    RIFIUTO: il magic del payload non torna dopo la copia.
 *   blu      RIFIUTO: la cartuccia HA il boot di Smeraldo italiano ma il corpo
 *            della ROM non e' quello profilato (altra revisione o riproduzione
 *            rimaneggiata). Il gioco partirebbe perfetto e il payload sarebbe
 *            muto per sempre: meglio dirlo qui che scoprirlo dal silenzio.
 *
 * Un rifiuto non prosegue mai. Saltare dentro AgbMain con un payload monco
 * significherebbe un hook che punta a spazzatura al primo VBlank, cioe' un
 * blocco del gioco a schermo nero senza nessuna indicazione di cosa sia andato
 * storto.
 */

/* La versione del gioco si sceglie in build (hw/mbstub/build.ps1 -Syms usa):
 * il blocco di boot e' lo stesso, cambiano gamecode e firme del corpo. */
#if defined(MBSTUB_SYMS_USA)
#include "boot_syms_usa.h"
#else
#include "boot_syms_it.h"
#endif

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;

#define REG_DISPCNT  (*(volatile u16 *)0x04000000)
#define REG_VCOUNT   (*(volatile u16 *)0x04000006)
#define BG_PALETTE   ((volatile u16 *)BG_PLTT_ADDR)

#define RGB(r, g, b) ((u16)((r) | ((g) << 5) | ((b) << 10)))
#define COL_RED      RGB(31,  0,  0)
#define COL_YELLOW   RGB(31, 31,  0)
#define COL_GREEN    RGB( 0, 31,  0)
#define COL_MAGENTA  RGB(31,  0, 31)
#define COL_CYAN     RGB( 0, 31, 31)
#define COL_BLUE     RGB( 0,  0, 31)

/* Il payload, incollato nell'immagine come blob da build.ps1 (.incbin). Lo stub
 * non ne conosce il contenuto: e' un artefatto separato, con la sua build. */
extern const u8 payload_bin[];
extern const u8 payload_bin_end[];

/* Confini di .handoff: VMA nella coda protetta, LMA qui in mezzo all'immagine.
 * Li definisce mbstub.ld. */
extern const u8 __handoff_start[];
extern const u8 __handoff_end[];
extern const u8 __handoff_lma[];

static void setColor(u16 c)
{
    REG_DISPCNT = 0;        /* mode 0, nessun BG acceso: si vede il backdrop */
    BG_PALETTE[0] = c;
}

static void waitFrame(void)
{
    while (REG_VCOUNT >= 160) { }   /* fine del VBlank precedente */
    while (REG_VCOUNT <  160) { }   /* inizio del prossimo        */
}

/* La cartuccia c'e' ED e' quella profilata?
 *
 * Non un checksum dell'intera ROM: si controllano il gamecode e le DUE word di
 * literal pool su cui lo stub si appoggia davvero (InitIntrHandlers legge da
 * li' l'indirizzo di IntrMain e quello di IntrMain_Buffer). Se quelle tornano,
 * il blocco di boot e' quello verificato con objdump; se non tornano,
 * proseguire sarebbe indovinare.
 *
 * A slot vuoto il bus della cartuccia e' aperto e restituisce valori che non
 * hanno nessuna ragione di coincidere con tre costanti scelte: il test vale
 * quindi anche come rilevatore di presenza.
 */
static int cartIsOurs(void)
{
    if (*(volatile u32 *)ADDR_GAMECODE      != GAMECODE_EXPECTED)      return 0;
    if (*(volatile u32 *)ADDR_POOL_INTRMAIN != ADDR_INTR_MAIN)         return 0;
    if (*(volatile u32 *)ADDR_POOL_INTRBUF  != ADDR_INTR_MAIN_BUFFER)  return 0;
    return 1;
}

/* Il CORPO della ROM e' quello per cui il payload e' stato costruito?
 *
 * cartIsOurs() guarda solo i primi 4 KB, e non basta: il caso reale del
 * 2026-08-02 e' una cartuccia che passa quel controllo, avvia il gioco
 * perfettamente, e ha il payload muto per sempre perche' CB2_Overworld non
 * sta all'indirizzo profilato. Quattro parole sparse su 1,2 MB fanno da
 * firma: una ROM spostata o rimaneggiata non puo' azzeccarle tutte.
 *
 * Si controlla DOPO il debounce, a bus stabile: durante l'inserimento a
 * caldo una lettura lontana potrebbe fallire per un contatto che balla, e
 * un BLU detto per sbaglio manderebbe la diagnosi fuori strada. */
static int cartMatchesPayloadSyms(void)
{
    if (*(volatile u32 *)ADDR_CHK_CB2_OVERWORLD  != VAL_CHK_CB2_OVERWORLD)  return 0;
    if (*(volatile u32 *)ADDR_CHK_SPAWN_OBJEVENT != VAL_CHK_SPAWN_OBJEVENT) return 0;
    if (*(volatile u32 *)ADDR_CHK_SET_HELD_MOVE  != VAL_CHK_SET_HELD_MOVE)  return 0;
    if (*(volatile u32 *)ADDR_CHK_CB2_BAGMENU    != VAL_CHK_CB2_BAGMENU)    return 0;
    return 1;
}

/* Arrotondamento PER ECCESSO, non per difetto: una dimensione non multipla di 4
 * troncata perderebbe fino a 3 byte in coda al payload, e sarebbero gli ultimi -
 * cioe' proprio quelli che nessuna verifica di magic andrebbe a guardare.
 * Copiare qualche byte in piu' e' innocuo: la regione e' stata azzerata prima ed
 * e' piu' grande del payload. */
static void copy32(volatile u32 *dst, const u32 *src, u32 bytes)
{
    u32 i;
    for (i = 0; i < (bytes + 3) / 4; i++)
        dst[i] = src[i];
}

static void zero32(volatile u32 *dst, u32 bytes)
{
    u32 i;
    for (i = 0; i < bytes / 4; i++)
        dst[i] = 0;
}

int main(void)
{
    const u32 payloadSize = (u32)(payload_bin_end - payload_bin);
    const u32 handoffSize = (u32)(__handoff_end - __handoff_start);
    u32 stable;

    /* Il payload viene copiato PRIMA di handoff.S, e sotto di esso. Se fosse
     * cresciuto oltre HANDOFF_BASE la copia di handoff gli mangerebbe la coda,
     * e il difetto si vedrebbe solo molto piu' tardi come hook impazzito.
     * build.ps1 lo verifica gia' a freddo; qui e' la seconda rete. */
    if (payloadSize > (HANDOFF_BASE - PAYLOAD_BASE)) {
        for (;;) setColor(COL_MAGENTA);
    }

    setColor(COL_RED);

    /* Debounce lungo, un secondo pieno. L'inserimento a caldo non e' istantaneo:
     * i contatti ballano, e una lettura valida presa a meta' inserimento
     * porterebbe a seminare il payload mentre il bus e' ancora instabile. */
    stable = 0;
    while (stable < 60) {
        waitFrame();
        stable = cartIsOurs() ? (stable + 1) : 0;
    }

    /* Bus stabile da un secondo: ora la firma profonda. Doppia lettura a un
     * frame di distanza, e si boccia solo se sbaglia DUE volte: una lettura
     * sporca isolata non deve produrre un BLU. */
    if (!cartMatchesPayloadSyms()) {
        waitFrame();
        if (!cartMatchesPayloadSyms()) {
            for (;;) setColor(COL_BLUE);
        }
    }

    setColor(COL_YELLOW);

    /* Coda pulita prima di scrivere: il .bss del payload lo pretende azzerato,
     * esattamente come fa zeroRegion() in mgba/inject_body.lua. */
    zero32((volatile u32 *)PAYLOAD_BASE, EWRAM_END - PAYLOAD_BASE);
    copy32((volatile u32 *)PAYLOAD_BASE, (const u32 *)payload_bin, payloadSize);
    copy32((volatile u32 *)HANDOFF_BASE, (const u32 *)__handoff_lma, handoffSize);

    /* Stessa verifica dell'iniettore Lua: il magic 'HOOK' a +0x08 dice che il
     * payload e' arrivato intero e allineato. */
    if (*(volatile u32 *)(PAYLOAD_BASE + PAYLOAD_OFF_MAGIC) != PAYLOAD_MAGIC) {
        for (;;) setColor(COL_CYAN);
    }

    setColor(COL_GREEN);

    /* Un istante di verde perche' sia visibile: da qui in poi comanda il gioco
     * e questo schermo non torna piu'. */
    for (stable = 0; stable < 30; stable++)
        waitFrame();

    /* handoff.S e' ARM e questo file e' compilato in Thumb. Il salto si scrive a
     * mano invece di usare un puntatore a funzione: cosi' non dipende dal fatto
     * che il compilatore emetta un veneer di interworking corretto, che su
     * ARMv4T (niente blx) e' esattamente il genere di dettaglio che funziona in
     * emulatore e si rompe altrove. L'indirizzo e' pari, quindi bx passa in ARM.
     * Non torna: finisce dentro AgbMain. */
    __asm__ volatile ("bx %0" :: "r"(HANDOFF_BASE));

    for (;;) { }
}
