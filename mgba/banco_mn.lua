-- =============================================================================
-- banco_mn.lua - la casella dei field effect resta del giocatore? (2026-09-26)
-- =============================================================================
--
-- IL DIFETTO DAL CAMPO. «Ogni tanto la MN fuori lotta mostra il Pokemon
-- sbagliato: sprite buggato, in negativo, MissingNo.» Le MN lasciano l'indice
-- di squadra in gFieldEffectArguments[0] e lo rileggono frame DOPO (la domanda
-- SI'/NO degli script, la posa dal menu). Se in mezzo l'avatar dell'amico fa
-- un passo con un effetto a terra, il GIOCO scrive in quella casella per conto
-- suo, e FldEff_FieldMoveShowMonInit legge gPlayerParty[x] fuori squadra.
-- La cura (RemoteSpriteCb in main.c) rimette la casella com'era dopo la
-- callback dello sprite dell'amico.
--
-- COME LO PROVOCA. Aspettare che l'amico passi nell'erba alta proprio mentre
-- si risponde a una domanda e' una lotteria. Ma il SALTO sul posto produce
-- sempre due scritture, su qualunque terreno: l'ombra
-- (DoShadowFieldEffect: [0] = localId dell'amico, 0xE0..0xE2) e la polvere
-- dell'atterraggio (GroundEffect_JumpLandingDust: [0] = x del tile). Il banco
-- da' all'avatar dell'amico MOVEMENT_ACTION_JUMP_IN_PLACE_DOWN a mano, come
-- farebbe ObjectEventSetHeldMovement, e a ogni frame:
--   1. controlla che la casella contenga ancora la SENTINELLA scritta al frame
--      prima (il posto di "indice di squadra lasciato da una MN");
--   2. se no, la sporcatura e' dell'amico se cade entro 60 frame dal suo
--      salto, altrimenti di qualcun altro (un NPC nell'erba, il gioco stesso);
--   3. riscrive la sentinella.
-- Il giocatore locale viene tenuto FERMO (tasti azzerati dopo l'autopilota),
-- cosi' le sue orme non sporcano la misura.
--
-- CRITERIO: sporcature durante i salti dell'amico = 0 e `casella MN protetta`
-- (fxArgsKept) salita almeno quanto i salti. Controprova (guardia spenta):
-- sporcature attribuite all'amico >= salti.
--
-- USO: .\tools\prova-in-tre.ps1 -Rom <rom> -Giocatori 2 -Syms it -Banco <...>\mgba\banco_mn.lua
-- Verdetto in build\prova-in-tre\banco_mn.txt

local BASE = PAYLOAD_BASE or 0x0203CF80
local STAT = BASE + 0x10
local MAGIC_STATE = 0x53544154

-- Offset di PayloadState: build.ps1 li confronta con la struct (prefisso PS_).
local PS_OBJECTID   = 0x18
local PS_FXARGSKEPT = 0x1A4

local SALTI_VOLUTI  = 20
local OGNI          = 90      -- frame fra un salto e il successivo
local JUMP_IN_PLACE_DOWN = 0x46
local SENT = 0x5A5A0000       -- sentinella: 0x5A5A0000 + i nella casella i

local A = {}
do
    local f = io.open(AUTO_SYMS or "", "r")
    if f then
        for riga in f:lines() do
            local n, v = riga:match("#define%s+ADDR_(%w+)%s+%(?0[xX](%x+)")
            if n then A[n] = tonumber(v, 16) end
        end
        f:close()
    end
end

local out = io.open((AUTO_DIR or ".") .. "/banco_mn.txt", "w")
local function dire(s)
    console:log("[mn] " .. s)
    if out then out:write(s .. "\n"); out:flush() end
end

if not (A.gObjectEvents and A.gSprites and A.gFieldEffectArguments and A.gPlayerAvatar) then
    dire("SIMBOLI MANCANTI: servono gObjectEvents, gSprites, gFieldEffectArguments, gPlayerAvatar")
    return
end

local FX = A.gFieldEffectArguments
local function oe(i) return A.gObjectEvents + i * 0x24 end
local function cont(off) return emu:read32(STAT + off) end

-- L'identita' prima di toccare: attivo E col localId di un remoto.
local function slotRemoto()
    if emu:read32(STAT) ~= MAGIC_STATE then return nil end
    local id = cont(PS_OBJECTID)
    if id >= 16 then return nil end
    local o = oe(id)
    if (emu:read8(o) & 0x01) == 0 then return nil end
    local lid = emu:read8(o + 0x08)
    if lid < 0xE0 or lid > 0xE2 then return nil end
    return id
end

local function scriviSentinella()
    for i = 0, 7 do emu:write32(FX + i * 4, SENT + i) end
end

local function sentinellaIntatta()
    for i = 0, 7 do
        if emu:read32(FX + i * 4) ~= SENT + i then return false end
    end
    return true
end

local armata = false
local frame, ultimoSalto = 0, -OGNI
local salti, sporcheAmico, sporcheAltri = 0, 0, 0
local keptIniziale = nil
local playerXY0 = nil
local finito = false
local esempi = {}
local mosso = false

local function playerXY()
    local pid = emu:read8(A.gPlayerAvatar + 0x05)   -- gPlayerAvatar.objectEventId
    if pid >= 16 then return nil end
    return emu:read16(oe(pid) + 0x10), emu:read16(oe(pid) + 0x12)
end

local function verdetto()
    finito = true
    local kept = cont(PS_FXARGSKEPT) - (keptIniziale or 0)
    dire(string.format("salti dati all'amico     %d", salti))
    dire(string.format("casella MN protetta      +%d (fxArgsKept)", kept))
    dire(string.format("sporcature dell'amico    %d", sporcheAmico))
    dire(string.format("sporcature di altri      %d", sporcheAltri))
    dire(string.format("giocatore locale fermo   %s", mosso and "NO (conta solo l'attribuzione per valore)" or "si'"))
    for _, e in ipairs(esempi) do dire("  esempio: " .. e) end
    if salti >= SALTI_VOLUTI and sporcheAmico == 0 and kept >= salti then
        dire("VERDETTO: OK - la casella resta del giocatore anche mentre l'amico salta")
    else
        dire("VERDETTO: FALLITO")
    end
end

local dopo = 0
callbacks:add("frame", function()
    if finito then
        -- Battito dopo il verdetto: l'emulatore e il corpo del payload devono
        -- continuare a girare (bodyTicks, +0x188) anche a banco finito.
        dopo = dopo + 1
        if dopo % 240 == 0 and dopo <= 240 * 8 then
            dire(string.format("battito +%d frame: bodyTicks %d", dopo, cont(0x188)))
        end
        return
    end
    frame = frame + 1
    emu:clearKeys(0x3FF)          -- il giocatore locale sta fermo

    local id = slotRemoto()
    if not id then
        armata = false
        if frame % 600 == 0 then dire(string.format("frame %d: amico non ancora a schermo", frame)) end
        if frame > 60 * 150 then dire("amico mai comparso"); verdetto() end
        return
    end
    if keptIniziale == nil then
        keptIniziale = cont(PS_FXARGSKEPT)
        local px, py = playerXY()
        playerXY0 = { px, py }
        dire(string.format("amico a schermo (object event %d), si parte", id))
    end

    local px, py = playerXY()
    if px ~= playerXY0[1] or py ~= playerXY0[2] then mosso = true end

    -- 1-2. la sentinella del frame prima e' ancora li'?
    if armata and not sentinellaIntatta() then
        local v0 = emu:read32(FX)
        local o = oe(id)
        local lid = emu:read8(o + 0x08)
        local x = emu:read16(o + 0x10)
        -- Attribuzione per TEMPO, non per valore: l'ombra scrive col
        -- ObjectEventGetLocalIdAndMap solo il BYTE basso ([0] = 0x5A5A00E0,
        -- e per la MN conta proprio quello: (u8) 0xE0 = gPlayerParty[224]),
        -- la polvere scrive la x in PIXEL dello sprite (0x78, 0x88), non del
        -- tile. Quello che si sa per certo e' che il salto dell'amico dura
        -- una quarantina di frame: una sporcatura li' dentro e' sua.
        if frame - ultimoSalto <= 60 then
            sporcheAmico = sporcheAmico + 1
        else
            sporcheAltri = sporcheAltri + 1
        end
        if #esempi < 6 then
            esempi[#esempi + 1] = string.format("frame %d: [0]=0x%X [1]=0x%X [2]=0x%X (amico lid 0x%X x %d)",
                frame, v0, emu:read32(FX + 4), emu:read32(FX + 8), lid, x)
        end
    end

    -- il salto sul posto, come ObjectEventSetHeldMovement (event_object_movement.c:4870)
    local o = oe(id)
    local flags0 = emu:read8(o)
    local held = (flags0 & 0x40) ~= 0 and (flags0 & 0x80) == 0
    if salti < SALTI_VOLUTI and not held and frame - ultimoSalto >= OGNI then
        local spr = emu:read8(o + 0x04)
        if spr < 64 then
            emu:write8(o + 0x1C, JUMP_IN_PLACE_DOWN)
            emu:write8(o, (flags0 | 0x40) & 0x7F)
            emu:write16(A.gSprites + spr * 0x44 + 0x32, 0)   -- sActionFuncId = 0
            salti = salti + 1
            ultimoSalto = frame
        end
    end

    -- 3. si riarma
    scriviSentinella()
    armata = true

    if salti >= SALTI_VOLUTI and frame - ultimoSalto > 120 then verdetto() end
end)

dire("banco MN caricato")
