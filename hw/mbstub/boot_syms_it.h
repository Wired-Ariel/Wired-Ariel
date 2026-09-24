/* boot_syms_it.h - indirizzi del blocco di boot di Smeraldo.
 *
 * REGOLA DEL PROGETTO: un indirizzo che non viene da un .map di una build reale
 * o da una misura sulla ROM non si usa. Questi vengono da entrambi.
 *
 * PROVENIENZA
 * -----------
 * Presi dal .map della build matching di pokeemerald (repo-studio/pokeemerald/
 * pokeemerald.map) e poi VERIFICATI sulla ROM italiana di Lain
 * (sha1 1692db322400c3141c5de2db38469913ceb1f4d4, gamecode BPEI) con objdump,
 * istruzione per istruzione.
 *
 * Esito della verifica (2026-08-02, tredicesima sessione): il blocco di boot sta
 * nei primi 4 KB della ROM, non contiene testo ne' dati di lingua, ed e'
 * BYTE-IDENTICO fra BPEE e BPEI. Non e' servito il pattern matching di
 * port_syms.py: gli indirizzi sono gli stessi.
 *
 * Le uniche differenze nel blocco sono i BL verso funzioni FUORI dal blocco
 * (RegisterRamReset 0x082E70A8 -> 0x082E8034, m4aSoundInit 0x082E0070 ->
 * 0x082E0FFC) e una word di literal pool (gIntrTableTemplate 0x082E9548 ->
 * 0x082EA4D4). Nessuna delle tre serve allo stub: le funzioni le chiamiamo per
 * indirizzo dentro il blocco, e RegisterRamReset lo chiamiamo noi via SWI.
 */

#ifndef MBSTUB_BOOT_SYMS_IT_H
#define MBSTUB_BOOT_SYMS_IT_H

/* --- funzioni del blocco di boot (Thumb: sommare 1 per il bx) -------------- */
#define ADDR_INIT                0x08000204  /* crt0 del gioco               */
#define ADDR_INTR_MAIN           0x08000248  /* dispatcher IRQ, in ROM       */
#define ADDR_AGB_MAIN            0x080003A4
#define ADDR_INIT_KEYS           0x080005BC
#define ADDR_INIT_INTR_HANDLERS  0x08000684
#define ADDR_INIT_GPU_REG_MGR    0x08000FE4

/* --- i due punti di rientro dentro AgbMain --------------------------------
 *
 *   80003a4  push {r4,r5,r6,r7,lr} / mov r7,r8 / push {r7}
 *   80003aa  movs r0,#255              <- RESET_ALL
 *   80003ac  bl   RegisterRamReset     <- L'AZZERAMENTO DELLA EWRAM
 *   80003b0  ...  BG_PLTT = RGB_WHITE  <- ADDR_AGBMAIN_AFTER_RESET
 *   80003ba  bl   InitGpuRegManager
 *   80003be  ...  REG_WAITCNT = 0x4014
 *   80003c6  bl   InitKeys
 *   80003ca  bl   InitIntrHandlers
 *   80003ce  bl   m4aSoundInit         <- ADDR_AGBMAIN_AFTER_INTR: rientro nostro
 *
 * Rientrare a 0x080003CE e non a 0x080003B0 e' tutta la differenza: a quel
 * punto InitIntrHandlers ha gia' scritto INTR_VECTOR, quindi l'hook si installa
 * concatenando invece di farsi sovrascrivere un attimo dopo.
 */
#define ADDR_AGBMAIN_AFTER_RESET 0x080003B0  /* = AgbMain + 0x0C, non usato   */
#define ADDR_AGBMAIN_AFTER_INTR  0x080003CE  /* = AgbMain + 0x2A              */

/* Byte che AgbMain avrebbe messo sullo stack col prologo saltato:
 * push {r4,r5,r6,r7,lr} = 20 + push {r7} = 4. AgbMain non ritorna mai (finisce
 * in for(;;)), quindi non verranno mai ripescati, ma teniamo sp identico. */
#define AGBMAIN_PROLOGUE_BYTES   24

/* --- dati in IWRAM, dal literal pool di InitIntrHandlers ------------------ */
#define ADDR_INTR_MAIN_BUFFER    0x03002750  /* pool a 0x080006D4            */
#define ADDR_GINTRTABLE          0x03002710  /* pool a 0x080006DC            */

/* --- costanti hardware ---------------------------------------------------- */
#define INTR_VECTOR              0x03007FFC
#define REG_IME_ADDR             0x04000208
#define REG_WAITCNT_ADDR         0x04000204
#define REG_WAITCNT_VALUE        0x4014      /* pool a 0x08000470            */
#define BG_PLTT_ADDR             0x05000000
#define RGB_WHITE_VALUE          0x7FFF      /* pool a 0x08000468            */

/* Stack come li imposta il crt0 del gioco (src/crt0.s:28-29), con
 * IWRAM_END = 0x03008000. Ricalcarli conta: il RegisterRamReset del BIOS
 * azzera la IWRAM tranne gli ultimi 0x200 byte, e sp_sys/sp_irq cadono
 * dentro quella fascia protetta. */
#define GAME_SP_SYS              0x03007E40  /* IWRAM_END - 0x1C0            */
#define GAME_SP_IRQ              0x03007FA0  /* IWRAM_END - 0x60             */

/* --- la nostra coda di EWRAM ---------------------------------------------- */
/* Abbassato da 0x0203D000 il 2026-08-02 (blocco W''), per far entrare il driver
 * SIO. Il confine e' un FATTO DEL LINKER, non una misura: la sezione `ewram` di
 * Smeraldo finisce a 0x0203CF64 e nel .map non esiste un simbolo sopra.
 * Verificato in modo esaustivo anche sulla ROM italiana da tools/ewram_bound.py.
 * I 28 byte fino a 0x0203CF80 sono guardia. Deve restare uguale al PAYLOAD_BASE
 * di overworld-link\build.ps1: build.ps1 di mbstub lo ricontrolla. */
#define PAYLOAD_BASE             0x0203CF80
#define EWRAM_END                0x02040000
/* handoff.S vive qui: dentro la coda protetta, sopra il BINARIO del payload.
 *
 * Con PAYLOAD_BASE a 0x0203CF80 la regione riservata del payload arriva a
 * ~0x0203FFC8, quindi questi 512 byte cadono DENTRO lo stack privato del
 * payload. E' voluto e innocuo, ma va capito invece che scoperto:
 *
 *   - il BINARIO del payload finisce sotto HANDOFF_BASE (build.ps1 lo verifica:
 *     e' l'unico vincolo che conta al momento della copia);
 *   - lo stack del payload cresce verso il basso da 0x0203FFC8 e arriva qui
 *     dopo ~456 byte - ma solo DOPO che handoff ha saltato dentro AgbMain,
 *     quindi sovrascrive codice gia' eseguito;
 *   - conseguenza per la misura W''-4: sull'hardware il watermark dello stack
 *     e' sporcato da questi 512 byte. In emulatore no (li' il caricatore e'
 *     l'harness Lua e handoff non esiste), ed e' li' che la misura si fa. */
#define HANDOFF_BASE             0x0203FE00

/* Offset dentro il payload, dal contratto di payload/hook.S:
 *   +0x00  b hook_entry     <- INTR_VECTOR punta qui
 *   +0x04  g_origHandler    <- lo riempie chi installa
 *   +0x08  g_magic = 'HOOK' */
#define PAYLOAD_OFF_ORIG         0x04
#define PAYLOAD_OFF_MAGIC        0x08
#define PAYLOAD_MAGIC            0x4B4F4F48  /* 'HOOK' little-endian         */

/* --- riconoscimento della cartuccia ---------------------------------------
 * Non un checksum dell'intera ROM (troppo lento e inutile): si controllano il
 * gamecode e le due word di literal pool su cui lo stub si appoggia davvero.
 * Se quelle due non tornano, il blocco di boot non e' quello profilato e
 * proseguire sarebbe indovinare. */
#define ADDR_GAMECODE            0x080000AC
#define GAMECODE_BPEI            0x49455042  /* "BPEI" little-endian         */
#define GAMECODE_EXPECTED        GAMECODE_BPEI  /* boot_syms_usa.h lo ridefinisce */
#define ADDR_POOL_INTRMAIN       0x080006D0  /* deve contenere ADDR_INTR_MAIN */
#define ADDR_POOL_INTRBUF        0x080006D4  /* deve contenere ADDR_INTR_MAIN_BUFFER */

/* --- il corpo della ROM e' quello per cui il payload e' stato costruito? ---
 *
 * Il caso reale che ha imposto questo controllo (2026-08-02, prova con
 * l'amico): boot block identico, gamecode identico, multiboot e gioco
 * perfetti - e payload muto per sempre, perche' il suo cancello di TX
 * confronta gMain.callback2 con l'INDIRIZZO ASSOLUTO di CB2_Overworld. Se il
 * corpo della ROM e' diverso (altra revisione, riproduzione rimaneggiata),
 * i tre controlli sopra passano lo stesso: stanno tutti nei primi 4 KB.
 *
 * Queste quattro parole sono le prime istruzioni di quattro funzioni che il
 * payload usa davvero, sparse su 1,2 MB di ROM. Lette dalla ROM profilata
 * (sha1 1692db322400c3141c5de2db38469913ceb1f4d4) il 2026-08-02. Se una non
 * torna, il payload DEVE restare fuori: il gioco funzionerebbe, il payload no.
 */
#define ADDR_CHK_CB2_OVERWORLD   0x08085E70  /* il cancello del TX            */
#define VAL_CHK_CB2_OVERWORLD    0x4809B510
#define ADDR_CHK_SPAWN_OBJEVENT  0x0808DC58  /* SpawnSpecialObjectEventParameterized */
#define VAL_CHK_SPAWN_OBJEVENT   0x4646B570
#define ADDR_CHK_SET_HELD_MOVE   0x080931D4  /* ObjectEventSetHeldMovement    */
#define VAL_CHK_SET_HELD_MOVE    0x1C04B570
#define ADDR_CHK_CB2_BAGMENU     0x081AA854  /* CB2_BagMenuRun, a 1,7 MB      */
#define VAL_CHK_CB2_BAGMENU      0xF6FEB500

#endif /* MBSTUB_BOOT_SYMS_IT_H */
