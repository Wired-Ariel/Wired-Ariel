# Compila il firmware del Pico per il multiboot GBA.
#
# E' `gb-link-firmware-reconfigurable` di lorenzooone con una sola aggiunta: un
# pacchetto di configurazione da 40 byte che permette al PC di scegliere a
# runtime quali GPIO usare per SC / uscita dati / ingresso dati, e di leggere
# il livello delle linee. Serve perche' su quale GPIO arrivi la risposta del
# GBA le fonti si contraddicono, e questo trasforma la domanda in una misura
# (vedi hw/siotest/mbsend.py).
#
# Uso:  powershell -File hw\gbalink-fw\build.ps1
# Esce: hw\gbalink-fw\build\gbalink-mb.uf2

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$build = Join-Path $here "build"

# L'SDK gia' scaricato dalla build del repo originale: si riusa per non
# rifare un download da centinaia di MB.
$sdk = "D:/Progettini/trades/gb-link-firmware-reconfigurable-main/build/_deps/pico_sdk-src"

if (-not (Test-Path (Join-Path $sdk "pico_sdk_version.cmake"))) {
    Write-Error @"
Pico SDK non trovato in:
  $sdk
Modifica `$sdk in questo script, oppure imposta PICO_SDK_PATH e togli il
parametro -DPICO_SDK_PATH dalla riga di cmake qui sotto (in quel caso CMake
scarica l'SDK da git, e ci mette parecchio).
"@
}

foreach ($tool in @("cmake", "ninja", "arm-none-eabi-gcc")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Error "$tool non e' nel PATH. Serve per compilare il firmware del Pico."
    }
}

Write-Output "configuro..."
cmake -S $here -B $build -G Ninja "-DPICO_SDK_PATH=$sdk" -DPICO_BOARD=pico | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "cmake ha fallito la configurazione" }

Write-Output "compilo..."
cmake --build $build | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "la compilazione e' fallita" }

$src = Join-Path $build "gbusb.uf2"
$dst = Join-Path $build "gbalink-mb.uf2"
if (-not (Test-Path $src)) { Write-Error "gbusb.uf2 non prodotto" }
Copy-Item $src $dst -Force

$size = (Get-Item $dst).Length
$hash = (Get-FileHash $dst -Algorithm SHA1).Hash.ToLower()
Write-Output ""
Write-Output ("firmware  : {0}" -f $dst)
Write-Output ("dimensione: {0} byte" -f $size)
Write-Output ("sha1      : {0}" -f $hash)
Write-Output ""
Write-Output "Flashalo tenendo premuto BOOTSEL mentre colleghi il Pico, poi copiando"
Write-Output "il file dentro l'unita' RPI-RP2."
