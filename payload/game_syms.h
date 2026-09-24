/* GENERATO DA tools/gen_syms.py - non modificare a mano.
 * Fonte: pokeemerald.map (build matching, Smeraldo USA BPEE).
 *
 * ATTENZIONE: questi indirizzi valgono per la ROM USA. Su cartuccia italiana
 * sono diversi e vanno ritrovati per pattern matching - vedi il punto 0 delle
 * domande aperte in NOTES.md.
 */

#ifndef GAME_SYMS_H
#define GAME_SYMS_H

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;
typedef signed char    s8;
typedef signed short   s16;
typedef signed int     s32;
typedef unsigned char  bool8;

/* --- funzioni del gioco (bit Thumb gia' impostato) --- */

/* SpawnSpecialObjectEventParameterized @ 0x0808DC44 (Thumb) */
#define SpawnSpecialObjectEventParameterized ((u8 (*)(u8 graphicsId, u8 movementBehavior, u8 localId, s16 x, s16 y, u8 elevation))(0x0808DC45))

/* ObjectEventSetHeldMovement @ 0x080931C0 (Thumb) */
#define ObjectEventSetHeldMovement ((bool8 (*)(void *objectEvent, u8 movementActionId))(0x080931C1))

/* ObjectEventClearHeldMovement @ 0x0809323C (Thumb) */
#define ObjectEventClearHeldMovement ((void (*)(void *objectEvent))(0x0809323D))

/* ObjectEventIsHeldMovementActive @ 0x080931A4 (Thumb) */
#define ObjectEventIsHeldMovementActive ((bool8 (*)(void *objectEvent))(0x080931A5))

/* ObjectEventCheckHeldMovementStatus @ 0x08093270 (Thumb) */
#define ObjectEventCheckHeldMovementStatus ((u8 (*)(void *objectEvent))(0x08093271))

/* ObjectEventClearHeldMovementIfActive @ 0x08093224 (Thumb) */
#define ObjectEventClearHeldMovementIfActive ((void (*)(void *objectEvent))(0x08093225))

/* MoveObjectEventToMapCoords @ 0x0808EB08 (Thumb) */
#define MoveObjectEventToMapCoords ((void (*)(void *objectEvent, s16 x, s16 y))(0x0808EB09))

/* GetRivalAvatarGraphicsIdByStateIdAndGender @ 0x0808BD3C (Thumb) */
#define GetRivalAvatarGraphicsIdByStateIdAndGender ((u8 (*)(u8 state, u8 gender))(0x0808BD3D))

/* GetObjectEventGraphicsInfo @ 0x0808E694 (Thumb) */
#define GetObjectEventGraphicsInfo ((u32 (*)(u8 graphicsId))(0x0808E695))

/* ObjectEventSetGraphicsId @ 0x0808E3F8 (Thumb) */
#define ObjectEventSetGraphicsId ((void (*)(void *objectEvent, u8 graphicsId))(0x0808E3F9))

/* FieldEffectStart @ 0x080B5B18 (Thumb) */
#define FieldEffectStart ((u8 (*)(u8 fldEff))(0x080B5B19))

/* SetSurfBlob_BobState @ 0x081555AC (Thumb) */
#define SetSurfBlob_BobState ((void (*)(u8 spriteId, u8 state))(0x081555AD))

/* DestroySprite @ 0x080070E8 (Thumb) */
#define DestroySprite ((void (*)(void *sprite))(0x080070E9))

/* ObjectEventTurn @ 0x0808E544 (Thumb) */
#define ObjectEventTurn ((void (*)(void *objectEvent, u8 direction))(0x0808E545))

/* GetMapHeaderFromConnection @ 0x08087D44 (Thumb) */
#define GetMapHeaderFromConnection ((u32 (*)(u32 connection))(0x08087D45))

/* GetMapConnectionAtPos @ 0x08088A8C (Thumb) */
#define GetMapConnectionAtPos ((u32 (*)(s16 x, s16 y))(0x08088A8D))

/* MapGridGetMetatileBehaviorAt @ 0x080882BC (Thumb) */
#define MapGridGetMetatileBehaviorAt ((u32 (*)(s32 x, s32 y))(0x080882BD))

/* FieldAnimateDoorOpen @ 0x0808A8E4 (Thumb) */
#define FieldAnimateDoorOpen ((s8 (*)(u32 x, u32 y))(0x0808A8E5))

/* FieldAnimateDoorClose @ 0x0808A8AC (Thumb) */
#define FieldAnimateDoorClose ((s8 (*)(u32 x, u32 y))(0x0808A8AD))

/* FieldIsDoorAnimationRunning @ 0x0808A91C (Thumb) */
#define FieldIsDoorAnimationRunning ((bool8 (*)(void))(0x0808A91D))

/* GetDoorSoundEffect @ 0x0808A930 (Thumb) */
#define GetDoorSoundEffect ((u32 (*)(u32 x, u32 y))(0x0808A931))

/* PlaySE @ 0x080A37A4 (Thumb) */
#define PlaySE ((void (*)(u16 songNum))(0x080A37A5))

/* CreateSprite @ 0x08006DF4 (Thumb) */
#define CreateSprite ((u8 (*)(const void *template, s16 x, s16 y, u8 subpriority))(0x08006DF5))

/* LoadSpriteSheet @ 0x080084F8 (Thumb) */
#define LoadSpriteSheet ((u16 (*)(const void *sheet))(0x080084F9))

/* LoadCompressedSpriteSheet @ 0x08034530 (Thumb) */
#define LoadCompressedSpriteSheet ((u16 (*)(const void *sheet))(0x08034531))

/* GetSpriteTileStartByTag @ 0x08008620 (Thumb) */
#define GetSpriteTileStartByTag ((u16 (*)(u16 tag))(0x08008621))

/* LoadSpritePalette @ 0x08008744 (Thumb) */
#define LoadSpritePalette ((u8 (*)(const void *palette))(0x08008745))

/* IndexOfSpritePaletteTag @ 0x08008804 (Thumb) */
#define IndexOfSpritePaletteTag ((u8 (*)(u16 tag))(0x08008805))

/* ArePlayerFieldControlsLocked @ 0x08098E6C (Thumb) */
#define ArePlayerFieldControlsLocked ((bool8 (*)(void))(0x08098E6D))

/* FuncIsActiveTask @ 0x080A91E4 (Thumb) */
#define FuncIsActiveTask ((bool8 (*)(void *func))(0x080A91E5))

/* TrainerCard_GenerateCardForLinkPlayer @ 0x080C30A4 (Thumb) */
#define TrainerCard_GenerateCardForLinkPlayer ((void (*)(void *trainerCard))(0x080C30A5))

/* ShowTrainerCardInLink @ 0x080C4E74 (Thumb) */
#define ShowTrainerCardInLink ((void (*)(u8 cardId, void *callback))(0x080C4E75))

/* FadeScreen @ 0x080ABCD0 (Thumb) */
#define FadeScreen ((void (*)(u8 mode, s8 delay))(0x080ABCD1))

/* CleanupOverworldWindowsAndTilemaps @ 0x08085D34 (Thumb) */
#define CleanupOverworldWindowsAndTilemaps ((void (*)(void))(0x08085D35))

/* LockPlayerFieldControls @ 0x08098E54 (Thumb) */
#define LockPlayerFieldControls ((void (*)(void))(0x08098E55))

/* UnlockPlayerFieldControls @ 0x08098E60 (Thumb) */
#define UnlockPlayerFieldControls ((void (*)(void))(0x08098E61))

/* --- dati del gioco --- */

#define ADDR_gObjectEvents (0x02037350u)
#define ADDR_gSaveBlock1Ptr (0x03005D8Cu)
#define ADDR_gSaveBlock2Ptr (0x03005D90u)
#define ADDR_gMain (0x030022C0u)
#define ADDR_gPlayerAvatar (0x02037590u)
#define ADDR_gSprites (0x02020630u)
#define ADDR_gFieldEffectArguments (0x02038C08u)
#define ADDR_gMapHeader (0x02037318u)
#define ADDR_CB2_Overworld (0x08085E5Cu)
#define ADDR_CB2_OpenPokedex (0x080BB534u)
#define ADDR_CB2_PartyMenuFromStartMenu (0x081B7F34u)
#define ADDR_CB2_BagMenuFromStartMenu (0x081AAB9Cu)
#define ADDR_CB2_BagMenuRun (0x081AAD5Cu)
#define ADDR_CB2_InitPokeNav (0x081C7250u)
#define ADDR_Task_ShowStartMenu (0x0809FA34u)
#define ADDR_gTrainerCards (0x02039B58u)
#define ADDR_gLinkPlayers (0x020229E8u)
#define ADDR_CB2_ReturnToFieldWithOpenMenu (0x08086194u)
#define ADDR_gLinkCallback (0x03003140u)
#define ADDR_gReceivedRemoteLinkPlayers (0x03003124u)
#define ADDR_gLinkVSyncDisabled (0x03002748u)
#define ADDR_gLink (0x03003170u)
#define ADDR_gLinkPlayerObjectEvents (0x02032308u)
#define ADDR_gFieldCamera (0x03005DD0u)

#endif /* GAME_SYMS_H */
