-- banco_audio.lua - il difetto dell'audio in lotta (2026-08-30), provato in
-- emulatore con la build DA HARDWARE (-WithSio) e il vero avvio del multiboot.
--
-- PERCHE' ESISTE (2026-09-25)
-- ---------------------------
-- Si credeva che il difetto dell'audio fosse "provabile solo sul fisico,
-- perche' in emulatore sio.c non gira". Non e' vero: handoff_test.lua (generato da
-- hw\mbstub\build.ps1) carica in mGBA il payload -WithSio e rifa' lo stesso
-- ingresso in AgbMain dello stub - e SioInit/SioShutdown girano davvero,
-- anche senza adattatore dall'altra parte. Quello che manca in emulatore e'
-- solo il master che batte le parole; ma il difetto non stava li'.
--
-- COSA MISURA
-- -----------
-- Il meccanismo del difetto, non il suo sintomo: la fonte di verita' del gioco
-- per gli interrupt e' sRegIE (gpu_regs.c:14, .bss di gpu_regs.o a 0x03000818
-- + 0xC2 = 0x030008DA, IWRAM identica fra ROM USA e italiana), e REG_IE deve
-- contenerne SEMPRE tutti i bit. Un bit del gioco che manca in REG_IE e' un
-- handler che non gira: VCount = m4aSoundVSync (la musica). Il bit seriale e'
-- escluso dal confronto: in lotta il gioco lo chiede (sRegIE 0x00C5 = VBlank,
-- VCount, Timer3, Seriale) ma SioShutdown lo spegne apposta, ed e' la cura:
-- con l'adattatore attaccato ogni parola svegliava SerialCB e il Timer3.
-- Misurato il 2026-09-25: in lotta il gioco NON chiede mai l'HBlank (la
-- colonna "HBlank chiesto" resta a 0 anche in vanilla) - la vecchia ipotesi
-- sugli effetti a scanline dell'ingresso era un'ipotesi.
--
-- Il banco fa N lotte vere (erba alta del Percorso 102: selvatiche, e
-- l'allenatore che ci vede; si combatte premendo A) e per ogni lotta conta i frame in cui manca un bit del gioco.
--
-- PARAMETRI (globali, da impostare prima del dofile)
--   BANCO_DIR    cartella dove scrivere banco_audio.txt e le foto
--   BANCO_TEST   percorso di handoff_test.lua; nil = gioco vanilla (controllo)
--   BANCO_LOTTE  quante lotte (default 10)
--   BANCO_GSIO   indirizzo di g_sio nel payload.map della build provata
--
-- CRITERI (tutti numeri)
--   frame con bit del gioco mancanti in lotta = 0, in ogni lotta
--   frame con inBattle alto e SIOCNT con l'IRQ armato = 0
--   solo col payload: SCRUB IE (g_sio.ieScrubs) sale di 1 a ogni lotta,
--                     e l'IRQ del payload continua a salire.

local DIR     = BANCO_DIR or "."
local TEST    = BANCO_TEST
local LOTTE   = BANCO_LOTTE or 10

local CB2_OVERWORLD   = 0x08085E70      -- ROM italiana (game_syms_it.h)
local ADDR_GMAIN      = 0x030022C0
local ADDR_CB2        = ADDR_GMAIN + 0x04
local ADDR_VBLANK1    = ADDR_GMAIN + 0x20
local ADDR_INBATTLE   = ADDR_GMAIN + 0x439   -- bit 1 (game_types.h)
local ADDR_SB1PTR     = 0x03005D8C
local ADDR_SREGIE     = 0x030008DA
local REG_IE          = 0x04000200
local IRQ_SERIAL      = 0x0080
local IRQ_HBLANK      = 0x0002
local IRQ_VCOUNT      = 0x0004
local REG_SIOCNT      = 0x04000128
local SIOCNT_IRQ      = 0x4000

local PAYLOAD_BASE    = 0x0203CF80
local ST_IRQ          = PAYLOAD_BASE + 0x10 + 0x04
-- g_sio viene da build\payload.map della build che si prova: si sposta a
-- ogni modifica di sio.c, quindi si passa da fuori se diverso.
local G_SIO_IESCRUBS  = (BANCO_GSIO or 0x0203FB98) + 0x34

local K_A, K_RIGHT, K_LEFT = 1, 16, 32

local f = io.open(DIR .. "/banco_audio.txt", "w")
local function scrivi(s)
    f:write(s .. "\n"); f:flush()
    console:log("[audio] " .. s)
end

local modo = TEST and "PAYLOAD -WithSio (handoff)" or "VANILLA (nessun payload)"
scrivi("banco_audio: " .. modo .. ", lotte " .. LOTTE)

local stato, dalla, frame = "SBLOCCA", 0, 0
local sparato = (TEST == nil)
local vblPrima = 0
local lotte, lotta = 0, nil
local totMancanti, lotteMalate = 0, 0
local scrubPrima = 0

local function cb2() return emu:read32(ADDR_CB2) & 0xFFFFFFFE end
local function inBattle() return (emu:read8(ADDR_INBATTLE) & 0x02) ~= 0 end
local function payload() return TEST ~= nil end
local function scrubs() return payload() and emu:read32(G_SIO_IESCRUBS) or 0 end

local function posX()
    local sb1 = emu:read32(ADDR_SB1PTR)
    local x = emu:read16(sb1)
    if x >= 0x8000 then x = x - 0x10000 end
    return x
end

local function fine(msg)
    scrivi("")
    scrivi(msg)
    local fx = io.open(DIR .. "/FINE", "w"); fx:write(msg); fx:close()
    emu:setKeys(0)
    stato = "FINE"
end

local dir = K_LEFT

callbacks:add("frame", function()
    frame = frame + 1; dalla = dalla + 1
    if stato == "FINE" then return end
    if frame > 60 * 60 * 25 then
        fine("KO  TEMPO SCADUTO dopo " .. lotte .. " lotte"); return
    end

    if stato == "SBLOCCA" then
        emu:setKeys((dalla % 20 < 8) and K_A or 0)
        if cb2() == CB2_OVERWORLD and dalla > 120 then
            emu:setKeys(0)
            scrivi(("in overworld dopo %d frame, x=%d"):format(frame, posX()))
            if not sparato then
                stato, dalla = "CARICA", 0
            else
                stato, dalla = "ASSESTA", 0
            end
        elseif dalla > 3600 then
            fine("KO  non sono entrato in partita")
        end

    elseif stato == "CARICA" and dalla > 60 then
        vblPrima = emu:read32(ADDR_VBLANK1)
        scrivi("carico " .. TEST)
        local ok, err = pcall(dofile, TEST)
        if not ok then fine("KO  caricamento fallito: " .. tostring(err)); return end
        stato, dalla = "ATTENDI_RIAVVIO", 0

    elseif stato == "ATTENDI_RIAVVIO" then
        -- handoff.S rientra in AgbMain: il contatore VBlank del gioco riparte.
        local v = emu:read32(ADDR_VBLANK1)
        if v < vblPrima then
            sparato = true
            scrivi(("handoff avvenuto (VBlank %d -> %d): riparto dal titolo"):format(vblPrima, v))
            stato, dalla = "SBLOCCA", 0
        else
            vblPrima = v
            if dalla > 900 then fine("KO  handoff mai avvenuto") end
        end

    elseif stato == "ASSESTA" and dalla > 90 then
        local ie, sr = emu:read16(REG_IE), emu:read16(ADDR_SREGIE)
        scrivi(("overworld: REG_IE 0x%04X, sRegIE 0x%04X, irq payload %s"):format(
            ie, sr, payload() and tostring(emu:read32(ST_IRQ)) or "-"))
        if (sr & IRQ_VCOUNT) == 0 then
            fine("KO  sRegIE non ha VCount in overworld: indirizzo di sRegIE sbagliato?")
            return
        end
        stato, dalla = "CAMMINA", 0

    elseif stato == "CAMMINA" then
        if inBattle() then
            lotte = lotte + 1
            lotta = { frame = 0, mancanti = 0, maschera = 0, primo = nil,
                      vcount = 0, hblank = 0, sioirq = 0, irq0 = payload() and emu:read32(ST_IRQ) or 0 }
            scrubPrima = scrubs()
            emu:setKeys(0)
            stato, dalla = "LOTTA", 0
            return
        end
        local x = posX()
        if x <= 3 then dir = K_RIGHT elseif x >= 6 then dir = K_LEFT end
        -- A a impulsi: chiude i dialoghi (un allenatore che ci vede apre un
        -- testo che aspetta A prima della lotta).
        emu:setKeys(((dalla % 40) < 4) and K_A or dir)
        if dalla == 1 or dalla % 600 == 0 then
            local sb1 = emu:read32(ADDR_SB1PTR)
            scrivi(("  erba: x=%d y=%d mappa %d.%d"):format(x, emu:read16(sb1 + 2),
                emu:read8(sb1 + 4), emu:read8(sb1 + 5)))
            if dalla == 600 then emu:screenshot(DIR .. "/erba.png") end
        end
        if dalla > 60 * 120 then fine("KO  due minuti nell'erba senza lotte") end

    elseif stato == "LOTTA" then
        local ie, sr = emu:read16(REG_IE), emu:read16(ADDR_SREGIE)
        local manca = sr & (~ie) & (~IRQ_SERIAL) & 0xFFFF
        lotta.frame = lotta.frame + 1
        if (ie & IRQ_VCOUNT) ~= 0 then lotta.vcount = lotta.vcount + 1 end
        if (sr & IRQ_HBLANK) ~= 0 then lotta.hblank = lotta.hblank + 1 end
        -- Solo finche' inBattle e' alto: negli ultimi ~10 frame (inBattle gia'
        -- sceso, callback2 non ancora CB2_Overworld) il payload si riprende la
        -- porta di proposito - "nostra sempre, tranne lotta e link".
        if inBattle() and (emu:read16(REG_SIOCNT) & SIOCNT_IRQ) ~= 0 then
            lotta.sioirq = lotta.sioirq + 1
            lotta.sioA = lotta.sioA or lotta.frame
            lotta.sioB = lotta.frame
        end
        if manca ~= 0 then
            lotta.mancanti = lotta.mancanti + 1
            lotta.maschera = lotta.maschera | manca
            if not lotta.primo then
                lotta.primo = ("frame %d: REG_IE 0x%04X, sRegIE 0x%04X"):format(lotta.frame, ie, sr)
            end
        end
        if lotta.frame == 150 then
            emu:screenshot(("%s/lotta-%02d.png"):format(DIR, lotte))
        end
        -- Solo A: LOTTA, prima mossa, e avanti coi testi. La squadra e' al
        -- livello 40 contro i livelli 3-5 del Percorso 102: si vince al primo
        -- colpo. (La fuga non va: contro gli allenatori non si scappa.)
        emu:setKeys((dalla % 20 < 6) and K_A or 0)

        if not inBattle() and cb2() == CB2_OVERWORLD then
            local ds = scrubs() - scrubPrima
            local irq = payload() and (emu:read32(ST_IRQ) - lotta.irq0) or 0
            local esito = (lotta.mancanti == 0 and lotta.sioirq == 0) and "OK" or "KO"
            scrivi(("%s  lotta %2d: %5d frame | bit del gioco mancanti in %d frame%s | "
                .. "VCount in REG_IE %d/%d | HBlank chiesto dal gioco %d | SIOCNT con IRQ %d%s | SCRUB IE +%d | irq payload +%d"):format(
                esito, lotte, lotta.frame, lotta.mancanti,
                lotta.mancanti > 0 and (" (maschera 0x%04X, primo %s)"):format(lotta.maschera, lotta.primo) or "",
                lotta.vcount, lotta.frame, lotta.hblank, lotta.sioirq,
                lotta.sioA and (" (frame %d..%d)"):format(lotta.sioA, lotta.sioB) or "", ds, irq))
            totMancanti = totMancanti + lotta.mancanti
            if esito == "KO" then lotteMalate = lotteMalate + 1 end
            if payload() and ds ~= 1 then
                scrivi(("    !! SCRUB IE e' salito di %d invece che di 1"):format(ds))
            end
            if lotte >= LOTTE then
                if lotteMalate == 0 then
                    fine(("OK  %d lotte, nessun bit del gioco mai spento (%s)"):format(lotte, modo))
                else
                    fine(("KO  %d lotte su %d con bit del gioco spenti, %d frame in tutto (%s)"):format(
                        lotteMalate, lotte, totMancanti, modo))
                end
                return
            end
            stato, dalla = "CAMMINA", 0
        elseif lotta.frame > 60 * 120 then
            fine("KO  lotta bloccata oltre due minuti")
        end
    end
end)
