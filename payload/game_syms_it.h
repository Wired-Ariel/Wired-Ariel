/* GENERATO DA tools/port_syms.py - non modificare a mano.
 * ROM italiana: gamecode BPEI, sha1 1692db322400c3141c5de2db38469913ceb1f4d4
 * Riferimento USA: sha1 f3ae088181bf583e55daf962a92bb46f4f1d07b7
 *
 * Ogni indirizzo qui dentro e' stato RITROVATO nella ROM italiana, non
 * dedotto: vedi build/port_report.md per il metodo simbolo per simbolo.
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

/* SpawnSpecialObjectEventParameterized @ 0x0808DC58 (Thumb) */
#define SpawnSpecialObjectEventParameterized ((u8 (*)(u8 graphicsId, u8 movementBehavior, u8 localId, s16 x, s16 y, u8 elevation))(0x0808DC59))

/* ObjectEventSetHeldMovement @ 0x080931D4 (Thumb) */
#define ObjectEventSetHeldMovement ((bool8 (*)(void *objectEvent, u8 movementActionId))(0x080931D5))

/* ObjectEventClearHeldMovement @ 0x08093250 (Thumb) */
#define ObjectEventClearHeldMovement ((void (*)(void *objectEvent))(0x08093251))

/* ObjectEventIsHeldMovementActive @ 0x080931B8 (Thumb) */
#define ObjectEventIsHeldMovementActive ((bool8 (*)(void *objectEvent))(0x080931B9))

/* ObjectEventCheckHeldMovementStatus @ 0x08093284 (Thumb) */
#define ObjectEventCheckHeldMovementStatus ((u8 (*)(void *objectEvent))(0x08093285))

/* ObjectEventClearHeldMovementIfActive @ 0x08093238 (Thumb) */
#define ObjectEventClearHeldMovementIfActive ((void (*)(void *objectEvent))(0x08093239))

/* MoveObjectEventToMapCoords @ 0x0808EB1C (Thumb) */
#define MoveObjectEventToMapCoords ((void (*)(void *objectEvent, s16 x, s16 y))(0x0808EB1D))

/* GetRivalAvatarGraphicsIdByStateIdAndGender @ 0x0808BD50 (Thumb) */
#define GetRivalAvatarGraphicsIdByStateIdAndGender ((u8 (*)(u8 state, u8 gender))(0x0808BD51))

/* GetObjectEventGraphicsInfo @ 0x0808E6A8 (Thumb) */
#define GetObjectEventGraphicsInfo ((u32 (*)(u8 graphicsId))(0x0808E6A9))

/* ObjectEventSetGraphicsId @ 0x0808E40C (Thumb) */
#define ObjectEventSetGraphicsId ((void (*)(void *objectEvent, u8 graphicsId))(0x0808E40D))

/* FieldEffectStart @ 0x080B5B2C (Thumb) */
#define FieldEffectStart ((u8 (*)(u8 fldEff))(0x080B5B2D))

/* SetSurfBlob_BobState @ 0x08155140 (Thumb) */
#define SetSurfBlob_BobState ((void (*)(u8 spriteId, u8 state))(0x08155141))

/* DestroySprite @ 0x080070E8 (Thumb) */
#define DestroySprite ((void (*)(void *sprite))(0x080070E9))

/* ObjectEventTurn @ 0x0808E558 (Thumb) */
#define ObjectEventTurn ((void (*)(void *objectEvent, u8 direction))(0x0808E559))

/* GetMapHeaderFromConnection @ 0x08087D58 (Thumb) */
#define GetMapHeaderFromConnection ((u32 (*)(u32 connection))(0x08087D59))

/* GetMapConnectionAtPos @ 0x08088AA0 (Thumb) */
#define GetMapConnectionAtPos ((u32 (*)(s16 x, s16 y))(0x08088AA1))

/* MapGridGetMetatileBehaviorAt @ 0x080882D0 (Thumb) */
#define MapGridGetMetatileBehaviorAt ((u32 (*)(s32 x, s32 y))(0x080882D1))

/* FieldAnimateDoorOpen @ 0x0808A8F8 (Thumb) */
#define FieldAnimateDoorOpen ((s8 (*)(u32 x, u32 y))(0x0808A8F9))

/* FieldAnimateDoorClose @ 0x0808A8C0 (Thumb) */
#define FieldAnimateDoorClose ((s8 (*)(u32 x, u32 y))(0x0808A8C1))

/* FieldIsDoorAnimationRunning @ 0x0808A930 (Thumb) */
#define FieldIsDoorAnimationRunning ((bool8 (*)(void))(0x0808A931))

/* GetDoorSoundEffect @ 0x0808A944 (Thumb) */
#define GetDoorSoundEffect ((u32 (*)(u32 x, u32 y))(0x0808A945))

/* PlaySE @ 0x080A37B8 (Thumb) */
#define PlaySE ((void (*)(u16 songNum))(0x080A37B9))

/* CreateSprite @ 0x08006DF4 (Thumb) */
#define CreateSprite ((u8 (*)(const void *template, s16 x, s16 y, u8 subpriority))(0x08006DF5))

/* LoadSpriteSheet @ 0x080084F8 (Thumb) */
#define LoadSpriteSheet ((u16 (*)(const void *sheet))(0x080084F9))

/* LoadCompressedSpriteSheet @ 0x08034534 (Thumb) */
#define LoadCompressedSpriteSheet ((u16 (*)(const void *sheet))(0x08034535))

/* GetSpriteTileStartByTag @ 0x08008620 (Thumb) */
#define GetSpriteTileStartByTag ((u16 (*)(u16 tag))(0x08008621))

/* LoadSpritePalette @ 0x08008744 (Thumb) */
#define LoadSpritePalette ((u8 (*)(const void *palette))(0x08008745))

/* IndexOfSpritePaletteTag @ 0x08008804 (Thumb) */
#define IndexOfSpritePaletteTag ((u8 (*)(u16 tag))(0x08008805))

/* ArePlayerFieldControlsLocked @ 0x08098E80 (Thumb) */
#define ArePlayerFieldControlsLocked ((bool8 (*)(void))(0x08098E81))

/* FuncIsActiveTask @ 0x080A91F8 (Thumb) */
#define FuncIsActiveTask ((bool8 (*)(void *func))(0x080A91F9))

/* TrainerCard_GenerateCardForLinkPlayer @ 0x080C2E68 (Thumb) */
#define TrainerCard_GenerateCardForLinkPlayer ((void (*)(void *trainerCard))(0x080C2E69))

/* ShowTrainerCardInLink @ 0x080C4AC8 (Thumb) */
#define ShowTrainerCardInLink ((void (*)(u8 cardId, void *callback))(0x080C4AC9))

/* FadeScreen @ 0x080ABCE4 (Thumb) */
#define FadeScreen ((void (*)(u8 mode, s8 delay))(0x080ABCE5))

/* CleanupOverworldWindowsAndTilemaps @ 0x08085D48 (Thumb) */
#define CleanupOverworldWindowsAndTilemaps ((void (*)(void))(0x08085D49))

/* LockPlayerFieldControls @ 0x08098E68 (Thumb) */
#define LockPlayerFieldControls ((void (*)(void))(0x08098E69))

/* UnlockPlayerFieldControls @ 0x08098E74 (Thumb) */
#define UnlockPlayerFieldControls ((void (*)(void))(0x08098E75))

/* --- dati del gioco --- */

#define ADDR_gObjectEvents (0x02037350u)
#define ADDR_gSaveBlock1Ptr (0x03005D8Cu)
#define ADDR_gSaveBlock2Ptr (0x03005D90u)
#define ADDR_gMain (0x030022C0u)
#define ADDR_gPlayerAvatar (0x02037590u)
#define ADDR_gSprites (0x02020630u)
#define ADDR_gFieldEffectArguments (0x02038C08u)
#define ADDR_gMapHeader (0x02037318u)
#define ADDR_CB2_Overworld (0x08085E70u)
#define ADDR_CB2_OpenPokedex (0x080BB55Cu)
#define ADDR_CB2_PartyMenuFromStartMenu (0x081B7A3Cu)
#define ADDR_CB2_BagMenuFromStartMenu (0x081AA694u)
#define ADDR_CB2_BagMenuRun (0x081AA854u)
#define ADDR_CB2_InitPokeNav (0x081C6D58u)
#define ADDR_Task_ShowStartMenu (0x0809FA48u)
#define ADDR_gTrainerCards (0x02039B58u)
#define ADDR_gLinkPlayers (0x020229E8u)
#define ADDR_CB2_ReturnToFieldWithOpenMenu (0x080861A8u)
#define ADDR_gLinkCallback (0x03003140u)
#define ADDR_gReceivedRemoteLinkPlayers (0x03003124u)
#define ADDR_gLinkVSyncDisabled (0x03002748u)
#define ADDR_gLink (0x03003170u)
#define ADDR_gLinkPlayerObjectEvents (0x02032308u)
#define ADDR_gFieldCamera (0x03005DD0u)

#endif /* GAME_SYMS_H */
