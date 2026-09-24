# build.ps1 - costruisce siotest.gba, il banco di prova multiboot.
#
# NON serve devkitARM. Un multiboot e' un binario che parte a 0x02000000 con un
# header GBA valido nei primi 0xC0 byte: bastano l'ARM GNU Toolchain gia' usata
# per il payload, un crt0 e un linker script nostri.
#
# Il logo Nintendo dell'header non si puo' inventare - il BIOS lo verifica e
# senza handshake il caricamento fallisce - e non lo teniamo versionato: si
# copia a build time dai byte 0x04..0x9F di una ROM/multiboot che c'e' gia' sul
# disco. Con -LogoFrom si indica quale.
#
#   .\build.ps1
#   .\build.ps1 -LogoFrom "C:\percorso\a\una\rom.gba"

param(
    [string]$LogoFrom = "",
    [string]$Title = "SIOTEST"
)

$ErrorActionPreference = "Stop"
$root  = $PSScriptRoot
$owl   = Split-Path (Split-Path $root -Parent) -Parent   # overworld-link
$repo  = Split-Path $owl -Parent                          # radice del progetto
$build = Join-Path $root "build"

$MULTIBOOT_MAX = 0x3FF40   # limite imposto dal caricatore multiboot

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

# --- da dove prendiamo il logo ----------------------------------------------
if (-not $LogoFrom) {
    # Candidati in ordine: un multiboot gia' funzionante e' la fonte migliore,
    # perche' e' gia' passato dal caricatore almeno una volta.
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

# --- compilazione ------------------------------------------------------------
$common = @("-mcpu=arm7tdmi", "-mthumb-interwork", "-ffreestanding", "-fno-builtin",
            "-fno-strict-aliasing", "-O2", "-Wall", "-Wextra")

& $gcc @common -marm  -c (Join-Path $root "crt0.S") -o (Join-Path $build "crt0.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di crt0.S fallita" }

& $gcc @common -mthumb -c (Join-Path $root "main.c") -o (Join-Path $build "main.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di main.c fallita" }

# sio.c e' LO STESSO FILE del payload, senza fork: e' il motivo per cui questa
# misura vale anche per il payload e non solo per il banco di prova.
& $gcc @common -mthumb -c (Join-Path $owl "payload\sio.c") -o (Join-Path $build "sio.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di sio.c fallita" }

# PowerShell non espande (Join-Path ...) dentro un argomento non quotato: la
# stringa va composta prima, o il parser si ferma su "-Wl,-Map,(".
$mapArg = "-Wl,-Map," + (Join-Path $build "siotest.map")

& $gcc @common -nostdlib -nostartfiles `
    -T (Join-Path $root "siotest.ld") $mapArg `
    (Join-Path $build "crt0.o") (Join-Path $build "main.o") (Join-Path $build "sio.o") `
    -o (Join-Path $build "siotest.elf")
if ($LASTEXITCODE -ne 0) { throw "link fallito" }

& $objcopy -O binary (Join-Path $build "siotest.elf") (Join-Path $build "siotest.gba")

# --- header GBA --------------------------------------------------------------
$bin = [System.IO.File]::ReadAllBytes((Join-Path $build "siotest.gba"))
if ($bin.Length -lt 0xC0) { throw "binario piu' corto dell'header: qualcosa non ha linkato" }

$src = [System.IO.File]::ReadAllBytes($LogoFrom)

# Logo Nintendo: 0x04..0x9F, copiato verbatim. Il BIOS lo confronta con la
# propria copia e senza corrispondenza non risponde all'handshake.
[Array]::Copy($src, 0x04, $bin, 0x04, 0x9C)

# Titolo (0xA0..0xAB), gamecode e maker: non li guarda nessuno nel multiboot, ma
# un header coerente rende il file riconoscibile dagli strumenti.
for ($i = 0; $i -lt 12; $i++) {
    $ch = if ($i -lt $Title.Length) { [byte][char]$Title[$i] } else { 0 }
    $bin[0xA0 + $i] = $ch
}
[Array]::Copy($src, 0xAC, $bin, 0xAC, 4)      # gamecode
[Array]::Copy($src, 0xB0, $bin, 0xB0, 2)      # maker
$bin[0xB2] = 0x96                              # byte fisso richiesto dal BIOS
$bin[0xB3] = 0x00
$bin[0xBC] = 0x00                              # versione

# Checksum dell'header: somma di 0xA0..0xBC, meno 0x19, su 8 bit.
$sum = 0
for ($i = 0xA0; $i -lt 0xBD; $i++) { $sum += $bin[$i] }
$bin[0xBD] = [byte]((-($sum + 0x19)) -band 0xFF)

[System.IO.File]::WriteAllBytes((Join-Path $build "siotest.gba"), $bin)

# --- controlli ---------------------------------------------------------------
$size = $bin.Length
Write-Output "siotest   : $size byte"

if ($size -gt $MULTIBOOT_MAX) {
    throw "TROPPO GRANDE: $size byte, il limite multiboot e' $MULTIBOOT_MAX"
}

# Il branch iniziale deve essere sopravvissuto alla scrittura dell'header.
if ($bin[0x03] -ne 0xEA) {
    throw "i primi 4 byte non sono un branch ARM: l'header ha sovrascritto il codice"
}
Write-Output ("header    : branch ok, logo copiato, checksum 0x{0:X2}" -f $bin[0xBD])
Write-Output ""
Write-Output "Pronto: $((Join-Path $build 'siotest.gba'))"
Write-Output ""
Write-Output "Per caricarlo servono, in quest'ordine:"
Write-Output "  1. Pico col firmware gb-link-firmware-reconfigurable (NON Celio: non fa multiboot)"
Write-Output "  2. GBA SP con lo SLOT CARTUCCIA VUOTO, acceso dopo aver collegato il cavo"
Write-Output "  3. python tools\mbsend.py build\siotest.gba"
