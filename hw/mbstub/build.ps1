# build.ps1 - costruisce mbstub.gba, lo stub multiboot del blocco W'.
#
# NOTA: questo file va tenuto in ASCII puro. PowerShell 5.1 legge gli .ps1 come
# ANSI e i caratteri accentati o i trattini lunghi rompono il parser.
#
# Modellato su hw\siotest\build.ps1, che produce gia' un multiboot che
# l'hardware accetta (M-3 passato). In piu' fa una cosa sola: incolla dentro
# l'immagine il payload gia' compilato, come blob .incbin.
#
# PREREQUISITO: il payload deve essere gia' stato costruito, con gli indirizzi
# della ROM italiana:
#
#     cd overworld-link
#     .\build.ps1 -Syms it
#
# Uso:
#     .\build.ps1
#     .\build.ps1 -LogoFrom "C:\percorso\a\una\rom.gba"
#     .\build.ps1 -Syms usa      # cartuccia INGLESE (BPEE): esce mbstub-usa.gba
#                                  (prima: cd overworld-link ; .\build.ps1 -Syms usa -WithSio)

param(
    # La versione della cartuccia (2026-09-24): "it" = Smeraldo italiano (BPEI,
    # mbstub.gba), "usa" = Emerald inglese USA/Europa (BPEE, mbstub-usa.gba).
    # Il payload inglobato DEVE essere della stessa versione: lo controlla la
    # guardia "versione" qui sotto.
    [ValidateSet("it", "usa")]
    [string]$Syms = "it",
    [string]$LogoFrom = "",
    [string]$Title = "MBSTUB",
    [string]$PayloadBin = ""
)

$ErrorActionPreference = "Stop"
$root  = $PSScriptRoot
$owl   = Split-Path (Split-Path $root -Parent) -Parent   # overworld-link
$repo  = Split-Path $owl -Parent                          # radice del progetto
$build = Join-Path $root "build"
$outName = if ($Syms -eq "usa") { "mbstub-usa" } else { "mbstub" }

$MULTIBOOT_MAX = 0x3FF40

# Devono coincidere con boot_syms_it.h e mbstub.ld. Sono ripetuti qui perche' e'
# il solo posto dove si puo' VERIFICARE il vincolo prima di accendere il GBA.
$PAYLOAD_BASE = 0x0203CF80
$HANDOFF_BASE = 0x0203FE00
$EWRAM_END    = 0x02040000

# --- toolchain ---------------------------------------------------------------
# Toolchain sull'HDD per prima (vedi overworld-link\build.ps1: la copia su C:
# e' sparita il 2026-08-02).
$gcc = $null
$guesses = @(
    "D:\Progettini\arm-gnu-toolchain-14.3\bin\arm-none-eabi-gcc.exe",
    "C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\14.3 rel1\bin\arm-none-eabi-gcc.exe"
)
foreach ($guess in $guesses) { if (Test-Path $guess) { $gcc = $guess; break } }
if (-not $gcc) { $gcc = (Get-Command arm-none-eabi-gcc -ErrorAction SilentlyContinue).Source }
if (-not $gcc) { throw "arm-none-eabi-gcc non trovato (atteso D:\Progettini\arm-gnu-toolchain-14.3\bin)" }
$binDir  = Split-Path $gcc
$objcopy = Join-Path $binDir "arm-none-eabi-objcopy.exe"

if (-not (Test-Path $build)) { New-Item -ItemType Directory -Path $build | Out-Null }

Write-Output "toolchain : $gcc"

# --- il payload --------------------------------------------------------------
if (-not $PayloadBin) { $PayloadBin = Join-Path $owl "build\payload.bin" }
if (-not (Test-Path $PayloadBin)) {
    throw "payload non trovato: $PayloadBin`nCostruiscilo prima:  cd $owl ; .\build.ps1 -Syms it"
}
# LA BUILD DA EMULATORE NON DEVE FINIRE QUI DENTRO. Lo stub va sul GBA FISICO,
# dove l'unico modo di parlare e' il driver SIO: un payload compilato senza
# -WithSio si carica, il gioco parte, e il cavo resta MUTO. E' successo il
# 2026-08-22, e sembrava un guasto hardware.
#
# Non e' una svista che capita una volta: e' una trappola COSTRUITA nel flusso,
# perche' tools\prepara-sito-web.ps1 compila un payload per l'emulatore
# (-LinkRole relay) come ultimo passo, e quindi LASCIA build\payload.bin nello
# stato sbagliato. Chi rifa' lo stub dopo aver preparato il sito - cioe'
# l'ordine naturale - inglobava la build muta senza che niente lo dicesse.
#
# La guardia guarda il .map, che e' la sola prova di cosa e' stato linkato
# davvero: sio.o c'e' o non c'e'. Stesso controllo che prepara-sito-web fa
# gia' sul suo lato; qui serve perche' e' QUI che il byte sbagliato entra.
$payloadMap = Join-Path (Split-Path $PayloadBin) "payload.map"
if (Test-Path $payloadMap) {
    if ((Get-Content $payloadMap -Raw) -notmatch "sio\.o") {
        throw "build\payload.bin e' la build da EMULATORE (nel .map non c'e' sio.o): sul cavo sarebbe MUTA. " +
              "Rifai  .\build.ps1 -Syms it -WithSio  e poi questo script. " +
              "Se arrivi da tools\prepara-sito-web.ps1, e' NORMALE: quello ricompila per l'emulatore alla fine."
    }
} else {
    Write-Warning "build\payload.map assente: non posso verificare che il payload abbia il driver SIO dentro."
}

# LA VERSIONE DEL PAYLOAD DEVE ESSERE QUELLA DELLO STUB (2026-09-24). Il payload
# confronta gMain.callback2 con l'indirizzo ASSOLUTO di CB2_Overworld, che nella
# ROM italiana e in quella inglese e' diverso: un payload dell'altra lingua
# partirebbe, e resterebbe muto per sempre. Il suo binario contiene quel
# indirizzo come costante: si cerca quello giusto e si esclude l'altro.
$cb2Atteso = if ($Syms -eq "usa") { 0x08085E5C } else { 0x08085E70 }
$cb2Altro  = if ($Syms -eq "usa") { 0x08085E70 } else { 0x08085E5C }
$pbytes = [System.IO.File]::ReadAllBytes($PayloadBin)
$ptext  = [System.Text.Encoding]::GetEncoding(28591).GetString($pbytes)
function Contiene([uint32]$v) {
    foreach ($x in @($v, ($v -bor 1))) {
        $s = [System.Text.Encoding]::GetEncoding(28591).GetString([BitConverter]::GetBytes([uint32]$x))
        if ($ptext.IndexOf($s, [System.StringComparison]::Ordinal) -ge 0) { return $true }
    }
    return $false
}
if (-not (Contiene $cb2Atteso) -or (Contiene $cb2Altro)) {
    throw ("build\payload.bin non e' della versione $Syms (CB2_Overworld atteso 0x{0:X8}). " -f $cb2Atteso) +
          "Rifai  .\build.ps1 -Syms $Syms -WithSio  e poi questo script con -Syms $Syms."
}
Write-Output ("versione  : {0} (payload con CB2_Overworld 0x{1:X8}, stub -> {2}.gba)" -f $Syms, $cb2Atteso, $outName)

$payloadSize = (Get-Item $PayloadBin).Length
$roomBelowHandoff = $HANDOFF_BASE - $PAYLOAD_BASE

# Il .bin e' solo cio' che si copia. Quello che il payload OCCUPA a runtime e'
# di piu' - codice piu' stack privato - e lo dice il simbolo payload_end, come
# fa gia' la build del payload. Riportare la dimensione del .bin come "margine
# libero" darebbe un numero piu' generoso del vero, che e' il modo classico di
# accorgersi del problema quando e' tardi.
$payloadElf = Join-Path (Split-Path $PayloadBin) "payload.elf"
$reservedSize = $payloadSize
if (Test-Path $payloadElf) {
    $nm = Join-Path $binDir "arm-none-eabi-nm.exe"
    $endLine = & $nm $payloadElf | Select-String -Pattern "payload_end" | Select-Object -First 1
    if ($endLine) {
        $payloadEnd = [Convert]::ToUInt32(($endLine.Line -split "\s+")[0], 16)
        $reservedSize = $payloadEnd - $PAYLOAD_BASE
    }
}

Write-Output ("payload   : {0} byte copiati, {1} byte riservati (codice + stack)  ({2})" -f $payloadSize, $reservedSize, $PayloadBin)

# Il vincolo che main.c ricontrolla a caldo. Qui si scopre a freddo, che e'
# molto meglio: a caldo l'unico segnale sarebbe uno schermo magenta.
if ($payloadSize -gt $roomBelowHandoff) {
    throw ("PAYLOAD TROPPO GRANDE: {0} byte, ma sotto HANDOFF_BASE ce ne stanno {1}. " -f $payloadSize, $roomBelowHandoff) +
          "Alza HANDOFF_BASE in boot_syms_it.h E in mbstub.ld (devono restare uguali)."
}

# Blob generato, non versionato: il payload resta un artefatto separato con la
# sua build, e questo stub non ne conosce il contenuto.
$blobPath = Join-Path $build "payload_blob.S"
$incPath  = $PayloadBin.Replace("\", "/")
@"
@ GENERATO DA build.ps1 - non modificare a mano.
@ Il payload gia' linkato a $("{0:X8}" -f $PAYLOAD_BASE), incollato nell'immagine multiboot.
    .section .rodata
    .align 2
    .global payload_bin
payload_bin:
    .incbin "$incPath"
    .align 2
    .global payload_bin_end
payload_bin_end:
"@ | Set-Content -Path $blobPath -Encoding ASCII

# --- compilazione ------------------------------------------------------------
$common = @("-mcpu=arm7tdmi", "-mthumb-interwork", "-ffreestanding", "-fno-builtin",
            "-fno-strict-aliasing", "-O2", "-Wall", "-Wextra", "-I", $root)
if ($Syms -eq "usa") { $common += "-DMBSTUB_SYMS_USA" }

& $gcc @common -marm  -c (Join-Path $root "crt0.S")    -o (Join-Path $build "crt0.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di crt0.S fallita" }

& $gcc @common -marm  -c (Join-Path $root "handoff.S") -o (Join-Path $build "handoff.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di handoff.S fallita" }

& $gcc @common -mthumb -c (Join-Path $root "main.c")   -o (Join-Path $build "main.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di main.c fallita" }

& $gcc @common -marm  -c $blobPath -o (Join-Path $build "payload_blob.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione del blob fallita" }

$mapArg = "-Wl,-Map," + (Join-Path $build "$outName.map")

& $gcc @common -nostdlib -nostartfiles `
    -T (Join-Path $root "mbstub.ld") $mapArg `
    (Join-Path $build "crt0.o") (Join-Path $build "main.o") `
    (Join-Path $build "handoff.o") (Join-Path $build "payload_blob.o") `
    -o (Join-Path $build "$outName.elf")
if ($LASTEXITCODE -ne 0) { throw "link fallito" }

& $objcopy -O binary (Join-Path $build "$outName.elf") (Join-Path $build "$outName.gba")

# --- quanto occupa davvero handoff.S ----------------------------------------
# Non e' curiosita': e' il numero che dice quanti byte della coda restano al
# payload quando un giorno il driver SIO ci entrera' dentro.
$mapText = Get-Content (Join-Path $build "$outName.map") -Raw
$hs = [regex]::Match($mapText, "0x0*([0-9a-f]{8})\s+__handoff_start")
$he = [regex]::Match($mapText, "0x0*([0-9a-f]{8})\s+__handoff_end")
if ($hs.Success -and $he.Success) {
    $handoffSize = [Convert]::ToInt64($he.Groups[1].Value, 16) - [Convert]::ToInt64($hs.Groups[1].Value, 16)
    $slack = $HANDOFF_BASE + 0x200 - ([Convert]::ToInt64($he.Groups[1].Value, 16))
    Write-Output ("handoff   : {0} byte a 0x{1:X8}, {2} byte di margine nella sua finestra" -f $handoffSize, $HANDOFF_BASE, $slack)
    if ($slack -lt 0) {
        throw "handoff.S non ci sta nei 512 byte della finestra: allarga la regione 'tail' in mbstub.ld"
    }
    # L'INCASTRO CON IL PAYLOAD (2026-08-25). Nella stessa finestra, dal boot in
    # poi, vivono lo stack privato del payload (in cima, dal primo IRQ) e le
    # code .lateclear (dal basso, azzerate al SECONDO VBlank apposta - vedi il
    # commento su sRamCleanDone in payload/main.c). Il primo IRQ pero' puo'
    # interrompere le ULTIME istruzioni di handoff.S, e il suo stack scende da
    # 0x02040000: il codice di handoff deve finire SOTTO il punto piu' basso
    # che quello stack puo' legalmente toccare (mezza pila = 144 byte, la
    # stessa meta' che la guardia di build.ps1 impone al percorso IRQ).
    $stackFloorMin = 0x02040000 - 144
    if ($HANDOFF_BASE + $handoffSize -gt $stackFloorMin) {
        throw ("handoff.S finisce a 0x{0:X8}, OLTRE il pavimento dello stack del primo IRQ (0x{1:X8}): " -f ($HANDOFF_BASE + $handoffSize), $stackFloorMin) +
              "un VBlank pendente sull'ultimo salto scriverebbe lo stack sopra il codice in esecuzione. Accorcia handoff.S."
    }
}
$tailFree = $EWRAM_END - ($PAYLOAD_BASE + $reservedSize)
Write-Output ("coda      : {0} byte liberi sopra il payload riservato" -f $tailFree)

# handoff.S vive dentro quel margine, ma solo durante il boot: quando il payload
# comincia a usare il suo stack, handoff ha gia' finito e saltare sopra i suoi
# byte non fa danno. Va detto, pero', invece di lasciarlo dedurre dal silenzio.
if (($PAYLOAD_BASE + $reservedSize) -gt $HANDOFF_BASE) {
    Write-Output ("            NOTA: il payload riservato arriva a 0x{0:X8}, sopra HANDOFF_BASE 0x{1:X8}." -f ($PAYLOAD_BASE + $reservedSize), $HANDOFF_BASE)
    Write-Output "            Innocuo (handoff ha gia' finito quando il payload parte), ma se il"
    Write-Output "            payload cresce ancora conviene alzare HANDOFF_BASE per non confondersi."
}

# --- script di prova per mGBA ------------------------------------------------
# Lo stub intero non e' provabile in emulatore (serve uno slot vuoto e un
# inserimento a caldo). handoff.S invece si', ed e' la parte a esito incerto:
# lo si scrive in EWRAM e lo si fa partire dal vettore IRQ, che e' il modo in
# cui ci arriverebbe comunque. Vedi mgba\handoff_test_body.lua.
& $objcopy -O binary --only-section=.handoff (Join-Path $build "$outName.elf") (Join-Path $build "handoff.bin")
if ($LASTEXITCODE -ne 0) { throw "estrazione di .handoff fallita" }

$handoffBytes = [System.IO.File]::ReadAllBytes((Join-Path $build "handoff.bin"))
$payloadBytes = [System.IO.File]::ReadAllBytes($PayloadBin)

# Byte emessi in una TABELLA, mai in una catena di "..": il parser di Lua annida
# a destra e sfonda LUAI_MAXCCALLS (200), e quando succede mGBA non stampa
# niente - sembra uno script muto invece che rotto. Misurato il 2026-07-30 sul
# payload, e vale identico qui.
# Il nome della tabella e' un parametro perche' tools\check_lua.py riconosce il
# payload da una riga esatta - "PAYLOAD_BYTES = table.concat(__chunks)" - e senza
# quella salta il confronto byte per byte con payload.bin, che e' proprio il
# controllo che serve.
function Format-LuaChunks([byte[]]$data, [string]$varName, [string]$tableName) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine("local $tableName = {}")
    $perLine = 64
    for ($i = 0; $i -lt $data.Length; $i += $perLine) {
        $chunk = ""
        for ($j = $i; ($j -lt $i + $perLine) -and ($j -lt $data.Length); $j++) {
            $chunk += "\" + $data[$j].ToString()
        }
        [void]$sb.AppendLine("$tableName[#$tableName + 1] = `"$chunk`"")
    }
    [void]$sb.AppendLine("$varName = table.concat($tableName)")
    return $sb.ToString()
}

$lua = New-Object System.Text.StringBuilder
[void]$lua.AppendLine("-- GENERATO DA hw\mbstub\build.ps1 - non modificare a mano.")
[void]$lua.AppendLine("-- Modifica mgba\handoff_test_body.lua oppure hw\mbstub\handoff.S e ricompila.")
[void]$lua.AppendLine(("PAYLOAD_BASE = 0x{0:X8}" -f $PAYLOAD_BASE))
[void]$lua.AppendLine(("HANDOFF_BASE = 0x{0:X8}" -f $HANDOFF_BASE))
[void]$lua.Append((Format-LuaChunks $payloadBytes "PAYLOAD_BYTES" "__chunks"))
[void]$lua.Append((Format-LuaChunks $handoffBytes "HANDOFF_BYTES" "__chunks_handoff"))
[void]$lua.AppendLine("")
[void]$lua.AppendLine((Get-Content -Raw (Join-Path $owl "mgba\handoff_test_body.lua")))

$outLua = Join-Path $owl "mgba\handoff_test.lua"
[System.IO.File]::WriteAllText($outLua, $lua.ToString(), (New-Object System.Text.UTF8Encoding($false)))
Write-Output "lua       : $outLua"

# Compilato davvero prima di dichiararlo pronto: uno script che non compila, in
# mGBA, non da' un errore - da' silenzio.
$checkLua = Join-Path $owl "tools\check_lua.py"
if (Test-Path $checkLua) {
    # Percorso pieno: l'interprete sta sull'HDD e non e' sul PATH (dove c'e'
    # solo il segnaposto del Microsoft Store, che esce muto con 9009).
    $py = if (Test-Path 'D:\Progettini\Python313\python.exe') { 'D:\Progettini\Python313\python.exe' } else { 'python' }
    & $py $checkLua $outLua
    if ($LASTEXITCODE -ne 0) { throw "lo script Lua generato non compila" }
}

# --- header GBA --------------------------------------------------------------
if (-not $LogoFrom) {
    $candidates = @(
        "$env:USERPROFILE\Desktop\PokemonGB_Online_Trades-main\pokemon_gen3_to_genx_mb.gba",
        (Join-Path $repo "repo-studio\pokeemerald\pokeemerald.gba")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $LogoFrom = $c; break }
    }
}
if (-not $LogoFrom -or -not (Test-Path $LogoFrom)) {
    throw "Nessuna sorgente per il logo Nintendo. Passa -LogoFrom <file .gba>"
}
Write-Output "logo da   : $LogoFrom"

$bin = [System.IO.File]::ReadAllBytes((Join-Path $build "$outName.gba"))
if ($bin.Length -lt 0xC0) { throw "binario piu' corto dell'header: qualcosa non ha linkato" }

$src = [System.IO.File]::ReadAllBytes($LogoFrom)
[Array]::Copy($src, 0x04, $bin, 0x04, 0x9C)

for ($i = 0; $i -lt 12; $i++) {
    $ch = if ($i -lt $Title.Length) { [byte][char]$Title[$i] } else { 0 }
    $bin[0xA0 + $i] = $ch
}
[Array]::Copy($src, 0xAC, $bin, 0xAC, 4)
[Array]::Copy($src, 0xB0, $bin, 0xB0, 2)
$bin[0xB2] = 0x96
$bin[0xB3] = 0x00
$bin[0xBC] = 0x00

$sum = 0
for ($i = 0xA0; $i -lt 0xBD; $i++) { $sum += $bin[$i] }
$bin[0xBD] = [byte]((-($sum + 0x19)) -band 0xFF)

[System.IO.File]::WriteAllBytes((Join-Path $build "$outName.gba"), $bin)

# --- controlli ---------------------------------------------------------------
$size = $bin.Length
Write-Output "mbstub    : $size byte"

if ($size -gt $MULTIBOOT_MAX) {
    throw "TROPPO GRANDE: $size byte, il limite multiboot e' $MULTIBOOT_MAX"
}
if ($bin[0x03] -ne 0xEA) {
    throw "i primi 4 byte non sono un branch ARM: l'header ha sovrascritto il codice"
}
Write-Output ("header    : branch ok, logo copiato, checksum 0x{0:X2}" -f $bin[0xBD])
Write-Output ""
Write-Output "Pronto: $((Join-Path $build ($outName + '.gba')))"
Write-Output ""
Write-Output "Sequenza (UN CAVO SOLO, UN FIRMWARE SOLO - dal 2026-08-02):"
Write-Output "  1. Pico col firmware Celio celio-f1f2b-f3.uf2, SW1 su 3,3 V"
Write-Output "  2. cavo GBA nel verso marcato - lo stesso del link in gioco"
Write-Output "  3. GBA con lo SLOT CARTUCCIA VUOTO, acceso dopo aver collegato il cavo"
Write-Output "  4. cd ..\..\net  poi  python mb_multi.py ..\hw\mbstub\build\$outName.gba"
Write-Output "  5. schermo ROSSO -> inserisci la cartuccia -> giallo, verde, gioco"
Write-Output "  6. poi, senza toccare niente: python usb_link.py --ascolta 30"
Write-Output ""
Write-Output "Il multiboot passa in modo MultiPlay 16 bit (GBATEK SWI 25h, modo 1),"
Write-Output "cioe' lo stesso modo del link in gioco: niente cambio cavo, niente riflash."
Write-Output "Percorso storico, solo come ripiego: gbalink-mb.uf2 + cavo DMG/GBC +"
Write-Output "..\siotest\mbsend.py --legacy (richiede di riflashare il Pico due volte)."
