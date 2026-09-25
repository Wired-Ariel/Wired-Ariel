-- =============================================================================
-- inject_body.lua — iniettore + ponte di rete per mGBA (testato su 0.10.5)
-- =============================================================================
--
-- IL LOG SU FILE (2026-08-27). Su PC la console di mGBA basta e avanza; su un
-- palmare (TrimUI Brick con Knulli, l'ambiente in cui gira davvero il ruolo
-- "relay") la finestra di scripting NON c'e', e senza un file resta solo
-- "non funziona". Quindi ogni riga che finisce nella console finisce anche in
-- `gen3-poke-multiplayer.log`, accanto allo script.
--
-- Due scelte che vanno tenute come sono:
--   * la console NON viene mai filtrata. Il rapporto periodico e i suoi
--     contatori sono lo strumento di diagnosi del progetto ("un contatore che
--     resta a zero e' un difetto da indagare"): silenziarli per fare un file
--     piu' pulito significherebbe spegnere lo strumento.
--   * nel FILE, invece, il rapporto periodico si scrive solo con
--     GEN3PM_DEBUG = true, e comunque uno ogni cinque. Un rapporto al
--     secondo per un'ora sono decine di MB sulla microSD. Avvisi ed errori
--     passano sempre, DEBUG o no.
-- =============================================================================

-- Un solo local per tutti e tre: il chunk principale di questo script e'
-- vicino al tetto dei 200 registri locali di Lua, e sfondarlo in mGBA si
-- presenta come silenzio totale (la build lo controlla, vedi check_lua.py).
local LOG = { inizio = function() end, fine = function() end, riga = function() end }
do local function installa()   -- UNA FUNZIONE, non un semplice `do`:
-- i local di un blocco `do` restano registri del CHUNK PRINCIPALE, che
-- qui e' al tetto dei 200 di Lua (oltre, mGBA non da' un errore: da'
-- silenzio). Quelli di una funzione no. Verificato il 2026-08-27.
    local OGNI          = 5      -- un rapporto completo su cinque
    local DEBUG_ATTIVO  = rawget(_G, "GEN3PM_DEBUG") == true
    local consoleVera   = console
    local file, percorso
    local numero, dentroRapporto, tengoRapporto = 0, false, false

    local percorsi = {}
    if type(script) == "table" then
        local dir = script.dir or script.path
        if type(dir) == "string" and dir ~= "" then
            -- Alcune build espongono la cartella, altre il percorso del .lua.
            percorsi[#percorsi + 1] = dir:gsub("[\\/][^\\/]*%.lua$", "") .. "/gen3-poke-multiplayer.log"
        end
    end
    percorsi[#percorsi + 1] = "gen3-poke-multiplayer.log"

    if type(io) == "table" and type(io.open) == "function" then
        for _, p in ipairs(percorsi) do
            file = io.open(p, "w")
            if file then percorso = p; break end
        end
    end
    if file then pcall(function() file:setvbuf("line") end) end

    local function orologio()
        if type(os) == "table" and type(os.date) == "function" then
            local ok, v = pcall(os.date, "%H:%M:%S")
            if ok and type(v) == "string" then return v end
        end
        return "--:--:--"
    end

    -- "[rete ] collegato" -> categoria "RETE", corpo "collegato". Senza
    -- parentesi quadre la riga vale LUA: nessuna riga si perde.
    local function pezzi(testo)
        local cat, corpo = string.match(testo, "^%[([^%]]+)%]%s*(.*)$")
        if not cat then return "LUA", testo end
        cat = string.upper((string.gsub(cat, "%s+", "")))
        return (cat ~= "" and cat or "LUA"), corpo
    end

    local ETICHETTE = { log = "INFO", warn = "WARN", error = "ERROR" }

    local function scrivi(livello, messaggio)
        local testo = tostring(messaggio)
        -- LA CONSOLE PRIMA E SEMPRE: il file e' un di piu', non un filtro.
        if consoleVera then
            local f = consoleVera[livello]
            if type(f) == "function" then pcall(f, consoleVera, testo) end
        end
        if not file then return end
        local cat, corpo = pezzi(testo)
        -- Dentro un rapporto le righe informative sono diagnostica: nel file
        -- ci vanno solo a DEBUG acceso e solo nel campione tenuto. I cambi
        -- mappa e tutto cio' che non e' "log" non si filtrano mai.
        if dentroRapporto and livello == "log"
            and not (tengoRapporto or cat == "MAPPA") then
            return
        end
        local ok, err = pcall(function()
            if dentroRapporto and tengoRapporto and livello == "log" then
                file:write(string.format("  %-8s | %s\n", cat, corpo))
            else
                file:write(string.format("%s  %-5s  %-8s  %s\n", orologio(),
                    ETICHETTE[livello] or string.upper(livello), cat, corpo))
            end
            file:flush()
        end)
        if not ok then
            file = nil
            if consoleVera then
                pcall(consoleVera.error, consoleVera,
                      "[log   ] scrittura fallita, proseguo senza file: " .. tostring(err))
            end
        end
    end

    console = {
        log   = function(_, m) scrivi("log", m) end,
        warn  = function(_, m) scrivi("warn", m) end,
        error = function(_, m) scrivi("error", m) end,
        createBuffer = function(_, nome)
            if consoleVera and type(consoleVera.createBuffer) == "function" then
                return consoleVera:createBuffer(nome)
            end
            return nil
        end,
    }

    LOG.inizio = function()
        numero = numero + 1
        dentroRapporto = true
        tengoRapporto = DEBUG_ATTIVO and (numero == 1 or numero % OGNI == 0)
        if file and tengoRapporto then
            pcall(function()
                file:write(string.format(
                    "\n--------- RAPPORTO #%04d | %s ---------\n", numero, orologio()))
            end)
        end
    end

    LOG.fine = function()
        if file and tengoRapporto then
            pcall(function() file:write("-------------------------------------------\n") end)
        end
        dentroRapporto, tengoRapporto = false, false
    end

    -- Una riga nel FILE soltanto: serve per la configurazione attiva, che a
    -- schermo c'e' gia' e nel file va messa in evidenza.
    LOG.riga = function(testo)
        if not file then return end
        pcall(function() file:write(testo .. "\n"); file:flush() end)
    end

    if file then
        pcall(function()
            local quando = "data non disponibile"
            if type(os) == "table" and type(os.date) == "function" then
                local ok, v = pcall(os.date, "%Y-%m-%d %H:%M:%S")
                if ok then quando = v end
            end
            file:write("\239\187\191")   -- BOM UTF-8, per gli editor Windows
            file:write("===============================================\n")
            file:write(" GEN3-POKE-MULTIPLAYER - SESSIONE " .. quando .. "\n")
            file:write(" Log   : " .. percorso .. "\n")
            file:write(string.format(" Debug : %s (un rapporto ogni %d nel file)\n",
                DEBUG_ATTIVO and "ATTIVO" or "spento - GEN3PM_DEBUG = true per accenderlo",
                OGNI))
            file:write("===============================================\n\n")
            file:flush()
        end)
    end
end installa() end

--
-- Non si carica da solo: build.ps1 lo concatena con l'intestazione generata
-- (PAYLOAD_BASE, PAYLOAD_RESERVED, PAYLOAD_BYTES, LINK_ROLE, LINK_HOST,
-- LINK_PORT) producendo `inject.generated.lua`, che è il file da aprire in mGBA
-- con Tools > Scripting > Load script.
--
-- I byte del payload sono incorporati nello script apposta: l'ambiente Lua di
-- mGBA è ristretto e non garantisce l'accesso ai file.
--
-- Questo iniettore è il sostituto dell'ACE per tutte le fasi in emulatore, e il
-- sostituto dell'adattatore Celio per il trasporto: in Fase 5 entrambi i ruoli
-- passano all'hardware, ma il payload non cambia perché il punto di contatto
-- resta la mailbox.
--
-- Modalità (LINK_ROLE):
--   loopback  i nostri eventi rientrano da noi con un ritardo: si prova tutta
--             la catena RX con UN SOLO emulatore. Da usare per primo.
--   server    apre una porta e aspetta l'altra istanza
--   client    si collega a LINK_HOST:LINK_PORT
--   off       solo diagnostica, nessuna rete

local IRQ_VECTOR   = 0x03007FFC
local OFF_ORIG     = 0x04          -- g_origHandler
local OFF_MAGIC    = 0x08          -- g_magic  = 'HOOK'
local OFF_STATE    = 0x10          -- struct PayloadState
-- Spostata da 0x100 a 0x200 il 2026-07-30: PayloadState era a 12 byte dal muro.
-- Il valore vive in tre posti (payload.ld, build.ps1, qui) e devono concordare.
-- struct Mailbox. NON piu' una costante scritta qui: dal 2026-08-02 la mailbox
-- segue PayloadState nel linker script, e build.ps1 legge l'offset vero
-- dall'ELF e lo emette in testa a questo file come PAYLOAD_OFF_MAILBOX. Il
-- fallback a 0x200 copre solo gli script generati prima di quel cambio: se un
-- giorno sparisce, si tolga.
local OFF_MAILBOX  = PAYLOAD_OFF_MAILBOX or 0x200
local MAGIC_HOOK   = 0x4B4F4F48
local MAGIC_STATE  = 0x53544154    -- 'STAT'
local MAGIC_MBOX   = 0x584F424D    -- 'MBOX'

local EVENT_SIZE   = 12
-- struct Mailbox: magic, txHead, txTail, txSlots, rxHead, rxTail, rxSlots,
-- rxApplied, poi tx[txSlots] e rx[rxSlots]. L'inizio di rx[] NON e' un offset
-- fisso: dipende da txSlots (0x20 + txSlots*12), che si legge dalla mailbox
-- stessa. Era cablato a 0xE0 e il 2026-08-02, riducendo gli slot da 16 a 12,
-- sarebbe diventato una scrittura dentro tx[] senza nessun errore a schermo.
local MB_TXHEAD    = 0x04
local MB_TXTAIL    = 0x08
local MB_TXSLOTS   = 0x0C
local MB_RXHEAD    = 0x10
local MB_RXTAIL    = 0x14
local MB_RXSLOTS   = 0x18
local MB_TX        = 0x20

-- Contatore di VBlank del gioco: gMain + 0x20 (include/main.h:22).
-- Indirizzo dalla build matching USA. È l'unico orologio affidabile: il
-- callback "frame" di mGBA scatta da 3 a 5 volte per frame emulato, in modo
-- variabile (misurato il 2026-07-29).
local ADDR_GMAIN_VBLANK1 = 0x030022E0

-- Impronta della ROM per cui game_syms.h e' stato generato: build matching di
-- pokeemerald, sha1 f3ae088181bf583e55daf962a92bb46f4f1d07b7.
-- Il codice nell'header dice la localizzazione (BPEE = USA), ma da solo non
-- basta: si controlla anche che a CB2_Overworld ci sia davvero il prologo di
-- quella funzione. Su qualunque altra ROM - a cominciare da Smeraldo ITALIANO -
-- ogni indirizzo del payload punta altrove e non funziona NIENTE.
--
-- I valori sotto sono quelli USA e restano il default. Con `build.ps1 -Syms it`
-- l'intestazione generata definisce ROM_GAMECODE / ROM_CB2_OVERWORLD /
-- ROM_CB2_WORD dal profilo ritrovato sulla cartuccia italiana (BPEI), e vincono
-- loro. Senza questo l'iniettore rifiuterebbe proprio la ROM per cui il payload
-- e' stato compilato.
--
-- Da sapere: il PROLOGO di CB2_Overworld e' identico nelle due ROM (0x4809B510),
-- perche' e' codice puro. Quindi da solo non distingue una localizzazione
-- dall'altra: quello che le distingue e' il gamecode, e il prologo serve a
-- confermare che a quell'indirizzo ci sia davvero la funzione giusta.
local ADDR_ROM_GAMECODE  = 0x080000AC  -- 4 byte ASCII
local EXPECT_GAMECODE    = ROM_GAMECODE or "BPEE"
local ADDR_CB2_OVERWORLD = ROM_CB2_OVERWORLD or 0x08085E5C
local EXPECT_CB2_WORD    = ROM_CB2_WORD or 0x4809B510  -- prime 4 byte di CB2_Overworld

-- Il payload si aggancia anche a gMain.callback1 (gMain + 0x00) per togliere il
-- tasto A davanti al remoto. Serve qui per DISINNESCARE l'hook prima di azzerare
-- la regione: ricaricare lo script con callback1 che punta dentro il payload
-- farebbe saltare il gioco su memoria a zero al frame successivo.
-- gMain sta allo stesso indirizzo in USA e in italiano: la RAM la assegna il
-- linker dalle dimensioni delle struct, che i testi non cambiano (verificato,
-- 8 dati su 8 con delta zero - vedi build/port_report.md).
local ADDR_GMAIN_CB1     = 0x030022C0
local ADDR_CB1_OVERWORLD = ROM_CB1_OVERWORLD or 0x08085E05  -- bit Thumb incluso
local ST_ORIG_CB1        = 0xB8        -- offset di origCallback1 in PayloadState

local base     = PAYLOAD_BASE
local payload  = PAYLOAD_BYTES
local reserved = PAYLOAD_RESERVED

local injected     = false
local reinjections = 0
local lastVblank   = 0
-- Ingressi TOTALI dell'hook (ogni IRQ, non solo VBlank). Serve a leggere le
-- raffiche: se in un rapporto `vblank noi` sale di migliaia, il confronto con
-- `irq` dice se l'hook e' entrato migliaia di volte (tempesta di IRQ vera) o se
-- e' entrato 60 volte e ha visto il bit VBlank sempre alzato (ack in ritardo).
local lastIrq      = 0

local VBLANK_PER_STATUS = 60
local lastStatusVblank  = nil
local statusVblankDelta = 0
local outOfFieldStreak  = 0
-- Lo scostamento fra dove il gioco DISEGNA il remoto e dove l'amico DICE di
-- essere. Il difetto del 2026-07-30 era visibile in venti righe di log di fila e
-- nessuno lo stava cercando: da qui in poi lo dice il log.
local driftStreak       = 0
-- Da quanti rapporti di fila il possesso della porta seriale (prese - mollate)
-- non coincide con l'overworld: la trattenuta e' il difetto della Torre Lotta.
local sioOwnStreak      = 0
local lastMapChanges    = nil
-- Il massimo di `settleFramesLast` osservato nella sessione: e' l'unico modo di
-- accorgersi che il gate di assestamento non ha mai intercettato una finestra
-- di transizione vera. Vedi la riga [assesta].
local maxSettleFrames   = 0
-- bodyTicks all'ultimo rapporto: se in overworld non avanza, il corpo del
-- payload (OverworldTick, dal main loop) e' fermo - hook su callback1 perso.
local lastBodyTicks     = nil

-- =============================================================================
-- Iniezione
-- =============================================================================

local function zeroRegion()
    for i = 0, reserved - 1 do
        emu:write8(base + i, 0)
    end
end

local function writePayload()
    for i = 1, #payload do
        emu:write8(base + i - 1, string.byte(payload, i))
    end
end

local romChecked   = false
local lastBootVbl  = nil
local waitLogged   = false

-- E' la ROM giusta? Tutti gli indirizzi del payload vengono dal .map della build
-- matching di pokeemerald: su qualunque altra ROM puntano altrove. Il caso che si
-- e' presentato davvero (2026-07-30) e' Smeraldo ITALIANO in una finestra e la
-- build nell'altra: sull'italiana l'iniezione riesce, il gioco gira, e il payload
-- resta muto per sempre perche' confronta gMain.callback2 con l'indirizzo USA di
-- CB2_Overworld. Un errore esplicito vale mille minuti di log identici.
local function romMatches()
    local code = ""
    for i = 0, 3 do
        code = code .. string.char(emu:read8(ADDR_ROM_GAMECODE + i))
    end
    local cb2 = emu:read32(ADDR_CB2_OVERWORLD)

    if code == EXPECT_GAMECODE and cb2 == EXPECT_CB2_WORD then
        return true
    end

    console:error(string.format(
        "[inject] ROM SBAGLIATA: codice header '%s' (atteso '%s'), parola a CB2_Overworld "
        .. "0x%08X (attesa 0x%08X).", code, EXPECT_GAMECODE, cb2, EXPECT_CB2_WORD))
    console:error("[inject] gli indirizzi del payload valgono SOLO per la build matching di "
        .. "pokeemerald (BPEE, sha1 f3ae0881...). Su Smeraldo italiano o su qualsiasi altra "
        .. "ROM commerciale non funziona niente: le fasi in emulatore vanno fatte con la ROM "
        .. "che abbiamo compilato noi, su ENTRAMBE le istanze.")
    return false
end

-- =============================================================================
-- W''-4: quanto stack usa davvero il payload
-- =============================================================================
--
-- IL RISCHIO, COM'ERA. hook.S passa a uno stack NOSTRO dentro il payload, e
-- fino al 2026-08-21 da li' payload_frame() chiamava routine del GIOCO (spawn
-- di object event, field effect, movimento), scritte aspettandosi i ~1428 byte
-- che il gioco ha fra sp_sys e i propri dati: una sola che ne usasse piu' dello
-- stack nostro lo avrebbe sfondato, corrompendo il payload dall'interno.
--
-- COM'E' DAL 2026-08-23. Il corpo gira in payload_cb1 sullo stack del gioco;
-- sullo stack privato resta SOLO il percorso IRQ, che non chiama nessuna
-- funzione del gioco, e build.ps1 ne calcola il LIMITE statico dal grafo delle
-- chiamate (-fstack-usage): 112 byte su 320 (0x140 in payload.ld), con la
-- build che fallisce sopra la meta' o se nell'IRQ compare una chiamata
-- indiretta. Questa misura resta come CONTROPROVA sul campo, con lo stesso
-- criterio della meta': se il PEGGIO qui supera il limite calcolato, il
-- calcolo o l'ipotesi "niente gioco dall'IRQ" sono falsi, e va capito quale.
--
-- LA MISURA. Si riempie lo stack di un motivo riconoscibile al momento
-- dell'iniezione, e si guarda fin dove viene consumato. E' sicura per
-- costruzione: la memoria e' nostra, non del gioco.
--
-- PERCHE' QUI E NON NEL PAYLOAD. Il riempimento lo fa il caricatore e il calcolo
-- lo fa questo script: cosi' la misura non costa un byte della coda di EWRAM,
-- che e' la risorsa scarsa.
--
-- Il motivo dipende dall'indirizzo, come in ewram_probe.lua: una COPIA di un
-- blocco altrove non passerebbe per intatta.
local STACK_LO = PAYLOAD_STACK_BOTTOM
local STACK_HI = PAYLOAD_STACK_TOP

local stackMin = nil

local function stackSeed(addr)
    return ((addr // 4) * 2654435761) & 0xFFFFFFFF
end

local function fillStackPattern()
    if not STACK_LO or not STACK_HI then return end
    for a = STACK_LO, STACK_HI - 4, 4 do
        emu:write32(a, stackSeed(a))
    end
    stackMin = nil
end

-- Ritorna (usati, liberi, totale) oppure nil se lo script e' vecchio.
local function stackUsage()
    if not STACK_LO or not STACK_HI then return nil end
    local a = STACK_LO
    while a < STACK_HI and emu:read32(a) == stackSeed(a) do
        a = a + 4
    end
    local free = a - STACK_LO
    if stackMin == nil or free < stackMin then stackMin = free end
    return STACK_HI - a, stackMin, STACK_HI - STACK_LO
end

-- Il payload con il driver SIO dentro NON va iniettato in emulatore. SioInit()
-- riconfigura REG_RCNT/REG_SIOCNT/REG_IE in Multi-Player con IRQ, e in mGBA non
-- c'e' nessun partner: il codice link del gioco - che gira a ogni VBlank via
-- LinkVSync - si trova la porta seriale mossa sotto i piedi. Il 2026-08-02 il
-- gioco si e' piantato alla Torre Lotta esattamente cosi', perche' la build
-- -WithSio aveva sovrascritto questo file.
local function refuseIfSioBuild()
    if not PAYLOAD_HAS_SIO then return false end
    -- Scavalco ESPLICITO: build.ps1 -WithSio -AllowSioInEmu. Serve a UNA cosa:
    -- eseguire in emulatore, dove c'e' il log, il prendi/molla della porta
    -- seriale (SioInit/SioShutdown legati all'overworld) PRIMA che la sua prima
    -- esecuzione avvenga sul GBA fisico, dove non c'e' niente. Il rischio che la
    -- guardia para resta reale: senza partner il gioco puo' piantarsi dove arma
    -- la macchina del link (Torre Lotta, 2026-08-02) - ma adesso e' il
    -- comportamento SOTTO ESAME, non un incidente. Criterio: [sio] prese-mollate
    -- coerente con l'overworld, e la Torre Lotta rigiocata senza blocco.
    if PAYLOAD_SIO_EMU_OK then
        console:warn("[inject] build -WithSio in EMULATORE per scelta esplicita (-AllowSioInEmu).")
        console:warn("[inject] Niente partner sul SIO: e' la prova del prendi/molla della porta.")
        console:warn("[inject] Se il gioco si pianta fuori dall'overworld, guarda [sio] prese/mollate.")
        return false
    end
    console:error("[inject] QUESTO PAYLOAD HA IL DRIVER SIO DENTRO: non si inietta in emulatore.")
    console:error("[inject] SioInit() prende la porta seriale, e qui non c'e' nessun partner.")
    console:error("[inject] Ricompila senza -WithSio:  .\\build.ps1 -Syms it")
    console:error("[inject] La build -WithSio serve all'HARDWARE, dove il partner e' il Celio.")
    console:error("[inject] Per la prova del prendi/molla in emulatore: -WithSio -AllowSioInEmu.")
    return true
end

local function inject()
    if refuseIfSioBuild() then return "stop" end

    local orig = emu:read32(IRQ_VECTOR)

    if orig >= base and orig < base + reserved then
        console:log("[inject] il vettore punta già dentro il payload, salto")
        return false
    end

    if not romChecked then
        if not romMatches() then
            return "stop"   -- inutile riprovare: la ROM non cambia da sola
        end
        romChecked = true
    end

    -- Si aspetta che il GIOCO sia davvero avviato, cioe' che il suo contatore di
    -- VBlank stia avanzando. E' un test POSITIVO: non prova a indovinare dove
    -- debba stare l'handler (il primo tentativo pretendeva che il vettore fosse
    -- in IWRAM, e bloccava all'infinito), verifica che il gioco giri.
    local vbl = emu:read32(ADDR_GMAIN_VBLANK1)
    if lastBootVbl == nil or vbl <= lastBootVbl then
        if not waitLogged then
            console:log("[inject] aspetto che il gioco parta (contatore VBlank fermo)")
            waitLogged = true
        end
        lastBootVbl = vbl
        return false
    end

    -- Disinnescare l'hook su callback1 PRIMA di azzerare, non dopo.
    local cb1 = emu:read32(ADDR_GMAIN_CB1)
    if cb1 >= base and cb1 < base + reserved then
        local saved = emu:read32(base + OFF_STATE + ST_ORIG_CB1)
        -- Se anche l'originale salvato cade dentro il payload, lo stato e' gia'
        -- stato azzerato da un giro precedente: si rimette CB1_Overworld.
        if saved < base or saved >= base + reserved then
            emu:write32(ADDR_GMAIN_CB1, saved)
            console:log(string.format(
                "[inject] hook su callback1 rimosso, ripristinato 0x%08X", saved))
        else
            emu:write32(ADDR_GMAIN_CB1, ADDR_CB1_OVERWORLD)
            console:warn(string.format(
                "[inject] callback1 puntava nel payload e l'originale non era leggibile: "
                .. "rimesso CB1_Overworld (0x%08X)", ADDR_CB1_OVERWORLD))
        end
    end

    zeroRegion()
    writePayload()
    fillStackPattern()
    emu:write32(base + OFF_ORIG, orig)
    emu:write32(IRQ_VECTOR, base)

    local magic = emu:read32(base + OFF_MAGIC)
    if magic ~= MAGIC_HOOK then
        console:error(string.format(
            "[inject] magic sbagliato dopo la scrittura: 0x%08X (atteso 0x%08X)",
            magic, MAGIC_HOOK))
        return false
    end

    console:log(string.format(
        "[inject] payload a 0x%08X (%d byte), handler originale 0x%08X",
        base, #payload, orig))
    return true
end

-- =============================================================================
-- Mailbox
-- =============================================================================

local function mbox() return base + OFF_MAILBOX end

local function readS16(addr)
    local v = emu:read16(addr)
    if v >= 0x8000 then return v - 0x10000 end
    return v
end

-- Un evento è 12 byte crudi: si passano sul socket così come sono, senza
-- serializzazione. Il formato è già compatto e allineato al protocollo.
local function readEventRaw(addr)
    local bytes = {}
    for i = 0, EVENT_SIZE - 1 do
        bytes[i + 1] = string.char(emu:read8(addr + i))
    end
    return table.concat(bytes)
end

local function writeEventRaw(addr, raw)
    for i = 1, EVENT_SIZE do
        emu:write8(addr + i - 1, string.byte(raw, i))
    end
end

local function describeEvent(raw)
    local DIR   = { [0] = "-", [1] = "giu", [2] = "su", [3] = "sx", [4] = "dx" }
    local SPEED = { [0] = "cammina", [1] = "corre", [2] = "bici", [3] = "surf" }
    local TYPE  = { [1] = "PASSO", [2] = "SYNC", [3] = "GIRA", [4] = "VIA" }

    -- Il nibble alto del tipo e' lo SLOT del mittente (fino a 4 giocatori,
    -- 2026-08-25): sugli eventi in ARRIVO dal client c'e', su quelli in
    -- USCITA dal gioco e' zero. Si separa per non leggere "?17" al posto di
    -- "PASSO slot 1".
    local t     = string.byte(raw, 1)
    local slot  = math.floor(t / 16) % 4
    t = t % 16
    local dir   = string.byte(raw, 2)
    local speed = string.byte(raw, 3)
    local seq   = string.byte(raw, 4)
    local mapG  = string.byte(raw, 5)
    local mapN  = string.byte(raw, 6)
    local x     = string.byte(raw, 7) + string.byte(raw, 8) * 256
    local y     = string.byte(raw, 9) + string.byte(raw, 10) * 256
    if x >= 0x8000 then x = x - 0x10000 end
    if y >= 0x8000 then y = y - 0x10000 end

    return string.format("#%3d %-5s%s mappa %d.%d (%d,%d) %s %s",
        seq, TYPE[t] or ("?" .. t),
        (slot > 0) and (" s" .. slot) or "",
        mapG, mapN, x, y,
        DIR[dir] or "?", SPEED[speed] or "?")
end

-- Svuota la coda TX del payload e restituisce gli eventi grezzi.
local function drainTx()
    local m = mbox()
    local out = {}

    if emu:read32(m) ~= MAGIC_MBOX then
        console:error("[coda ] magic mailbox errato: payload non allineato allo script")
        return out
    end

    local head  = emu:read32(m + MB_TXHEAD)
    local tail  = emu:read32(m + MB_TXTAIL)
    local slots = emu:read32(m + MB_TXSLOTS)
    if slots == 0 then return out end

    while tail ~= head do
        out[#out + 1] = readEventRaw(m + MB_TX + tail * EVENT_SIZE)
        tail = (tail + 1) % slots
    end

    emu:write32(m + MB_TXTAIL, tail)
    return out
end

local rxDropped = 0

-- Consegna un evento al payload. Se la coda RX è piena si scarta: meglio
-- perdere un passo vecchio che bloccare il flusso.
local function deliverRx(raw)
    local m = mbox()
    local head  = emu:read32(m + MB_RXHEAD)
    local tail  = emu:read32(m + MB_RXTAIL)
    local slots = emu:read32(m + MB_RXSLOTS)
    if slots == 0 then return false end

    local next = (head + 1) % slots
    if next == tail then
        rxDropped = rxDropped + 1
        return false
    end

    local rxBase = MB_TX + emu:read32(m + MB_TXSLOTS) * EVENT_SIZE
    writeEventRaw(m + rxBase + head * EVENT_SIZE, raw)
    emu:write32(m + MB_RXHEAD, next)
    return true
end

-- =============================================================================
-- Rete
-- =============================================================================

local link = {
    role = LINK_ROLE,
    sock = nil,        -- socket verso il peer
    listener = nil,
    connected = false,
    sent = 0,
    received = 0,
    inbox = "",        -- byte ricevuti non ancora divisi in eventi
}

-- In loopback gli eventi rientrano dopo un po' di VBlank, per simulare la
-- latenza e verificare che il remoto non teletrasporti.
local LOOPBACK_DELAY_VBLANK = 45
local loopbackQueue = {}

-- PERDITA SIMULATA (solo loopback): scarta un PASSO ogni N, per riprodurre in
-- emulatore la perdita del cavo (~6% misurata sul fisico il 2026-08-02) e
-- vedere lavorare la correzione da fermo. 0 = spenta. DETERMINISTICA di
-- proposito: due giri devono dare lo stesso log. Il PASSO scartato non conta
-- in `inviati`, come un evento perso sul filo.
local OPT_PERDI_UN_PASSO_OGNI = 0
local lossSeen = 0      -- PASSI passati dalla manopola
local lossDropped = 0   -- PASSI scartati di proposito

-- =============================================================================
-- RUOLO "relay" (2026-08-25): il client DENTRO il Lua, per chi non puo'
-- eseguire Python accanto all'emulatore - nato per il Trimui Brick con
-- Knulli, dove mGBA con lo scripting c'e' ma Python no (e lo zip porta
-- Python per Windows: su Linux ARM non parte).
--
-- Si parla DIRETTAMENTE con relay_ws.py via WebSocket IN CHIARO (ws://): il
-- Lua di mGBA ha socket TCP ma niente TLS, quindi wss:// sulla 443 non si
-- puo' fare - per questo sulla VPS esiste l'endpoint in chiaro sulla 80
-- (nginx, stessa location del wss). Il frame di gioco resta il datagramma
-- OWL1 di protocol.py, uno per frame WebSocket binario: identico a cio' che
-- fa il browser (web/js/relay.js) e ws_link.py.
--
-- COSA QUESTO CLIENT MINIMO NON FA, E PERCHE' VA BENE COSI'.
-- Niente COPIE e niente RICUCITURA: esistono per curare l'ultimo tratto
-- PC->GBA via SIO (perde ~8%) e i buchi UDP - qui il tratto verso il gioco
-- e' una scrittura in mailbox nello stesso processo, e il tratto dal relay
-- e' TCP, che non perde. I buchi che ARRIVANO gia' bucati (l'amico su GBA
-- fisico che ha perso un passo in salita) li cura il RAMMENDO del payload,
-- come per tutti. Restano: gli SLOT AVATAR (il nibble alto del type, senza
-- il quale tre amici finirebbero tutti sullo slot 0), il dedup/arretrati sul
-- seq, il VIA sul T_BYE, HELLO e PING di presenza.
-- =============================================================================

local RELAY_URL  = rawget(_G, "RELAY_URL")  or ""
local RELAY_ROOM = rawget(_G, "RELAY_ROOM") or 0
local RELAY_PEER = rawget(_G, "RELAY_PEER") or 0

local ws = {
    host = nil, port = 80, path = "/",
    sock = nil,
    handshaken = false,
    buf = "",           -- byte TCP non ancora spezzati in frame
    head = "",          -- risposta HTTP dell'handshake, accumulata
    outSeq = 0,         -- seq u8 dei nostri datagrammi OWL1
    pingAt = 0,         -- vblank dell'ultimo PING
    retryAt = 0,        -- vblank prima del quale non si ritenta la connessione
    riagganci = -1,     -- -1: il primo aggancio non e' un ri-aggancio
    -- Stanza e peer ANNUNCIATI sull'attuale WebSocket (2026-08-27). Non sono
    -- doppioni di RELAY_ROOM/RELAY_PEER: quelli dicono dove VOGLIAMO stare,
    -- questi dove il relay CREDE che stiamo. Servono a cambiare stanza sulla
    -- connessione viva (vedi riaggancia) e a salutare quella vecchia.
    activeRoom = nil,
    activePeer = nil,
    -- L'ultima posizione ASSOLUTA che abbiamo mandato, gia' convertita in
    -- SYNC. Si rispedisce dopo un riaggancio o un cambio stanza: HELLO
    -- iscrive alla stanza ma non porta la posizione, e senza questo un
    -- giocatore fermo resta invisibile finche' non fa un passo.
    lastEvent = nil,
    peers = {},         -- peerId -> {slot=0..2|false, lastSeq=, mapKey=}
    slotsFree = {0, 1, 2},
    scartati = 0,       -- eventi di amici SENZA slot (5o giocatore in su)
    arretrati = 0,
    bye = 0,
}

-- ws://host[:porta][/path] -> host, porta, path. Nessun wss: niente TLS qui.
local function wsParseUrl(url)
    local host, resto = url:match("^ws://([^/:]+)(.*)$")
    if not host then return nil end
    local port = 80
    local p = resto:match("^:(%d+)")
    if p then port = tonumber(p); resto = resto:gsub("^:%d+", "") end
    local path = (resto ~= "" and resto) or "/"
    return host, port, path
end

-- Un frame WebSocket dal client DEVE avere il bit di mask: si usa la chiave
-- 0x00000000, che e' una mask legittima e lascia i byte come sono (XOR con
-- zero). relay_ws.py la accetta e nginx i frame non li guarda proprio.
local function wsFrame(opcode, payload)
    local n = #payload
    local head
    if n < 126 then
        head = string.char(0x80 | opcode, 0x80 | n)
    else
        head = string.char(0x80 | opcode, 0x80 | 126) .. string.pack(">I2", n)
    end
    return head .. "\0\0\0\0" .. payload
end

local function wsSendRaw(opcode, payload)
    if not ws.sock then return false end
    local ok, err = ws.sock:send(wsFrame(opcode, payload))
    if not ok then
        console:error("[rete ] invio al relay fallito: " .. tostring(err))
        ws.sock = nil
        ws.handshaken = false
        return false
    end
    return true
end

-- Il datagramma OWL1 (protocol.py): "OWL1" ver tipo peer:u16 stanza:u16 seq:u8.
local OWL_HELLO, OWL_EVENT, OWL_PING, OWL_PONG, OWL_BYE = 0, 1, 2, 3, 4
local OWL_CLUB = 6      -- T_CLUB di net/protocol.py
local clubRotto = false

-- A NOME DI CHI (2026-08-27). `peerId`/`roomId` servono per un caso solo: il
-- BYE alla stanza VECCHIA, che va spedito col peer e la stanza vecchi - se no
-- il relay lo attribuisce al peer nuovo e i compagni di prima non vedono mai
-- sparire il nostro avatar. Omessi (sempre, tranne li') valgono quelli in uso.
-- Sono parametri e non una seconda funzione perche' il chunk principale di
-- questo script e' al tetto dei 200 registri locali di Lua.
local function owlSend(tipo, body, peerId, roomId)
    ws.outSeq = (ws.outSeq + 1) % 256
    return wsSendRaw(2, string.pack("<c4BBI2I2B", "OWL1", 1, tipo,
                                    peerId or RELAY_PEER, roomId or RELAY_ROOM,
                                    ws.outSeq)
                        .. (body or ""))
end

-- Il club (mgba/club_lua.lua, appeso qui davanti da build.ps1) manda i suoi
-- corpi T_CLUB sullo stesso canale della camminata: un solo WebSocket, una sola
-- stanza, una cosa sola da aprire.
if ClubLua then
    ClubLua.collega(owlSend,
                    function() return RELAY_PEER end,
                    function() return LINK_ROLE == "relay" end)
end

local function wsConnect()
    local now = emu:read32(ADDR_GMAIN_VBLANK1)
    if now < ws.retryAt then return end
    ws.retryAt = now + 600     -- un tentativo ogni ~10 s, non un martello
    -- socket.connect e' BLOCCANTE: l'emulatore si ferma qualche istante se il
    -- server non risponde. E' il prezzo del riaggancio automatico.
    local sock = socket.connect(ws.host, ws.port)
    if not sock then
        console:log("[rete ] relay " .. ws.host .. ":" .. ws.port
                    .. " non raggiungibile, riprovo tra 10 s")
        return
    end
    ws.sock = sock
    ws.handshaken = false
    ws.buf = ""
    ws.head = ""
    -- Sec-WebSocket-Key fissa: il server la usa solo per calcolare l'Accept,
    -- che noi non verifichiamo (niente SHA1 in Lua; si verifica il 101).
    sock:send("GET " .. ws.path .. " HTTP/1.1\r\nHost: " .. ws.host ..
              "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" ..
              "Sec-WebSocket-Key: cGFzc290aWxlLWx1YS0wMDE=\r\n" ..
              "Sec-WebSocket-Version: 13\r\n\r\n")
end

-- Lo SLOT AVATAR di un amico (0..2), o false se i tre sono occupati: identico
-- a slot_for di client.py, l'assegnazione e' locale al ricevente.
local function relaySlotFor(peerId)
    local p = ws.peers[peerId]
    if p then return p.slot end
    local slot = false
    if #ws.slotsFree > 0 then
        slot = table.remove(ws.slotsFree, 1)
        console:log(string.format("[rete ] amico %d -> avatar slot %d", peerId, slot))
    else
        console:log(string.format(
            "[rete ] amico %d SENZA avatar: 3 amici gia' a schermo", peerId))
    end
    ws.peers[peerId] = { slot = slot, lastSeq = false, mapKey = false }
    return slot
end

local function relayDatagram(data)
    if #data < 11 then return end
    local _, _, tipo, peerId, _, seq = string.unpack("<c4BBI2I2B", data)
    local body = string.sub(data, 12)

    if tipo == OWL_PONG then return end

    -- T_CLUB (6): la sessione del Cable Club. Non passa dal dedup dei
    -- datagrammi di camminata - ha numeri di serie suoi, per blocco.
    if tipo == OWL_CLUB then
        if ClubLua then ClubLua.riceviCorpo(body, peerId) end
        return
    end

    if tipo == OWL_BYE then
        local p = ws.peers[peerId]
        ws.peers[peerId] = nil
        ws.bye = ws.bye + 1
        if p and p.slot then
            table.insert(ws.slotsFree, p.slot)
            table.sort(ws.slotsFree)
            if p.mapKey then
                -- Il VIA nel formato che il payload capisce, TIMBRATO con lo
                -- slot che l'amico occupava (come make_leave_event + deliver).
                local via = string.char(4 | (p.slot << 4), 0, 0, 0,
                                        (p.mapKey >> 8) & 0xFF, p.mapKey & 0xFF,
                                        0, 0, 0, 0, 0, 0)
                deliverRx(via)
            end
            console:log(string.format(
                "[rete ] amico %d se n'e' andato: slot %d libero", peerId, p.slot))
        end
        return
    end

    if tipo ~= OWL_EVENT or #body < EVENT_SIZE then return end

    -- Il relay non conserva le posizioni e non inoltra gli HELLO: il PRIMO
    -- evento di un peer mai visto vale quindi come "ci sono, e tu?", e gli si
    -- risponde con la nostra ultima posizione assoluta. Senza, chi entra per
    -- secondo non vede chi era gia' dentro finche' quello non si muove.
    local peerNuovo = ws.peers[peerId] == nil
    local slot = relaySlotFor(peerId)
    local p = ws.peers[peerId]

    -- Dedup e arretrati sul seq, come accept_seq di client.py: su UDP (il
    -- tratto relay_ws -> relay e' UDP in loopback) un doppione e' raro ma
    -- possibile, e un arretrato applicherebbe un passo vecchio.
    if p.lastSeq then
        local delta = (seq - p.lastSeq) % 256
        if delta == 0 then return end
        if delta > 128 then ws.arretrati = ws.arretrati + 1; return end
    end
    p.lastSeq = seq

    local ev = string.sub(body, 1, EVENT_SIZE)
    local kind = string.byte(ev, 1) & 0x0F
    if kind >= 1 and kind <= 3 then
        p.mapKey = (string.byte(ev, 5) << 8) | string.byte(ev, 6)
    end

    if not slot then
        ws.scartati = ws.scartati + 1     -- 4o amico: solo per il conto
        return
    end
    if slot > 0 then
        ev = string.char(string.byte(ev, 1) | (slot << 4)) .. string.sub(ev, 2)
    end
    if deliverRx(ev) then
        link.received = link.received + 1
    end
    if peerNuovo and ws.lastEvent then owlSend(OWL_EVENT, ws.lastEvent) end
end

local function relayPump()
    local now = emu:read32(ADDR_GMAIN_VBLANK1)

    if not ws.sock then
        wsConnect()
        if not ws.sock then return end
    end

    -- Si legge tutto quel che c'e', poi si spezza: prima l'handshake HTTP,
    -- poi i frame (che il TCP puo' consegnare a fette o incollati).
    while ws.sock and ws.sock:hasdata() do
        local data, err = ws.sock:receive(2048)
        if not data then
            if err then
                console:error("[rete ] relay disconnesso: " .. tostring(err))
                ws.sock = nil
                ws.handshaken = false
            end
            break
        end
        if ws.handshaken then
            ws.buf = ws.buf .. data
        else
            ws.head = ws.head .. data
            local fine = ws.head:find("\r\n\r\n", 1, true)
            if fine then
                if not ws.head:find("101", 1, true) then
                    local prima = ws.head:match("^[^\r]*") or "?"
                    console:error("[rete ] il relay ha rifiutato l'upgrade: " .. prima)
                    -- LA DIAGNOSI, non solo il codice. Il 3xx e' il caso che
                    -- capita davvero: quell'indirizzo esiste solo in HTTPS e il
                    -- server rimanda li'. Ma il Lua di mGBA non ha TLS e non
                    -- segue i redirect: da qui e' un vicolo cieco, e senza
                    -- questa riga il sintomo sarebbe "emulatore muto".
                    if prima:find(" 30") then
                        local dove = ws.head:match("[Ll]ocation:%s*([^\r\n]+)") or "https"
                        console:error("[rete ] quel relay vive solo in HTTPS (rimanda a "
                                      .. dove .. ") e il Lua di mGBA NON sa parlare TLS."
                                      .. " Serve un indirizzo ws:// in chiaro: chiedilo a chi"
                                      .. " tiene il relay, oppure cambialo qui con"
                                      .. " relay(\"ws://host/ws\")")
                    elseif prima:find(" 40") then
                        console:error("[rete ] percorso sbagliato o inesistente: controlla"
                                      .. " quello che viene dopo l'host in " .. RELAY_URL)
                    end
                    ws.sock = nil
                    return
                end
                ws.buf = ws.head:sub(fine + 4)
                ws.head = ""
                ws.handshaken = true
                ws.riagganci = ws.riagganci + 1
                ws.activeRoom = RELAY_ROOM
                ws.activePeer = RELAY_PEER
                link.connected = true
                console:log("[rete ] collegato al relay ws://" .. ws.host .. ":"
                            .. ws.port .. ws.path
                            .. (ws.riagganci > 0
                                and (" (riaggancio n." .. ws.riagganci .. ")") or ""))
                owlSend(OWL_HELLO, "")
                -- L'HELLO iscrive alla stanza ma non porta la posizione:
                -- senza questo, dopo un riaggancio l'amico non ci vede
                -- finche' non facciamo un passo (e da fermi, mai).
                if ws.lastEvent then owlSend(OWL_EVENT, ws.lastEvent) end
                ws.pingAt = now
            end
        end
    end

    if not ws.handshaken then return end

    -- I frame. Dal server arrivano NON mascherati (RFC 6455).
    while #ws.buf >= 2 do
        local b0 = string.byte(ws.buf, 1)
        local b1 = string.byte(ws.buf, 2)
        local op = b0 & 0x0F
        local ln = b1 & 0x7F
        local pos = 3
        if ln == 126 then
            if #ws.buf < 4 then break end
            ln = string.unpack(">I2", ws.buf, 3)
            pos = 5
        elseif ln == 127 then
            break   -- 64 bit: nessun frame nostro e' cosi', si tronca il canale
        end
        if (b1 & 0x80) ~= 0 then pos = pos + 4 end   -- mask dal server: mai, ma...
        if #ws.buf < pos + ln - 1 then break end
        local payload = string.sub(ws.buf, pos, pos + ln - 1)
        ws.buf = string.sub(ws.buf, pos + ln)
        if op == 2 then
            relayDatagram(payload)
        elseif op == 9 then
            wsSendRaw(10, payload)              -- ping -> pong
        elseif op == 8 then
            console:log("[rete ] il relay ha chiuso il canale")
            ws.sock = nil
            ws.handshaken = false
            link.connected = false
            return
        end
    end

    -- Il battito: tiene vivo il peer UDP dentro il relay (timeout) e fa da
    -- keepalive per NAT e proxy. Ogni ~2 s. Si porta dietro anche l'ultimo
    -- stato ASSOLUTO: dopo un cambio stanza (o un riavvio del relay) chi
    -- resta fermo si fa comunque vedere, senza aspettare il prossimo passo.
    if now - ws.pingAt >= 120 then
        ws.pingAt = now
        owlSend(OWL_PING, "")
        if ws.lastEvent then owlSend(OWL_EVENT, ws.lastEvent) end
    end
end

local function relaySend(raw)
    -- Si conserva una FOTOGRAFIA, non un passo da ripetere: rispedire un
    -- PASSO sposterebbe il remoto una seconda volta. PASSO (1), SYNC (2) e
    -- GIRA (3) portano tutti mappa e coordinate, quindi si forza il tipo a
    -- SYNC lasciando intatto il nibble alto (lo slot).
    local kind = string.byte(raw, 1) & 0x0F
    if kind >= 1 and kind <= 3 then
        ws.lastEvent = string.char((string.byte(raw, 1) & 0xF0) | 2)
                       .. string.sub(raw, 2)
    end
    if not ws.handshaken then return end
    if owlSend(OWL_EVENT, raw) then
        link.sent = link.sent + 1
    end
end

-- --- stanza, peer e relay SENZA modificare questo file (2026-08-26) ---------
--
-- Il file lo prepara il SITO ("Scarica lo script per l'emulatore"): scrive
-- lui i tre valori qui sotto in cima allo script. Ma cambiare stanza non deve
-- costare un nuovo download, quindi ci sono altre due strade, e vincono in
-- quest'ordine:
--
--   1. i comandi dalla console di mGBA:  stanza(4242)  peer(7)  relay("ws://...")
--   2. il file gen3-poke-multiplayer-config.lua accanto allo script
--      (o, se manca, il vecchio passotile-config.lua: chi l'aveva gia' non perde la stanza)
--   3. i valori scritti nello script (quelli del sito)
--
-- Chi decide cosa e' scritto nel log all'avvio: una configurazione che non si
-- vede e' una configurazione che non si puo' correggere.

local CONFIG_FILE = "gen3-poke-multiplayer-config.lua"
-- Il nome di prima del cambio di nome del progetto (2026-09-25): si LEGGE ancora,
-- cosi' chi aveva gia' salvato stanza e peer non li perde; si scrive solo il nuovo.
local CONFIG_FILE_VECCHIO = "passotile-config.lua"
local cfgOrigine = "script"    -- da dove vengono i valori attuali

-- Il file di config sta accanto allo script se l'API dice dove siamo, altrimenti
-- nella cartella corrente di mGBA. Si prova l'una e l'altra: costa due open.
local function cfgPercorsi(nome)
    nome = nome or CONFIG_FILE
    local out = {}
    local dir = nil
    if type(script) == "table" then dir = script.dir or script.path end
    if type(dir) == "string" and dir ~= "" then
        dir = dir:gsub("[\\/][^\\/]*%.lua$", "")
        out[#out + 1] = dir .. "/" .. nome
    end
    out[#out + 1] = nome
    return out
end

local function cfgLeggi()
    local tutti = cfgPercorsi(CONFIG_FILE)
    for _, p in ipairs(cfgPercorsi(CONFIG_FILE_VECCHIO)) do tutti[#tutti + 1] = p end
    for _, path in ipairs(tutti) do
        local f = io.open(path, "r")
        if f then
            local testo = f:read("*a")
            f:close()
            local carica = load(testo, CONFIG_FILE, "t", {})
            local ok, dati = pcall(carica)
            if ok and type(dati) == "table" then
                return dati, path
            end
            console:error("[rete ] " .. path .. " illeggibile: uso i valori dello script")
            return nil, path
        end
    end
    return nil, nil
end

local function cfgScrivi()
    for _, path in ipairs(cfgPercorsi()) do
        local f = io.open(path, "w")
        if f then
            f:write(string.format(
                "-- scritto dal ruolo relay di gen3-poke-multiplayer: si puo' modificare a mano\n"
                .. "return { stanza = %d, peer = %d, relay = %q }\n",
                RELAY_ROOM, RELAY_PEER, RELAY_URL))
            f:close()
            return path
        end
    end
    return nil
end

-- Il peer NON serve sceglierlo (2026-08-26): come fa il browser, se ne
-- sorteggia uno. Serve solo che sia unico nella stanza, e 1 su 65000 di
-- collisione e' un rischio che si corre volentieri per non dover coordinare
-- dei numeri al telefono. `os` puo' non esserci in tutte le build di mGBA:
-- il ripiego e' il contatore VBlank del gioco, che all'avvio vale qualunque
-- cosa a seconda di quanto ci si e' messi a caricare la partita.
local function peerSorteggiato()
    local seme
    if os and os.time then
        seme = os.time() + (os.clock and math.floor(os.clock() * 1000) or 0)
    else
        seme = emu:read32(ADDR_GMAIN_VBLANK1) + 7919
    end
    math.randomseed(seme)
    return 1 + (math.random(65000))
end

local function relayStart()
    -- 1. il file di config, se c'e', vince sui valori dello script
    local dati, path = cfgLeggi()
    if dati then
        if tonumber(dati.stanza) then RELAY_ROOM = math.floor(tonumber(dati.stanza)) end
        if tonumber(dati.peer) then RELAY_PEER = math.floor(tonumber(dati.peer)) end
        if type(dati.relay) == "string" and dati.relay ~= "" then RELAY_URL = dati.relay end
        cfgOrigine = path
    end

    -- 2. il peer, se non e' stato scelto, se lo sorteggia
    if RELAY_PEER < 1 then
        RELAY_PEER = peerSorteggiato()
        console:log(string.format(
            "[rete ] peer sorteggiato: %d (dev'essere UNICO nella stanza; per "
            .. "fissarlo: peer(N) qui nella console)", RELAY_PEER))
    end

    ws.host, ws.port, ws.path = wsParseUrl(RELAY_URL)
    if not ws.host then
        console:error("[rete ] relay non valido: " .. tostring(RELAY_URL)
                      .. " (serve ws://host[:porta]/path - wss NON si puo': "
                      .. "il Lua di mGBA non ha TLS)")
        link.role = "off"
        return
    end
    if RELAY_ROOM < 1 or RELAY_ROOM > 65535 then
        console:error("[rete ] stanza mancante o fuori range (1..65535): "
                      .. tostring(RELAY_ROOM) .. " - scrivi  stanza(N)  qui nella console")
        link.role = "off"
        return
    end

    console:log(string.format(
        "[rete ] ruolo relay: ws://%s:%d%s stanza %d peer %d (valori da: %s)",
        ws.host, ws.port, ws.path, RELAY_ROOM, RELAY_PEER, cfgOrigine))
    -- La stessa cosa in evidenza nel file: e' la PRIMA riga da guardare
    -- quando "non ci vediamo piu'", perche' nove volte su dieci la stanza in
    -- uso non e' quella che si crede (il file di configurazione vince sullo
    -- script, e resta li' anche dopo aver riscaricato lo script dal sito).
    LOG.riga(string.format(
        "\nCONFIGURAZIONE ATTIVA\n  relay   : %s\n  stanza  : %d\n  peer    : %d\n"
        .. "  origine : %s\n", RELAY_URL, RELAY_ROOM, RELAY_PEER, cfgOrigine))
    console:log("[rete ] per cambiare al volo:  stanza(4242)   peer(7)   "
                .. "relay(\"ws://host/ws\")  - resta salvato per la prossima volta")
    wsConnect()
end

-- --- i comandi dalla console di mGBA ----------------------------------------
-- Globali apposta: si digitano nella finestra Scripting. Ognuno cambia il
-- valore, salva e RIAGGANCIA, cosi' l'effetto e' immediato e non serve
-- ricaricare lo script.
local function riaggancia(cosa)
    -- Prima di tutto si TOLGONO dallo schermo gli amici della sessione
    -- vecchia: cambiare stanza senza questo lascia i loro avatar piantati
    -- sulla mappa, e il payload non ha modo di sapere che non esistono piu'.
    for _, p in pairs(ws.peers) do
        if type(p.slot) == "number" and p.mapKey then
            local via = string.char(4 | (p.slot << 4), 0, 0, 0,
                                    (p.mapKey >> 8) & 0xFF, p.mapKey & 0xFF,
                                    0, 0, 0, 0, 0, 0)
            deliverRx(via)
        end
    end
    ws.peers = {}
    ws.slotsFree = { 0, 1, 2 }

    local salvato = cfgScrivi()
    console:log(string.format("[rete ] %s -> stanza %d peer %d %s", cosa,
        RELAY_ROOM, RELAY_PEER,
        salvato and ("(salvato in " .. salvato .. ")")
                 or "(NON salvato: permessi? vale solo per questa sessione)"))
    LOG.riga(string.format(
        "\nCONFIGURAZIONE CAMBIATA (%s)\n  relay   : %s\n  stanza  : %d\n  peer    : %d\n",
        cosa, RELAY_URL, RELAY_ROOM, RELAY_PEER))

    local nuovoHost, nuovaPorta, nuovoPath = wsParseUrl(RELAY_URL)
    if not nuovoHost then return end

    -- STANZA E PEER STANNO NEL DATAGRAMMA, NON NEL WEBSOCKET (2026-08-27).
    -- Se il relay e' lo stesso non c'e' niente da riaprire: basta un nuovo
    -- HELLO e il relay ci sposta (move_to_room manda da solo il T_BYE ai
    -- compagni di prima). Chiudere e riaprire il socket da QUI - cioe' da
    -- dentro il callback frame - e' proprio la cosa da non fare: mGBA
    -- registra e toglie i callback del socket nello stesso giro e ci sono
    -- casi in cui il socket nuovo resta muto fino al riavvio della ROM.
    local stessoRelay = ws.host == nuovoHost
                        and ws.port == nuovaPorta and ws.path == nuovoPath
    if stessoRelay and ws.sock and ws.handshaken then
        -- Cambiando SOLO stanza basta l'HELLO. Se cambia anche l'identita',
        -- la stanza vecchia va salutata a nome del peer VECCHIO, o resta li'
        -- un avatar nostro fino al timeout.
        if ws.activePeer and ws.activeRoom and ws.activePeer ~= RELAY_PEER then
            owlSend(OWL_BYE, "", ws.activePeer, ws.activeRoom)
        end
        owlSend(OWL_HELLO, "")
        ws.activeRoom = RELAY_ROOM
        ws.activePeer = RELAY_PEER
        ws.pingAt = emu:read32(ADDR_GMAIN_VBLANK1)
        link.connected = true
        if ws.lastEvent then owlSend(OWL_EVENT, ws.lastEvent) end
        console:log("[rete ] sessione aggiornata sulla connessione esistente")
        return
    end

    -- Solo un cambio di INDIRIZZO richiede davvero un socket nuovo. La
    -- connessione la apre relayPump al frame dopo, non qui.
    if ws.sock and ws.handshaken and ws.activePeer and ws.activeRoom then
        pcall(function()
            owlSend(OWL_BYE, "", ws.activePeer, ws.activeRoom)
        end)
    end
    if ws.sock then pcall(function() ws.sock:close() end) end
    ws.sock = nil
    ws.handshaken = false
    ws.activeRoom = nil
    ws.activePeer = nil
    ws.buf = ""
    ws.head = ""
    ws.pingAt = 0
    link.connected = false
    ws.host, ws.port, ws.path = nuovoHost, nuovaPorta, nuovoPath
    ws.retryAt = emu:read32(ADDR_GMAIN_VBLANK1) + 1
    console:log("[rete ] cambio relay: nuova connessione dal prossimo frame")
end

function stanza(n)
    n = math.floor(tonumber(n) or 0)
    if n < 1 or n > 65535 then
        console:error("[rete ] stanza fuori range: serve 1..65535")
        return
    end
    RELAY_ROOM = n
    riaggancia("stanza cambiata")
end

function peer(n)
    n = math.floor(tonumber(n) or 0)
    if n < 1 or n > 65534 then
        console:error("[rete ] peer fuori range: serve 1..65534")
        return
    end
    RELAY_PEER = n
    riaggancia("peer cambiato")
end

function relay(url)
    if type(url) ~= "string" or not wsParseUrl(url) then
        console:error("[rete ] relay non valido: serve ws://host[:porta]/path")
        return
    end
    RELAY_URL = url
    riaggancia("relay cambiato")
end

-- --- IL CONFIGURATORE DAL PAD (2026-08-27) ----------------------------------
--
-- Nato per il TrimUI Brick con Knulli, dove mGBA gira a schermo intero e la
-- console di scripting NON c'e': senza questo, cambiare stanza vuol dire
-- spegnere, tirare fuori la microSD e riscrivere un file. Su PC resta comodo
-- lo stesso, e non toglie niente ai comandi  stanza(N) / peer(N) / relay(url).
--
--   L+R+B         apre (e richiude, annullando)
--   Su / Giu      sceglie il campo: stanza o peer
--   L / R         passo di modifica: 1, 10, 100, 1000
--   Sin / Destra  valore -/+
--   A             salva e riaggancia        B  annulla ed esce
--
-- ATTENZIONE, e' voluto e non e' un difetto da correggere di nascosto: mentre
-- il pannello e' aperto il GIOCO continua a ricevere gli stessi tasti, quindi
-- il personaggio cammina. Aprilo da fermo, in un posto senza porte.
--
-- Perche' la grafica sta su BG0 e non sugli sprite: l'OAM non e' scrivibile
-- durante la scansione visibile, e le scritture di Lua arrivano fuori dal
-- VBlank - l'hardware emulato le scarta. La VRAM di BG0 invece accetta gli
-- aggiornamenti dal main loop. Registri, tile, tilemap e palette vengono
-- salvati all'apertura e rimessi byte per byte alla chiusura: la ROM non si
-- tocca, il payload nemmeno.
do local function installa()   -- UNA FUNZIONE, non un semplice `do`:
-- i local di un blocco `do` restano registri del CHUNK PRINCIPALE, che
-- qui e' al tetto dei 200 di Lua (oltre, mGBA non da' un errore: da'
-- silenzio). Quelli di una funzione no. Verificato il 2026-08-27.
    -- Gli indici dei tasti GBA sono fissi. Alcune build espongono C.GBA_KEY,
    -- altre no: il ripiego locale evita che il pannello resti muto.
    local GBA_KEY_INDEX = {
        A = 0, B = 1, SELECT = 2, START = 3,
        RIGHT = 4, LEFT = 5, UP = 6, DOWN = 7, R = 8, L = 9,
    }

    local function gbaKey(name)
        local index = GBA_KEY_INDEX[name]
        if type(C) == "table" and type(C.GBA_KEY) == "table"
            and type(C.GBA_KEY[name]) == "number" then
            index = C.GBA_KEY[name]
        end
        return index ~= nil and (1 << index) or 0
    end

    local KEY_A     = gbaKey("A")
    local KEY_B     = gbaKey("B")
    local KEY_LEFT  = gbaKey("LEFT")
    local KEY_RIGHT = gbaKey("RIGHT")
    local KEY_UP    = gbaKey("UP")
    local KEY_DOWN  = gbaKey("DOWN")
    local KEY_R     = gbaKey("R")
    local KEY_L     = gbaKey("L")
    -- SELECT e START restano interamente al gioco.
    local KEY_OPEN  = KEY_L | KEY_R | KEY_B

    -- gMain.heldKeysRaw (include/main.h: 0x028, senza il rimappaggio L=A).
    -- Si legge la copia che il gioco ha gia' fatto di KEYINPUT invece delle
    -- API input di Lua: su Knulli quelle sono instabili, questa RAM no.
    local ADDR_HELD_KEYS_RAW = ADDR_GMAIN_CB1 + 0x28

    local configUi = {
        open = false,
        field = 1,                  -- 1 = stanza, 2 = peer
        stepIndex = 1,
        steps = { 1, 10, 100, 1000 },
        room = 0,
        peer = 0,
        previousKeys = 0,
        comboHeld = false,
        notice = "",
    }

    local UI_DISPCNT      = 0x04000000
    local UI_BG0CNT       = 0x04000008
    local UI_BG0HOFS      = 0x04000010
    local UI_BG0VOFS      = 0x04000012
    local UI_BLDCNT       = 0x04000050
    local UI_BG_CHAR      = 0x0600C000   -- character block 3
    local UI_BG_MAP       = 0x0600F800   -- screen block 31
    local UI_PALETTE      = 0x05000000   -- palette BG, non OBJ
    local UI_TILE         = 1
    local UI_PALETTE_BANK = 15
    local UI_SCREEN_X, UI_SCREEN_Y = 56, 8
    local UI_BYTES        = 128 * 64 // 2   -- 16x8 tile a 4bpp
    local UI_PIXELS       = {}
    local UI_READY        = false
    local UI_BACKUP       = nil

    local function uiBackup()
        if UI_BACKUP then return end
        UI_BACKUP = {
            dispcnt = emu:read16(UI_DISPCNT),
            bg0cnt  = emu:read16(UI_BG0CNT),
            bg0hofs = emu:read16(UI_BG0HOFS),
            bg0vofs = emu:read16(UI_BG0VOFS),
            bldcnt  = emu:read16(UI_BLDCNT),
            map = {}, chars = {}, palette = {},
        }
        for i = 0, 0x7FF do UI_BACKUP.map[i] = emu:read8(UI_BG_MAP + i) end
        for i = 0, UI_BYTES - 1 do
            UI_BACKUP.chars[i] = emu:read8(UI_BG_CHAR + UI_TILE * 32 + i)
        end
        for i = 0, 9 do
            UI_BACKUP.palette[i] = emu:read8(UI_PALETTE + UI_PALETTE_BANK * 32 + i)
        end
    end

    local function uiRestore()
        if not UI_BACKUP then return end
        for i = 0, 0x7FF do emu:write8(UI_BG_MAP + i, UI_BACKUP.map[i]) end
        for i = 0, UI_BYTES - 1 do
            emu:write8(UI_BG_CHAR + UI_TILE * 32 + i, UI_BACKUP.chars[i])
        end
        for i = 0, 9 do
            emu:write8(UI_PALETTE + UI_PALETTE_BANK * 32 + i, UI_BACKUP.palette[i])
        end
        emu:write16(UI_BG0CNT, UI_BACKUP.bg0cnt)
        emu:write16(UI_BG0HOFS, UI_BACKUP.bg0hofs)
        emu:write16(UI_BG0VOFS, UI_BACKUP.bg0vofs)
        emu:write16(UI_BLDCNT, UI_BACKUP.bldcnt)
        emu:write16(UI_DISPCNT, UI_BACKUP.dispcnt)
        UI_BACKUP = nil
        UI_READY = false
    end

    -- Font 5x7, solo maiuscole, cifre e pochi segni: il ripiego e' lo spazio.
    local UI_FONT = {
        [" "] = { "00000", "00000", "00000", "00000", "00000", "00000", "00000" },
        ["+"] = { "00000", "00100", "00100", "11111", "00100", "00100", "00000" },
        ["-"] = { "00000", "00000", "00000", "11111", "00000", "00000", "00000" },
        ["/"] = { "00001", "00010", "00100", "01000", "10000", "00000", "00000" },
        [":"] = { "00000", "00100", "00100", "00000", "00100", "00100", "00000" },
        ["0"] = { "01110", "10001", "10011", "10101", "11001", "10001", "01110" },
        ["1"] = { "00100", "01100", "00100", "00100", "00100", "00100", "01110" },
        ["2"] = { "01110", "10001", "00001", "00010", "00100", "01000", "11111" },
        ["3"] = { "11110", "00001", "00001", "01110", "00001", "00001", "11110" },
        ["4"] = { "00010", "00110", "01010", "10010", "11111", "00010", "00010" },
        ["5"] = { "11111", "10000", "10000", "11110", "00001", "00001", "11110" },
        ["6"] = { "01110", "10000", "10000", "11110", "10001", "10001", "01110" },
        ["7"] = { "11111", "00001", "00010", "00100", "01000", "01000", "01000" },
        ["8"] = { "01110", "10001", "10001", "01110", "10001", "10001", "01110" },
        ["9"] = { "01110", "10001", "10001", "01111", "00001", "00001", "01110" },
        ["A"] = { "01110", "10001", "10001", "11111", "10001", "10001", "10001" },
        ["B"] = { "11110", "10001", "10001", "11110", "10001", "10001", "11110" },
        ["C"] = { "01110", "10001", "10000", "10000", "10000", "10001", "01110" },
        ["D"] = { "11110", "10001", "10001", "10001", "10001", "10001", "11110" },
        ["E"] = { "11111", "10000", "10000", "11110", "10000", "10000", "11111" },
        ["F"] = { "11111", "10000", "10000", "11110", "10000", "10000", "10000" },
        ["G"] = { "01110", "10001", "10000", "10111", "10001", "10001", "01110" },
        ["H"] = { "10001", "10001", "10001", "11111", "10001", "10001", "10001" },
        ["I"] = { "01110", "00100", "00100", "00100", "00100", "00100", "01110" },
        ["K"] = { "10001", "10010", "10100", "11000", "10100", "10010", "10001" },
        ["L"] = { "10000", "10000", "10000", "10000", "10000", "10000", "11111" },
        ["M"] = { "10001", "11011", "10101", "10101", "10001", "10001", "10001" },
        ["N"] = { "10001", "11001", "10101", "10011", "10001", "10001", "10001" },
        ["O"] = { "01110", "10001", "10001", "10001", "10001", "10001", "01110" },
        ["P"] = { "11110", "10001", "10001", "11110", "10000", "10000", "10000" },
        ["R"] = { "11110", "10001", "10001", "11110", "10100", "10010", "10001" },
        ["S"] = { "01111", "10000", "10000", "01110", "00001", "00001", "11110" },
        ["T"] = { "11111", "00100", "00100", "00100", "00100", "00100", "00100" },
        ["U"] = { "10001", "10001", "10001", "10001", "10001", "10001", "01110" },
        ["V"] = { "10001", "10001", "10001", "10001", "10001", "01010", "00100" },
        ["X"] = { "10001", "10001", "01010", "00100", "01010", "10001", "10001" },
    }

    local function uiSetPixel(x, y, color)
        if x < 0 or x >= 128 or y < 0 or y >= 64 then return end
        local tile = (y // 8) * 16 + (x // 8)
        local offset = tile * 32 + (y % 8) * 4 + ((x % 8) // 2)
        local byte = UI_PIXELS[offset] or 0x11
        if (x % 2) == 0 then
            UI_PIXELS[offset] = (byte & 0xF0) | color
        else
            UI_PIXELS[offset] = (byte & 0x0F) | (color << 4)
        end
    end

    local function uiRect(x, y, w, h, color)
        for yy = y, y + h - 1 do
            for xx = x, x + w - 1 do uiSetPixel(xx, yy, color) end
        end
    end

    local function uiText(x, y, text, color)
        for i = 1, #text do
            local glyph = UI_FONT[string.sub(text, i, i)] or UI_FONT[" "]
            for gy = 1, 7 do
                local row = glyph[gy]
                for gx = 1, 5 do
                    if string.sub(row, gx, gx) == "1" then
                        uiSetPixel(x + (i - 1) * 6 + gx - 1, y + gy - 1, color)
                    end
                end
            end
        end
    end

    local function uiWriteMap()
        -- BG0 e' un layer che il GIOCO usa: azzerarne lo scroll spostava a
        -- sinistra i pannelli di Emerald. Si tiene il suo scroll e si mette la
        -- nostra tilemap nella posizione equivalente, con il wrap 32x32.
        local hofs = emu:read16(UI_BG0HOFS) & 0x1FF
        local vofs = emu:read16(UI_BG0VOFS) & 0x1FF
        local mapX = ((hofs + UI_SCREEN_X + 7) // 8) % 32
        local mapY = ((vofs + UI_SCREEN_Y + 7) // 8) % 32
        for row = 0, 7 do
            for col = 0, 15 do
                local mapOffset = (((mapY + row) % 32) * 32
                                   + ((mapX + col) % 32)) * 2
                local tile = UI_TILE + row * 16 + col
                emu:write16(UI_BG_MAP + mapOffset, tile | (UI_PALETTE_BANK << 12))
            end
        end
    end

    local function uiShowPanel()
        -- BG0 davanti agli altri layer, 4bpp, charblock 3, screenblock 31.
        emu:write16(UI_BG0CNT, 0x1F0C)
        emu:write16(UI_BLDCNT, 0)
        -- BG0 acceso e finestre hardware spente: una maschera WIN di Emerald
        -- nasconderebbe il pannello.
        emu:write16(UI_DISPCNT, (emu:read16(UI_DISPCNT) | 0x0100) & 0x1FFF)
        uiWriteMap()
        -- Emerald riscrive gli stessi tile a ogni VBlank: rimetterli qui, ogni
        -- frame, e' quello che toglie i caratteri corrotti.
        emu:write16(UI_PALETTE + UI_PALETTE_BANK * 32 + 0, 0x0000)
        emu:write16(UI_PALETTE + UI_PALETTE_BANK * 32 + 2, 0x4210)
        emu:write16(UI_PALETTE + UI_PALETTE_BANK * 32 + 4, 0x7FFF)
        emu:write16(UI_PALETTE + UI_PALETTE_BANK * 32 + 6, 0x03FF)
        emu:write16(UI_PALETTE + UI_PALETTE_BANK * 32 + 8, 0x001F)
        for i = 0, UI_BYTES - 1, 2 do
            emu:write16(UI_BG_CHAR + UI_TILE * 32 + i,
                        UI_PIXELS[i] | (UI_PIXELS[i + 1] << 8))
        end
    end

    local function uiRender()
        for i = 0, UI_BYTES - 1 do UI_PIXELS[i] = 0x11 end
        -- 1 = fondo, 2 = bordo, 3 = testo, 4 = riga selezionata.
        uiRect(0, 0, 128, 64, 2)
        uiRect(2, 2, 124, 60, 1)
        uiText(25, 3, "GEN3PM CONFIG", 3)
        uiText(8, 14, "ROOM:" .. tostring(configUi.room),
               configUi.field == 1 and 4 or 3)
        uiText(8, 24, "PEER:" .. tostring(configUi.peer),
               configUi.field == 2 and 4 or 3)
        uiText(8, 36, "UD FIELD LR STEP", 3)
        uiText(8, 46, "LEFT - RIGHT +", 3)
        -- 54: a 55 l'ultima riga toccava il bordo, a 53 si sovrapponeva a
        -- quella sopra. Provato a schermo il 2026-08-27 (build\prova-pannello).
        uiText(8, 54, "A SAVE B EXIT", 3)
        UI_READY = true
        uiShowPanel()
    end

    -- Il pannello si vede a schermo, ma la stessa riga va anche nel log: su
    -- una foto non si legge, e il log e' l'unica prova che resta.
    local function configUiDraw()
        if configUi.open then uiRender() end
        local rete = link.connected and "CONNESSO"
                     or (ws.handshaken and "COLLEGAMENTO" or "DISCONNESSO")
        console:log(string.format(
            "[config] rete %s | %s stanza %d | %s peer %d | passo %d | %s",
            rete,
            configUi.field == 1 and ">" or " ", configUi.room,
            configUi.field == 2 and ">" or " ", configUi.peer,
            configUi.steps[configUi.stepIndex], configUi.notice))
    end

    local function configUiWrap(value, minimo, massimo, delta)
        value = value + delta
        if value > massimo then return minimo end
        if value < minimo then return massimo end
        return value
    end

    local function configUiOpen()
        configUi.open = true
        configUi.field = 1
        configUi.stepIndex = 1
        configUi.room = RELAY_ROOM
        configUi.peer = RELAY_PEER
        configUi.previousKeys = 0
        configUi.notice = "Modifica e premi A per applicare."
        uiBackup()
        configUiDraw()
        console:log("[config] pannello aperto (L+R+B per aprire, B per annullare)")
        console:warn("[config] il gioco riceve ancora i tasti: se cammini mentre "
                     .. "il pannello e' aperto, cammini davvero")
    end

    local function configUiClose(messaggio)
        configUi.open = false
        configUi.previousKeys = 0
        configUi.notice = messaggio or "Pannello chiuso."
        uiRestore()
        configUiDraw()
    end

    local function configUiApply()
        -- Le stesse soglie di stanza() e peer(), ma un solo riaggancio e una
        -- sola scrittura del file di configurazione.
        if configUi.room < 1 or configUi.room > 65535
            or configUi.peer < 1 or configUi.peer > 65534 then
            configUi.notice = "Fuori range: stanza 1..65535, peer 1..65534"
            configUiDraw()
            return
        end
        RELAY_ROOM = configUi.room
        RELAY_PEER = configUi.peer
        configUiClose("Salvato: riaggancio in corso...")
        riaggancia("pannello dal pad")
    end

    local function premuto(keys, mask)
        return mask ~= 0 and (keys & mask) ~= 0
               and (configUi.previousKeys & mask) == 0
    end

    local function configUiHandleKeys(keys)
        if KEY_OPEN == 0 then return end
        local combo = (keys & KEY_OPEN) == KEY_OPEN
        if not combo then configUi.comboHeld = false end

        if not configUi.open then
            if combo and not configUi.comboHeld then
                configUi.comboHeld = true
                configUiOpen()
            end
            return
        end

        if combo then
            if not configUi.comboHeld then
                configUi.comboHeld = true
                configUiClose("Annullato: nessuna modifica applicata.")
            end
            configUi.previousKeys = keys
            return
        end

        if premuto(keys, KEY_B) then
            configUiClose("Annullato: nessuna modifica applicata.")
        elseif premuto(keys, KEY_A) then
            configUiApply()
        elseif premuto(keys, KEY_UP) then
            configUi.field = configUi.field == 1 and 2 or configUi.field - 1
            configUiDraw()
        elseif premuto(keys, KEY_DOWN) then
            configUi.field = configUi.field == 2 and 1 or configUi.field + 1
            configUiDraw()
        elseif premuto(keys, KEY_R) then
            -- Sulla mappatura Knulli osservata i due dorsali arrivano invertiti.
            configUi.stepIndex = (configUi.stepIndex - 2) % #configUi.steps + 1
            configUiDraw()
        elseif premuto(keys, KEY_L) then
            configUi.stepIndex = configUi.stepIndex % #configUi.steps + 1
            configUiDraw()
        elseif premuto(keys, KEY_RIGHT) or premuto(keys, KEY_LEFT) then
            local passo = configUi.steps[configUi.stepIndex]
            if premuto(keys, KEY_LEFT) then passo = -passo end
            if configUi.field == 1 then
                configUi.room = configUiWrap(configUi.room, 1, 65535, passo)
            else
                configUi.peer = configUiWrap(configUi.peer, 1, 65534, passo)
            end
            configUiDraw()
        end
        configUi.previousKeys = keys
    end

    -- Tutto il pannello vive qui dentro: un callback solo, nessun local in
    -- piu' nel chunk principale (che e' al tetto dei 200 di Lua).
    --   * i tasti si leggono dalla copia del GIOCO, non dalle API input di
    --     mGBA: su Knulli quelle sono instabili, questa RAM no. A questo
    --     punto del frame gMain ha ancora i tasti del frame precedente, che
    --     per un menu e' esattamente lo stesso;
    --   * il pannello va ridisegnato ogni frame o dura un lampo: il gioco
    --     riscrive quella VRAM a ogni VBlank.
    -- Prima dell'iniezione non si guarda niente: gMain e' ancora spazzatura e
    -- una combinazione a caso aprirebbe il pannello all'accensione.
    callbacks:add("keysRead", function()
        if not injected then return end
        local keys = emu:read16(ADDR_HELD_KEYS_RAW)
        if configUi.open or configUi.comboHeld or (keys & KEY_OPEN) == KEY_OPEN then
            configUiHandleKeys(keys)
        end
        if configUi.open and UI_READY then uiShowPanel() end
    end)

    -- Chiudendo la ROM col pannello aperto, la VRAM resterebbe quella nostra.
    callbacks:add("stop", function()
        if UI_BACKUP then uiRestore() end
    end)
end installa() end

local function linkSend(raw)
    if link.role == "loopback" then
        -- string.byte(raw, 1) e' il tipo dell'evento: 1 = PASSO.
        if OPT_PERDI_UN_PASSO_OGNI > 0 and string.byte(raw, 1) == 1 then
            lossSeen = lossSeen + 1
            if lossSeen % OPT_PERDI_UN_PASSO_OGNI == 0 then
                lossDropped = lossDropped + 1
                return
            end
        end
        loopbackQueue[#loopbackQueue + 1] = {
            at = emu:read32(ADDR_GMAIN_VBLANK1) + LOOPBACK_DELAY_VBLANK,
            raw = raw,
        }
        link.sent = link.sent + 1
        return
    end

    if link.role == "relay" then
        relaySend(raw)
        return
    end

    if not link.connected then return end

    local ok, err = link.sock:send(raw)
    if not ok then
        console:error("[rete ] invio fallito: " .. tostring(err))
        link.connected = false
        return
    end
    link.sent = link.sent + 1
end

local function linkPump()
    if link.role == "relay" then
        relayPump()
        return
    end

    -- Il server prova ad accettare a ogni frame. Affidarsi al solo callback
    -- "received" del listener non basta: quello scatta quando arrivano DATI, e
    -- il client non ne manda finche' non e' in partita. Risultato misurato il
    -- 2026-07-30: client "collegato" al vblank 0, server "peer collegato" solo
    -- al 7501 - due minuti in cui i due si credevano connessi a senso unico, con
    -- tanto di "peer disconnesso" per timeout.
    if link.role == "server" and link.listener and not link.connected then
        local client = link.listener:accept()
        if client then
            link.sock = client
            link.connected = true
            link.inbox = ""
            console:log("[rete ] peer collegato")
        end
    end

    if link.role == "loopback" then
        local now = emu:read32(ADDR_GMAIN_VBLANK1)
        while #loopbackQueue > 0 and loopbackQueue[1].at <= now do
            local item = table.remove(loopbackQueue, 1)
            if deliverRx(item.raw) then
                link.received = link.received + 1
            end
        end
        return
    end

    if not link.connected then return end

    -- Si legge finché c'è roba; gli eventi sono a lunghezza fissa, quindi
    -- basta accumulare e tagliare a fette da 12 byte.
    while link.sock:hasdata() do
        local data, err = link.sock:receive(1024)
        if not data then
            if err then
                console:error("[rete ] peer disconnesso: " .. tostring(err))
                link.connected = false
            end
            break
        end
        link.inbox = link.inbox .. data
    end

    while #link.inbox >= EVENT_SIZE do
        local raw = string.sub(link.inbox, 1, EVENT_SIZE)
        link.inbox = string.sub(link.inbox, EVENT_SIZE + 1)
        if deliverRx(raw) then
            link.received = link.received + 1
        end
    end
end

local function linkStart()
    if link.role == "loopback" then
        console:log(string.format(
            "[rete ] modalità loopback: i tuoi eventi rientrano dopo %d VBlank",
            LOOPBACK_DELAY_VBLANK))
        if OPT_PERDI_UN_PASSO_OGNI > 0 then
            console:warn(string.format(
                "[perdita] SIMULATA ATTIVA: scarto 1 PASSO ogni %d. "
                .. "Per il funzionamento normale rimetti OPT_PERDI_UN_PASSO_OGNI = 0",
                OPT_PERDI_UN_PASSO_OGNI))
        end
        return
    end

    if link.role == "server" then
        link.listener = socket.bind(nil, LINK_PORT)
        if not link.listener then
            console:error("[rete ] bind fallito sulla porta " .. LINK_PORT ..
                          " (già in uso?)")
            return
        end
        link.listener:listen()
        -- L'accept vero lo fa linkPump a ogni frame, vedi il commento la' sopra.
        console:log("[rete ] in ascolto sulla porta " .. LINK_PORT ..
                    " - ora avvia l'istanza client")
        console:warn("[rete ] modalita' DIRETTA fra due emulatori: il relay NON e' "
            .. "coinvolto. Per passare dal relay servono DUE istanze 'client' "
            .. "collegate ai bridge - usa net\\run-local.ps1")
        return
    end

    if link.role == "relay" then
        relayStart()
        return
    end

    if link.role == "client" then
        -- Attenzione: socket.connect è BLOCCANTE, l'emulatore si ferma finché
        -- non risponde. Avvia prima l'istanza server.
        link.sock = socket.connect(LINK_HOST, LINK_PORT)
        if not link.sock then
            console:error("[rete ] connessione a " .. LINK_HOST .. ":" ..
                          LINK_PORT .. " fallita: il server è avviato?")
            return
        end
        link.connected = true
        console:log("[rete ] collegato a " .. LINK_HOST .. ":" .. LINK_PORT ..
            " (bridge del relay, oppure l'altro emulatore in modalita' diretta)")
        return
    end

    console:log("[rete ] disattivata (LINK_ROLE = " .. tostring(link.role) .. ")")
end

-- =============================================================================
-- Diagnostica
-- =============================================================================

local S = {
    magic = 0x00, irq = 0x04, vblank = 0x08, lastIf = 0x0C,
    inOverworld = 0x10, state = 0x14, objectId = 0x18,
    spawnAttempts = 0x1C, spawnFailures = 0x20, mapKey = 0x24,
    playerX = 0x28, playerY = 0x2C, remoteX = 0x30, remoteY = 0x34,
    -- +0x38 era `moveIndex`, morto dalla Fase 4: dal 2026-08-28 ci sta il
    -- contatore delle correzioni in PIXEL dello sprite del remoto.
    pixelFixes = 0x38, movesQueued = 0x3C, despawns = 0x40,
    playerDir = 0x44, playerSpeed = 0x48,
    eventsEmitted = 0x4C, eventsDropped = 0x50,
    movesRejected = 0x54, spawnMapKey = 0x58, adoptions = 0x5C,
    remoteKnown = 0x60, remoteMapKey = 0x64,
    remoteTargetX = 0x68, remoteTargetY = 0x6C,
    rxSteps = 0x70, rxSyncs = 0x74, rxCorrections = 0x78, remoteAway = 0x7C,
    movesHurried = 0x80, rxPending = 0x84,
    remoteGender = 0x88, remoteState = 0x8C, gfxChanges = 0x90,
    remoteGfxId = 0x94, remoteAnimNum = 0x98, spawnSkipped = 0x9C,
    gfxResizes = 0xA0, rxTurns = 0xA4,
    blobsCreated = 0xA8, blobsDestroyed = 0xAC, settles = 0xB0, idleFixes = 0xB4,
    origCallback1 = 0xB8, cb1Installs = 0xBC, aBlocked = 0xC0,
    remotesRemoved = 0xC4, entryResyncs = 0xC8, rxOtherMap = 0xCC,
    rxImplausible = 0xD0, entrySyncs = 0xD4, rxLeaves = 0xD8, rxDrained = 0xDC,
    prevMapKey = 0xE0, mapChanges = 0xE4,
    -- Fase 7. La tabella S e la struct PayloadState sono un CONTRATTO: si
    -- toccano insieme, nello stesso commit. Un campo aggiunto di la' e non di
    -- qua da' `attempt to perform arithmetic on a nil value` per ogni riga di
    -- stato (gia' successo il 2026-07-29).
    remoteLocalX = 0xE8, remoteLocalY = 0xEC, remoteVia = 0xF0,
    rxTranslated = 0xF4, stripRejects = 0xF8, borderCarries = 0xFC,
    doorState = 0x100, doorEnters = 0x104, doorExits = 0x108,
    doorBusy = 0x10C, doorTimeouts = 0x110,
    borderMismatch = 0x114, borderDriftX = 0x118, borderDriftY = 0x11C,
    -- Blocco A: il gate di assestamento del cambio mappa.
    settleEntries = 0x120, settleTimeouts = 0x124, settleFramesLast = 0x128,
    borderWalks = 0x12C, borderHolds = 0x130,
    -- Blocco B: indicatori di stato e pass-through.
    statusSent = 0x134, statusRx = 0x138,
    localStatus = 0x13C, remoteStatus = 0x140,
    indicatorShown = 0x144, indicatorLoadFails = 0x148, passThroughs = 0x14C,
    -- Quarto blocco: sezioni del menu e tasto A nei menu.
    menuLatches = 0x150, aFreedInMenu = 0x154,
    -- Possesso della porta seriale (contano solo nella build -WithSio).
    sioAcquires = 0x158, sioReleases = 0x15C,
    -- Cancello anti-raffica: ingressi col bit VBlank alzato ma scartati.
    vbSkips = 0x160,
    -- Doppio invio: copie scartate dal dedup.
    rxDupes = 0x164,
    -- Spazzino: fantasmi (object event col NOSTRO localId, REMOTE_LOCAL_ID)
    -- del continue dal save distrutti in STATE_IDLE.
    remotesSwept = 0x168,
    -- Scudo del salvataggio: voci col nostro localId ripulite come
    -- ClearObjectEvent nella copia d'appoggio che la flash serializza
    -- (gSaveBlock1Ptr->objectEvents).
    stagingScrubs = 0x16C,
    -- Consegna B: la scheda allenatore. Bitmap completa = 0x3FFF.
    cardChunksTx = 0x170, cardChunksRx = 0x174,
    cardRxBitmap = 0x178, cardShows = 0x17C,
    -- Consegna C: rese al link del gioco (Cable Club) e risvegli.
    linkYields = 0x180, linkWakes = 0x184,
    -- Audit del salvataggio (2026-08-21): il corpo gira dal main loop
    -- (OverworldTick da payload_cb1) e il tasto A ha la regola generale.
    bodyTicks = 0x188, aUnscripted = 0x18C,
    -- Fino a 4 giocatori (2026-08-25): i campi remote* qui sopra valgono per
    -- lo slot PRIMARIO (il primo con notizie); le bitmask dicono il quadro.
    rxQueueDrops = 0x190, slotsKnown = 0x194, slotsSpawned = 0x198,
    -- I tre difetti dal campo del 2026-08-30: il fantasma (gli allenatori
    -- vedono ATTRAVERSO l'amico), la bolla del surf che non rinasceva, e il
    -- cambio di avatar bici/surf che spariva invece di cambiare.
    blobScrubbed = 0x19C, resizeRespawns = 0x1A0,
}

local STATUS_NAMES = {
    [0] = "overworld",
    [1] = "LOTTA",
    [2] = "dialogo",
    [3] = "menu",        -- generico: Trainer Card, Opzioni, PC, sezione ignota
    [4] = "zaino",
    [5] = "squadra",
    [6] = "pokedex",
    [7] = "pokenav",
}

local DOOR_NAMES = {
    [0] = nil,              -- niente da dire
    [1] = "apre (ingresso)",
    [2] = "entra",
    [3] = "chiude (ingresso)",
    [4] = "attesa cambio mappa",
    [5] = "apre (uscita)",
    [6] = "esce",
}

local function field(name) return emu:read32(base + OFF_STATE + S[name]) end

local function signedField(name)
    local v = field(name)
    if v >= 0x80000000 then return v - 0x100000000 end
    return v
end

local STATE_NAMES = { [0] = "IDLE", [1] = "SPAWNED" }

local function status()
    local magic = field("magic")
    if magic ~= MAGIC_STATE then
        console:error(string.format(
            "[stato] struttura corrotta: magic 0x%08X — il payload è stato sovrascritto",
            magic))
        return
    end

    local vblanks = field("vblank")
    local delta = vblanks - lastVblank
    lastVblank = vblanks
    local irqs = field("irq")
    local irqDelta = irqs - lastIrq
    lastIrq = irqs
    local vector = emu:read32(IRQ_VECTOR)

    console:log(string.format(
        "[hook ] vettore 0x%08X | vblank noi %d (+%d) / gioco (+%d) | irq +%d | risparmiati %d | IF 0x%04X | reinject %d",
        vector, vblanks, delta, statusVblankDelta, irqDelta,
        field("vbSkips"), field("lastIf") % 0x10000, reinjections))

    -- La raffica misurata il 2026-08-02: HBlank/VCount armati, ~26000 ingressi
    -- per 60 frame del gioco, bit VBlank leggibile per meta' di essi, corpo a
    -- 150-270 giri a frame, gioco che rallenta a vista. Da allora c'e' il
    -- cancello in payload_frame (contatore VBlank del gioco): la raffica resta
    -- - `irq` esplode, `risparmiati` corre - ma il corpo deve restare a ~60/s.
    -- Se questo scatta, il cancello e' rotto e la lentezza e' tornata.
    if delta > statusVblankDelta * 3 and statusVblankDelta > 0 then
        console:error(string.format(
            "[hook ] IL CANCELLO NON MORDE: corpo a %d giri per %d VBlank del gioco (irq +%d, IF 0x%04X)",
            delta, statusVblankDelta, irqDelta, field("lastIf") % 0x10000))
    end

    -- W''-4: lo stack privato. Si riporta il PEGGIO visto, non l'istante: il
    -- picco dura pochi frame ed e' proprio quello che deve starci.
    local used, worstFree, total = stackUsage()
    if used then
        local worstUsed = total - worstFree
        console:log(string.format(
            "[stack] ora %d/%d byte | PEGGIO %d/%d (%d%%) | liberi al peggio %d",
            used, total, worstUsed, total,
            math.floor(worstUsed * 100 / total), worstFree))
        -- Il criterio del piano W''-4 e' meta' stack libera nel caso peggiore.
        -- Sotto quella soglia non si e' ancora rotto niente, ma il margine non
        -- basta piu' a coprire una routine del gioco che non abbiamo esercitato.
        if worstUsed * 2 >= total then
            console:error(string.format(
                "[stack] MARGINE INSUFFICIENTE: usati %d su %d. Allargare payload_stack "
                .. "in payload.ld (e ricontrollare il margine in coda alla EWRAM).",
                worstUsed, total))
        end
    end

    if statusVblankDelta > delta + 2 then
        console:warn(string.format(
            "[hook ] stiamo perdendo VBlank: il gioco ne conta %d, noi %d",
            statusVblankDelta, delta))
    end

    local mapKey = field("mapKey")
    local remoteMapKey = field("remoteMapKey")

    -- Fuori dall'overworld il payload va in letargo APPOSTA (titolo, menu,
    -- lotte): non emette eventi, quindi non si vede niente e non c'e' niente da
    -- debuggare. Va detto, perche' altrimenti sembra un guasto.
    if field("inOverworld") == 0 then
        outOfFieldStreak = outOfFieldStreak + 1
        if outOfFieldStreak == 5 then
            console:warn("[campo] cinque secondi fuori dall'overworld: carica il "
                .. "salvataggio e mettiti a camminare, finche' sei sul titolo o nei "
                .. "menu il payload dorme e non manda niente")
        end
    else
        outOfFieldStreak = 0
    end
    console:log(string.format(
        "[campo] overworld %d | stato %s | mappa %d.%d | io (%d,%d) %s",
        field("inOverworld"), STATE_NAMES[field("state")] or "?",
        (mapKey >> 8) & 0xFF, mapKey & 0xFF,
        signedField("playerX"), signedField("playerY"),
        field("remoteAway") == 1 and "| amico NON DISEGNABILE QUI" or ""))

    -- Il cambio mappa e' un EVENTO del protocollo, non un caso limite: qui si
    -- vede se la transizione e' costata qualcosa e quanto.
    local mapChanges = field("mapChanges")
    if lastMapChanges ~= nil and mapChanges > lastMapChanges then
        local prev = field("prevMapKey")
        console:log(string.format(
            "[mappa ] %d.%d -> %d.%d (%d cambi) | sync ingresso %d | resync ingresso %d "
            .. "| eventi non disegnabili %d | passi implausibili %d | remoti rimossi %d "
            .. "| portati oltre il bordo %d",
            (prev >> 8) & 0xFF, prev & 0xFF, (mapKey >> 8) & 0xFF, mapKey & 0xFF,
            mapChanges, field("entrySyncs"), field("entryResyncs"),
            field("rxOtherMap"), field("rxImplausible"), field("remotesRemoved"),
            field("borderCarries")))
    end
    lastMapChanges = mapChanges

    local STATES = { [0] = "a piedi", [1] = "mach bike", [2] = "acro bike",
                     [3] = "surf", [4] = "sott'acqua" }

    local doorState = field("doorState")

    console:log(string.format(
        "[remoto] slot %d | disegnato (%d,%d) | atteso qui (%d,%d) | dichiarato (%d,%d) mappa %d.%d | %s %s | sprite cambiato %d volte%s",
        field("objectId"), signedField("remoteX"), signedField("remoteY"),
        signedField("remoteLocalX"), signedField("remoteLocalY"),
        signedField("remoteTargetX"), signedField("remoteTargetY"),
        (remoteMapKey >> 8) & 0xFF, remoteMapKey & 0xFF,
        field("remoteGender") == 0 and "maschio" or "femmina",
        STATES[field("remoteState")] or "?", field("gfxChanges"),
        field("remoteVia") == 1 and " | OLTRE IL BORDO" or ""))

    -- `disallineamenti al bordo` DEVE restare 0: e' l'unica prova che la nostra
    -- formula di traduzione e quella del gioco danno lo stesso tile fisico.
    -- Senza, la correzione della deriva rimetterebbe a posto un eventuale
    -- errore entro un secondo e non lo si vedrebbe mai. Se sale, `scarto` dice
    -- di quanto e in che verso.
    local borderMismatch = field("borderMismatch")
    console:log(string.format(
        "[bordo ] eventi tradotti %d | fuori striscia %d | remoti portati oltre il bordo %d "
        .. "| disallineamenti al bordo %d | scarto (%d,%d)",
        field("rxTranslated"), field("stripRejects"), field("borderCarries"),
        borderMismatch, signedField("borderDriftX"), signedField("borderDriftY")))

    if borderMismatch > 0 then
        console:error(string.format(
            "[bordo ] LA FORMULA DI TRADUZIONE E' SBAGLIATA: %d disallineamenti, "
            .. "ultimo scarto (%d,%d). Il gioco e noi mettiamo il remoto su tile "
            .. "diversi dopo un bordo — la correzione della deriva lo sta nascondendo.",
            borderMismatch, signedField("borderDriftX"), signedField("borderDriftY")))
    end

    -- IL GATE DI ASSESTAMENTO. `scaduti` deve restare 0.
    --
    -- `ultimo` e' la durata della finestra di transizione MISURATA: 1 frame per
    -- un warp (li' le coordinate sono gia' coerenti quando si rientra), 2-4 per
    -- un bordo di route, dove il gioco sta caricando la mappa connessa.
    --
    -- E qui c'e' l'unico controllo che puo' davvero dire "questa correzione e'
    -- codice morto": se dopo cinque assestamenti il MASSIMO osservato e' ancora
    -- 1 frame, il gate non ha mai intercettato una finestra vera, e il SYNC
    -- avvelenato del 2026-07-30 e' tornato senza che nessun altro contatore se
    -- ne accorga. E' la stessa lezione di `riallineamenti da fermo 0`.
    local settleEntries = field("settleEntries")
    local settleTimeouts = field("settleTimeouts")
    local settleLast = field("settleFramesLast")
    if settleLast > maxSettleFrames then maxSettleFrames = settleLast end

    console:log(string.format(
        "[assesta] assestamenti %d | scaduti %d | ultimo %d frame (max %d) "
        .. "| passi al bordo %d | gia' allineati %d",
        settleEntries, settleTimeouts, settleLast, maxSettleFrames,
        field("borderWalks"), field("borderHolds")))

    if settleTimeouts > 0 then
        console:error(string.format(
            "[assesta] IL WATCHDOG E' SCATTATO %d volte: c'e' un percorso in cui le "
            .. "coordinate del giocatore e la mappa non tornano mai a dire la stessa "
            .. "cosa. Il commit e' avvenuto lo stesso, quindi il difetto e' silenzioso: "
            .. "e' questo contatore l'unica prova.", settleTimeouts))
    end

    -- Dal 2026-08-21 il corpo gira dal main loop e NON puo' piu' cadere dentro
    -- il caricamento mappa: `ultimo 1 frame` e' l'atteso, non un allarme. Il
    -- gate resta come rete di sicurezza; l'allarme che resta vero e' quello
    -- del watchdog qui sopra. Se il massimo supera 1, il corpo sta vedendo
    -- una transizione a meta' - e allora qualcosa gira di nuovo dall'IRQ.
    if maxSettleFrames > 1 then
        console:warn(string.format(
            "[assesta] massimo %d frame: il corpo ha visto una transizione a meta'. "
            .. "Dal main loop non dovrebbe succedere: qualcosa del corpo gira ancora "
            .. "dall'IRQ?", maxSettleFrames))
    end

    if DOOR_NAMES[doorState] then
        console:log(string.format(
            "[porte ] %s | ingressi %d | uscite %d | ripieghi porta occupata %d | attese scadute %d",
            DOOR_NAMES[doorState], field("doorEnters"), field("doorExits"),
            field("doorBusy"), field("doorTimeouts")))
    else
        console:log(string.format(
            "[porte ] ingressi %d | uscite %d | ripieghi porta occupata %d | attese scadute %d",
            field("doorEnters"), field("doorExits"),
            field("doorBusy"), field("doorTimeouts")))
    end

    -- Indicatori di stato e pass-through. `icone` sale a ogni creazione dello
    -- sprite (quindi anche dopo un warp, che azzera il sistema sprite e obbliga
    -- a ricrearlo): non e' il numero di volte che l'amico e' entrato in un menu.
    -- `grafica fallita` DEVE restare 0, `pass-through` DEVE salire spingendo
    -- contro il remoto - se resta 0 quella correzione e' codice morto.
    -- `sezioni` sale quando il latch riconosce una schermata (Zaino, Squadra,
    -- Pokedex, PokeNav): se apri lo Zaino e resta 0, il riconoscimento e'
    -- codice morto e l'icona degrada sempre a quella generica. `A nei menu`
    -- sale premendo A col remoto davanti MENTRE un menu o un dialogo e' aperto:
    -- e' la prova che la correzione del tasto A morde (prima veniva mangiata).
    local indicatorFails = field("indicatorLoadFails")
    console:log(string.format(
        "[stato ] io %s | amico %s | stato inviati %d / ricevuti %d | icone %d "
        .. "| grafica fallita %d | pass-through %d | sezioni %d | A nei menu %d",
        STATUS_NAMES[field("localStatus")] or "?",
        STATUS_NAMES[field("remoteStatus")] or "?",
        field("statusSent"), field("statusRx"), field("indicatorShown"),
        indicatorFails, field("passThroughs"),
        field("menuLatches"), field("aFreedInMenu")))

    if indicatorFails > 0 then
        console:error(string.format(
            "[stato ] la grafica dell'icona non si carica (%d volte): niente tile OAM "
            .. "liberi, niente slot di palette, oppure nessuno sprite libero.",
            indicatorFails))
    end

    -- Possesso della porta seriale: solo nella build -WithSio, dove i contatori
    -- possono muoversi (nelle altre restano 0 per costruzione e la riga sarebbe
    -- un contatore che non puo' che essere zero: non si stampa).
    -- L'invariante: prese - mollate = 1 dentro l'overworld, 0 fuori. Se dopo
    -- una lotta la differenza resta 1, la porta non e' stata restituita ed e'
    -- ESATTAMENTE il difetto della Torre Lotta - la riga deve gridarlo, non
    -- limitarsi a mostrare due numeri.
    if PAYLOAD_HAS_SIO then
        local acq, rel = field("sioAcquires"), field("sioReleases")
        local owned = acq - rel
        local expected = (field("inOverworld") == 1) and 1 or 0
        console:log(string.format(
            "[sio   ] porta: prese %d | mollate %d | in mano ora %d (atteso %d)",
            acq, rel, owned, expected))
        if owned ~= expected then
            sioOwnStreak = sioOwnStreak + 1
            if sioOwnStreak >= 3 then
                console:error(string.format(
                    "[sio   ] la porta e' %s da %d secondi: il prendi/molla non segue l'overworld",
                    owned > expected and "TRATTENUTA fuori dall'overworld"
                                      or "assente dentro l'overworld",
                    sioOwnStreak))
            end
        else
            sioOwnStreak = 0
        end
        if acq == 0 and field("vblankCount") > 600 then
            console:warn("[sio   ] 10 secondi di gioco e nessuna presa: il cablaggio SioInit e' codice morto?")
        end
        -- I contatori del driver (g_sio in sio.c). In emulatore non c'e' nessun
        -- partner, quindi: `irq seriali` deve restare 0 (se sale, qualcosa
        -- genera IRQ seriali dal nulla ed e' LA misura che cerchiamo);
        -- `resync` sale di 1 ogni ~2 s di porta presa e silenziosa - se corre
        -- piu' veloce, il tempo interno del payload sta correndo (raffica).
        if PAYLOAD_G_SIO then
            local function sioc(off) return emu:read32(PAYLOAD_G_SIO + off) end
            console:log(string.format(
                "[sio   ] driver: irq seriali %d | tx %d parole / %d frame | rx %d parole / %d frame "
                .. "| errori frame %d | resync %d | tx pieno %d | rx pieno %d | zero %d | err SIOCNT %d",
                sioc(0x00), sioc(0x04), sioc(0x0C), sioc(0x08), sioc(0x10),
                sioc(0x14), sioc(0x18), sioc(0x1C), sioc(0x20), sioc(0x24), sioc(0x28)))
        end
    end

    -- ALLARME SULLO SCOSTAMENTO. Con la coda vuota e nessun movimento in corso,
    -- "disegnato" e "atteso qui" DEVONO coincidere: se non coincidono per piu'
    -- di tre secondi, la correzione della deriva non sta scattando. E'
    -- esattamente il difetto del 2026-07-30 (la posa da fermo teneva
    -- heldMovementActive a TRUE per sempre e rendeva il ramo irraggiungibile),
    -- che il log conteneva gia' ma nessuno leggeva.
    --
    -- Si confronta con `remoteLocalX/Y`, cioe' con la posizione dichiarata GIA'
    -- TRADOTTA nel nostro spazio, e non con `remoteTargetX/Y`: da quando l'amico
    -- puo' stare nella route accanto, quelle due sono in spazi diversi e
    -- confrontarle direttamente darebbe un allarme falso ogni secondo. Il
    -- controllo continua quindi a valere ANCHE oltre il bordo, che e' il punto:
    -- una correzione che non si puo' verificare e' una correzione di cui non
    -- sapremo mai se e' codice morto.
    --
    -- Niente math.abs: la libreria `math` non e' mai stata usata in questo
    -- script e l'ambiente Lua di mGBA e' ristretto. Un `math` nil darebbe un
    -- errore per ogni riga di stato, che e' gia' successo con la tabella S.
    local dx = signedField("remoteX") - signedField("remoteLocalX")
    local dy = signedField("remoteY") - signedField("remoteLocalY")
    local spread = (dx < 0 and -dx or dx) + (dy < 0 and -dy or dy)
    if field("state") == 1 and field("remoteAway") == 0 and doorState == 0
       and field("rxPending") == 0 and spread > 0 then
        driftStreak = driftStreak + 1
        if driftStreak >= 3 then
            console:warn(string.format(
                "[deriva] scostamento (%d,%d) fermo da %d secondi con la coda vuota: "
                .. "la correzione da fermo non sta scattando (riallineamenti %d)",
                dx, dy, driftStreak, field("idleFixes")))
        end
    else
        driftStreak = 0
    end

    -- Le ANIM_RUN_* stanno a 20-23 e ESISTONO SOLO nella tabella dello sprite a
    -- piedi. Con uno sprite in bici o in surf un animNum >= 20 vuol dire lettura
    -- fuori dall'array e DMA di dimensione arbitraria nella OBJ VRAM: e' la causa
    -- degli NPC che cambiano aspetto a caso. Qui si vede subito.
    local animNum = field("remoteAnimNum")
    local remoteState = field("remoteState")
    console:log(string.format(
        "[sprite] gfxId %d | animNum %d | girate %d | ricreato per dimensione %d | spawn saltati %d",
        field("remoteGfxId"), animNum, field("rxTurns"),
        field("gfxResizes"), field("spawnSkipped")))

    console:log(string.format(
        "[posa  ] pose da fermo %d | riallineamenti da fermo %d | blob surf creati %d / distrutti %d",
        field("settles"), field("idleFixes"),
        field("blobsCreated"), field("blobsDestroyed")))

    -- Fino a 4 giocatori (2026-08-25): le bitmask dicono QUALI slot hanno
    -- notizie e quali un avatar a schermo; i campi remote* qui sopra valgono
    -- per lo slot primario. `coda piena` deve restare ~0: se sale, un amico
    -- produce piu' di quanto il suo remoto consumi.
    console:log(string.format(
        "[slot  ] notizie %d%d%d | a schermo %d%d%d | scarti coda piena %d",
        field("slotsKnown") % 2, math.floor(field("slotsKnown") / 2) % 2,
        math.floor(field("slotsKnown") / 4) % 2,
        field("slotsSpawned") % 2, math.floor(field("slotsSpawned") / 2) % 2,
        math.floor(field("slotsSpawned") / 4) % 2,
        field("rxQueueDrops")))

    -- Il trampolino sui tasti: `A soppresse` che sale premendo A davanti al
    -- remoto e' la prova che l'hook morde. Il vettore deve puntare dentro il
    -- payload; se punta altrove il gioco ce l'ha riscritto e lo rimetteremo.
    local cb1 = emu:read32(ADDR_GMAIN_CB1)
    console:log(string.format(
        "[tasti ] callback1 0x%08X %s | originale 0x%08X | installato %d volte "
        .. "| A soppresse %d (+%d senza script) | corpo dal main loop %d giri",
        cb1,
        (cb1 >= base and cb1 < base + reserved) and "(nostro)" or "(del gioco)",
        field("origCallback1"), field("cb1Installs"), field("aBlocked"),
        field("aUnscripted"), field("bodyTicks")))

    -- Il corpo gira dal main loop (2026-08-21): in overworld bodyTicks DEVE
    -- avanzare fra un rapporto e l'altro. Fermo = il remoto e' morto anche se
    -- il VBlank batte, ed e' l'unico modo di accorgersene dal log.
    if field("inOverworld") == 1 and lastBodyTicks ~= nil
        and field("bodyTicks") == lastBodyTicks then
        console:error("[tasti ] CORPO FERMO: in overworld ma OverworldTick non gira "
            .. "(hook su callback1 perso? cb1Installs non sale?)")
    end
    lastBodyTicks = field("bodyTicks")

    -- Un blob creato e mai distrutto e' uno sprite orfano che galleggia da solo.
    if field("blobsCreated") - field("blobsDestroyed") > 1 then
        console:warn("[posa  ] blob del surf orfani: creati piu' di quanti ne siano stati distrutti")
    end

    if animNum >= 20 and remoteState ~= 0 then
        console:error(string.format(
            "[sprite] animNum %d fuori range per lo stato %s: overrun della tabella di animazione",
            animNum, STATES[remoteState] or "?"))
    end

    console:log(string.format(
        "[rete ] %s | inviati %d | ricevuti %d | applicati %d passi + %d sync | in coda %d | recupero %d | correzioni %d (%d in pixel)",
        link.role .. (link.connected and " connesso" or ""),
        link.sent, link.received, field("rxSteps"), field("rxSyncs"),
        field("rxPending"), field("movesHurried"), field("rxCorrections"),
        field("pixelFixes")))

    -- `%d in pixel` sono gli offset di disegno azzerati sullo sprite del
    -- remoto: oggi SOLO il residuo del dondolio del surf. Zero e' il valore
    -- normale finche' nessuno surfa - non c'e' niente da gridare. (Fino al
    -- 2026-08-28 qui c'era un avviso che diceva il contrario: nasceva dalla
    -- diagnosi del sub-pixel, che il banco ha poi smentito.)

    -- I CONTATORI DEL RUOLO RELAY, che finora salivano senza che nessuno li
    -- leggesse (regola del progetto: un contatore che nessun log dice e' un
    -- contatore che non esiste). `senza avatar` sale col QUARTO amico in poi
    -- (16 object event per mappa: e' il tetto del motore, non un guasto);
    -- `arretrati` e `via` dicono che il dedup e i saluti stanno lavorando.
    if link.role == "relay" then
        local amici = 0
        for _ in pairs(ws.peers) do amici = amici + 1 end
        console:log(string.format(
            "[rete ] stanza %d peer %d | amici visti %d | senza avatar %d eventi "
            .. "| arretrati %d | VIA %d%s",
            RELAY_ROOM, RELAY_PEER, amici, ws.scartati, ws.arretrati, ws.bye,
            ws.riagganci > 0 and (" | riagganci " .. ws.riagganci) or ""))
    end

    -- IL DOPPIO INVIO E' LA CURA DEGLI SCATTINI, e "copie scartate" e' l'unico
    -- modo di sapere se sta arrivando. Con --copie 2 deve valere circa quanto
    -- i passi applicati: se resta 0 le copie non arrivano e la ridondanza non
    -- c'e' (regola del progetto: un contatore che non puo' che restare a zero
    -- e' un difetto, non un dettaglio).
    console:log(string.format(
        "[copie ] scartate %d", field("rxDupes")))

    console:log(string.format(
        -- "spawn %d/%d falliti" si leggeva al contrario (sembrava "N riusciti su M"),
        -- e ha gia' fatto perdere tempo a interpretare un log riuscito come un difetto.
        "[code ] TX emessi %d, persi %d | RX scartati %d | mosse %d ok / %d rifiutate | spawn %d tentati / %d FALLITI | adottati %d | despawn %d",
        field("eventsEmitted"), field("eventsDropped"), rxDropped,
        field("movesQueued"), field("movesRejected"),
        field("spawnAttempts"), field("spawnFailures"),
        field("adoptions"), field("despawns")))

    console:log(string.format(
        "[pulizia] remoti distrutti %d | fantasmi spazzati %d | scudo save %d "
        .. "| VIA ricevuti %d | RX buttati in letargo %d | eventi altra mappa %d "
        .. "| passi implausibili %d",
        field("remotesRemoved"), field("remotesSwept"), field("stagingScrubs"),
        field("rxLeaves"), field("rxDrained"), field("rxOtherMap"),
        field("rxImplausible")))

    console:log(string.format(
        "[carta ] chunk inviati %d, ricevuti %d | bitmap 0x%04X%s | aperture %d",
        field("cardChunksTx"), field("cardChunksRx"), field("cardRxBitmap"),
        (field("cardRxBitmap") == 0x3FFF) and " (COMPLETA: A per aprirla)" or "",
        field("cardShows")))

    -- Stampata solo se e' successo: in una sessione senza Cable Club il
    -- contatore non puo' che essere 0 e la riga sarebbe rumore.
    if field("linkYields") > 0 then
        -- rese == risvegli a link finito; una resa NON pareggiata mentre non
        -- si sta scambiando e' il congelamento del 2026-08-15.
        console:log(string.format(
            "[club  ] rese al link del gioco %d, risvegli %d%s",
            field("linkYields"), field("linkWakes"),
            (field("linkYields") > field("linkWakes"))
                and " (LINK APERTO: se non state scambiando, e' il payload congelato)"
                or ""))
    end

    -- Stampata SOLO a manopola attiva: spenta, i contatori non possono che
    -- essere 0 e la riga sarebbe rumore (regola del progetto).
    if OPT_PERDI_UN_PASSO_OGNI > 0 then
        console:log(string.format(
            "[perdita] simulata 1 ogni %d | PASSI visti %d | persi di proposito %d "
            .. "| implausibili a valle %d | riallineamenti da fermo %d",
            OPT_PERDI_UN_PASSO_OGNI, lossSeen, lossDropped,
            field("rxImplausible"), field("idleFixes")))
    end

    -- Il club, se e' successo qualcosa. Stessa regola delle altre righe: un
    -- contatore che non puo' che essere zero non si stampa.
    if ClubLua then
        local riga = ClubLua.riga()
        if riga then console:log(riga) end
    end

    if field("eventsDropped") > 0 then
        console:warn("[code ] eventi TX persi: la coda si riempie più in fretta di quanto la svuoto")
    end
    if field("spawnAttempts") > 0 and field("spawnAttempts") == field("spawnFailures") then
        console:warn("[remoto] tutti gli spawn falliti: nessuno slot object event libero?")
    end
end

-- =============================================================================

local giveUp = false

callbacks:add("frame", function()
    if giveUp then return end

    if not injected then
        local r = inject()
        if r == "stop" then
            giveUp = true          -- ROM sbagliata: non ha senso riprovare a ogni frame
            return
        end
        injected = r
        if injected then linkStart() end
        return
    end

    -- Il CLUB per primo: quando e' in corso, il gioco non e' nell'overworld e
    -- gli eventi di camminata non hanno senso. La rete pero' va pompata lo
    -- stesso, o i blocchi del partner non arriverebbero mai.
    local inClub = false
    if ClubLua then
        local ok, r = pcall(ClubLua.tick)
        if ok then
            inClub = r
        elseif not clubRotto then
            clubRotto = true
            console:error("[club ] la sessione si e' rotta: " .. tostring(r))
        end
    end

    -- Le code vanno servite ogni frame, non una volta al secondo: 16 slot si
    -- riempiono in fretta se si corre in bici. DURANTE il club la coda si
    -- svuota lo stesso ma gli eventi si BUTTANO: al bancone la camminata non
    -- ha senso, e lasciarli accumulare faceva salire "TX persi" e gridare
    -- l'avviso della coda piena per tutta la sessione (campo 2026-08-27).
    for _, raw in ipairs(drainTx()) do
        if not inClub then linkSend(raw) end
    end
    linkPump()

    local gameVblank = emu:read32(ADDR_GMAIN_VBLANK1)

    if lastStatusVblank == nil then
        lastStatusVblank = gameVblank   -- primo campione: nessun delta sensato
        return
    end

    if gameVblank - lastStatusVblank >= VBLANK_PER_STATUS then
        statusVblankDelta = gameVblank - lastStatusVblank
        lastStatusVblank = gameVblank
        LOG.inizio()
        status()
        LOG.fine()
    end
end)

console:log(string.format(
    "[inject] script caricato, payload %d byte, base 0x%08X, rete '%s' — inietto al prossimo frame",
    #payload, base, tostring(LINK_ROLE)))
