# =============================================================================
# prepara-pacchetto-emulatore.ps1 - il test LOCALE dell'amico: GBA <-> mGBA
# =============================================================================
#
# NOTA: file in ASCII puro, come gli altri .ps1 (PowerShell 5.1 li legge come
# ANSI e gli accenti rompono il parser). Il LEGGIMI generato e' UTF-8.
#
# PERCHE' ESISTE: la prima partita via internet (2026-08-02) ha lasciato un
# dubbio non risolto - quando il GBA dell'amico tace, e' colpa del suo lato
# (hardware/payload) o della rete/relay? Questo pacchetto fa girare TUTTA la
# catena sul suo PC: GBA fisico <-> relay locale <-> mGBA con il payload Lua.
# Se qui funziona, il suo lato e' scagionato per intero; se non funziona, il
# relay e internet non c'entrano per costruzione.
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File D:\Progettini\GBA-USB\overworld-link\tools\prepara-pacchetto-emulatore.ps1
#
# Prerequisito: build gia' fatte nell'ordine canonico, in particolare
#   .\build.ps1 -LinkRole client -LinkPort 8201 -OutName amico -Syms it
# che genera mgba\inject.amico.lua (il ponte E2 ascolta su 8201).

# Parametri opzionali (2026-08-25, partite in 3-4): con i default il pacchetto
# resta il test LOCALE di sempre (relay sul PC dell'amico). Per usare l'mGBA
# come TERZO GIOCATORE di una partita via internet si passa il relay pubblico
# e un peer-id unico della stanza, es.:
#   ... -Relay "IP-DI-LUCA:9000" -Stanza 58243 -PeerId 3
param(
    [string]$Relay = "127.0.0.1:9000",
    [ValidateRange(1, 65535)]
    [int]$Stanza = 58243,
    # peer-id del PONTE EMULATORE (l'altro .bat, quello del GBA locale, usa
    # PeerId+1: nel test locale restano 1 e 2 come sempre)
    [ValidateRange(1, 65534)]
    [int]$PeerId = 1
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)  # ...\overworld-link
$out  = Join-Path $root "build\pacchetto-emulatore"
$zip  = Join-Path $root "build\pacchetto-emulatore.zip"

# --- i pezzi -----------------------------------------------------------------
$mbstub   = Join-Path $root "hw\mbstub\build\mbstub.gba"
$lua      = Join-Path $root "mgba\inject.amico.lua"
$sorgenti = @("relay.py", "client.py", "protocol.py", "usb_link.py", "ws_link.py", "mb_multi.py",
              "club_link.py", "finto_club.py", "pannello.py", "pannello.html", "mappa.html") |
    ForEach-Object { Join-Path $root "net\$_" }

foreach ($f in @($mbstub, $lua) + $sorgenti) {
    if (-not (Test-Path $f)) { throw "manca $f - la catena non e' stata costruita?" }
}

# Guardia anti-versione-vecchia, stessa logica del pacchetto amico: se un
# sorgente del payload e' piu' recente degli artefatti, qualcuno ha corretto
# il codice senza ricompilare, e l'amico proverebbe la versione prima.
$fonti = @("payload\main.c", "payload\sio.c", "payload\hook.S",
           "payload\game_syms_it.h", "hw\mbstub\main.c", "mgba\inject_body.lua",
           # Il club vive qui dal 2026-08-27 ed entra nello script generato:
           # senza questa riga un pacchetto vecchio partirebbe SENZA scambi.
           "mgba\club_lua.lua") |
    ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ }
foreach ($art in @($mbstub, $lua)) {
    $artTime = (Get-Item $art).LastWriteTime
    foreach ($f in $fonti) {
        if ((Get-Item $f).LastWriteTime -gt $artTime) {
            throw ("{0} e' PIU' RECENTE di {1}: ricompila prima di impacchettare." `
                   -f $f, (Split-Path -Leaf $art))
        }
    }
}

if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

foreach ($f in $sorgenti) { Copy-Item $f $out }

# La mappa live (2026-08-21): dati generati da tools\gen_mappa.py dalla decomp.
$mappa = Join-Path $root "net\mappa"
if (-not (Test-Path (Join-Path $mappa "dati.json"))) {
    throw "manca net\mappa\dati.json: genera la mappa con tools\gen_mappa.py prima di impacchettare"
}
Copy-Item -Recurse $mappa (Join-Path $out "mappa")
# La testa del pannello e' fatta dai PNG in net\img\: i pixel della decomp,
# non risorse remote. Senza, la pagina resta un rettangolo verde vuoto -
# e il pannello deve funzionare col PC offline, cioe' proprio mentre si gioca.
$img = Join-Path $root "net\img"
if (-not (Test-Path (Join-Path $img "tile-erba-alta.png"))) {
    throw "manca $img - il pannello resterebbe senza insegna"
}
New-Item -ItemType Directory -Force (Join-Path $out "img") | Out-Null
Copy-Item (Join-Path $img "*.png") (Join-Path $out "img")
Copy-Item $mbstub (Join-Path $out "mbstub.gba")
Copy-Item $lua    (Join-Path $out "inject.amico.lua")

# --- i .bat ------------------------------------------------------------------
# %~dp0 = cartella del .bat.
#
# Preambolo PY: sul PC dell'amico "python" sta sul PATH; su quello di Lain no
# (solo il segnaposto muto del Microsoft Store - l'interprete vero e' su D:).
# Con il preambolo gli stessi .bat girano su entrambe le macchine, e il test
# locale lo puo' rifare anche Lain senza incappare in "Python non trovato"
# (successo il 2026-08-02).
$pyPreamble = @"
set PY=python
if exist "D:\Progettini\Python313\python.exe" set "PY=D:\Progettini\Python313\python.exe"
"@

$batE1 = @"
@echo off
rem Il relay LOCALE del test. Va avviato per primo e lasciato aperto.
rem Qui non c'entra internet: ascolta solo sul tuo PC.
cd /d "%~dp0"
$pyPreamble
"%PY%" relay.py --port 9000 --verbose
pause
"@

$batE2 = @"
@echo off
rem Il ponte dell'EMULATORE (giocatore 1). Da avviare PRIMA di caricare lo
rem script in mGBA: il ponte ascolta sulla porta 8201, lo script si collega.
cd /d "%~dp0"
$pyPreamble
"%PY%" client.py --listen 8201 --relay $Relay --room $Stanza --peer-id $PeerId
pause
"@

$batE3 = @"
@echo off
rem Il tuo GBA (giocatore 2), collegato al relay LOCALE - non a quello di Lain.
rem Da lanciare DOPO 1-multiboot e DOPO aver caricato la partita nell'overworld.
cd /d "%~dp0"
$pyPreamble
"%PY%" client.py --transport usb --relay $Relay --room $Stanza --peer-id $($PeerId + 1) --usb-timing 7400
pause
"@

$bat1 = @"
@echo off
rem Carica il programma nel GBA via cavo. Slot cartuccia VUOTO, GBA appena
rem acceso. Se dice "nessuna risposta" rilancialo: 1 volta su 3 e' normale.
cd /d "%~dp0"
$pyPreamble
"%PY%" mb_multi.py mbstub.gba
pause
"@

$bat3 = @"
@echo off
rem SBLOCCA l'adattatore se un client dice "endpoint USB del Pico inceppato".
rem NON spegnere il GBA: il programma vive in RAM e sopravvive.
cd /d "%~dp0"
$pyPreamble
"%PY%" usb_link.py --riavvia
pause
"@

[IO.File]::WriteAllText((Join-Path $out "E1-relay-locale.bat"),    $batE1, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $out "E2-ponte-emulatore.bat"), $batE2, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $out "E3-gba.bat"),             $batE3, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $out "1-multiboot.bat"),        $bat1,  [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $out "3-sblocca.bat"),          $bat3,  [Text.Encoding]::ASCII)

# --- LEGGIMI -----------------------------------------------------------------
$leggimi = @'
# Test locale: il tuo GBA e un emulatore, tutto sul TUO PC

Questo test fa girare l'intera catena senza internet: il tuo GBA parla con un
Pokemon Smeraldo emulato sul tuo stesso PC. Serve a rispondere a UNA domanda:
il tuo lato (GBA, cavo, adattatore, programma) funziona da solo?

- Se qui funziona -> il tuo lato e' a posto al 100%, e i problemi visti in
  partita con Lain stavano altrove (rete, ordine delle operazioni).
- Se qui NON funziona -> internet e il relay di Lain non c'entrano niente,
  e il problema e' sul tuo tavolo: manda a Lain le schermate.

## Cosa ti serve in piu' rispetto al pacchetto principale

1. **mGBA**: scaricalo da https://mgba.io/downloads.html (versione 0.10 o piu'
   recente: serve il menu Tools > Scripting). Installa o usa la portable.
2. **Un file .gba di Pokemon Smeraldo ITALIANO.** Il gioco NON e' incluso in
   questo pacchetto. Deve essere la versione italiana: lo script controlla la
   ROM all'avvio e si rifiuta se non e' quella giusta.

Il resto (Python, driver Zadig, firmware del Pico) l'hai gia' fatto per il
pacchetto principale e vale anche qui.

## La sequenza, nell'ordine esatto

**1.** Doppio clic su `E1-relay-locale.bat`.
Criterio: la finestra dice `in ascolto su UDP 9000`. Lasciala aperta.

**2.** Il GBA, come sempre: spento, cartuccia FUORI, SW1 su 3,3 V, cavo nel
verso marcato, accendi -> `1-multiboot.bat` -> `DONE!` e schermo ROSSO ->
inserisci la cartuccia -> giallo -> verde -> carica la partita -> esci
**all'aperto nel mondo di gioco**.

**3.** Doppio clic su `E3-gba.bat`.
Criterio: entro pochi secondi compare **`io su mappa X.Y`** e camminando
`inviati` sale. (Se dopo 30 secondi ti chiede "sei nell'overworld?", non sei
nel mondo di gioco: esci dai menu.)

**4.** Apri mGBA e carica il tuo file .gba di Smeraldo. Arriva in partita
nel mondo di gioco. Va bene anche una partita nuova: dopo l'intro sei nel
camion del trasloco, e quello e' gia' overworld.

**5.** Doppio clic su `E2-ponte-emulatore.bat` (PRIMA dello script: il ponte
ascolta, lo script si collega).

**6.** In mGBA: **Tools -> Scripting -> File -> Load script** e scegli
`inject.amico.lua` da questa cartella.
Criterio: nella finestra E2 compare **`io su mappa X.Y`**.

> **Scambi e lotte (Cable Club)**: questo pacchetto usa il PONTE LOCALE, e su
> quel canale il club non passa - lo script te lo dice chiaro nella console di
> mGBA. Per scambiare serve lo script in modo **relay** (quello che si scarica
> dal sito con "Scarica lo script per l'emulatore"): li' il club c'e', ed e'
> nello stesso script della camminata.

## Come leggere il risultato

| Cosa vedi | Verdetto |
|---|---|
| Su E3 E su E2 `ricevuti` sale (e nel relay 2 peer nella stessa stanza) | **Il tuo lato funziona tutto.** Il problema visto con Lain non era il tuo hardware |
| E3 resta a `inviati 0` con te all'aperto che cammini | Il problema e' fra GBA e PC da te (cavo/adattatore/payload) - internet non c'entra. Schermate a Lain |
| Lo script in mGBA si lamenta della ROM | Il file .gba non e' Smeraldo italiano (giusto). Serve quello |
| `endpoint USB del Pico inceppato` | `3-sblocca.bat`, poi rilancia. NON spegnere il GBA |

**Bonus**: se nel gioco andate nello stesso posto (tu sul GBA e tu
nell'emulatore), vi VEDETE camminare - stai giocando contro te stesso.

Quando hai finito: chiudi le finestre nere, spegni il GBA. Il tuo salvataggio
vero (cartuccia) non e' mai stato toccato.
'@

[IO.File]::WriteAllText((Join-Path $out "LEGGIMI-EMULATORE.md"), $leggimi, (New-Object Text.UTF8Encoding $true))

# --- Python portabile (2026-08-25) -------------------------------------------
# Serve a chi gioca SOLO in emulatore e non ha il pacchetto-amico: senza, il
# .bat del ponte non parte e l'amico dovrebbe installare Python. Si riusa
# quello gia' preparato dal pacchetto amico (interprete + _ssl + pyusb), che
# e' passato dalle sue guardie di import e TLS.
$pySorgente = Join-Path $root "build\pacchetto-amico\python"
if (Test-Path (Join-Path $pySorgente "python.exe")) {
    Copy-Item -Recurse $pySorgente (Join-Path $out "python")
    $prova = & (Join-Path $out "python\python.exe") -B -c "import sys; sys.path.insert(0, r'$out'); import ssl, client, ws_link, protocol; print('PY-OK', sys.version.split()[0])" 2>&1
    if ($LASTEXITCODE -ne 0 -or ($prova -join ' ') -notmatch 'PY-OK') {
        throw "Python portabile: la prova di import e' fallita: $prova"
    }
    Get-ChildItem -Recurse -Directory $out -Filter "__pycache__" | Remove-Item -Recurse -Force
    Write-Output ("Python portabile: {0}" -f ($prova -join ' '))
} else {
    Write-Warning ("nessun Python portabile in {0}: genera prima un pacchetto amico, " +
                   "oppure l'amico dovra' avere Python installato" -f $pySorgente)
}

# --- lo zip ------------------------------------------------------------------
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path (Join-Path $out "*") -DestinationPath $zip

Write-Output ""
Write-Output "Pacchetto emulatore pronto:"
Write-Output ("  cartella : {0}" -f $out)
Write-Output ("  zip      : {0}  ({1:N0} byte)" -f $zip, (Get-Item $zip).Length)
Write-Output ""
Write-Output "Contenuto:"
Get-ChildItem $out | ForEach-Object { Write-Output ("  {0,-24} {1,8:N0} byte" -f $_.Name, $_.Length) }
Write-Output ""
Write-Output "NON contiene la ROM del gioco (e non deve): serve il dump dell'amico."
