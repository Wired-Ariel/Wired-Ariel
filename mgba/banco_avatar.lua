-- =============================================================================
-- banco_avatar.lua - bici e bolla del surf, provati a schermo
--                    (2026-08-30)
-- =============================================================================
--
-- COSA PROVA, e perche' serviva un banco apposta.
--
-- Due difetti riferiti dal campo il 30/08 vivono sull'aspetto dell'amico:
--
--   1. BICI. «L'amico non vede le animazioni delle bici.» Il cambio di sprite
--      fra 256 byte (a piedi) e 512 (bici, surf) obbliga a distruggere e
--      ricreare l'object event, e il respawn passava da quattro cancelli che
--      potevano rimandarlo all'infinito: l'avatar spariva invece di cambiare.
--   2. BOLLA DEL SURF. «Al cambio mappa o dopo la lotta l'amico e' senza
--      animazione di surf.» L'id della bolla resta scritto dentro l'object
--      event e nessuno lo azzera: dopo un reset degli sprite era un id
--      stantio, e chi lo credeva valido non ricreava piu' la bolla.
--
-- PERCHE' NON BASTA GIOCARE. Per vederli servirebbe un amico che sale in
-- bici e va in surf, cioe' un salvataggio con bici e Surf e qualcuno che li
-- usi. Ma il remoto e' un object event come un altro e il suo aspetto lo
-- decide UN BYTE del protocollo (avatarState, byte 11 di NetEvent): il banco
-- lo scrive a mano nella mailbox RX, esattamente come farebbe la rete. Cosi'
-- si esercitano gli stessi rami di codice senza dipendere dalla partita.
--
-- IL CRITERIO E' NUMERICO, non visivo: si leggono i contatori di
-- PayloadState. Le foto restano come contorno.
--
-- USO: si carica DOPO l'iniettore, in una partita gia' collegata a un amico.
--   AUTO_SYMS deve essere gia' definito (l'autopilota lo fa).

local BASE = PAYLOAD_BASE or 0x0203CF80
local STAT = BASE + 0x10
local MBOX = BASE + (PAYLOAD_OFF_MAILBOX or 0x200)

local MAGIC_STATE = 0x53544154
local MAGIC_MBOX  = 0x584F424D

-- Gli offset dentro PayloadState che servono qui. Il prefisso PS_ non e'
-- decorativo: build.ps1 cerca ogni `local PS_NOMECAMPO = 0x...` sotto mgba/ e
-- pretende che coincida con la struct in main.c.
--
-- Serve perche' il 2026-08-30 e' successo il contrario: tolto un campo in
-- mezzo alla struct, questo file ha continuato a leggere gli indirizzi vecchi
-- e ha riportato numeri PLAUSIBILI MA SBAGLIATI - i respawn contati come
-- scrub, e uno zero letto oltre la fine della struct. Un banco che mente e'
-- peggio di un banco rotto.
local PS_OBJECTID       = 0x18
local PS_REMOTESTATE    = 0x8C
local PS_GFXCHANGES     = 0x90
local PS_GFXRESIZES     = 0xA0
local PS_BLOBSCREATED   = 0xA8
local PS_BLOBSDESTROYED = 0xAC
local PS_BLOBSCRUBBED   = 0x19C
local PS_RESIZERESPAWNS = 0x1A0

local MB_TXSLOTS = 0x0C
local MB_RXHEAD  = 0x10
local MB_RXTAIL  = 0x14
local MB_RXSLOTS = 0x18
local MB_TX      = 0x20
local EVENT_SIZE = 12

local ADDR_gObjectEvents
do
    local f = io.open(AUTO_SYMS or "", "r")
    if f then
        for riga in f:lines() do
            local n, v = riga:match("#define%s+ADDR_(%w+)%s+%(?0[xX](%x+)")
            if n == "gObjectEvents" then ADDR_gObjectEvents = tonumber(v, 16) end
        end
        f:close()
    end
end

local out = io.open((AUTO_DIR or ".") .. "/banco_avatar.txt", "w")
local function dire(s)
    console:log("[avatar] " .. s)
    if out then out:write(s .. "\n"); out:flush() end
end

if not ADDR_gObjectEvents then
    dire("SIMBOLI MANCANTI: serve AUTO_SYMS con gObjectEvents")
    return
end

local function oe(i)  return ADDR_gObjectEvents + i * 0x24 end
local function cont(off) return emu:read32(STAT + off) end

-- Lo slot dell'object event del remoto primario, se e' a schermo.
--
-- L'IDENTITA' PRIMA DI LEGGERE, come fa il payload stesso: non basta che lo
-- slot sia `active`. Fra un despawn e il respawn il gioco puo' aver
-- riassegnato quello slot a un NPC qualunque, e misurargli l'elevazione
-- vorrebbe dire attribuire al nostro fantasma i numeri di un altro. I remoti
-- hanno localId 0xE0/0xE1/0xE2 (+0x08 dell'object event).
local function slotRemoto()
    if emu:read32(STAT) ~= MAGIC_STATE then return nil end
    local id = emu:read32(STAT + PS_OBJECTID)
    if id >= 16 then return nil end
    local o = oe(id)
    if (emu:read8(o) & 0x01) == 0 then return nil end
    local lid = emu:read8(o + 0x08)
    if lid < 0xE0 or lid > 0xE2 then return nil end
    return id
end

-- L'ULTIMO EVENTO ARRIVATO, ricopiato cambiando il solo avatarState: cosi' il
-- finto evento porta la mappa e le coordinate VERE dell'amico e il payload non
-- lo scarta come "di un'altra mappa". Si prende dalla coda RX l'ultimo slot
-- scritto (head - 1), che e' l'evento piu' recente consegnato al payload.
local function ultimoRx()
    if emu:read32(MBOX) ~= MAGIC_MBOX then return nil end
    local head  = emu:read32(MBOX + MB_RXHEAD)
    local slots = emu:read32(MBOX + MB_RXSLOTS)
    if slots == 0 then return nil end
    local rxBase = MB_TX + emu:read32(MBOX + MB_TXSLOTS) * EVENT_SIZE
    local idx = (head - 1) % slots
    local a = MBOX + rxBase + idx * EVENT_SIZE
    local b = {}
    for i = 0, EVENT_SIZE - 1 do b[i + 1] = emu:read8(a + i) end
    return b
end

-- Consegna un evento al payload, come fa deliverRx dell'iniettore.
local function deliverRx(b)
    local head  = emu:read32(MBOX + MB_RXHEAD)
    local tail  = emu:read32(MBOX + MB_RXTAIL)
    local slots = emu:read32(MBOX + MB_RXSLOTS)
    if slots == 0 then return false end
    local nxt = (head + 1) % slots
    if nxt == tail then return false end
    local rxBase = MB_TX + emu:read32(MBOX + MB_TXSLOTS) * EVENT_SIZE
    local a = MBOX + rxBase + head * EVENT_SIZE
    for i = 1, EVENT_SIZE do emu:write8(a + i - 1, b[i]) end
    emu:write32(MBOX + MB_RXHEAD, nxt)
    return true
end

-- Un SYNC con lo stato voluto, ricalcato sull'ultimo evento vero.
-- byte 1 = type (nibble basso 2 = SYNC, nibble alto = slot mittente)
-- byte 3 = speed, byte 12 = avatarState
local seqFinto = 200
local function mandaStato(avatarState, speed)
    local b = ultimoRx()
    if not b then return false end
    b[1] = (b[1] & 0xF0) | 2          -- SYNC, stesso slot mittente
    b[3] = speed
    seqFinto = (seqFinto + 1) % 256
    b[4] = seqFinto
    b[12] = avatarState
    return deliverRx(b)
end

-- =============================================================================
-- La macchina del banco
-- =============================================================================
--
-- Ogni passo dura ATTESA frame, poi si guardano i contatori. Il finto SYNC si
-- RIPETE per tutta la durata del passo, perche' il payload rilegge lo stato a
-- ogni evento e un solo pacchetto potrebbe cadere in un frame in cui il remoto
-- non e' pronto ad accettare movimenti.
local PASSI = {
    { stato = 0, speed = 0, nome = "a piedi" },
    { stato = 1, speed = 2, nome = "MACH BIKE" },
    { stato = 3, speed = 3, nome = "SURF" },
    { stato = 2, speed = 2, nome = "ACRO BIKE" },
    { stato = 0, speed = 0, nome = "a piedi (ritorno)" },
}
local ATTESA = 90        -- frame per passo: il respawn ha bisogno di qualche giro

local frame, fase, iPasso, quando = 0, "ASPETTA", 0, 0
local base = {}
local esiti = {}
local finito = false

local function fotografa(nome)
    if not emu.screenshot then return false end
    return pcall(function()
        emu:screenshot((AUTO_DIR or ".") .. "/foto-avatar-" .. nome .. ".png")
    end)
end

callbacks:add("frame", function()
    if finito then return end
    frame = frame + 1

    local id = slotRemoto()

    if frame % 2 ~= 0 then return end

    if fase == "ASPETTA" then
        if not id then return end
        if not ultimoRx() then return end
        dire("amico a schermo nello slot " .. id ..
             " | comincio")
        fase, quando, iPasso = "PASSO", frame, 1
        return
    end

    if fase == "PASSO" then
        local p = PASSI[iPasso]
        if not p then fase = "FINE"; return end
        -- si ripete il finto SYNC ogni 10 frame per tutta la durata del passo
        if (frame - quando) % 10 == 0 then mandaStato(p.stato, p.speed) end

        if frame - quando >= ATTESA then
            local e = {
                nome = p.nome,
                aSchermo = (id ~= nil),
                gfxId = id and emu:read8(oe(id) + 0x05) or nil,
                statoVisto = cont(PS_REMOTESTATE),
                gfxResizes = cont(PS_GFXRESIZES),
                resizeRespawns = cont(PS_RESIZERESPAWNS),
                gfxChanges = cont(PS_GFXCHANGES),
                blobsCreated = cont(PS_BLOBSCREATED),
                blobsDestroyed = cont(PS_BLOBSDESTROYED),
                blobScrubbed = cont(PS_BLOBSCRUBBED),
            }
            esiti[#esiti + 1] = e
            dire(string.format(
                "%-18s -> %s gfx %s | stato visto %d | resize %d / respawn %d | "
                .. "cambi %d | bolla +%d -%d scrub %d",
                p.nome, e.aSchermo and "A SCHERMO" or "SPARITO!",
                tostring(e.gfxId), e.statoVisto, e.gfxResizes, e.resizeRespawns,
                e.gfxChanges, e.blobsCreated, e.blobsDestroyed, e.blobScrubbed))
            fotografa(iPasso .. "-" .. (p.nome:gsub("[^%w]", "")))
            iPasso = iPasso + 1
            quando = frame
        end
        return
    end

    if fase == "FINE" then
        finito = true
        dire("")
        dire("=== VERDETTO ===")

        -- bici e surf: mai sparito, e ogni cambio di dimensione ha avuto
        -- il suo respawn
        local sempreVisto = true
        for _, e in ipairs(esiti) do
            if not e.aSchermo then sempreVisto = false end
        end
        dire((sempreVisto and "OK  " or "KO  ") ..
             "AVATAR: l'amico e' rimasto a schermo in tutti i " .. #esiti ..
             " stati" .. (sempreVisto and "" or " <- e' SPARITO in almeno uno"))
        -- QUANTI RESPAWN SU QUANTI RESIZE, e perche' non si pretende 1 a 1.
        -- Il respawn immediato salta i quattro cancelli del giro normale, ma
        -- non puo' saltare l'ultimo: TrySpawnRemote rifiuta se il tile e'
        -- fuori dalla finestra in cui il gioco tiene vivi gli object event
        -- (InsideView). Se l'amico e' lontano in quell'istante, il respawn
        -- resta al giro normale - che e' giusto cosi': spawnare fuori
        -- finestra significa farsi cancellare al frame dopo. Il criterio vero
        -- e' quello sopra, «non e' mai sparito»; questo dice quanto spesso la
        -- strada veloce ha funzionato.
        local rz = cont(PS_GFXRESIZES)
        local rr = cont(PS_RESIZERESPAWNS)
        dire(((rz > 0 and rr > 0) and "OK  " or "KO  ") ..
             "BICI/SURF: " .. rz .. " cambi di dimensione, " .. rr ..
             " ripresi SUBITO" ..
             ((rz == 0) and " <- ZERO cambi: lo stato finto non e' arrivato"
                         or ((rr == rz) and " (tutti)"
                             or string.format(" (%d hanno aspettato il giro normale: amico fuori finestra)", rz - rr))))

        -- la bolla del surf
        local bc = cont(PS_BLOBSCREATED)
        dire(((bc > 0) and "OK  " or "KO  ") ..
             "BOLLA: " .. bc .. " create, " .. cont(PS_BLOBSDESTROYED) ..
             " distrutte, " .. cont(PS_BLOBSCRUBBED) .. " id stantii azzerati" ..
             ((bc > 0) and "" or " <- ZERO: in surf non e' mai nata"))
        dire("=== fine ===")
        if out then out:close(); out = nil end
    end
end)

dire("banco avatar armato: aspetto l'amico a schermo")
