/* boot_syms_usa.h - lo stub per la cartuccia INGLESE di Smeraldo (USA/Europa, BPEE).
 *
 * REGOLA DEL PROGETTO: un indirizzo che non viene da un .map di una build reale
 * o da una misura sulla ROM non si usa. Questi vengono da entrambi.
 *
 * PROVENIENZA (2026-09-24)
 * ------------------------
 * La ROM inglese e' quella della decomp: repo-studio/pokeemerald/pokeemerald.gba,
 * sha1 f3ae088181bf583e55daf962a92bb46f4f1d07b7, gamecode BPEE (identica alla
 * cartuccia USA/Europa: di Emerald in inglese esiste una sola revisione).
 *
 * Il blocco di boot: gli indirizzi usati dallo stub e da handoff.S sono GLI STESSI
 * della ROM italiana, verificati il 2026-09-24:
 *   - dal .map della decomp: AgbMain 0x080003A4, InitKeys 0x080005BC,
 *     InitIntrHandlers 0x08000684, InitGpuRegManager 0x08000FE4, IntrMain 0x08000248;
 *   - dalla ROM: le word di literal pool 0x080006D0 = 0x08000248 e
 *     0x080006D4 = 0x03002750 (IntrMain_Buffer), come nella italiana;
 *   - confronto byte per byte dei primi 4 KB IT/USA: differiscono solo
 *     l'intestazione, i BL verso funzioni fuori dal blocco (spostate) e la word
 *     di gIntrTableTemplate - nessuno dei punti che lo stub esegue o legge.
 *     Il rientro 0x080003CE resta l'inizio di un'istruzione in entrambe.
 *
 * Cambiano solo il gamecode e le quattro parole-firma del corpo della ROM, che
 * nella versione inglese stanno ad altri indirizzi (dal .map della decomp,
 * valori letti dalla ROM: sono le stesse istruzioni della italiana). Agli
 * indirizzi ITALIANI la ROM inglese contiene altro: la firma discrimina davvero,
 * quindi uno stub sbagliato da' BLU invece di avviare un payload muto.
 */

#ifndef MBSTUB_BOOT_SYMS_USA_H
#define MBSTUB_BOOT_SYMS_USA_H

#include "boot_syms_it.h"   /* tutto il blocco di boot e' identico */

#undef  GAMECODE_EXPECTED
#define GAMECODE_BPEE            0x45455042  /* "BPEE" little-endian         */
#define GAMECODE_EXPECTED        GAMECODE_BPEE

#undef  ADDR_CHK_CB2_OVERWORLD
#undef  ADDR_CHK_SPAWN_OBJEVENT
#undef  ADDR_CHK_SET_HELD_MOVE
#undef  ADDR_CHK_CB2_BAGMENU
#define ADDR_CHK_CB2_OVERWORLD   0x08085E5C  /* CB2_Overworld (IT 0x08085E70) */
#define ADDR_CHK_SPAWN_OBJEVENT  0x0808DC44  /* SpawnSpecialObjectEventParameterized */
#define ADDR_CHK_SET_HELD_MOVE   0x080931C0  /* ObjectEventSetHeldMovement    */
#define ADDR_CHK_CB2_BAGMENU     0x081AAD5C  /* CB2_BagMenuRun (IT 0x081AA854) */
/* I VALORI restano quelli di boot_syms_it.h: 0x4809B510, 0x4646B570,
 * 0x1C04B570, 0xF6FEB500 - letti dalla ROM inglese agli indirizzi qui sopra. */

#endif /* MBSTUB_BOOT_SYMS_USA_H */
