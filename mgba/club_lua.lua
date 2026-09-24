-- =============================================================================
-- club_lua.lua - IL CABLE CLUB IN EMULATORE, con il solo Lua (2026-08-27)
-- =============================================================================
--
-- Si carica DENTRO inject_body.lua (che ha gia' il payload della camminata, il
-- client WebSocket verso il relay e i contatori). Qui c'e' solo la parte club,
-- tenuta separata perche' e' un pezzo a se': se domani cambia il protocollo del
-- club si tocca questo file e `net/club_link.py`, non lo script della
-- camminata.
--
-- COSA FA, in una riga: prende il posto dell'IRQ seriale del gioco. Il gioco
-- parla col partner attraverso due code dentro `gLink` (sendQueue/recvQueue),
-- che sull'hardware riempie l'interrupt del cavo; qui le riempie il Lua, e i
-- comandi viaggiano sul relay dentro i corpi T_CLUB - gli stessi byte che usa
-- il pannello Python (net/protocol.py), cosi' l'emulatore e il GBA fisico si
-- parlano senza traduttori.
--
-- PERCHE' NON SI USA IL CAVO EMULATO: misurato il 2026-08-27 (banco hw/siolab)
-- che in mGBA il gioco si crede CHILD, il trasferimento non completa mai e
-- l'IRQ seriale non si puo' iniettare da Lua. Tre porte chiuse. Questa e' la
-- quarta, e si e' aperta: la prova e' `docs/prove/2026-08-27-club-lua-saletta.png`.
--
-- IL RUOLO. Il nostro gioco fa lo SLAVE (localId 1): nel club GBA<->mGBA il
-- master del protocollo Gen 3 e' il GBA fisico, che ha il Pico a fargli da
-- master del cavo. Se un giorno servisse il contrario, cambia solo CLUB_IO_ID.

local Club = {}

-- --- indirizzi: SOLO da payload/game_syms_it.h ------------------------------
local ADDR_gLink         = 0x03003170
local ADDR_gLinkCallback = 0x03003140
local ADDR_gLinkVSyncDis = 0x03002748
local REG_SIOCNT         = 0x04000128
local REG_RCNT           = 0x04000134

-- --- struct Link (include/link.h) -------------------------------------------
local L_ISMASTER, L_STATE, L_LOCALID, L_PLAYERCNT = 0x000, 0x001, 0x002, 0x003
local L_HWERROR, L_BADCHECKSUM, L_QUEUEFULL, L_LAG = 0x010, 0x011, 0x012, 0x013
local L_SENDQ, L_RECVQ = 0x018, 0x33C
local CMD_LENGTH, QUEUE_CAP = 8, 50
local SENDQ_POS   = L_SENDQ + CMD_LENGTH * QUEUE_CAP * 2
local SENDQ_COUNT = SENDQ_POS + 1
local RECVQ_POS   = L_RECVQ + 4 * CMD_LENGTH * QUEUE_CAP * 2
local RECVQ_COUNT = RECVQ_POS + 1

local LINK_STATE_HANDSHAKE        = 2
local LINK_STATE_CONN_ESTABLISHED = 4

-- IL RUOLO, e non e' una costante. Di norma il nostro gioco fa lo SLAVE:
-- il partner previsto e' il GBA fisico, che col Pico e' il master del cavo e
-- quindi il master del protocollo Gen 3. Ma se dall'altra parte c'e' un ALTRO
-- EMULATORE (due amici sul Brick, per dire) nessuno dei due sarebbe master e la
-- scala non partirebbe mai: allora il peer piu' basso si prende il ruolo, come
-- fa il sito (web/js/club.js).
local CLUB_IO_ID = 1
local CLUB_PARTNER_ID = 0
local LINK_MASTER = 8

-- --- i corpi T_CLUB (net/protocol.py, byte per byte) ------------------------
local T_CLUB = 6
local CLUB_STATUS, CLUB_DATA, CLUB_REQ, CLUB_ENTER, CLUB_LEAVE = 1, 2, 3, 4, 5
local VERSIONE_CLUB = 4
-- Impronta riservata all'emulatore: il pannello Python la riconosce e dice
-- "l'amico gioca in EMULATORE" invece di urlare al pacchetto sbagliato.
local IMPRONTA_LUA = 0x4C554131      -- "LUA1"

-- Gli stati del device che il partner si aspetta (usb_link.py): qui non c'e'
-- nessun Pico, ma la sessione dall'altra parte ragiona per stati e senza questi
-- non comanda mai il proprio.
-- I comandi Gen 3 che il Lua deve saper produrre da solo (uno solo, per il
-- ripiego del master): include/link.h.
local LINKCMD_SEND_LINK_TYPE = 0x2222
local LINKTYPE_TRADE_SETUP   = 0x1133

local ST_HANDSHAKE_RX = 0xFF03
local ST_CONNECTED    = 0xFF05
local ST_RECONNECTING = 0xFF06

-- I codici-tasto della saletta (link.h): 0x11 = nessun tasto, 0x17 = la porta.
local LINK_KEY_IDLE      = 0x11
local LINK_KEY_EXIT_ROOM = 0x17
local LINKCMD_HELD_KEYS       = 0xCAFE
local LINKCMD_READY_CLOSE_LINK = 0x5FFF
local ST_CLOSED       = 0xFF07

local stato = {
    attivo = false,          -- il gioco e' al bancone (annunciato)
    collegato = false,       -- ... e il link e' stato scritto nel gioco
    master = false,          -- il NOSTRO gioco e' il master del protocollo?
    partnerPeer = nil,       -- il peer dell'amico, dal primo T_CLUB che arriva
    epocaPartner = nil,      -- l'epoca della SUA sessione: se cambia, e' ripartito
    riagganci = 0,           -- quante volte l'abbiamo seguito in una sessione nuova
    partnerLua = false,      -- ... e se e' un emulatore come noi
    apertoDa = 0,            -- frame di apertura, per l'attesa del partner
    avvisato = false,        -- l'avviso "serve il relay" si dice una volta sola
    epoca = 0,
    seqTx = 0,          -- numero di serie dei NOSTRI blocchi
    sseq = 0,           -- numero di serie dei nostri stati
    attesoRx = 0,       -- il prossimo blocco del partner che ci aspettiamo
    storia = {},        -- seq -> blocco, per le richieste di rispedizione
    buffer = {},        -- blocchi arrivati fuori ordine
    codaPartner = {},   -- comandi dell'amico ancora da consegnare al gioco
    tx = 0, rx = 0, dup = 0, fuoriOrdine = 0, richiesti = 0, persi = 0,
    txSessione = 0, rxSessione = 0,   -- azzerati a ogni sessione: servono al
                                      -- ripiego del master, che riguarda la
                                      -- sessione in corso, non la vita intera
    aperture = 0,                     -- volte che le danze le abbiamo aperte noi
    sessioni = 0,
    -- LA RIAPERTURA DEL LINK A META' SESSIONE (macchina degli scambi, lotta):
    -- non e' la fine, e' come funziona il Cable Club. Il firmware la annuncia
    -- con LinkReconnecting e rifa' la sezione; qui si fa lo stesso. La fine
    -- VERA e' solo il tasto della porta (CAFE 0017, EXIT_ROOM), come in
    -- usbSection.cpp. Prima di questa distinzione il Lua mandava CLOSED alla
    -- macchina degli scambi e il partner (sito) obbediva: "riavvia il gioco"
    -- sul GBA (campo 2026-08-27).
    exitRoom = false,
    inRiapertura = false,
    riapertoDa = 0,
    riaperture = 0,
    -- Il filtro dei tasti: il cavo del Pico campiona i CAFE a ~10 Hz contro i
    -- 60 del cavo vero, quindi un passo del GBA arriva come 2 campioni
    -- identici distanziati - e il gioco li esegue come 2 passi (campo
    -- 2026-08-27: il personaggio del GBA correva doppio). Un codice identico
    -- al precedente entro 16 frame (la durata esatta di un passo) e' lo
    -- stesso gesto campionato due volte: si butta. La camminata tenuta non si
    -- rompe: 1 codice ogni 16 frame = 60/16 = la velocita' vera del passo.
    cafeCodice = -1,
    cafeFrame = -1000,
    cafeSoppressi = 0,
    -- IL CONGEDO: l'amico e' uscito dalla porta. Staccare il cavo qui e' la
    -- schermata nera di errore (campo 2026-08-27): il nostro gioco e' ancora
    -- dentro la coreografia d'uscita e resta senza partner. Il firmware fa
    -- l'opposto (usbSection.cpp:41-79): visto EXIT_ROOM CONTINUA a fare da
    -- cavo finche' i due si sono scambiati READY_CLOSE_LINK. Qui si fa uguale,
    -- ma le risposte del partner - che non c'e' piu' - le fabbrichiamo noi.
    congedo = false,
    congedoDa = 0,
    congedoPorta = false,   -- la porta e' gia' stata consegnata al gioco
    congedi = 0,
    -- L'ESITO del congedo, e non e' un dettaglio: "riuscito" vuol dire che il
    -- gioco e' uscito DALLA PORTA da solo; "scaduto" che abbiamo staccato noi
    -- dopo averci provato (e allora sullo schermo un errore c'e'). Senza
    -- questa parola il contatore direbbe solo che ci abbiamo provato.
    congedoEsito = nil,
}

Club.stato = stato

-- --- lettura/scrittura delle code -------------------------------------------
local function leggiSend(pos)
    local c = {}
    for j = 0, CMD_LENGTH - 1 do
        c[j + 1] = emu:read16(ADDR_gLink + L_SENDQ + (j * QUEUE_CAP + pos) * 2)
    end
    return c
end

local function consumaSend(pos, count)
    emu:write8(ADDR_gLink + SENDQ_POS, (pos + 1) % QUEUE_CAP)
    emu:write8(ADDR_gLink + SENDQ_COUNT, count - 1)
end

-- Un giro di trasferimento: la parola del partner nel suo slot, la nostra nel
-- nostro. L'eco della propria parola non e' un vezzo: sul cavo vero ogni GBA
-- rilegge anche se stesso, e il gioco ci conta.
local function spingiRecv(cmdPartner, cmdNostro)
    local pos = emu:read8(ADDR_gLink + RECVQ_POS)
    local count = emu:read8(ADDR_gLink + RECVQ_COUNT)
    if count >= QUEUE_CAP then return false end
    local idx = (pos + count) % QUEUE_CAP
    local base = ADDR_gLink + L_RECVQ
    for j = 0, CMD_LENGTH - 1 do
        emu:write16(base + ((CLUB_PARTNER_ID * CMD_LENGTH + j) * QUEUE_CAP + idx) * 2,
                    (cmdPartner and cmdPartner[j + 1]) or 0)
        emu:write16(base + ((CLUB_IO_ID * CMD_LENGTH + j) * QUEUE_CAP + idx) * 2,
                    (cmdNostro and cmdNostro[j + 1]) or 0)
    end
    emu:write8(ADDR_gLink + RECVQ_COUNT, count + 1)
    return true
end

-- --- blocchi: 8 parole + 48 byte di zeri, come `pacchetto()` del finto -------
local function bloccoDaCmd(cmd)
    local parti = {}
    for j = 1, CMD_LENGTH do
        parti[j] = string.pack("<I2", cmd[j] or 0)
    end
    return table.concat(parti) .. string.rep("\0", 48)
end

local function cmdDaBlocco(b)
    local cmd = {}
    for j = 1, CMD_LENGTH do
        cmd[j] = string.unpack("<I2", b, (j - 1) * 2 + 1)
    end
    return cmd
end

-- --- la rete: gli stessi corpi di net/protocol.py ----------------------------
-- `manda` e' iniettato da chi ci carica (inject_body.lua): e' owlSend.
-- `chiPeer` restituisce il NOSTRO numero di giocatore: si chiede a ogni giro
-- perche' puo' cambiare (con RELAY_PEER = 0 lo sorteggia il Lua all'avvio, e
-- leggerlo una volta sola darebbe 0 - cioe' il numero piu' basso di tutti, e ci
-- prenderemmo il ruolo di master a torto).
local manda, chiPeer, puoiParlare = nil, nil, nil

local function mandaStato(status)
    stato.sseq = (stato.sseq + 1) & 0xFFFF
    -- Tre copie come fa club_link.py: uno stato perso appende la sessione
    -- dall'altra parte, e costa molto meno rimandarlo che accorgersene.
    for _ = 1, 3 do
        manda(T_CLUB, string.pack("<BI4I2I2I2I4", CLUB_STATUS, stato.epoca,
                                  stato.sseq, status, VERSIONE_CLUB, IMPRONTA_LUA))
    end
end

local function mandaBlocco(blocco)
    local seq = stato.seqTx
    stato.seqTx = (stato.seqTx + 1) & 0xFFFFFFFF
    stato.storia[seq] = blocco
    -- La storia serve solo per le richieste: 512 blocchi come Celio.
    if seq >= 512 then stato.storia[seq - 512] = nil end
    stato.tx = stato.tx + 1
    stato.txSessione = stato.txSessione + 1
    manda(T_CLUB, string.pack("<BI4I4", CLUB_DATA, stato.epoca, seq) .. blocco)
end

-- --- apertura e chiusura -----------------------------------------------------
-- L'APERTURA E' IN DUE TEMPI, e il motivo e' il ruolo.
--
-- Il gioco, appena lo forziamo a "collegato", decide SUBITO se fare il master o
-- lo slave (cable_club.c, Task_LinkupAwaitConnection: se e' master aspetta la
-- conferma col tasto A, se e' slave aspetta che il master conduca). Quella
-- scelta non si puo' correggere dopo: se sbagliamo, tutti e due restano a
-- guardarsi con scritto "In attesa di collegamento..." - successo, misurato.
--
-- Quindi: appena il gioco apre il club ci si ANNUNCIA soltanto (CLUB_ENTER e lo
-- stato "handshake"), e si aspetta di sapere chi c'e' dall'altra parte. Solo
-- allora si scrive il ruolo e si collega. Nel frattempo il gioco mostra
-- "attendi", che e' esattamente cio' che farebbe un GBA vero col cavo attaccato
-- a nessuno.
-- Quanto si aspetta l'amico prima di decidere da soli. Va LUNGA: il gioco, in
-- handshake, aspetta indefinitamente (Task_LinkupAwaitConnection torna subito
-- finche' i giocatori sono meno di due) - esattamente come un GBA vero col cavo
-- attaccato a nessuno. Con 20 s il primo arrivato si dichiarava slave, il gioco
-- scadeva l'attesa dei dati (~10 s) e mollava proprio mentre l'altro arrivava:
-- misurato, due emulatori che non si trovavano mai.
local ATTESA_MAX_FRAME = 60 * 90

local function annuncia()
    stato.attivo = true
    stato.collegato = false
    stato.sessioni = stato.sessioni + 1
    -- IL PARTNER E' DELLA SESSIONE, NON DELLO SCRIPT (2026-08-28).
    --
    -- `partnerPeer` era scritto da OGNI T_CLUB in arrivo, anche a sessione
    -- spenta, e non veniva mai rimesso a nil. Bastava che un pacchetto
    -- dell'amico fosse passato una volta - una sessione di ieri, o lui al
    -- bancone mentre noi camminavamo - perche' da li' in avanti il gate
    -- dell'attesa (piu' sotto) si aprisse subito: `collega()` a 30 frame
    -- dall'ingresso, ruolo deciso contro un peer che non c'e', e soprattutto
    -- il ri-annuncio dell'ENTER SPENTO, perche' quel ramo vive solo finche'
    -- non si e' collegati. Chi entrava per primo non si annunciava piu': era
    -- il "mi collego prima dell'amico e non ci troviamo mai".
    if stato.partnerPeer then
        console:log(string.format(
            "[club ] partner della sessione precedente dimenticato (peer %d): "
            .. "questa sessione aspetta chi c'e' adesso", stato.partnerPeer))
    end
    stato.partnerPeer = nil
    stato.partnerLua = nil
    stato.riannunciEnter = 0
    -- Un'epoca diversa a ogni sessione: il partner che la vede cambiare sa che
    -- siamo ripartiti e riaggancia invece di macinare sequenze di due mondi.
    stato.epoca = ((emu:read32(0x030022E0) * 2654435761)
                   ~ (stato.sessioni << 24) ~ 0x5A5A0000) & 0xFFFFFFFF
    if stato.epoca == 0 then stato.epoca = 1 end
    stato.seqTx, stato.attesoRx, stato.sseq = 0, 0, 0
    stato.storia, stato.buffer, stato.codaPartner = {}, {}, {}
    stato.master = false
    stato.txSessione, stato.rxSessione = 0, 0
    stato.epocaPartner = nil       -- la sua epoca si riscopre a ogni sessione
    stato.apertoDa = emu:read32(0x030022E0)
    stato.exitRoom = false
    stato.inRiapertura = false
    stato.congedo = false
    stato.cafeCodice, stato.cafeFrame = -1, -1000
    CLUB_IO_ID, CLUB_PARTNER_ID = 1, 0

    manda(T_CLUB, string.pack("<BI4", CLUB_ENTER, stato.epoca))
    mandaStato(ST_HANDSHAKE_RX)
    console:log(string.format(
        "[club ] il TUO gioco e' al Cable Club (epoca 0x%08X): aspetto l'amico",
        stato.epoca))
end

-- IL REGISTRO DEL CAVO VA ZITTITO, e serve solo da MASTER (ma costa niente
-- farlo sempre). Da master il gioco controlla il cavo VERO: `CheckSioErrored`
-- (cable_club.c:190) chiama `GetSioMultiSI`, che legge il bit SI di SIOCNT. In
-- mGBA quel bit dice SEMPRE "sono child" (misurato col banco hw/siolab), quindi
-- il gioco dichiarava errore di collegamento e chiudeva il club dopo un blocco:
-- due emulatori che si agganciavano e morivano subito.
--
-- La cura non e' forzare SI (non si puo': mGBA lo ricalcola), e' TOGLIERE IL
-- SIO DI MEZZO: RCNT in modo generico e SIOCNT azzerato. Cosi' SI legge 0, il
-- controllo passa, e nessuno se ne accorge - il cavo qui non lo usa nessuno,
-- il postino siamo noi.
-- Si scrive SOLO se serve. Riscrivere i registri del SIO a ogni frame significa
-- far cambiare modo all'emulatore sessanta volte al secondo: durante lo scambio
-- una delle due istanze si e' piantata, e questa era l'unica cosa che tocca
-- l'hardware emulato. Ora si guarda prima: se sono gia' a posto, non si tocca
-- niente (e il contatore dice quante volte e' servito davvero).
local zittiti = 0

-- IL VALORE GIUSTO DI SIOCNT, e non e' zero. Il gioco legge questo registro per
-- sapere CHI E': `GetMultiplayerId()` (link.c) ritorna i **bit 4-5 di SIOCNT**.
-- Azzerandolo, tutti e due i giocatori si credevano l'id 0, cioe' il capo dello
-- scambio - e in trade.c il capo non comunica la propria scelta, la mette da
-- solo (`SetReadyToTrade`: se l'id e' 1 manda READY_TO_TRADE, altrimenti no).
-- Risultato: due capi che aspettano ciascuno la mossa dell'altro, per sempre.
-- E' esattamente lo stallo con «Un momento... Attendi...» su tutti e due gli
-- schermi (2026-08-27).
--
-- Quindi qui dentro si scrive l'identita': l'id nei bit 4-5, SI (bit 2) alzato
-- solo se siamo lo slave - com'e' sul cavo vero - e SD (bit 3) alzato, che vuol
-- dire "collegamento buono". SI a 0 per il master serve anche a passare
-- `CheckSioErrored` (cable_club.c:190), che e' un controllo del solo master.
local function siocntAtteso()
    local v = (CLUB_IO_ID << 4) | 0x0008          -- id + SD
    if CLUB_IO_ID ~= 0 then v = v | 0x0004 end    -- SI: 1 = child
    return v
end

local function zittisciSio()
    local atteso = siocntAtteso()
    if emu:read16(REG_SIOCNT) ~= atteso then
        emu:write16(REG_RCNT, 0x8000)
        emu:write16(REG_SIOCNT, atteso)
        zittiti = zittiti + 1
    end
end

-- IL RUOLO, e non e' "sempre slave". Chi sta dall'altra parte decide tutto, e
-- la regola qui sotto e' lo SPECCHIO ESATTO di quella del sito
-- (`ruoloMaster` in web/js/club.js) e del pannello Python.
--
-- Le due scale sono INVERTITE, ed e' la cosa che si sbaglia:
--   il DEVICE master (il Pico che fa da master del cavo) rende il suo GBA lo
--   SLAVE del gioco; il device slave rende il suo GBA il MASTER.
-- Quindi: noi facciamo il master del gioco **se e solo se l'amico e' device
-- master**. La funzione qui sotto calcola il ruolo del DEVICE dell'amico con la
-- sua stessa formula, vista dalla sua parte.
--
-- Perche' non basta guardare "l'amico e' peer 1": il sito SORTEGGIA i peer
-- (47001 e simili). Con l'amico a 47001 e noi a 2, la sua regola lo fa device
-- master e noi - guardando solo il suo numero - ci saremmo dichiarati slave
-- come lui: due slave, e nessuno parla.
local function partnerEDeviceMaster(mio, suo)
    if suo == 1 then return true end
    if suo == 2 then return false end
    if mio == 1 then return false end
    if mio == 2 then return true end
    return suo < mio
end

local function decidiRuolo()
    local mio = chiPeer and chiPeer() or 0
    if not stato.partnerPeer then return false end
    if stato.partnerLua then
        -- Due emulatori: nessuno dei due ha un device, quindi vince il peer
        -- piu' basso, come fra due browser.
        return mio > 0 and mio < stato.partnerPeer
    end
    return partnerEDeviceMaster(mio, stato.partnerPeer)
end

local function collega()
    stato.master = decidiRuolo()
    if stato.master then
        CLUB_IO_ID, CLUB_PARTNER_ID = 0, 1
    else
        CLUB_IO_ID, CLUB_PARTNER_ID = 1, 0
    end

    -- Si salta la stretta di mano del cavo, che qui non puo' avvenire, e si
    -- scrive lo stato "collegati in due".
    emu:write8(ADDR_gLink + L_ISMASTER, stato.master and LINK_MASTER or 0)
    emu:write8(ADDR_gLink + L_LOCALID, CLUB_IO_ID)
    emu:write8(ADDR_gLink + L_PLAYERCNT, 2)
    emu:write8(ADDR_gLink + L_STATE, LINK_STATE_CONN_ESTABLISHED)
    emu:write8(ADDR_gLinkVSyncDis, 1)
    zittisciSio()
    stato.collegato = true

    mandaStato(ST_CONNECTED)
    console:log(string.format(
        "[club ] collegato: faccio io da cavo, il mio gioco e' %s%s",
        stato.master and "MASTER" or "SLAVE",
        stato.partnerPeer and (" (amico peer " .. stato.partnerPeer
                               .. (stato.partnerLua and ", in emulatore)" or ")")) or ""))
end

local function riapri()
    stato.riaperture = stato.riaperture + 1
    stato.inRiapertura = true
    stato.riapertoDa = emu:read32(0x030022E0)
    stato.collegato = false
    stato.txSessione = 0
    stato.rxSessione = 0
    mandaStato(ST_RECONNECTING)
    console:log("[club ] il gioco ha riaperto il link (macchina/lotta): "
                .. "la sessione continua, aspetto che torni su")
end

local function chiudi(motivo)
    if not stato.attivo then return end
    if stato.congedo then
        stato.congedoEsito = motivo:find("entro 6 s") and "scaduto" or "riuscito"
    end
    mandaStato(ST_CLOSED)
    manda(T_CLUB, string.pack("<BI4", CLUB_LEAVE, stato.epoca))
    stato.attivo = false
    stato.collegato = false
    stato.congedo = false
    -- Difesa in profondita': il partner muore con la sessione, sempre.
    stato.partnerPeer = nil
    stato.partnerLua = nil
    emu:write8(ADDR_gLinkVSyncDis, 0)
    emu:write16(REG_RCNT, 0x0000)      -- il SIO torna come l'abbiamo trovato
    console:log(string.format(
        "[club ] sessione chiusa (%s) | blocchi tx %d rx %d, dup %d, "
        .. "fuori ordine %d, richiesti %d, ENTER ri-annunciati %d",
        motivo, stato.tx, stato.rx, stato.dup, stato.fuoriOrdine,
        stato.richiesti, stato.riannunciEnter or 0))
end

-- L'amico se n'e' andato dalla porta. NON si stacca: si accompagna fuori
-- anche il nostro gioco, con la stessa danza del cavo vero (EXIT_ROOM, poi
-- l'eco di READY_CLOSE_LINK). Chiudera' lui il link, e a quel punto chiudiamo
-- anche noi - come farebbe il firmware.
local function congeda(motivo)
    if not stato.attivo or stato.congedo then return end
    if not stato.collegato then
        -- Il link non l'abbiamo mai scritto: non c'e' niente da accompagnare.
        chiudi(motivo)
        return
    end
    stato.congedo = true
    stato.congedi = stato.congedi + 1
    stato.congedoDa = emu:read32(0x030022E0)
    stato.congedoPorta = false
    console:log("[club ] " .. motivo .. ": accompagno fuori il gioco dalla "
                .. "porta della saletta (niente strappo al cavo)")
end

-- --- ricezione dalla rete ----------------------------------------------------
local function consegnaOrdinati()
    while stato.buffer[stato.attesoRx] do
        local b = stato.buffer[stato.attesoRx]
        stato.buffer[stato.attesoRx] = nil
        stato.attesoRx = (stato.attesoRx + 1) & 0xFFFFFFFF
        stato.codaPartner[#stato.codaPartner + 1] = cmdDaBlocco(b)
        stato.rx = stato.rx + 1
        stato.rxSessione = stato.rxSessione + 1
    end
end

function Club.riceviCorpo(body, peerId)
    if #body < 5 then return end
    -- SOLO a sessione viva: un T_CLUB che arriva mentre camminiamo (l'amico
    -- al bancone, o un rilancio zombie in volo) non deve pre-armare il gate
    -- dell'attesa della sessione DOPO - vedi il commento in annuncia().
    if peerId and stato.attivo then stato.partnerPeer = peerId end
    local sub, epoca = string.unpack("<BI4", body)

    if sub == CLUB_ENTER then
        -- Un ENTER con epoca nuova = l'amico e' ripartito: il nostro lato
        -- ricevente va azzerato subito, prima ancora dei suoi blocchi.
        if stato.epocaPartner ~= nil and stato.epocaPartner ~= epoca then
            stato.riagganci = stato.riagganci + 1
            stato.epocaPartner = epoca
            stato.attesoRx = 0
            stato.buffer = {}
        end
        if not stato.attivo then
            console:log("[club ] l'AMICO e' entrato al Cable Club: "
                        .. "vai dalla signorina anche tu")
        end
        return
    end

    if sub == CLUB_LEAVE then
        congeda("l'amico e' uscito dalla saletta")
        return
    end

    if sub == CLUB_STATUS and #body >= 9 then
        local _, _, sseq, status = string.unpack("<BI4I2I2", body)
        if #body >= 15 then
            local _, _, _, _, _, impronta = string.unpack("<BI4I2I2I2I4", body)
            if impronta == IMPRONTA_LUA then stato.partnerLua = true end
        end
        if status == ST_CLOSED then congeda("il gioco dell'amico ha chiuso il link") end
        return
    end

    if sub == CLUB_REQ and #body >= 6 then
        local n = string.byte(body, 6)
        for k = 0, n - 1 do
            local seq = string.unpack("<I4", body, 7 + k * 4)
            local b = stato.storia[seq]
            if b then
                stato.richiesti = stato.richiesti + 1
                manda(T_CLUB, string.pack("<BI4I4", CLUB_DATA, stato.epoca, seq) .. b)
            end
        end
        return
    end

    if sub == CLUB_DATA and #body >= 9 + 64 then
        local _, _, seq = string.unpack("<BI4I4", body)
        -- L'EPOCA. Lo scambio, a un certo punto, CHIUDE E RIAPRE il link
        -- (cable_club.c, CreateTask_ReestablishCableClubLink): dall'altra parte
        -- nasce una sessione nuova, con numeri di serie che ripartono da zero.
        -- Senza accorgersene, quei blocchi sembrano vecchissimi e si buttano
        -- tutti: lo scambio si pianta a schermo nero. E' lo stesso difetto che
        -- club_link.py ha risolto con le epoche il 2026-08-19.
        if stato.epocaPartner ~= epoca then
            if stato.epocaPartner ~= nil then
                stato.riagganci = stato.riagganci + 1
                console:log(("[club ] l'amico ha riaperto il link (epoca 0x%08X): "
                             .. "riaggancio in corsa"):format(epoca))
            end
            stato.epocaPartner = epoca
            stato.attesoRx = 0
            stato.buffer = {}
        end
        local blocco = string.sub(body, 10, 10 + 63)
        if not stato.attivo then
            -- L'amico e' gia' al bancone e noi non ancora: i suoi blocchi si
            -- buttano, ma si dice perche'.
            stato.persi = stato.persi + 1
            return
        end
        local delta = (seq - stato.attesoRx) & 0xFFFFFFFF
        if delta >= 0x80000000 or stato.buffer[seq] then
            stato.dup = stato.dup + 1
            return
        end
        if seq ~= stato.attesoRx then stato.fuoriOrdine = stato.fuoriOrdine + 1 end
        stato.buffer[seq] = blocco
        consegnaOrdinati()
        return
    end
end

-- --- il giro di ogni frame ---------------------------------------------------
function Club.tick()
    local linkState = emu:read8(ADDR_gLink + L_STATE)

    if not stato.attivo then
        -- Il gioco apre il club: va in handshake e aspetta il cavo. Si pretende
        -- ANCHE gLinkCallback acceso: allo schermo del titolo il gioco passa da
        -- solo per gli stati 0 e 1 del link, e un club aperto per sbaglio li'
        -- sarebbe difficilissimo da riconoscere dopo.
        if linkState == LINK_STATE_HANDSHAKE
           and emu:read32(ADDR_gLinkCallback) ~= 0 then
            -- Il club viaggia sul canale del RELAY (corpi T_CLUB). Col ponte
            -- TCP fra due emulatori quel canale non c'e', e prendere in mano il
            -- link significherebbe piantare il gioco in silenzio: meglio dirlo e
            -- lasciare che sia il gioco a lamentarsi del cavo, come farebbe.
            if puoiParlare and not puoiParlare() then
                if not stato.avvisato then
                    stato.avvisato = true
                    console:warn("[club ] il Cable Club funziona solo col relay "
                                 .. "(-LinkRole relay): con questo collegamento "
                                 .. "non posso fare da cavo")
                end
                return false
            end
            annuncia()
        end
        return false
    end

    if not stato.collegato then
        -- LA RIAPERTURA IN CORSO: si aspetta che il gioco torni su (riapre il
        -- link da solo: macchina degli scambi, lotta) PRIMA di rifare
        -- collega(), o si scriverebbe il link dentro una macchina a stati a
        -- meta' del guado.
        if stato.inRiapertura then
            local ora = emu:read32(0x030022E0)
            if linkState == LINK_STATE_HANDSHAKE
               and emu:read32(ADDR_gLinkCallback) ~= 0 then
                stato.inRiapertura = false
                stato.apertoDa = ora   -- il mezzo secondo di respiro riparte
                console:log("[club ] il gioco e' tornato su: riaggancio la sessione")
            elseif ora - stato.riapertoDa > 600 then
                chiudi("il gioco non ha riaperto il link entro 10 s")
                return false
            end
            return true
        end
        -- Si aspetta di sapere chi c'e' dall'altra parte, per scegliere il
        -- ruolo prima che lo scelga il gioco. Se il partner non si fa vivo
        -- entro ATTESA_MAX_FRAME si va da slave: e' il caso del GBA fisico che
        -- arriva al bancone con calma.
        local aspettato = emu:read32(0x030022E0) - stato.apertoDa
        -- Mezzo secondo di respiro PRIMA di scrivere il link, anche quando
        -- l'amico e' gia' li'. Alla riapertura di meta' scambio il gioco ha
        -- appena chiuso la sua sessione e sta rifacendo la sua macchina a
        -- stati: trovarsi il link gia' fatto nello stesso frame lo lascia a
        -- meta' del guado (schermo nero, contatori fermi).
        if aspettato < 30 then return true end
        if stato.partnerPeer or aspettato > ATTESA_MAX_FRAME then
            collega()
        elseif aspettato % 180 == 0 then
            -- Ri-annuncio ogni 3 s: il primo ENTER puo' essere arrivato quando
            -- l'amico non era ancora in stanza. Ogni 15 s si dice anche a
            -- schermo, cosi' chi guarda sa che non e' piantato.
            manda(T_CLUB, string.pack("<BI4", CLUB_ENTER, stato.epoca))
            stato.riannunciEnter = (stato.riannunciEnter or 0) + 1
            if aspettato % 900 == 0 then
                console:log(("[club ] aspetto l'amico al bancone da %d s"):format(aspettato // 60))
            end
        end
        return true
    end

    -- Il gioco ha chiuso il link. Se e' passata la PORTA (EXIT_ROOM) e' la
    -- fine vera; altrimenti e' la riapertura di meta' sessione (macchina
    -- degli scambi, lotta): la sessione CONTINUA, con la stessa epoca e gli
    -- stessi numeri di serie, come fa il firmware con LinkReconnecting.
    -- Il congedo non puo' durare per sempre: se il gioco non esce da solo
    -- entro 6 s (schermata insolita, dialogo aperto) si stacca comunque - ma
    -- avendoci provato, che e' la differenza fra uscire e andare in errore.
    if stato.congedo and (emu:read32(0x030022E0) - stato.congedoDa) > 360 then
        chiudi("l'amico e' uscito e il gioco non ha chiuso il link entro 6 s")
        return false
    end

    if linkState ~= LINK_STATE_CONN_ESTABLISHED
       and emu:read32(ADDR_gLinkCallback) == 0 then
        if stato.exitRoom or stato.congedo then
            chiudi("il gioco ha chiuso il link (porta della saletta)")
            return false
        end
        riapri()
        return true
    end

    -- Gli errori vanno tenuti puliti: LinkMain1 li impacchetta in gLinkStatus e
    -- il gioco, vedendoli, mostra la schermata di errore del cavo.
    emu:write8(ADDR_gLink + L_HWERROR, 0)
    emu:write8(ADDR_gLink + L_BADCHECKSUM, 0)
    emu:write8(ADDR_gLink + L_QUEUEFULL, 0)
    emu:write8(ADDR_gLink + L_LAG, 0)
    emu:write8(ADDR_gLinkVSyncDis, 1)
    zittisciSio()

    -- IL RIPIEGO DEL MASTER, e vale a OGNI sessione. Il master del gioco apre
    -- le danze con SEND_LINK_TYPE (LinkCB_RequestPlayerDataExchange), ma quel
    -- callback gira UNA VOLTA SOLA, appena il link risulta stabilito: se in
    -- quell'istante il ruolo non era ancora scritto, non parte piu' nessuno e
    -- restano tutti e due a guardarsi. Succede alla RIAPERTURA che lo scambio fa
    -- a meta' strada (CreateTask_ReestablishCableClubLink): misurato, schermo
    -- nero e contatori fermi a meta' partita.
    -- Quindi: se siamo il master e dopo due secondi in questa sessione non e'
    -- passato niente, il comando lo mettiamo noi - in rete e nella nostra eco,
    -- che e' esattamente cio' che avrebbe fatto il gioco.
    if stato.master and stato.txSessione == 0 and stato.rxSessione == 0
       and (emu:read32(0x030022E0) - stato.apertoDa) > 120 then
        local apertura = { LINKCMD_SEND_LINK_TYPE, LINKTYPE_TRADE_SETUP }
        stato.aperture = stato.aperture + 1
        mandaBlocco(bloccoDaCmd(apertura))
        spingiRecv(nil, apertura)
        console:log("[club ] apro io le danze (SEND_LINK_TYPE): il gioco non "
                    .. "l'ha fatto da solo")
    end

    -- UN GIRO DI TRASFERIMENTO PER FRAME, e dev'essere UNO: il gioco ne consuma
    -- esattamente uno per frame (DequeueRecvCmds da LinkMain1). Spingendo il
    -- nostro eco e il comando dell'amico come due giri distinti la coda cresceva
    -- di un giro al secondo - misurato: 378 giri di arretrato in sei secondi,
    -- cioe' lo scambio che va a rilento e poi si impianta. Si accoppiano, come
    -- sul cavo vero, dove in un giro passano tutti insieme.
    local nostro = nil
    local count = emu:read8(ADDR_gLink + SENDQ_COUNT)
    if count > 0 then
        local pos = emu:read8(ADDR_gLink + SENDQ_POS)
        nostro = leggiSend(pos)
        consumaSend(pos, count)
        mandaBlocco(bloccoDaCmd(nostro))
        -- La PORTA della saletta: e' l'unica chiusura vera (usbSection.cpp).
        if nostro[1] == 0xCAFE and nostro[2] == LINK_KEY_EXIT_ROOM then
            stato.exitRoom = true
        end
    end

    -- IL CONGEDO: l'amico non c'e' piu', ma il nostro gioco deve uscire dalla
    -- porta come se ci fosse. Le sue risposte le mettiamo noi, nell'ordine
    -- esatto del cavo vero: prima la PORTA, poi l'ECO di READY_CLOSE_LINK
    -- quando il gioco lo chiede. Da li' il gioco chiude il link da solo e la
    -- sessione finisce dalla porta principale (vedi `chiudi`, sopra).
    local dellAmico = table.remove(stato.codaPartner, 1)
    if stato.congedo and not dellAmico then
        if not stato.congedoPorta then
            dellAmico = { LINKCMD_HELD_KEYS, LINK_KEY_EXIT_ROOM }
            stato.congedoPorta = true
        elseif nostro and nostro[1] == LINKCMD_READY_CLOSE_LINK then
            -- Il gioco e' pronto a chiudere: gli si risponde di si', ed e'
            -- ESATTAMENTE la condizione che il firmware aspetta per uscire
            -- dal suo giro (partnerReadyCloseLink && readyCloseLink).
            dellAmico = { LINKCMD_READY_CLOSE_LINK }
        else
            -- Nel frattempo: tasto premuto nullo, come un partner fermo.
            dellAmico = { LINKCMD_HELD_KEYS, LINK_KEY_IDLE }
        end
    end
    -- Il filtro dei tasti (vedi `cafeCodice` in `stato`): un codice di tasto
    -- identico al precedente entro 15 frame e' lo stesso gesto campionato due
    -- volte dal cavo lento del Pico - eseguito due volte fa il passo doppio.
    if dellAmico and dellAmico[1] == 0xCAFE then
        local codice = dellAmico[2]
        if codice == LINK_KEY_EXIT_ROOM then
            stato.exitRoom = true
        elseif codice ~= 0 and codice ~= LINK_KEY_IDLE then
            local ora = emu:read32(0x030022E0)
            if codice == stato.cafeCodice and (ora - stato.cafeFrame) < 15 then
                stato.cafeSoppressi = stato.cafeSoppressi + 1
                dellAmico = nil
            else
                stato.cafeCodice = codice
                stato.cafeFrame = ora
            end
        else
            stato.cafeCodice = codice   -- tasto rilasciato: il gesto e' finito
        end
    end
    if nostro or dellAmico then spingiRecv(dellAmico, nostro) end

    return true
end

function Club.attivo() return stato.attivo end

function Club.riga()
    if stato.sessioni == 0 then return nil end
    return string.format(
        "[club ] sessioni %d | %s | blocchi tx %d rx %d | dup %d fuori-ordine %d "
        .. "richiesti %d | in attesa %d",
        stato.sessioni,
        (not stato.attivo) and "chiuso"
            or (stato.collegato and (stato.master and "MASTER" or "slave") or "aspetto l'amico"),
        stato.tx, stato.rx, stato.dup, stato.fuoriOrdine,
        stato.richiesti, #stato.codaPartner)
        .. (" | riagganci %d | aperture mie %d | riaperture %d | tasti doppi filtrati %d | congedi %d%s | sio zittito %d volte")
           :format(stato.riagganci, stato.aperture, stato.riaperture, stato.cafeSoppressi,
                   stato.congedi,
                   stato.congedoEsito and (" (" .. stato.congedoEsito .. ")") or "",
                   zittiti)
end

function Club.collega(fnManda, fnPeer, fnCanale)
    manda = fnManda
    chiPeer = fnPeer
    puoiParlare = fnCanale
end

-- ATTENZIONE: niente `return` qui. Questo file viene CONCATENATO dentro lo
-- script generato (build.ps1), e un return in mezzo chiuderebbe il chunk -
-- cioe' spegnerebbe in silenzio meta' dell'iniettore. Si espone come globale.
ClubLua = Club
