/* main.c - il NODO GIOCATORE: banco di prova del trasporto SIO che parla il
 * protocollo vero (NetEvent da 12 byte) e impersona un giocatore.
 *
 * Gira AL POSTO di Smeraldo, caricato via multiboot: la cartuccia non e'
 * nemmeno inserita, quindi il salvataggio non corre nessun rischio.
 *
 * Dal 2026-08-02 non si limita a misurare il canale: genera gli stessi eventi
 * che il payload emette dentro il gioco - SYNC d'ingresso ripetuti, PASSO a
 * un tile per volta, SYNC periodico - e consuma quelli in arrivo. E' il capo
 * fisico della catena GBA <-> Celio <-> client --transport usb <-> relay <->
 * client --transport tcp <-> mGBA con Smeraldo + payload.
 *
 * LA MAPPA NON E' CABLATA: il nodo la ADOTTA dal primo evento remoto che
 * arriva, insieme a sesso e stato dell'avatar, e si mette a camminare in
 * quadrato a due tile dal giocatore remoto. Finche' non arriva nessuno, i
 * suoi eventi viaggiano su una mappa che non esiste (0.0) e il payload
 * dall'altra parte, giustamente, non li disegna.
 *
 * La semantica degli eventi e' quella ESATTA di payload/main.c (TrackPlayer):
 *   - PASSO porta il tile di DESTINAZIONE (le currentCoords in Gen 3 cambiano
 *     all'avvio del passo), un tile esatto per evento - "un passo e' un tile,
 *     per definizione";
 *   - direzioni DIR_SOUTH=1 NORTH=2 WEST=3 EAST=4, y che cresce verso sud;
 *   - all'ingresso in mappa niente PASSO: SYNC assoluto, RIPETUTO, perche'
 *     perderne uno li' costa caro;
 *   - SYNC periodico ogni 60 frame come correzione.
 *
 * REQUISITO, non dettaglio: con una board sola il Pico va riflashato a meta'
 * procedura (multiboot col firmware di lorenzooone, poi Celio per il test).
 * Per quei ~10 secondi i pin vanno in alta impedenza e non arriva niente.
 * Questo programma deve restare in attesa e riagganciarsi da solo. Per questo
 * non c'e' nessuna fase di "inizializzazione riuscita": c'e' un ciclo che vive
 * di quello che arriva, e SioTick() che si accorge del silenzio.
 */

#include "../../payload/types.h"

#define REG_DISPCNT (*(volatile u16 *)0x04000000)
#define REG_VCOUNT  (*(volatile u16 *)0x04000006)
#define REG_IME     (*(volatile u16 *)0x04000208)
#define VRAM        ((volatile u16 *)0x06000000)

#define MODE3_BITMAP (0x0003 | 0x0400)   /* mode 3 + BG2 */
#define SCREEN_W 240
#define SCREEN_H 160

#define RGB(r, g, b) ((u16)((r) | ((g) << 5) | ((b) << 10)))
#define COL_BG    RGB(2, 2, 6)
#define COL_LABEL RGB(14, 14, 18)
#define COL_VALUE RGB(31, 31, 31)
#define COL_GOOD  RGB(6, 28, 8)
#define COL_BAD   RGB(31, 6, 6)
#define COL_ADOPT RGB(8, 14, 31)

/* --- dal driver ---------------------------------------------------------- */
extern struct SioCounters {
    u32 irqs, wordsTx, wordsRx, framesTx, framesRx;
    u32 frameErr, resync, txFull, rxFull, zeroWords, errFlag;
} g_sio;

void SioInit(void);
void SioTick(void);
int  SioSendFrame(u8 type, const u16 *words, u8 count);

#define SIO_T_EVENT 0x01
#define SIO_T_PING  0x03

/* --- il giocatore che impersoniamo ---------------------------------------- */
/* I tipi e i codici sono quelli del protocollo (net/protocol.py e
 * payload/main.c): qui non si inventa niente, si specchia. */
#define EVENT_STEP   1u
#define EVENT_SYNC   2u
#define EVENT_LEAVE  4u

#define DIR_SOUTH 1u
#define DIR_NORTH 2u
#define DIR_WEST  3u
#define DIR_EAST  4u

#define STEP_FRAMES        16u  /* una camminata vera: 16 frame per tile     */
#define SYNC_PERIOD        60u  /* come SYNC_PERIOD del payload              */
#define ENTRY_SYNC_REPEATS  3u  /* SYNC d'ingresso ripetuti, come il payload */
#define ENTRY_SYNC_SPACING 10u
#define SQUARE_SIDE         3u  /* passi per lato del quadrato               */

static u8  sAdopted;            /* abbiamo una mappa vera su cui camminare?  */
static u8  sMapGroup, sMapNum;  /* adottate dal primo evento remoto          */
static u8  sGender, sAvatar;    /* idem: cosi' lo sprite e' sempre valido    */
static s16 sX, sY;              /* la NOSTRA posizione (tile, spazio oe_*)   */
static u8  sSeq;                /* seq del protocollo di gioco, un byte      */
static u8  sSide;               /* lato del quadrato: indice in sSquareDirs  */
static u8  sStepInSide;         /* passi gia' fatti su questo lato           */
static u8  sEntryLeft;          /* SYNC d'ingresso ancora da ripetere        */
static u8  sEntryTimer;

static u32 sRemoteEvents;       /* eventi remoti ricevuti (tutti i tipi)     */
static s16 sRemX, sRemY;        /* ultima posizione vista del remoto         */
static u8  sRemDir;

/* Il giro in quadrato: SQUARE_SIDE passi a sud, poi est, nord, ovest.
 * Indici DIR_*: le delta sono le stesse sDirDeltaX/Y del payload. */
static const u8 sSquareDirs[4] = { DIR_SOUTH, DIR_EAST, DIR_NORTH, DIR_WEST };
static const signed char sDirDeltaX[] = { 0,  0,  0, -1,  1 };
static const signed char sDirDeltaY[] = { 0,  1, -1,  0,  0 };

/* Un NetEvent (12 byte = 6 parole) nel layout ESATTO di protocol.py
 * (<BBBBBBhhBB): type, dir, speed, seq, mapGroup, mapNum, x, y, gender,
 * avatarState. GBA e PC sono entrambi little endian: le parole combaciano. */
static void SendNetEvent(u8 type, u8 dir)
{
    u16 w[6];
    w[0] = (u16)type | ((u16)dir << 8);
    w[1] = (u16)((u16)sSeq << 8);          /* speed 0: camminata */
    w[2] = (u16)sMapGroup | ((u16)sMapNum << 8);
    w[3] = (u16)sX;
    w[4] = (u16)sY;
    w[5] = (u16)sGender | ((u16)sAvatar << 8);
    if (SioSendFrame(SIO_T_EVENT, w, 6))
        sSeq++;
    /* TX pieno: l'evento si perde e txFull lo conta. Non si riprova: il
     * prossimo SYNC periodico corregge, e' il design di tutto il protocollo. */
}

/* --- il ritorno dal driver ----------------------------------------------- */
/* Il driver chiama questa quando un frame e' arrivato intero e col checksum
 * giusto: i PING tornano indietro (eco: il PC misura il giro completo), gli
 * EVENT sono l'amico che si muove - e il primo che arriva ci dice DOVE. */
static u32 sFramesIn;

int SioDeliverFrame(u8 type, const u16 *words, u8 count)
{
    sFramesIn++;

    if (type == SIO_T_PING)
    {
        SioSendFrame(SIO_T_PING, words, count);  /* eco: misura il round trip */
        return 1;
    }

    if (type == SIO_T_EVENT && count == 6)
    {
        u8 evType = (u8)(words[0] & 0xFF);
        u8 evDir  = (u8)(words[0] >> 8);

        sRemoteEvents++;

        if (evType == EVENT_LEAVE)
            return 1;                    /* l'amico se n'e' andato: noi restiamo */

        sRemX = (s16)words[3];
        sRemY = (s16)words[4];
        if (evType == EVENT_STEP || evType == EVENT_SYNC || evType == 3u)
            sRemDir = evDir;             /* nello STATO `dir` e' lo stato, non
                                          * una direzione: non si copia */

        if (!sAdopted)
        {
            /* L'adozione: la mappa, il sesso e lo stato avatar del primo
             * evento remoto diventano i nostri. Base a due tile dall'amico,
             * e da qui in poi si cammina. Niente PASSO adesso: all'ingresso
             * si mandano SYNC assoluti ripetuti, come fa il payload. */
            sMapGroup = (u8)(words[2] & 0xFF);
            sMapNum   = (u8)(words[2] >> 8);
            sGender   = (u8)(words[5] & 0xFF);
            sAvatar   = (u8)(words[5] >> 8);
            sX = (s16)(sRemX + 2);
            sY = sRemY;
            sAdopted = 1;
            sEntryLeft = ENTRY_SYNC_REPEATS;
            sEntryTimer = 0;             /* il primo SYNC parte subito */
        }
    }
    return 1;
}

/* --- schermo -------------------------------------------------------------
 * Niente libgba: un font 3x5 per le sole cifre esadecimali e poche lettere e'
 * tutto quello che serve. Su hardware non c'e' un log, e leggere numeri a
 * schermo e' l'unico modo di sapere cosa sta succedendo mentre succede.       */

/* Ogni glifo: 5 righe da 3 bit, dal basso verso l'alto nei bit 0..2. */
static const u8 sFont[16][5] = {
    {7,5,5,5,7}, {2,6,2,2,7}, {7,1,7,4,7}, {7,1,7,1,7},   /* 0 1 2 3 */
    {5,5,7,1,1}, {7,4,7,1,7}, {7,4,7,5,7}, {7,1,1,1,1},   /* 4 5 6 7 */
    {7,5,7,5,7}, {7,5,7,1,7}, {7,5,7,5,5}, {6,5,6,5,6},   /* 8 9 A B */
    {7,4,4,4,7}, {6,5,5,5,6}, {7,4,7,4,7}, {7,4,7,4,4},   /* C D E F */
};

static void PutPixel(int x, int y, u16 c)
{
    if (x >= 0 && x < SCREEN_W && y >= 0 && y < SCREEN_H)
        VRAM[y * SCREEN_W + x] = c;
}

static void DrawGlyph(int x, int y, u8 g, u16 c, int scale)
{
    int row, col, sx, sy;
    for (row = 0; row < 5; row++) {
        u8 bits = sFont[g][row];
        for (col = 0; col < 3; col++) {
            if (!(bits & (4 >> col)))
                continue;
            for (sy = 0; sy < scale; sy++)
                for (sx = 0; sx < scale; sx++)
                    PutPixel(x + col * scale + sx, y + row * scale + sy, c);
        }
    }
}

/* Stampa un u32 in esadecimale. L'esadecimale non e' pigrizia: con 16 glifi si
 * copre tutto, e i valori si confrontano a colpo d'occhio fra un frame e
 * l'altro senza che la larghezza cambi. */
static void DrawHex(int x, int y, u32 v, int digits, u16 c, int scale)
{
    int i;
    for (i = digits - 1; i >= 0; i--) {
        DrawGlyph(x, y, (u8)((v >> (i * 4)) & 0xF), c, scale);
        x += 4 * scale;
    }
}

static void DrawBar(int x, int y, int w, int h, u16 c)
{
    int i, j;
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++)
            PutPixel(x + i, y + j, c);
}

static void ClearScreen(u16 c)
{
    int i;
    for (i = 0; i < SCREEN_W * SCREEN_H; i++)
        VRAM[i] = c;
}

static void WaitVBlank(void)
{
    while (REG_VCOUNT >= SCREEN_H) { }
    while (REG_VCOUNT < SCREEN_H) { }
}

/* --- il test ------------------------------------------------------------- */

struct Row { const char *unused; u32 value; u16 colour; };

int main(void)
{
    /* Contatori alla rovescia invece di `frames % 60`: l'ARM7TDMI non ha la
     * divisione, e con -nostdlib non c'e' libgcc che la emuli. Su GBA e' anche
     * il modo idiomatico. (La divisione per costante nel disegno del quadrato
     * la fa il compilatore con moltiplicazioni: quella e' gratis.) */
    u32 toSecond = 60;
    u32 toRedraw = 30;
    u32 toStep = STEP_FRAMES;
    u32 toSync = SYNC_PERIOD;
    u32 lastWordsRx = 0, lastIrqs = 0;
    u32 wordsPerSec = 0, irqsPerSec = 0;
    u16 ping[4];
    u32 pingSeq = 0;

    REG_DISPCNT = MODE3_BITMAP;
    ClearScreen(COL_BG);

    SioInit();

    /* Il vettore a 0x03007FFC lo ha installato il crt0; SioInit ha alzato il
     * bit seriale in IE. Questo e' l'interruttore generale, e mancava: senza,
     * l'IRQ non parte mai e il driver e' sordo con i registri pieni. */
    REG_IME = 1;

    for (;;) {
        WaitVBlank();
        SioTick();

        /* Un ping al secondo: da' al canale qualcosa da fare anche quando il PC
         * tace, e permette di misurare il ritmo del master senza dipendere da
         * lui. */
        if (--toSecond == 0) {
            toSecond = 60;
            ping[0] = (u16)(pingSeq >> 16);
            ping[1] = (u16)pingSeq;
            ping[2] = 0x1234;
            ping[3] = 0xABCD;
            SioSendFrame(SIO_T_PING, ping, 4);
            pingSeq++;

            wordsPerSec = g_sio.wordsRx - lastWordsRx;
            lastWordsRx = g_sio.wordsRx;
            irqsPerSec = g_sio.irqs - lastIrqs;
            lastIrqs = g_sio.irqs;
        }

        /* Il giocatore. Parte solo dopo l'adozione (SioDeliverFrame): prima
         * i SYNC d'ingresso ripetuti - all'ingresso NIENTE PASSO, esattamente
         * come il payload - poi un passo da un tile ogni STEP_FRAMES e la
         * correzione assoluta ogni SYNC_PERIOD. */
        if (sAdopted) {
            if (sEntryLeft) {
                if (sEntryTimer) {
                    sEntryTimer--;
                } else {
                    sEntryLeft--;
                    sEntryTimer = ENTRY_SYNC_SPACING;
                    toSync = SYNC_PERIOD;
                    SendNetEvent(EVENT_SYNC, DIR_SOUTH);
                }
            } else {
                if (--toStep == 0) {
                    u8 dir = sSquareDirs[sSide];
                    toStep = STEP_FRAMES;
                    sX += sDirDeltaX[dir];
                    sY += sDirDeltaY[dir];
                    SendNetEvent(EVENT_STEP, dir);
                    if (++sStepInSide >= SQUARE_SIDE) {
                        sStepInSide = 0;
                        sSide = (u8)((sSide + 1) & 3);
                    }
                }
                if (--toSync == 0) {
                    toSync = SYNC_PERIOD;
                    SendNetEvent(EVENT_SYNC, sSquareDirs[sSide]);
                }
            }
        }

        /* Ridisegno due volte al secondo: piu' spesso non si legge, e ogni
         * ciclo speso a disegnare e' tempo tolto al canale. */
        if (--toRedraw != 0)
            continue;
        toRedraw = 30;

        ClearScreen(COL_BG);

        /* Riga di stato: verde se nell'ultimo secondo e' arrivato qualcosa,
         * rossa se il canale tace. E' l'informazione che si legge da lontano,
         * mentre si ha le mani sul cavo. Il tratto in fondo a destra e'
         * l'ADOZIONE: si accende quando il primo evento remoto ci ha detto su
         * quale mappa camminare. */
        DrawBar(0, 0, SCREEN_W, 6, wordsPerSec ? COL_GOOD : COL_BAD);
        DrawBar(SCREEN_W - 24, 0, 24, 6, sAdopted ? COL_ADOPT : COL_LABEL);

        {
            int y = 14;
            const int step = 13;
            const int xv = 100;

            DrawHex(8, y, 0x0, 1, COL_LABEL, 2);      /* 0: parole/s         */
            DrawHex(xv, y, wordsPerSec, 4, COL_VALUE, 2); y += step;

            DrawHex(8, y, 0x1, 1, COL_LABEL, 2);      /* 1: irq/s            */
            DrawHex(xv, y, irqsPerSec, 4, COL_VALUE, 2); y += step;

            DrawHex(8, y, 0x2, 1, COL_LABEL, 2);      /* 2: frame rx ok      */
            DrawHex(xv, y, g_sio.framesRx, 8, COL_VALUE, 2); y += step;

            DrawHex(8, y, 0x3, 1, COL_LABEL, 2);      /* 3: errori frame     */
            DrawHex(xv, y, g_sio.frameErr, 8,
                    g_sio.frameErr ? COL_BAD : COL_VALUE, 2); y += step;

            DrawHex(8, y, 0x4, 1, COL_LABEL, 2);      /* 4: resync           */
            DrawHex(xv, y, g_sio.resync, 8, COL_VALUE, 2); y += step;

            /* 5: zeroWords. NON e' piu' rosso: dopo la F-2 gli zeri nei
             * NetEvent (coordinate, direzioni) sono dati legittimi che il
             * canale trasporta. Deve SALIRE mentre si cammina - se restasse
             * a zero col nodo adottato, quello sarebbe il difetto. */
            DrawHex(8, y, 0x5, 1, COL_LABEL, 2);
            DrawHex(xv, y, g_sio.zeroWords, 8, COL_VALUE, 2); y += step;

            DrawHex(8, y, 0x6, 1, COL_LABEL, 2);      /* 6: errori SIOCNT    */
            DrawHex(xv, y, g_sio.errFlag, 8,
                    g_sio.errFlag ? COL_BAD : COL_VALUE, 2); y += step;

            DrawHex(8, y, 0x7, 1, COL_LABEL, 2);      /* 7: frame tx         */
            DrawHex(xv, y, g_sio.framesTx, 8, COL_VALUE, 2); y += step;

            DrawHex(8, y, 0x8, 1, COL_LABEL, 2);      /* 8: eventi remoti    */
            DrawHex(xv, y, sRemoteEvents, 8, COL_VALUE, 2); y += step;

            /* 9: il remoto - x, y, direzione. Sono i numeri che devono
             * cambiare A OGNI PASSO del giocatore mGBA: e' il criterio 2
             * della prova ufficiale. */
            DrawHex(8, y, 0x9, 1, COL_LABEL, 2);
            DrawHex(xv, y, (u16)sRemX, 4, COL_VALUE, 2);
            DrawHex(xv + 44, y, (u16)sRemY, 4, COL_VALUE, 2);
            DrawHex(xv + 88, y, sRemDir, 1, COL_VALUE, 2); y += step;

            /* A: noi - x, y, mappa adottata (gruppo.numero). */
            DrawHex(8, y, 0xA, 1, COL_LABEL, 2);
            DrawHex(xv, y, (u16)sX, 4, COL_ADOPT, 2);
            DrawHex(xv + 44, y, (u16)sY, 4, COL_ADOPT, 2);
            DrawHex(xv + 88, y, ((u32)sMapGroup << 8) | sMapNum, 4,
                    COL_ADOPT, 2);
        }
    }
}
