#!/usr/bin/env python3
"""
gen_syms.py - estrae gli indirizzi delle funzioni del gioco dal .map di pokeemerald
e genera payload/game_syms.h.

Il payload non reimplementa niente: chiama direttamente le routine originali del
gioco, che stanno in ROM. Questo file e' il ponte fra i due mondi.

Uso:
    python tools/gen_syms.py <percorso pokeemerald.map>

Nota sul bit Thumb: il codice Gen 3 e' quasi tutto Thumb. Un puntatore a funzione
usato con BLX deve avere il bit 0 a 1, altrimenti il salto avviene in modalita' ARM
e si schianta. Qui il bit viene messo automaticamente per tutti i simboli in ROM,
e ogni eccezione ARM va dichiarata esplicitamente in ARM_FUNCTIONS.
"""

import re
import sys
from pathlib import Path

# Funzioni del gioco che il payload chiama. Le firme sono quelle di
# src/event_object_movement.c e src/overworld.c della decomp.
FUNCTIONS = {
    "SpawnSpecialObjectEventParameterized":
        "u8 (*)(u8 graphicsId, u8 movementBehavior, u8 localId, s16 x, s16 y, u8 elevation)",
    # RemoveObjectEventByLocalIdAndMap: TOLTA il 2026-07-30, e non va rimessa.
    # Fa FlagSet(GetObjectEventFlagIdByObjectEventId(id)) (event_object_movement.c:1394),
    # e per il nostro localId 0xF0 non esiste template: la lookup ritorna NULL e
    # la lettura del flagId finisce a 0x00000014, dentro il BIOS. Con l'id
    # spazzatura che ne esce, FlagSet scrive in gSaveBlock1Ptr->flags[id / 8] con
    # id/8 fino a 2048: fuori dall'array dei flag, dentro SaveBlock1 - cioe'
    # dentro il salvataggio. Il payload usa RemoveRemoteObjectEvent, che replica
    # RemoveObjectEvent senza il FlagSet (vedi payload/main.c).
    #
    # TrySpawnObjectEvent e GetObjectEventIdByLocalIdAndMap: TOLTE il 2026-07-30
    # (seconda sessione). Erano dichiarate qui ma assenti dal binario compilato -
    # nessuna delle due e' piu' chiamata. Alla Fase 5 ogni simbolo di questa
    # tabella va ritrovato per pattern matching sulla ROM italiana: tenerne di
    # morti e' lavoro vero buttato.
    "ObjectEventSetHeldMovement":
        "bool8 (*)(void *objectEvent, u8 movementActionId)",
    "ObjectEventClearHeldMovement":
        "void (*)(void *objectEvent)",
    "ObjectEventIsHeldMovementActive":
        "bool8 (*)(void *objectEvent)",
    "ObjectEventCheckHeldMovementStatus":
        "u8 (*)(void *objectEvent)",
    "ObjectEventClearHeldMovementIfActive":
        "void (*)(void *objectEvent)",
    # Riposizionamento del remoto. NON si usa piu' la sequenza di
    # InitLinkPlayerObjectEventPos (overworld.c:2958): quella e' scritta per gli
    # object event dei giocatori link, che sono esenti dal culling e il cui sprite
    # viene riposizionato ogni frame da SpriteCB_LinkPlayer. Il nostro e' un object
    # event ordinario, e MoveObjectEventToMapCoords
    # (event_object_movement.c:2133) e' la routine che fa la cosa giusta: imposta
    # le coordinate E sposta lo sprite con gli offset centerToCornerVec.
    "MoveObjectEventToMapCoords":
        "void (*)(void *objectEvent, s16 x, s16 y)",
    # Grafica del giocatore: e' la stessa tabella che usa il gioco per l'avatar
    # locale, quindi il remoto ottiene lo sprite giusto - protagonista maschio o
    # femmina, in bici, in surf - con le animazioni corrispondenti.
    # Il remoto usa la grafica del RIVALE, non quella del giocatore. Stessi
    # disegni (il rivale e' Brendan/May), ma paletteSlot = PALSLOT_NPC_SPECIAL
    # invece di PALSLOT_PLAYER: cioe' una slot di palette tutta sua, invece di
    # quella condivisa con l'avatar locale. E' esattamente la scelta che fa il
    # gioco per i giocatori remoti del Cable Club / Union Room, in
    # CreateLinkPlayerSprite (overworld.c).
    "GetRivalAvatarGraphicsIdByStateIdAndGender":
        "u8 (*)(u8 state, u8 gender)",
    # Serve per leggere `size` (offset +0x06): le grafiche del rivale NON hanno
    # tutte la stessa dimensione (256 byte a piedi, 512 in bici e in surf),
    # mentre ObjectEventSetGraphicsId non rialloca i tile dello sprite.
    "GetObjectEventGraphicsInfo":
        "u32 (*)(u8 graphicsId)",
    "ObjectEventSetGraphicsId":
        "void (*)(void *objectEvent, u8 graphicsId)",
    # Il "blob" su cui si surfa NON fa parte dello sprite del giocatore: e' un
    # field effect a se'. Senza, il remoto in surf galleggia sull'acqua da solo.
    # Il blob segue un object event QUALUNQUE tramite gFieldEffectArguments[2]
    # (field_effect_helpers.c:1010), non solo quello del giocatore.
    "FieldEffectStart":
        "u8 (*)(u8 fldEff)",
    "SetSurfBlob_BobState":
        "void (*)(u8 spriteId, u8 state)",
    "DestroySprite":
        "void (*)(void *sprite)",
    # OBBLIGATORIA dopo ObjectEventSetGraphicsId. Quest'ultima sostituisce
    # sprite->anims ma NON tocca sprite->animNum: se l'animNum vecchio non esiste
    # nella tabella nuova (ANIM_RUN_* sta a 20-23, e la tabella della mach bike ne
    # ha solo 20) si legge fuori dall'array, si ottiene un imageValue spazzatura e
    # RequestSpriteFrameImageCopy (sprite.c:802) fa un DMA di dimensione arbitraria
    # nella OBJ VRAM, calpestando i tile degli altri sprite. Nel gioco ogni
    # chiamante di ObjectEventSetGraphicsId fa seguire ObjectEventTurn, che
    # riporta l'animNum a una FACE_* (0-3).
    "ObjectEventTurn":
        "void (*)(void *objectEvent, u8 direction)",

    # --- route adiacenti (Fase 7) --------------------------------------------
    # Il gioco carica in memoria una STRISCIA della mappa connessa (7 tile, 8 a
    # EAST: InitBackupMapLayoutConnections, fieldmap.c:133-343) e disegna
    # normalmente gli object event con coordinate oltre il bordo. Quindi un amico
    # nella route accanto e' rappresentabile, ma solo dentro quella striscia.
    #
    # GetMapHeaderFromConnection serve per larghezza/altezza della mappa
    # dell'amico, che entrano nella formula di traduzione.
    "GetMapHeaderFromConnection":
        "u32 (*)(u32 connection)",
    # Valida una posizione della griglia FUORI dal bordo e dice a quale
    # connessione appartiene. Si usa al posto di duplicare i limiti 7/8: e' la
    # logica del gioco, quindi non puo' divergere.
    #
    # NON usare GetMapConnection (0x08084FC0): dereferenzia connections->count
    # PRIMA di controllare che connections sia non NULL, e crasha sulle mappe
    # senza connessioni (che sono la maggioranza degli interni).
    "GetMapConnectionAtPos":
        "u32 (*)(s16 x, s16 y)",

    # --- porte (Fase 7) -------------------------------------------------------
    # Coordinate della GRIGLIA (con MAP_OFFSET). Fuori mappa ritorna il border
    # block invece di leggere fuori array (GetMapGridBlockAt, fieldmap.c:51-64).
    "MapGridGetMetatileBehaviorAt":
        "u32 (*)(s32 x, s32 y)",
    # Ritornano il taskId dell'animazione, oppure -1. Il task e' autonomo e gira
    # nel main loop: si avvia e si aspetta che finisca, senza toccare gTasks.
    # UNA SOLA porta animata alla volta in tutto il gioco: StartDoorAnimationTask
    # ritorna -1 se un Task_AnimateDoor e' gia' attivo (field_door.c:437).
    "FieldAnimateDoorOpen":
        "s8 (*)(u32 x, u32 y)",
    "FieldAnimateDoorClose":
        "s8 (*)(u32 x, u32 y)",
    "FieldIsDoorAnimationRunning":
        "bool8 (*)(void)",
    "GetDoorSoundEffect":
        "u32 (*)(u32 x, u32 y)",
    # Il suono della porta all'ingresso lo fa anche il gioco per gli NPC:
    # ScrCmd_opendoor (scrcmd.c:2050) e' PlaySE(GetDoorSoundEffect) +
    # FieldAnimateDoorOpen, esattamente la coppia che usiamo noi.
    "PlaySE":
        "void (*)(u16 songNum)",

    # --- indicatori di stato (Fase 7, terzo blocco) --------------------------
    # Il campo non ha nessun field effect "persistente che segue un NPC" per
    # ball / "..." / zaino: gli icon field effect (FLDEFF_EXCLAMATION_MARK_ICON
    # e compagni) sono animazioni one-shot che si spengono da sole. Quindi lo
    # sprite se lo crea il payload, con la stessa infrastruttura che servira'
    # poi al nametag.
    #
    # NOTA sui simboli che NON compaiono qui e sul perche':
    #   - SpriteCallbackDummy: la callback del nostro sprite e' una funzione del
    #     payload (PayloadSpriteDummy). Un simbolo in meno da ritrovare in
    #     Fase 5, e per giunta uno che non fa niente;
    #   - gDummySpriteAnimTable / gDummySpriteAffineAnimTable: le due tabelle
    #     sono rispettivamente un ANIMCMD_END e un AFFINEANIMCMD_END, cioe' due
    #     costanti. Il payload se le definisce da se'.
    "CreateSprite":
        "u8 (*)(const void *template, s16 x, s16 y, u8 subpriority)",
    # Ritorna il tile di partenza, 0 se non c'e' spazio. La grafica sta nel
    # payload (EWRAM) e viene copiata in OBJ VRAM da CpuCopy16: si chiama SOLO
    # da payload_cb1, cioe' dal main loop del gioco, MAI dall'IRQ - vedi il
    # commento su IndicatorTick in payload/main.c.
    "LoadSpriteSheet":
        "u16 (*)(const void *sheet)",
    # La sheet delle icone viaggia COMPRESSA (LZ77 formato BIOS, 2026-08-21):
    # il gioco la decomprime in gDecompressionBuffer con SWI 11h e poi chiama
    # LoadSpriteSheet (decompress.c:22-31). Zero decoder nostro, ~500 byte di
    # EWRAM recuperati. Stessa disciplina: solo da payload_cb1, mai dall'IRQ.
    "LoadCompressedSpriteSheet":
        "u16 (*)(const void *sheet)",
    # 0xFFFF se il tag non e' caricato: e' il test del caricamento pigro, che
    # rimette la grafica da solo dopo i warp (che resettano il sistema sprite).
    "GetSpriteTileStartByTag":
        "u16 (*)(u16 tag)",
    "LoadSpritePalette":
        "u8 (*)(const void *palette)",
    # 0xFF se il tag non e' caricato.
    "IndexOfSpritePaletteTag":
        "u8 (*)(u16 tag)",
    # Il flag che dice "il giocatore non ha i controlli": dialogo, script,
    # cutscene. E' sLockFieldControls (script.c:28), che e' static e quindi NON
    # compare nel .map: l'unico modo di leggerlo senza inventarsi un indirizzo
    # e' chiamare il suo getter, che e' una foglia (ldrb + bx lr).
    "ArePlayerFieldControlsLocked":
        "bool8 (*)(void)",
    # C'e' un task vivo con questa func? Scansione di gTasks fatta dal gioco
    # (task.c:155): 16 confronti, foglia senza dipendenze. Serve a distinguere
    # il menu START (che gira come task DENTRO CB2_Overworld a controlli
    # bloccati) da un dialogo vero: senza, chi sta nel menu viene annunciato
    # come ST_DIALOG e l'amico vede il balloon (riportato da Lain il
    # 2026-08-09). Il func da cercare e' Task_ShowStartMenu, vedi DATA.
    "FuncIsActiveTask":
        "bool8 (*)(void *func)",
    # --- Consegna B: la scheda allenatore dell'amico -------------------------
    # Genera la PROPRIA scheda (96 byte) leggendo solo il salvataggio: e' la
    # funzione che il gioco usa per mandarla al partner di link, quindi produce
    # esattamente il formato che il viewer di link si aspetta. La gemella
    # ...ForPlayer e' static e NON si usa (regola del .map).
    "TrainerCard_GenerateCardForLinkPlayer":
        "void (*)(void *trainerCard)",
    # Mostra gTrainerCards[cardId] col proprio CB2 e torna a `callback` alla
    # chiusura. Fuori da una sessione link (gReceivedRemoteLinkPlayers FALSE)
    # NON aspetta nessun handshake: trainer_card.c:452-462, B -> fade ->
    # CloseTrainerCard. Il callback va passato col bit Thumb acceso.
    "ShowTrainerCardInLink":
        "void (*)(u8 cardId, void *callback)",
    # Il fade out prima di lasciare il campo (field_screen_effect.c). Il modo
    # e' FADE_TO_BLACK = 1 (constants/field_weather.h), delay 0.
    "FadeScreen":
        "void (*)(u8 mode, s8 delay)",
    # La pulizia delle finestre dell'overworld prima di cedere lo schermo: e'
    # cio' che fa StartMenuPlayerNameCallback (start_menu.c:706) prima di
    # ShowPlayerTrainerCard. RemoveExtraStartMenuWindows non serve: non
    # apriamo dal menu.
    "CleanupOverworldWindowsAndTilemaps":
        "void (*)(void)",
    # Blocco dei controlli durante il fade di apertura della scheda: senza, il
    # giocatore puo' avviare un dialogo o un warp mentre lo schermo si spegne.
    # Lo sblocco al ritorno lo fa il menu START (il rientro passa da
    # CB2_ReturnToFieldWithOpenMenu); Unlock serve SOLO al ramo di abort.
    "LockPlayerFieldControls":
        "void (*)(void)",
    "UnlockPlayerFieldControls":
        "void (*)(void)",
}

# Variabili globali del gioco che il payload legge o scrive.
DATA = [
    "gObjectEvents",
    "gSaveBlock1Ptr",
    "gSaveBlock2Ptr",
    "gMain",
    "gPlayerAvatar",
    # Serve solo alla diagnostica: leggere animNum dello sprite del remoto e
    # accorgersi subito se esce dal range della tabella corrente.
    "gSprites",
    # Parametri di ingresso dei field effect: si riempiono subito prima di
    # chiamare FieldEffectStart. E' un global condiviso col gioco.
    "gFieldEffectArguments",
    # ATTENZIONE: e' la struct MapHeader PER VALORE, non un puntatore ad essa.
    # Da qui si leggono mapLayout (+0x00, per larghezza e altezza della nostra
    # mappa) e connections (+0x0C, la tabella delle route adiacenti).
    "gMapHeader",
    # Funzione, ma qui serve l'indirizzo grezzo (senza bit Thumb) per confrontarlo
    # con gMain.callback2 e capire se siamo nell'overworld.
    "CB2_Overworld",
    # --- sezioni del menu (Fase 7, quarto blocco) ----------------------------
    # Come CB2_Overworld: indirizzi grezzi, MAI chiamati. Servono a riconoscere
    # QUALE schermata l'amico ha aperto, confrontandoli con gMain.callback2.
    # Sono i callback che il menu START installa con SetMainCallback2
    # (start_menu.c:647-692): restano in gMain.callback2 per almeno un frame
    # prima di passare la mano al run loop (che e' quasi sempre static e NON
    # compare nel .map), quindi il payload li vede e fa latch.
    # CB2_BagMenuRun e' l'eccezione: e' il run loop vero dello Zaino ed e'
    # esportato, quindi copre anche lo Zaino aperto da dentro la Squadra.
    # Trainer Card, Opzioni e PC hanno solo callback static: ricadono
    # sull'icona di menu generica, per la regola "niente indirizzi fuori dal
    # .map".
    "CB2_OpenPokedex",
    "CB2_PartyMenuFromStartMenu",
    "CB2_BagMenuFromStartMenu",
    "CB2_BagMenuRun",
    "CB2_InitPokeNav",
    # Funzione, ma serve come VALORE da confrontare, non da chiamare: e' la func
    # che il task del menu START tiene per TUTTA la vita del menu, dialogo di
    # salvataggio compreso (start_menu.c:561-577 - il run loop passa da
    # gMenuCallback, ma il task resta questo finche' DestroyTask alla chiusura).
    # Si passa a FuncIsActiveTask con il bit Thumb rimesso (| 1), perche'
    # gTasks[].func e' un puntatore C e ce l'ha acceso.
    "Task_ShowStartMenu",
    # --- Consegna B ----------------------------------------------------------
    # gTrainerCards[4]: [0] = buffer della NOSTRA scheda (lo riempie
    # TrainerCard_GenerateCardForLinkPlayer), [1] = la scheda dell'amico,
    # ricostruita chunk per chunk dagli eventi CARD. Fuori dalle sessioni link
    # il gioco non li tocca.
    "gTrainerCards",
    # gLinkPlayers[1].language (offset 0x1C + 0x1A) va scritto prima di
    # mostrare la scheda: ShowTrainerCardInLink lo legge per il font, e a 0
    # uscirebbe la resa giapponese. LANGUAGE_ITALIAN = 4.
    "gLinkPlayers",
    # Il cb2 di ritorno alla chiusura della scheda: lo STESSO che usa il gioco
    # per la trainer card dal menu START. Riapre il menu (fade e sblocco dei
    # controlli li gestisce la sua macchina, gia' collaudata). Usato come
    # VALORE con il bit Thumb rimesso, mai chiamato da noi.
    "CB2_ReturnToFieldWithOpenMenu",
    # --- Consegna C: convivenza col Cable Club -------------------------------
    # I due testimoni del "il gioco vuole il cavo". gLinkCallback si alza
    # dentro OpenLink (link.c:375) e resta su per tutto l'handshake;
    # gReceivedRemoteLinkPlayers copre la sessione stabilita (e i buchi in cui
    # gLinkCallback torna NULL fra un'operazione e l'altra). Quando uno dei
    # due e' vivo, il payload SioYield() e dorme: vedi il blocco linkBusy in
    # payload_frame. Solo LETTI, mai scritti.
    "gLinkCallback",
    "gReceivedRemoteLinkPlayers",
    # --- 2026-08-16: la macchina di link del gioco va tenuta ADDORMENTATA ----
    # Questi due si SCRIVONO, ed e' l'unica scrittura del genere nel progetto.
    # Il motivo, misurato: i nostri IRQ seriali possono trapelare
    # nell'handler del gioco (fra il nostro ACK e il ritorno a IntrMain c'e'
    # una finestra), e ogni trafila che passa risveglia SerialCB ->
    # CheckMasterOrSlave -> InitTimer: il gioco si convince di essere in una
    # sessione di link, arma il Timer 3 (visto nel log: IE 0x00C5) e da li'
    # in poi StartTransfer gli riscrive SIOCNT addosso e lo blocca ad
    # aspettare un partner che non parla la sua lingua.
    #   gLinkVSyncDisabled = TRUE  -> LinkVSync() non gira (main.c:344)
    #   gLink.state        = 1     -> LinkMain1 non fa niente (link.c:1899+)
    # Il gioco li rimette a posto da solo quando apre un link vero (OpenLink),
    # e in quel caso il payload ha gia' ceduto la porta.
    "gLinkVSyncDisabled",
    "gLink",
    # --- 2026-08-27: le stanze del Cable Club --------------------------------
    # Dentro la Sala Scambi il giocatore NON e' gObjectEvents[0]: i giocatori
    # collegati stanno in gLinkPlayerObjectEvents (overworld.c:201), e da li' si
    # risale allo slot vero (campo objEventId). Serve all'autopilota del banco
    # per sapere dove si trova l'avatar e portarlo sul posto a sedere.
    "gLinkPlayerObjectEvents",
    # --- 2026-08-28: lo scorrimento della telecamera, per il sub-pixel -------
    # Struct CameraObject (include/field_camera.h): x a +0x10 e y a +0x14,
    # s32, e valgono 0..15 - sono i pixel di cui la vista e' avanti rispetto
    # al tile mentre il giocatore cammina.
    #
    # NON LO USA IL PAYLOAD: lo legge il banco mgbaanco_subpixel.lua, che
    # il 2026-08-28 ha dimostrato che il difetto per cui era stato aggiunto
    # NON ESISTE (MoveObjectEventToMapCoords piazza bene anche a camera in
    # movimento: 5 riposizionamenti su 5 allineati al pixel). Resta qui
    # perche' e' il simbolo con cui quella misura si rifa'.
    "gFieldCamera",
]

# Funzioni compilate in ARM anziche' Thumb: niente bit 0.
ARM_FUNCTIONS = set()

# Nel .map i simboli compaiono in due forme diverse.
#
# 1) simboli normali (codice in ROM, dati in EWRAM):
#                 0x080a1b2c                NomeSimbolo
SYMBOL_RE = re.compile(r"^\s+0x([0-9a-fA-F]{8})\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")

# 2) simboli definiti per assegnazione nel linker script (tipicamente IWRAM:
#    gMain, gSaveBlock1Ptr, ...):
#                 0x03005d8c                        gSaveBlock1Ptr = .
ASSIGN_RE = re.compile(r"^\s+0x([0-9a-fA-F]{8})\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\.\s*$")


def parse_map(path: Path) -> dict:
    """Ritorna {nome: indirizzo}. In caso di duplicati tiene il primo e lo segnala."""
    symbols = {}
    duplicates = {}
    with path.open("r", errors="replace") as f:
        for line in f:
            m = SYMBOL_RE.match(line) or ASSIGN_RE.match(line)
            if not m:
                continue
            addr = int(m.group(1), 16)
            name = m.group(2)
            if name in symbols:
                if symbols[name] != addr:
                    duplicates.setdefault(name, {symbols[name]}).add(addr)
                continue
            symbols[name] = addr
    for name, addrs in duplicates.items():
        formatted = ", ".join(f"0x{a:08X}" for a in sorted(addrs))
        print(f"  ATTENZIONE: {name} compare a piu' indirizzi ({formatted}), tengo il primo",
              file=sys.stderr)
    return symbols


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    map_path = Path(sys.argv[1])
    if not map_path.is_file():
        print(f"ERRORE: {map_path} non esiste", file=sys.stderr)
        return 1

    symbols = parse_map(map_path)
    print(f"simboli letti dal .map: {len(symbols)}")

    missing = [n for n in list(FUNCTIONS) + DATA if n not in symbols]
    if missing:
        print("ERRORE: simboli richiesti assenti dal .map:", file=sys.stderr)
        for n in missing:
            print(f"  - {n}", file=sys.stderr)
        print("La build e' quella giusta? (serve MODERN=0, matching)", file=sys.stderr)
        return 1

    out = Path(__file__).resolve().parent.parent / "payload" / "game_syms.h"
    lines = [
        "/* GENERATO DA tools/gen_syms.py - non modificare a mano.",
        f" * Fonte: {map_path.name} (build matching, Smeraldo USA BPEE).",
        " *",
        " * ATTENZIONE: questi indirizzi valgono per la ROM USA. Su cartuccia italiana",
        " * sono diversi e vanno ritrovati per pattern matching - vedi il punto 0 delle",
        " * domande aperte in NOTES.md.",
        " */",
        "",
        "#ifndef GAME_SYMS_H",
        "#define GAME_SYMS_H",
        "",
        "typedef unsigned char  u8;",
        "typedef unsigned short u16;",
        "typedef unsigned int   u32;",
        "typedef signed char    s8;",
        "typedef signed short   s16;",
        "typedef signed int     s32;",
        "typedef unsigned char  bool8;",
        "",
        "/* --- funzioni del gioco (bit Thumb gia' impostato) --- */",
        "",
    ]

    for name, signature in FUNCTIONS.items():
        addr = symbols[name]
        thumb = 0 if name in ARM_FUNCTIONS else 1
        mode = "ARM" if thumb == 0 else "Thumb"
        lines.append(f"/* {name} @ 0x{addr:08X} ({mode}) */")
        lines.append(f"#define {name} (({signature})(0x{addr | thumb:08X}))")
        lines.append("")

    lines.append("/* --- dati del gioco --- */")
    lines.append("")
    for name in DATA:
        addr = symbols[name]
        lines.append(f"#define ADDR_{name} (0x{addr:08X}u)")
    lines.append("")
    lines.append("#endif /* GAME_SYMS_H */")

    out.write_text("\n".join(lines) + "\n")
    print(f"scritto {out}")
    for name in list(FUNCTIONS) + DATA:
        print(f"  0x{symbols[name]:08X}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
