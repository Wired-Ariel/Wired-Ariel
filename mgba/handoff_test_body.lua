-- =============================================================================
-- handoff_test_body.lua - prova in EMULATORE il pezzo decisivo del blocco W'
-- =============================================================================
--
-- COSA PROVA, e cosa no
-- ---------------------
-- Lo stub multiboot completo NON e' provabile in emulatore: richiede un GBA
-- acceso a slot vuoto e una cartuccia inserita a caldo, e mGBA non sa fare
-- nessuna delle due cose. Quello che si puo' provare - ed e' la parte a esito
-- incerto, il resto e' contorno - e' handoff.S: che entrare dentro AgbMain a
-- 0x080003CE, dopo aver azzerato la EWRAM tranne la coda, lasci un gioco
-- funzionante con il nostro payload gia' agganciato.
--
-- COME CI ENTRA SENZA writeRegister
-- ---------------------------------
-- ace_probe.lua ha gia' imparato che il PC non e' esposto da tutte le build di
-- mGBA. Quindi non si tocca: si usa la strada che il progetto ha gia'
-- dimostrata, cioe' il vettore IRQ. Si scrive handoff.S nella coda di EWRAM e
-- si punta 0x03007FFC su di lui: al primo interrupt il processore ci salta
-- dentro in ARM, che e' esattamente il modo in cui ci arriverebbe dallo stub.
-- handoff non torna - si imposta i suoi stack e riparte da AgbMain - quindi il
-- fatto di essere entrati da un IRQ non lascia niente in sospeso.
--
-- E' un reboot del gioco fatto dall'interno. Se funziona qui, l'unica cosa che
-- resta da dimostrare sull'hardware e' il trasporto (multiboot + inserimento a
-- caldo), che e' meccanica nota.
--
-- USO
--   1. mGBA con la ROM ITALIANA di Smeraldo, ENTRA IN PARTITA e cammina.
--   2. Tools > Scripting > Load script > mgba\handoff_test.lua
--   3. dopo 3 secondi il gioco riparte da solo: e' il test.
--
-- CRITERI, tutti contatori, nessuna impressione visiva:
--   W'-2a  il contatore VBlank del gioco AZZERA e poi risale
--          -> AgbMain e' stato rieseguito e InitMainCallbacks ha girato
--   W'-2b  0x03007FFC vale PAYLOAD_BASE, scritto da handoff.S e non da qui
--   W'-2c  il magic 'HOOK' in coda e' sopravvissuto all'azzeramento
--   W'-2d  irqCount del payload sale di ~60/s
--          -> il nostro codice gira dentro il gioco appena riavviato
--
-- Se W'-2a passa e W'-2d no, l'errore e' nell'hook. Se non passa nemmeno
-- W'-2a, l'errore e' in handoff.S prima del salto.

local IRQ_VECTOR   = 0x03007FFC
local OFF_MAGIC    = 0x08
local MAGIC_HOOK   = 0x4B4F4F48        -- 'HOOK'
local OFF_STATE    = 0x10
local MAGIC_STATE  = 0x53544154        -- 'STAT'
local ST_IRQ       = 0x04
local EWRAM_END    = 0x02040000

-- Contatore VBlank del gioco (gMain.vblankCounter1), ROM italiana. Stesso
-- indirizzo che usa mgba\inject_body.lua.
local ADDR_GMAIN_VBLANK1 = 0x030022E0

local DELAY_FRAMES  = 180              -- 3 s: il tempo di essere in partita
local REPORT_EVERY  = 60
local REPORT_FOR    = 600              -- 10 s di osservazione dopo lo sparo

local frame        = 0
local fired        = false
local firedAt      = 0
local vblBeforeMax = 0
local vblAfterSeen = false
local irqAtReport  = nil
local vblAtReport  = nil
local verdict      = {}

-- Fase W'-3, dopo il verdetto: si continua a guardare mentre Lain gioca.
local lastVbl      = nil
local stalls       = 0

local function say(s)  console:log("[handoff] " .. s)  end
local function bad(s)  console:error("[handoff] " .. s) end

local function writeBytes(addr, s)
    for i = 1, #s do
        emu:write8(addr + i - 1, s:byte(i))
    end
end

-- --- sonda IWRAM (sola lettura) ---------------------------------------------
--
-- A cosa serve: il driver SIO non ci sta nel payload (926 byte richiesti contro
-- 776 liberi in coda alla EWRAM, deficit 150). Ma dal .map di pokeemerald il
-- gioco alloca IWRAM solo fino a 0x030078AC, e la IWRAM arriva a 0x03008000:
-- sopra ci sono 1876 byte che il linker non da' a nessuno. Dentro quel tratto
-- pero' scendono gli stack (sp_sys 0x03007E40, sp_irq 0x03007FA0, entrambi
-- verso il basso), quindi quanto sia DAVVERO libero non si deduce dal .map: si
-- misura.
--
-- Come: handoff.S chiama RegisterRamReset(0xFE), che azzera la IWRAM fino a
-- 0x03007DFF. Da quel momento ogni word diversa da zero l'ha scritta qualcuno -
-- i dati del gioco da sotto, lo stack da sopra. Il buco di zeri in mezzo e' lo
-- spazio libero, e si restringe man mano che lo stack scende. Si tiene il
-- PEGGIORE visto nella sessione, non l'ultimo: e' l'unico numero su cui si
-- possa poi appoggiare un .bss.
--
-- Nessuna scrittura: se questa misura sbagliasse per eccesso non se ne
-- accorgerebbe nessuno finche' il driver SIO non corrompe lo stack del gioco.

-- LA FINESTRA E' FISSA, e la prima stesura sbagliava proprio qui. Cercare "il
-- piu' lungo tratto di zeri" in tutta la IWRAM trova, all'inizio, un buco
-- DENTRO il .bss del gioco (0x030063C0-0x030073A4, 4068 byte, misurato): dati
-- veri che non sono ancora stati scritti. Alla passata dopo il massimo si
-- spostava sulla coda, e l'intersezione fra due regioni diverse dava zero.
-- Un numero che non e' ne' il primo ne' il secondo: era rumore con l'aria di
-- una misura convergente.
--
-- Qui si guarda solo dove ha senso: da dove finisce la sezione `iwram` del
-- gioco (0x030078AC dal .map di pokeemerald, confine confermato sull'italiana)
-- fino a sotto sp_sys. Si sale finche' si trovano zeri: la prima word diversa
-- da zero e' il punto piu' basso toccato dallo stack.
local IWRAM_TAIL_LO = 0x030078AC     -- fine della sezione iwram del gioco
local IWRAM_TAIL_HI = 0x03007E00     -- sotto sp_sys, che parte da 0x03007E40

local freeMin, lowMark = nil, nil

-- Massimo IRQ/VBlank visto. Serve a UNA cosa sola: sapere se il gioco e' stato
-- davvero sollecitato. A riposo il rapporto vale 2,00 fisso; salvataggi e lotte
-- fanno raffiche di decine o centinaia. Senza almeno una raffica, un buco IWRAM
-- "stabile" non e' un caso peggiore - e' solo un sistema che non ha fatto
-- niente. E' la trappola del contatore fermo: sembra una misura convergente ed
-- e' una misura mai iniziata.
local maxRatio     = 0
local BURST_RATIO  = 5.0

local function scanIwramTail()
    local a = IWRAM_TAIL_LO
    while a < IWRAM_TAIL_HI and emu:read32(a) == 0 do
        a = a + 4
    end
    local free = a - IWRAM_TAIL_LO
    -- Si tiene il MINIMO: appena lo stack scende una volta fin li', quel tratto
    -- non e' piu' utilizzabile, anche se dopo risale.
    if freeMin == nil or free < freeMin then
        freeMin = free
        lowMark = a
    end
    return free
end

local function fire()
    -- Coda pulita prima di scrivere, come fa zeroRegion() dell'iniettore: il
    -- .bss del payload lo pretende azzerato.
    for a = PAYLOAD_BASE, EWRAM_END - 4, 4 do
        emu:write32(a, 0)
    end

    writeBytes(PAYLOAD_BASE, PAYLOAD_BYTES)
    writeBytes(HANDOFF_BASE, HANDOFF_BYTES)

    local magic = emu:read32(PAYLOAD_BASE + OFF_MAGIC)
    if magic ~= MAGIC_HOOK then
        bad(string.format("payload scritto male: magic 0x%08X invece di 0x%08X",
                          magic, MAGIC_HOOK))
        return false
    end

    vblBeforeMax = emu:read32(ADDR_GMAIN_VBLANK1)

    -- NOTA: qui si punta il vettore su HANDOFF, non sul payload. Se alla fine
    -- 0x03007FFC vale PAYLOAD_BASE, quella scrittura l'ha fatta handoff.S da
    -- solo - ed e' precisamente cio' che il test deve dimostrare.
    emu:write32(IRQ_VECTOR, HANDOFF_BASE)

    say(string.format("sparato: payload %d byte a 0x%08X, handoff %d byte a 0x%08X",
                      #PAYLOAD_BYTES, PAYLOAD_BASE, #HANDOFF_BYTES, HANDOFF_BASE))
    say(string.format("contatore VBlank del gioco prima dello sparo: %d", vblBeforeMax))
    return true
end

local function report()
    local since  = frame - firedAt
    local vbl    = emu:read32(ADDR_GMAIN_VBLANK1)
    local vec    = emu:read32(IRQ_VECTOR)
    local magic  = emu:read32(PAYLOAD_BASE + OFF_MAGIC)
    local stMag  = emu:read32(PAYLOAD_BASE + OFF_STATE)
    local irq    = emu:read32(PAYLOAD_BASE + OFF_STATE + ST_IRQ)

    -- IL RAPPORTO, non la frequenza. Ancorare il criterio ai frame dello script
    -- e' sbagliato: fra due esecuzioni il passo del gioco rispetto al callback
    -- di mGBA e' cambiato (60 VBlank ogni 60 frame la prima volta, 30 la
    -- seconda) senza che il nostro codice c'entrasse niente, e un criterio
    -- "+120/s" avrebbe fatto sospettare un guasto inesistente.
    -- L'invariante vera, identica nelle due prove, e' 2 IRQ per VBlank del
    -- gioco: VBlank + VCount, e il VCount lo abilita EnableVCountIntrAtLine150
    -- due istruzioni dopo il nostro punto di rientro in AgbMain.
    local ratio = "-"
    if irqAtReport ~= nil and vblAtReport ~= nil and vbl > vblAtReport then
        local r = (irq - irqAtReport) / (vbl - vblAtReport)
        if r > maxRatio then maxRatio = r end
        ratio = string.format("%.2f", r)
    end

    say(string.format("+%5d frame | VBlank gioco %-8d | vettore 0x%08X | HOOK %s | STAT %s | irqCount %-7d | IRQ/VBlank %s",
        since, vbl, vec,
        magic == MAGIC_HOOK and "si" or "NO",
        stMag == MAGIC_STATE and "si" or "NO",
        irq, ratio))
    vblAtReport = vbl

    -- W'-2a: il contatore del gioco e' ripartito da zero. E' la firma di
    -- InitMainCallbacks, cioe' la prova che AgbMain e' stato rieseguito.
    if not vblAfterSeen and vbl < vblBeforeMax then
        vblAfterSeen = true
        verdict["W'-2a  AgbMain rieseguito (VBlank azzerato e risalito)"] = true
    end

    if verdict["W'-2b  vettore IRQ scritto da handoff.S"] == nil and vec == PAYLOAD_BASE then
        verdict["W'-2b  vettore IRQ scritto da handoff.S"] = true
    end

    if verdict["W'-2c  coda EWRAM sopravvissuta all'azzeramento"] == nil and magic == MAGIC_HOOK then
        verdict["W'-2c  coda EWRAM sopravvissuta all'azzeramento"] = true
    end

    -- W'-2d: non basta che irqCount sia diverso da zero, deve SALIRE fra due
    -- rilevazioni a un secondo di distanza. Un valore fermo e non nullo
    -- significherebbe che l'hook ha girato una volta e poi e' morto, che e' un
    -- esito diverso e va distinto.
    if irqAtReport ~= nil then
        if irq > irqAtReport then
            verdict["W'-2d  il payload gira nel gioco riavviato (irqCount sale)"] = true
            stalls = 0
        else
            -- Fermo fra due rilevazioni: l'hook non viene piu' chiamato. Si
            -- CONTA, invece di limitarsi a non segnare il criterio, perche' e'
            -- il difetto che W'-3 deve saper vedere mentre Lain gioca.
            stalls = stalls + 1
        end
    end
    irqAtReport = irq

    -- Il verdetto si stampa UNA volta sola, a REPORT_FOR esatto (che e' multiplo
    -- di REPORT_EVERY). Poi non si smette di guardare: comincia W'-3.
    if since == REPORT_FOR then
        say("---- esito W'-2 ----")
        local names = {
            "W'-2a  AgbMain rieseguito (VBlank azzerato e risalito)",
            "W'-2b  vettore IRQ scritto da handoff.S",
            "W'-2c  coda EWRAM sopravvissuta all'azzeramento",
            "W'-2d  il payload gira nel gioco riavviato (irqCount sale)",
        }
        local allOk = true
        for _, n in ipairs(names) do
            if verdict[n] then
                say("  PASSATO  " .. n)
            else
                bad("  FALLITO  " .. n)
                allOk = false
            end
        end
        if allOk then
            say("TUTTI PASSATI: handoff.S regge.")
        else
            bad("Almeno un criterio non e' passato: NON portare questo build sull'hardware.")
        end
        say("")
        say("---- W'-3: sopravvivenza. Ora GIOCA. ----")
        say("Entra e esci da un edificio, cambia route, fai una lotta, salva.")
        say("Ogni 2 s si controlla che irqCount sia salito. Deve salire SEMPRE.")
        say("Il numero da guardare e' IRQ/VBlank, non la frequenza: a riposo")
        say("vale 2,00 (VBlank + VCount). Le raffiche sopra 2 sono normali e")
        say("dicono cosa sta facendo il gioco - il salvataggio su Flash ne")
        say("genera migliaia, perche' programma la memoria a colpi di timer.")
        say("L'unica cosa che deve ucciderlo e' il SOFT RESET A+B+Start+Select,")
        say("che fa SoftReset(RESET_ALL) e azzera la EWRAM: e' un limite noto.")
        lastVbl = vbl
        return false
    end

    -- --- W'-3 ---------------------------------------------------------------
    if since > REPORT_FOR then
        -- Un soft reset azzera la EWRAM e uccide il payload. Va riconosciuto e
        -- detto, non lasciato passare per un guasto: e' l'unico modo di
        -- distinguere "limite previsto" da "l'hook si e' rotto da solo".
        if lastVbl ~= nil and vbl < lastVbl then
            bad(string.format("il gioco e' RIPARTITO (VBlank %d -> %d).", lastVbl, vbl))
            bad("Se hai premuto A+B+Start+Select e' il soft reset: previsto, il payload e' morto.")
            bad("Se NON l'hai premuto, questo e' un difetto e va annotato.")
        end
        lastVbl = vbl

        -- Misura IWRAM: il numero che decide se il driver SIO entra o no.
        -- IWRAM: lo SCOPO E' CAMBIATO il 2026-08-02.
        --
        -- Prima serviva a misurare quanto spazio potevamo prendere sopra
        -- 0x030078AC per il .bss del driver SIO. Quella strada e' stata
        -- abbandonata: lassu' ci sono gli stack del gioco, e il gioco e'
        -- autorizzato a scendere fino a 0x030078AC. Nessuna misura di quanto
        -- scende davvero e' un limite - la memoria e' sua.
        --
        -- Ora la stessa lettura serve al contrario: a VERIFICARE CHE NOI NON LA
        -- TOCCHIAMO. Il numero deve restare grande e mosso solo dallo stack del
        -- gioco; se un giorno si restringesse in modo stabile, vorrebbe dire
        -- che qualcosa di nostro e' finito lassu'.
        local free = scanIwramTail()
        say(string.format("        IWRAM (non nostra): libera ora %d byte, minimo %d "
            .. "(stack del gioco sceso a 0x%08X)", free, freeMin, lowMark))
        if maxRatio < BURST_RATIO then
            say(string.format("        (gioco non ancora sollecitato: max IRQ/VBlank %.2f)",
                maxRatio))
        end

        -- Stallo = l'hook ha smesso di essere chiamato senza che il gioco sia
        -- ripartito. E' il difetto che W'-3 cerca, e va gridato a ogni
        -- rilevazione finche' dura, non detto una volta e poi dimenticato.
        if stalls > 0 then
            bad(string.format("STALLO: irqCount fermo a %d da %d rilevazioni "
                .. "(~%d s). L'hook non viene piu' chiamato.",
                irq, stalls, stalls * 2))
        end
        return false
    end
    return false
end

local done = false

local function onFrame()
    if done then return end
    frame = frame + 1

    if not fired then
        if frame < DELAY_FRAMES then
            if frame % 60 == 0 then
                say(string.format("sparo fra %d s (sii in partita)",
                                  math.floor((DELAY_FRAMES - frame) / 60)))
            end
            return
        end
        fired = true
        firedAt = frame
        if not fire() then done = true end
        return
    end

    -- Prima del verdetto si guarda ogni secondo, dopo ogni due: la fase W'-3
    -- dura quanto Lain gioca, e una riga al secondo per dieci minuti sarebbe
    -- rumore in cui uno stallo passerebbe inosservato.
    local since = frame - firedAt
    local every = (since > REPORT_FOR) and (REPORT_EVERY * 2) or REPORT_EVERY
    if since % every == 0 then
        if report() then done = true end
    end
end

callbacks:add("frame", onFrame)
say(string.format("caricato. payload a 0x%08X, handoff a 0x%08X",
                  PAYLOAD_BASE, HANDOFF_BASE))
