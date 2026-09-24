/* types.h - i tipi interi, e nient'altro.
 *
 * Estratti da game_syms.h perche' `sio.c` ha bisogno SOLO di questi, mentre
 * game_syms.h porta con se' i 42 indirizzi del gioco. Il driver SIO deve poter
 * girare anche dove quegli indirizzi non esistono - nel banco di prova
 * multiboot, che gira AL POSTO di Smeraldo e non ha nessun gioco da chiamare.
 *
 * Cosi' lo stesso file misura il canale su hardware e poi entra nel payload
 * senza una riga di differenza: e' l'unico modo perche' la misura valga davvero
 * per il payload invece che per un prototipo che gli somiglia.
 */

#ifndef PAYLOAD_TYPES_H
#define PAYLOAD_TYPES_H

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;
typedef signed char    s8;
typedef signed short   s16;
typedef signed int     s32;
typedef unsigned char  bool8;

#endif /* PAYLOAD_TYPES_H */
