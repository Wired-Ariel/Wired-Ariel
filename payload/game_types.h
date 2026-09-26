/* =============================================================================
 * game_types.h - strutture del gioco viste dal payload
 * =============================================================================
 *
 * Rispecchiano le definizioni della decomp, ma con accesso per offset espliciti
 * invece che con i bitfield originali. Motivo: il gioco e' compilato con agbcc e
 * il payload con gcc 14, e non voglio dipendere dal fatto che i due mettano i
 * bitfield negli stessi bit. Gli offset in byte invece sono un fatto.
 *
 * Riferimenti (build matching Smeraldo USA):
 *   struct ObjectEvent  -> include/global.fieldmap.h:194, 0x24 byte
 *   struct PlayerAvatar -> include/global.fieldmap.h:342
 *   struct SaveBlock1   -> include/global.h (pos 0x00, location 0x04)
 *   struct WarpData     -> mapGroup, mapNum, warpId, x, y
 *   struct Main         -> include/main.h:8 (callback1 0x00, callback2 0x04)
 */

#ifndef GAME_TYPES_H
#define GAME_TYPES_H

#include "game_syms.h"

/* --- costanti della decomp ------------------------------------------------ */

#define OBJECT_EVENTS_COUNT                 16
#define OBJECT_EVENT_SIZE                   0x24

#define MAP_OFFSET                          7      /* include/fieldmap.h:18 */
/* La griglia in memoria e' piu' grande della mappa: MAP_OFFSET tile di bordo per
 * lato in orizzontale piu' uno (MAP_OFFSET_W = 15), e MAP_OFFSET per lato in
 * verticale (MAP_OFFSET_H = 14). E' dentro quel margine che il gioco copia la
 * striscia della mappa connessa, quindi e' anche il limite oltre il quale un
 * amico nella route accanto non ha piu' terreno sotto i piedi. */
#define MAP_OFFSET_W                        (MAP_OFFSET * 2 + 1)
#define MAP_OFFSET_H                        (MAP_OFFSET * 2)

#define MOVEMENT_TYPE_NONE                  0x0
#define MOVEMENT_ACTION_FACE_DOWN           0x0
#define MOVEMENT_ACTION_WALK_NORMAL_DOWN    0x8
#define MOVEMENT_ACTION_WALK_NORMAL_UP      0x9
#define MOVEMENT_ACTION_WALK_NORMAL_LEFT    0xA
#define MOVEMENT_ACTION_WALK_NORMAL_RIGHT   0xB

/* Direzioni delle connessioni fra mappe (include/constants/global.h:147-154).
 * ATTENZIONE: valgono 1/2/3/4 come i DIR_*, ma con lo stesso ordine SUD, NORD,
 * OVEST, EST - non e' l'ordine che verrebbe naturale scrivere. DIVE (5) ed
 * EMERGE (6) esistono e sono fuori scopo: si ignorano. */
#define CONNECTION_SOUTH                    1
#define CONNECTION_NORTH                    2
#define CONNECTION_WEST                     3
#define CONNECTION_EAST                     4

/* Comportamenti di metatile che ci interessano (include/constants/metatile_behaviors.h,
 * valori calcolati contando l'enum).
 * MB_ANIMATED_DOOR e' l'UNICO che MetatileBehavior_IsWarpDoor accetta
 * (metatile_behavior.c:220): e' quindi l'unico tile su cui si possa entrare
 * camminando verso nord. MB_PETALBURG_GYM_DOOR e' animato ma si warpa via
 * script: puo' capitare in USCITA, non in ingresso.
 * Le porte NON animate (MB_NON_ANIMATED_DOOR, scale, tappeti) non hanno nessuna
 * animazione da riprodurre: li' l'amico fa il passo e sparisce, che e' gia' il
 * comportamento di oggi. Per questo il valore non compare qui. */
#define MB_ANIMATED_DOOR                    0x69
#define MB_PETALBURG_GYM_DOOR               0x8D
/* Il bancone: MetatileBehavior_IsCounter (metatile_behavior.c:473). Serve al
 * blocco del tasto A, che deve guardare OLTRE il bancone come fa il gioco
 * (field_control_avatar.c:294-300). Enum metatile_behaviors.h: MB_NORMAL e'
 * la riga 5 = 0, MB_COUNTER la riga 133 = 128 = 0x80 (stesso conteggio che
 * da' 0x69 per MB_ANIMATED_DOOR, verificato). */
#define MB_COUNTER                          0x80

/* Direzioni (include/constants/global.h:137) */
#define DIR_NONE                            0
#define DIR_SOUTH                           1
#define DIR_NORTH                           2
#define DIR_WEST                            3
#define DIR_EAST                            4

/* Ogni famiglia di movement action e' ordinata DOWN, UP, LEFT, RIGHT, cioe'
 * esattamente DIR_SOUTH, DIR_NORTH, DIR_WEST, DIR_EAST. Quindi l'azione si
 * ricava come base + (dir - 1), e cambiare andatura vuol dire cambiare base. */
#define MOVEMENT_BASE_FACE                  0x00   /* FACE_DOWN, girarsi sul posto */
#define MOVEMENT_BASE_WALK                  0x08   /* WALK_NORMAL_DOWN        */
#define MOVEMENT_BASE_FAST                  0x15   /* WALK_FAST_DOWN, bici    */
#define MOVEMENT_BASE_SURF                  0x29   /* RIDE_WATER_CURRENT_DOWN */
#define MOVEMENT_BASE_RUN                   0x35   /* PLAYER_RUN_DOWN         */
/* Marcia di recupero: WALK_FASTER_DOWN, che nella tabella del gioco corrisponde
 * a MOVE_SPEED_FASTER, la velocita' massima della mach bike
 * (event_object_movement.c:50). Si usa solo per riassorbire il ritardo. */
#define MOVEMENT_BASE_FASTER                0x2D

/* Andature nel nostro protocollo, indipendenti dagli id del gioco. */
#define SPEED_WALK                          0
#define SPEED_RUN                           1
#define SPEED_BIKE                          2
#define SPEED_SURF                          3

/* struct PlayerAvatar flags (include/global.fieldmap.h:288) */
#define PLAYER_AVATAR_FLAG_ON_FOOT          (1 << 0)
#define PLAYER_AVATAR_FLAG_MACH_BIKE        (1 << 1)
#define PLAYER_AVATAR_FLAG_ACRO_BIKE        (1 << 2)
#define PLAYER_AVATAR_FLAG_SURFING          (1 << 3)
#define PLAYER_AVATAR_FLAG_UNDERWATER       (1 << 4)
#define PLAYER_AVATAR_FLAG_DASH             (1 << 7)

/* Stati dell'avatar (include/global.fieldmap.h:278). Sono gli indici della
 * tabella sPlayerAvatarGfxIds usata da GetPlayerAvatarGraphicsIdByStateIdAndGender:
 * scegliendo lo stato giusto si ottiene lo sprite giusto CON le sue animazioni
 * (camminata, corsa, pedalata, surf). */
#define PLAYER_AVATAR_STATE_NORMAL          0
#define PLAYER_AVATAR_STATE_MACH_BIKE       1
#define PLAYER_AVATAR_STATE_ACRO_BIKE       2
#define PLAYER_AVATAR_STATE_SURFING         3
#define PLAYER_AVATAR_STATE_UNDERWATER      4

/* Field effect del "blob" su cui si surfa (include/constants/field_effects.h:12)
 * e stato di ondeggiamento (include/field_effect_helpers.h). */
#define FLDEFF_SURF_BLOB                    8
#define BOB_PLAYER_AND_MON                  1

/* localId del nostro giocatore remoto.
 *
 * 0xE0 dal 2026-08-21 (era 0xF0). L'audit del salvataggio ha trovato che
 * 0xF0 = 240 = LOCALID_BERRY_BLENDER_PLAYER_END (constants/event_objects.h:
 * 304): il gioco lo usa DAVVERO, per i partner di link del Berry Blender e
 * delle sale link (field_specials.c:566, SpawnSpecialObjectEventParameterized
 * con localId 240-i). Con lo stesso id lo spazzino e lo scudo avrebbero
 * potuto distruggere o annullare un object event DEL GIOCO, e l'adozione
 * prendersi un partner di link per il nostro remoto.
 * I localId che il gioco usa: 1..64 i template delle mappe (OBJECT_EVENT_
 * TEMPLATES_COUNT, con #error in event_object_movement.c:41 se si avvicinano
 * ai riservati), 127 la camera, 236-240 i partner di link, 255 il giocatore,
 * 0 = nessuno. 0xE0 = 224 non compare in nessuna di queste liste ne' in
 * nessun SpawnSpecialObjectEvent della decomp (grep del 2026-08-21).
 *
 * DAL 2026-08-25 e' una BASE, non un id solo: fino a 4 giocatori per stanza
 * = 3 remoti, localId 0xE0/0xE1/0xE2. Anche 225 e 226 sono liberi (stesse
 * liste, ricontrollate il 2026-08-25: compaiono solo come OBJ_EVENT_GFX_*,
 * che sono id di GRAFICA, non localId). */
#define REMOTE_LOCAL_ID                     0xE0
#define N_REMOTES                           3
#define REMOTE_LID(slot)                    ((u8)(REMOTE_LOCAL_ID + (slot)))
/* Il confronto con sottrazione copre l'intervallo in un test solo. */
#define IS_REMOTE_LID(lid)                  ((u8)((lid) - REMOTE_LOCAL_ID) < N_REMOTES)

/* --- accesso alle strutture del gioco ------------------------------------- */

#define GAME_U8(addr)       (*(volatile u8  *)(addr))
#define GAME_U16(addr)      (*(volatile u16 *)(addr))
#define GAME_U32(addr)      (*(volatile u32 *)(addr))
#define GAME_S16(addr)      (*(volatile s16 *)(addr))
#define GAME_S32(addr)      (*(volatile s32 *)(addr))

/* struct Main (include/main.h).
 * callback1 e' il puntatore che AgbMain invoca DOPO ReadKeys e prima di
 * callback2 (CallCallbacks, src/main.c): e' l'unico punto in cui si possono
 * togliere tasti prima che il gioco li usi. Dall'IRQ non si puo': ReadKeys gira
 * dopo il VBlank e sovrascriverebbe. */
#define gMain_callback1     GAME_U32(ADDR_gMain + 0x00)
#define gMain_callback2     GAME_U32(ADDR_gMain + 0x04)
/* gMain.vblankCounter1 (include/main.h:22). Lo incrementa VBlankIntr del
 * gioco, che nella catena IRQ gira DOPO il nostro hook: al primo ingresso di
 * un frame si legge ancora il valore del frame prima, ed e' proprio cio' che
 * rende il confronto "e' avanzato?" un cancello affidabile. */
#define gMain_vblankCounter1 GAME_U32(ADDR_gMain + 0x20)
#define gMain_heldKeys      GAME_U16(ADDR_gMain + 0x2C)
#define gMain_newKeys       GAME_U16(ADDR_gMain + 0x2E)

/* struct Main, byte +0x439: e' un blocco di bit
 * (oamLoadDisabled:1, inBattle:1, anyLinkBattlerHasFrontierPass:1).
 *
 * IL BIT NON E' DEDOTTO, E' STATO LETTO NEL BINARIO. In game_types.h vale la
 * regola di non fidarsi di come i due compilatori dispongono i bitfield, quindi
 * la posizione e' stata verificata disassemblando la ROM della build matching:
 * a 0x080369E8 (dentro CB2_InitBattleInternal, la riga `gMain.inBattle = TRUE`
 * di battle_main.c:703) c'e' esattamente
 *
 *     ldr r1, =0x030022C0      ; gMain
 *     ldr r2, =0x00000439
 *     adds r1, r1, r2
 *     ldrb r0, [r1]
 *     movs r2, #2              ; <- il bit
 *     orrs r0, r2
 *     strb r0, [r1]
 *
 * inBattle si alza in CB2_InitBattleInternal, cioe' al PRIMO frame fuori
 * dall'overworld: la transizione di battaglia gira ancora dentro l'overworld
 * (Task_BattleStart, battle_setup.c:365, cambia callback2 solo a transizione
 * finita). Quindi non c'e' nessuna finestra in cui il gioco sia "fuori
 * dall'overworld ma non ancora in lotta". */
#define gMain_inBattle      ((GAME_U8(ADDR_gMain + 0x439) & 0x02) != 0)

#define A_BUTTON            0x0001
#define DPAD_RIGHT          0x0010
#define DPAD_LEFT           0x0020
#define DPAD_UP             0x0040
#define DPAD_DOWN           0x0080

/* struct PlayerAvatar */
#define gPlayerAvatar_flags          GAME_U8(ADDR_gPlayerAvatar + 0x00)
#define gPlayerAvatar_objectEventId  GAME_U8(ADDR_gPlayerAvatar + 0x05)

/* struct SaveBlock1 -> pos (struct Coords16 a +0x00), location (WarpData a +0x04).
 * `pos` e' l'angolo in alto a sinistra della finestra di mappa visibile: e' la
 * stessa cosa che usa RemoveObjectEventIfOutsideView per decidere chi eliminare. */
#define SaveBlock1()        (GAME_U32(ADDR_gSaveBlock1Ptr))
/* La COPIA D'APPOGGIO degli object event che il salvataggio scrive su flash:
 * SaveObjectEvents (load_save.c:184) copia gObjectEvents QUI, poi la flash
 * serializza SaveBlock1 settore per settore. global.h: offset 0xA30, 16 voci
 * da 0x24. Gli accessor oe_* funzionano anche su queste voci: prendono un
 * indirizzo qualunque. */
#define sb1_objEvent(sb1, i) ((sb1) + 0xA30u + (u32)(i) * OBJECT_EVENT_SIZE)
/* I TEMPLATE degli object event della mappa corrente: global.h +0xC70, 64
 * voci da 0x18 (struct ObjectEventTemplate, global.fieldmap.h:92: localId a
 * +0x00, script a +0x10). Il gioco li cerca per localId con
 * FindObjectEventTemplateByLocalId sulle prime objectEventCount voci
 * (gMapHeader.events->objectEventCount). Qui SOLO lettura: servono al blocco
 * del tasto A per sapere se un object event ha uno script vero. */
#define OBJECT_EVENT_TEMPLATES_COUNT   64
#define OBJECT_EVENT_TEMPLATE_SIZE     0x18
#define sb1_objEventTemplate(sb1, i) ((sb1) + 0xC70u + (u32)(i) * OBJECT_EVENT_TEMPLATE_SIZE)
#define oet_localId(t)      GAME_U8((t) + 0x00)

#define sb1_posX(sb1)       GAME_S16((sb1) + 0x00)
#define sb1_posY(sb1)       GAME_S16((sb1) + 0x02)
#define sb1_mapGroup(sb1)   GAME_U8((sb1) + 0x04)
#define sb1_mapNum(sb1)     GAME_U8((sb1) + 0x05)

/* Parametri dei field effect: gFieldEffectArguments[8], u32 ciascuno. */
#define gFieldEffectArguments(i) GAME_U32(ADDR_gFieldEffectArguments + (i) * 4)

/* --- mappa corrente e route adiacenti -------------------------------------
 *
 * gMapHeader e' la struct MapHeader PER VALORE (include/global.fieldmap.h:171),
 * non un puntatore: si legge direttamente all'indirizzo, senza dereferenziare.
 *
 *   MapHeader     mapLayout +0x00, connections +0x0C
 *   MapLayout     width s32 +0x00, height s32 +0x04
 *   MapConnections count s32 +0x00, connections (puntatore) +0x04
 *   MapConnection direction u8 +0x00, offset s32 +0x04, mapGroup +0x08,
 *                 mapNum +0x09, dimensione 0x0C
 *
 * Lo stride 0x0C non e' dedotto dal sizeof di una struct C: e' quello che emette
 * davvero la macro `connection` (asm/macros/map.inc:152) - byte + 3 di padding,
 * offset a 4 byte, i due byte della mappa e 2 di padding finale. */
#define gMapHeader_mapLayout    GAME_U32(ADDR_gMapHeader + 0x00)
#define gMapHeader_events       GAME_U32(ADDR_gMapHeader + 0x04)
#define gMapHeader_connections  GAME_U32(ADDR_gMapHeader + 0x0C)

/* struct MapEvents (global.fieldmap.h:145): objectEventCount e' il primo byte. */
#define mapevents_objectEventCount(e) GAME_U8((e) + 0x00)

#define maplayout_width(l)      ((s32)GAME_U32((l) + 0x00))
#define maplayout_height(l)     ((s32)GAME_U32((l) + 0x04))

#define mapconns_count(c)       ((s32)GAME_U32((c) + 0x00))
#define mapconns_list(c)        GAME_U32((c) + 0x04)

#define MAP_CONNECTION_SIZE     0x0C
#define mapconn_direction(c)    GAME_U8((c) + 0x00)
#define mapconn_offset(c)       ((s32)GAME_U32((c) + 0x04))
#define mapconn_group(c)        GAME_U8((c) + 0x08)
#define mapconn_num(c)          GAME_U8((c) + 0x09)

/* MapHeader di un'altra mappa: serve solo il suo mapLayout. */
#define maphdr_mapLayout(h)     GAME_U32((h) + 0x00)

/* struct SaveBlock2 -> playerGender a +0x08 (include/global.h) */
#define SaveBlock2()          (GAME_U32(ADDR_gSaveBlock2Ptr))
#define sb2_playerGender(sb2) GAME_U8((sb2) + 0x08)

/* struct ObjectEvent: indirizzo dello slot i-esimo */
#define ObjectEvent(i)      (ADDR_gObjectEvents + (i) * OBJECT_EVENT_SIZE)

/* Campi dell'ObjectEvent usati dal payload.
 * Il byte 0x00 e' un blocco di flag: il bit 0 e' `active`. */
#define oe_flags0(oe)       GAME_U8((oe) + 0x00)
#define oe_active(oe)       (oe_flags0(oe) & 0x01)
/* bit 1 dello stesso byte: il gioco lo alza quando l'object event sta
 * eseguendo un movimento "singolo" (un passo). Serve solo per diagnostica. */
#define oe_singleMovement(oe) ((oe_flags0(oe) & 0x02) != 0)
#define oe_spriteId(oe)     GAME_U8((oe) + 0x04)
/* Sprite del field effect agganciato all'object event: per il surf e' il blob. */
#define oe_fieldEffectSpriteId(oe) GAME_U8((oe) + 0x1A)
/* movementActionId (+0x1C): ClearObjectEvent lo mette a MOVEMENT_ACTION_NONE
 * (0xFF). Serve allo scudo del salvataggio per lasciare lo slot ESATTAMENTE
 * come uno slot mai usato. */
#define oe_movementActionId(oe) GAME_U8((oe) + 0x1C)
#define oe_graphicsId(oe)   GAME_U8((oe) + 0x05)
#define oe_localId(oe)      GAME_U8((oe) + 0x08)
#define oe_mapNum(oe)       GAME_U8((oe) + 0x09)
#define oe_mapGroup(oe)     GAME_U8((oe) + 0x0A)
/* byte 0x0B: currentElevation nei 4 bit bassi, previousElevation negli alti */
#define oe_elevation(oe)    (GAME_U8((oe) + 0x0B) & 0x0F)
/* initialCoords / currentCoords / previousCoords sono tre struct Coords16.
 * Le macro sono lvalue: si possono anche scrivere, e serve per riposizionare
 * il remoto. */
#define oe_initialX(oe)     GAME_S16((oe) + 0x0C)
#define oe_initialY(oe)     GAME_S16((oe) + 0x0E)
#define oe_currentX(oe)     GAME_S16((oe) + 0x10)
#define oe_currentY(oe)     GAME_S16((oe) + 0x12)
#define oe_previousX(oe)    GAME_S16((oe) + 0x14)
#define oe_previousY(oe)    GAME_S16((oe) + 0x16)
/* byte 0x18: facingDirection nei 4 bit bassi, movementDirection negli alti */
#define oe_facingDirection(oe)   (GAME_U8((oe) + 0x18) & 0x0F)
#define oe_movementDirection(oe) ((GAME_U8((oe) + 0x18) >> 4) & 0x0F)

/* struct Sprite (include/sprite.h), 0x44 byte. Serve solo per la diagnostica:
 * animNum e' l'indice nella tabella puntata da sprite->anims, e l'intero bug dei
 * glitch grafici consiste nel farlo uscire dal range di quella tabella. */
/* struct ObjectEventGraphicsInfo (include/global.fieldmap.h).
 * `size` sono i BYTE di grafica dello sprite, cioe' quanti tile OAM gli sono
 * stati allocati alla creazione. Le grafiche del rivale non hanno tutte la
 * stessa: 256 a piedi, 512 in bici e in surf. ObjectEventSetGraphicsId NON
 * rialloca i tile, quindi passare da una piccola a una grande farebbe scrivere
 * la copia dell'immagine oltre l'allocazione, dentro i tile di altri sprite. */
#define gfxinfo_size(info)      GAME_U16((info) + 0x06)

#define MAX_SPRITES             64
#define SPRITE_SIZE             0x44
#define Sprite(i)               (ADDR_gSprites + (i) * SPRITE_SIZE)
#define sprite_animNum(sp)      GAME_U8((sp) + 0x2A)

/* struct Sprite, i campi che servono all'indicatore di stato (include/sprite.h:194).
 *   +0x00 oam (8 byte), +0x14 template, +0x20 x, +0x22 y, +0x24 x2, +0x26 y2,
 *   +0x3E blocco di bit (inUse 0x01, coordOffsetEnabled 0x02, invisible 0x04),
 *   +0x43 subpriority
 * Il blocco di bit a +0x3E e' l'unico posto in cui si scrive un bitfield del
 * gioco: `coordOffsetEnabled` e' il bit 1, ed e' quello che il gioco stesso
 * alza per gli sprite del campo (SetIconSpriteData, field_effect_helpers.c) -
 * senza, l'icona resterebbe inchiodata allo schermo invece di scorrere con la
 * mappa. */
#define sprite_oamAttr2(sp)     GAME_U16((sp) + 0x04)
#define sprite_template(sp)     GAME_U32((sp) + 0x14)
#define sprite_callback(sp)     GAME_U32((sp) + 0x1C)
#define sprite_x(sp)            GAME_S16((sp) + 0x20)
#define sprite_y(sp)            GAME_S16((sp) + 0x22)
#define sprite_x2(sp)           GAME_S16((sp) + 0x24)
#define sprite_y2(sp)           GAME_S16((sp) + 0x26)
#define sprite_flags3E(sp)      GAME_U16((sp) + 0x3E)

/* LO SCORRIMENTO DELLA TELECAMERA (struct CameraObject,
 * include/field_camera.h: x a +0x10, y a +0x14, s32, valori -15..15 - il
 * gioco fa `gFieldCamera.x %= 16`).
 *
 * IL PAYLOAD NON LO LEGGE, e la storia vale la pena di restare qui. Il
 * 2026-08-28 sembrava che `MoveObjectEventToMapCoords` piazzasse lo sprite
 * sbagliato di questo valore quando la camera e' a meta' tile, e la
 * "correzione" era sommarlo dentro TeleportRemote. Il banco
 * mgbaanco_subpixel.lua ha misurato il contrario: SENZA compensazione i
 * riposizionamenti sono allineati al pixel (5 su 5), CON la compensazione
 * finivano fuori di 7-13 px. L'aritmetica sulle due formule del gioco era
 * sbagliata; il simbolo resta in gen_syms.py perche' lo legge il banco. */
/* data[0] (include/sprite.h:217, s16 data[8] a +0x2E). Il gioco lo usa come
 * spazio libero per la callback dello sprite; le NOSTRE icone hanno la
 * callback muta, quindi e' nostro: ci si timbra lo SLOT del remoto, per non
 * adottare l'icona di un altro slot dopo un reset degli sprite. */
#define sprite_data0(sp)        GAME_S16((sp) + 0x2E)
/* data[2] della BOLLA DEL SURF: l'indice dell'object event che sta seguendo
 * (`sPlayerObjId`, field_effect_helpers.c:992, scritto da FldEff_SurfBlob:1010
 * e riletto da UpdateSurfBlobFieldEffect:1054). E' l'unico modo di chiedere a
 * una bolla «sei DAVVERO la bolla di questo qui?» invece di fidarsi di un id
 * conservato dentro l'object event, che dopo un reset degli sprite puo'
 * indicare uno slot riciclato da qualcun altro. */
#define sprite_data2(sp)        GAME_S16((sp) + 0x32)
#define SPRITE_FLAG_IN_USE              0x0001
#define SPRITE_FLAG_COORD_OFFSET        0x0002
#define SPRITE_FLAG_INVISIBLE           0x0004
/* attr2: tileNum bit 0-9, priority 10-11, paletteNum 12-15 */
#define OAM_TILENUM_MASK        0x03FF
/* struct Sprite.images (include/sprite.h:198, offset 0x0C): puntatore all'array
 * di struct SpriteFrameImage. Serve per la rimozione: DestroySprite libera i
 * tile OAM leggendo `sprite->images->size` (sprite.c:625), e RemoveObjectEventInternal
 * gli sostituisce prima un descrittore con la size giusta. E' un lvalue. */
#define sprite_images(sp)       GAME_U32((sp) + 0x0C)

/* Finestra oltre la quale RemoveObjectEventIfOutsideView elimina un object event
 * (event_object_movement.c). Riprodotta identica: se spawniamo fuori da qui, il
 * gioco ce lo cancella al frame successivo. */
#define VIEW_LEFT(px)       ((px) - 2)
#define VIEW_RIGHT(px)      ((px) + 17)
#define VIEW_TOP(py)        (py)
#define VIEW_BOTTOM(py)     ((py) + 16)

/* Numero di voci nelle tabelle di animazione: le famiglie STD arrivano a 20
 * (ANIM_STD_COUNT, include/constants/event_object_movement.h:269), le ANIM_RUN_*
 * stanno a 20-23 e ESISTONO SOLO nella tabella dello sprite a piedi. La tabella
 * della mach bike (sAnimTable_Standard) si fermerebbe a 20. */
#define ANIM_STD_COUNT                      20

#endif /* GAME_TYPES_H */
