-- =============================================================================
-- autopilota.lua - fa girare una prova SENZA MANI (2026-08-25)
-- =============================================================================
--
-- PERCHE' ESISTE. Ogni prova a schermo di questo progetto è sempre finita
-- con "codice -> Lain esegue -> riporta cosa succede": Claude sull'hardware
-- è cieco, e in emulatore non vede né lo schermo né può premere un tasto.
-- mGBA 0.11 però ha `--script` e l'API di scripting con `emu:setKeys` e
-- `emu:screenshot`: tanto basta per portare la partita in overworld, far
-- camminare l'avatar e FOTOGRAFARE il risultato. La foto è la prova.
--
-- COME SI USA (lo fa tools\prova-in-tre.ps1, non a mano):
--   mGBA.exe --script mgba\autopilota.p1.lua  <rom>
-- dove autopilota.pN.lua è tre righe generate: le variabili qui sotto e poi
-- un dofile di questo file e dell'iniettore vero (inject.pN.lua).
--
-- LE VARIABILI CHE IL CHIAMANTE DEVE AVER GIA' DEFINITO:
--   AUTO_NOME     etichetta nei log e nel nome dei file ("p1", "p2", ...)
--   AUTO_DIR      cartella dove scrivere foto e rapporto
--   AUTO_INJECT   percorso dello script di iniezione da caricare DOPO
--                 essere entrati in partita
--   AUTO_PASSI    (opzionale) direzione della camminata: "verticale" o
--                 "orizzontale". Due giocatori che camminano su assi diversi
--                 si distinguono a colpo d'occhio nella foto.

local NOME   = AUTO_NOME or "p?"
local DIR    = AUTO_DIR or "."
local ASSE   = AUTO_PASSI or "verticale"

local function log(s)
    console:log(string.format("[auto %s] %s", NOME, s))
    local f = io.open(DIR .. "/autopilota-" .. NOME .. ".txt", "a")
    if f then f:write(s .. "\n"); f:close() end
end

-- --- lo stato del gioco, letto dagli stessi indirizzi dell'iniettore -------
-- Li ridefiniamo qui perché l'autopilota gira PRIMA dell'iniettore (deve
-- portare la partita in overworld: prima di allora l'iniezione non parte).
local ADDR_GMAIN_CB2 = 0x030022C0 + 0x04   -- gMain.callback2
-- gMain e' a 0x030022C0 (ADDR_gMain in game_syms_it.h) e callback2 sta a
-- +0x04 (include/main.h:11): callback1 +0x00, callback2 +0x04. Il primo
-- tentativo aveva +0x08, che e' un altro campo.
local CB2_OVERWORLD  = nil                 -- lo prende dal file dei simboli

do
    -- game_syms_it.h ha la riga: #define ADDR_CB2_Overworld 0x0808xxxx
    -- Il percorso lo passa il chiamante (AUTO_SYMS): indovinarlo da DIR era
    -- sbagliato di una cartella, e il sintomo e' subdolo - CB2_OVERWORLD
    -- resta nil, inOverworld() e' sempre falso e l'autopilota "non entra in
    -- partita" anche quando ci e' entrato.
    local f = io.open(AUTO_SYMS or (DIR .. "/../payload/game_syms_it.h"), "r")
    if f then
        for riga in f:lines() do
            -- Il file scrive: #define ADDR_CB2_Overworld (0x08085E70u)
            -- - parentesi e suffisso 'u' compresi. Il pattern li salta.
            local v = riga:match("#define%s+ADDR_CB2_Overworld%s+%(?0[xX](%x+)")
            if v then CB2_OVERWORLD = tonumber(v, 16) end
        end
        f:close()
    end
end

if not CB2_OVERWORLD then
    log("ATTENZIONE: non ho letto ADDR_CB2_Overworld da " .. tostring(AUTO_SYMS)
        .. " - non sapro' dire quando siamo in partita")
end

local function inOverworld()
    if not CB2_OVERWORLD then return false end
    local cb2 = emu:read32(ADDR_GMAIN_CB2)
    return (cb2 & 0xFFFFFFFE) == (CB2_OVERWORLD & 0xFFFFFFFE)
end

-- --- i tasti ---------------------------------------------------------------
local K = C.GBA_KEY
local function premi(tasto)
    emu:clearKeys(0x3FF)
    if tasto then emu:addKey(tasto) end
end

-- --- i contatori del payload ------------------------------------------------
-- Gli stessi offset della tabella S di inject_body.lua (contratto verificato
-- in build): stato a PAYLOAD_BASE + 0x10. Si leggono qui per scrivere,
-- accanto a ogni foto, i numeri che dicono se cio' che si vede e' NOSTRO.
local PAYLOAD_BASE = AUTO_BASE or 0x0203CF80
local S = { state = 0x14, objectId = 0x18, spawnAttempts = 0x1C,
            spawnFailures = 0x20, remoteX = 0x30, remoteY = 0x34,
            rxSteps = 0x70, rxSyncs = 0x74, despawns = 0x40,
            -- rxCorrections e pixelFixes: i riposizionamenti del remoto e,
            -- di quelli, quanti hanno dovuto correggere anche i PIXEL (la
            -- compensazione di gFieldCamera e il residuo del dondolio del
            -- surf). E' il criterio S-5: senza questo numero accanto alla
            -- foto, un allenatore a schermo non prova niente.
            rxCorrections = 0x78, pixelFixes = 0x38,
            slotsKnown = 0x194, slotsSpawned = 0x198 }

local function campo(nome)
    return emu:read32(PAYLOAD_BASE + 0x10 + S[nome])
end

local function statoPayload()
    local ok, s = pcall(function()
        local magic = emu:read32(PAYLOAD_BASE + 0x10)
        if magic ~= 0x53544154 then return "payload non iniettato" end
        return ("noti %d/a schermo %d | remoto (%d,%d) slot oe %d | passi rx %d "
                .. "sync %d | spawn %d/%d falliti | despawn %d | correzioni %d "
                .. "(%d in pixel)"):format(
            campo("slotsKnown"), campo("slotsSpawned"),
            campo("remoteX"), campo("remoteY"), campo("objectId"),
            campo("rxSteps"), campo("rxSyncs"),
            campo("spawnAttempts"), campo("spawnFailures"), campo("despawns"),
            campo("rxCorrections"), campo("pixelFixes"))
    end)
    return ok and s or ("lettura fallita: " .. tostring(s))
end

-- --- la macchina a stati ---------------------------------------------------
-- 1. SBLOCCA  : A a intervalli finché non siamo in overworld (titolo, "CONTINUA",
--               eventuali finestre di salvataggio)
-- 2. CARICA   : dofile dell'iniettore, una volta sola
-- 3. CAMMINA  : avanti e indietro sull'asse assegnato, con pause
-- 4. FOTO     : screenshot a intervalli + rapporto finale
local stato, frame, dallaFase = "SBLOCCA", 0, 0
local foto = 0
local passo = 0

callbacks:add("frame", function()
    frame = frame + 1
    dallaFase = dallaFase + 1

    if stato == "SBLOCCA" then
        -- A premuto a impulsi: tenerlo premuto non fa avanzare i menu.
        if dallaFase % 20 < 8 then premi(K.A) else premi(nil) end
        if inOverworld() and dallaFase > 120 then
            premi(nil)
            log(("in overworld dopo %d frame"):format(frame))
            stato, dallaFase = "CARICA", 0
        elseif dallaFase > 3600 then
            log("NON sono riuscito a entrare in partita in 60 s")
            stato, dallaFase = "FOTO", 0
        end
        return
    end

    if stato == "CARICA" then
        premi(nil)
        if dallaFase < 30 then return end     -- un mezzo secondo di assestamento
        log("carico l'iniettore: " .. tostring(AUTO_INJECT))
        local ok, err = pcall(dofile, AUTO_INJECT)
        if not ok then log("INIEZIONE FALLITA: " .. tostring(err)) end
        -- BANCO OPZIONALE (2026-08-28): con AUTO_BANCO si carica anche un
        -- secondo script, che nella partita gia' avviata provoca il caso da
        -- provare invece di aspettarlo. Oggi lo usa banco_subpixel.lua.
        if AUTO_BANCO then
            log("carico il banco: " .. tostring(AUTO_BANCO))
            local ok2, err2 = pcall(dofile, AUTO_BANCO)
            if not ok2 then log("BANCO FALLITO: " .. tostring(err2)) end
        end
        stato, dallaFase = "CAMMINA", 0
        return
    end

    if stato == "CAMMINA" then
        -- Un passo dura ~16 frame: si tiene premuto per 24 e si stacca per 8,
        -- così i passi escono netti e il payload vede currentCoords cambiare.
        local ciclo = dallaFase % 32
        local avanti = (math.floor(dallaFase / 256) % 2) == 0
        local tasto
        if ASSE == "orizzontale" then
            tasto = avanti and K.RIGHT or K.LEFT
        else
            tasto = avanti and K.DOWN or K.UP
        end
        if ciclo < 24 then premi(tasto) else premi(nil) end
        if ciclo == 0 then passo = passo + 1 end

        -- una foto ogni 4 secondi, e ACCANTO I NUMERI: una foto senza i
        -- contatori non dimostra niente (un allenatore sullo schermo puo'
        -- essere un NPC della mappa), e i contatori senza la foto nemmeno.
        if dallaFase % 240 == 120 then
            foto = foto + 1
            local nome = ("%s/foto-%s-%d.png"):format(DIR, NOME, foto)
            local ok = pcall(function() emu:screenshot(nome) end)
            log(("foto %d (%s) passi ~%d | %s"):format(
                foto, ok and "ok" or "FALLITA", passo, statoPayload()))
        end
        return
    end

    if stato == "FOTO" then
        premi(nil)
        if dallaFase == 30 then
            pcall(function() emu:screenshot(("%s/foto-%s-finale.png"):format(DIR, NOME)) end)
            log("foto finale scritta")
        end
        return
    end
end)

log("autopilota caricato (asse " .. ASSE .. ")")
