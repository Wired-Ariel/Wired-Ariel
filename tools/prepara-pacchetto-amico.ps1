# =============================================================================
# prepara-pacchetto-amico.ps1 - assembla lo zip da mandare al secondo giocatore
# =============================================================================
#
# NOTA: file in ASCII puro, come build.ps1 (PowerShell 5.1 legge gli .ps1 come
# ANSI e i caratteri accentati rompono il parser). Il LEGGIMI generato invece
# e' UTF-8 e gli accenti li ha.
#
# Cosa produce: una cartella pacchetto-amico\ e il suo zip, con dentro TUTTO
# quello che serve all'amico (Windows + GBA fisico + cartuccia italiana):
# script python, mbstub.gba, firmware del Pico, tre .bat numerati e la guida.
#
# Uso (percorsi assoluti, da qualunque cartella):
#   powershell -ExecutionPolicy Bypass -File D:\Progettini\GBA-USB\overworld-link\tools\prepara-pacchetto-amico.ps1 -Relay "IP-O-NOME:9000" -Stanza 51234
#
# -Relay  : come l'amico raggiunge il relay di Lain (IP pubblico o nome DuckDNS)
# -Stanza : numero di stanza della partita. Sceglierlo ALTO e non ovvio
#           (41000-65000): e' l'unica "password" che il relay ha.
param(
    [Parameter(Mandatory = $true)]
    [string]$Relay,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$Stanza,

    # Il peer-id dell'amico DENTRO la stanza. Deve essere UNICO: due peer con
    # lo stesso id si espellono a vicenda (il relay tratta il secondo come un
    # rientro NAT del primo). Lain e' 1; il primo amico 2, il secondo 3, il
    # terzo 4. Tetto della stanza: 4 giocatori (test_relay_cap.py).
    [ValidateRange(2, 65535)]
    [int]$PeerId = 2,

    # Il timing PIO del canale, lo stesso provato sul fisico di Lain.
    [int]$UsbTiming = 7400,

    # PYTHON PORTABILE DENTRO LO ZIP (2026-08-21): l'amico non installa niente
    # e non da' nessun pip. Si copia il minimo dell'embeddable distribution che
    # gia' usiamo (D:\Progettini\Python313 E' un embeddable con _pth disattivato)
    # piu' pyusb e libusb_package. -SenzaPython per il vecchio zip leggero.
    [switch]$SenzaPython,
    [string]$PythonSorgente = "D:\Progettini\Python313"
)

$ErrorActionPreference = "Stop"

# Due forme, e la seconda e' quella che passa da internet (2026-08-25):
#   host:porta               UDP diretto: va bene per un relay in casa con il
#                            port forwarding sul router;
#   ws:// o wss://.../ws     WebSocket: la strada del browser, porta 443. E'
#                            l'UNICA che funziona verso la VPS, dove la
#                            9000/udp e' filtrata dalla Security List della
#                            VCN (misurato: timeout, non rifiuto), e in
#                            generale l'unica che passa da qualunque rete.
if ($Relay -notmatch "^[A-Za-z0-9\.\-]+:\d+$" -and $Relay -notmatch "^wss?://") {
    throw "-Relay deve essere 'host:porta' (es. 93.42.10.20:9000) oppure un URL WebSocket (es. wss://gbcatrade.wired-ariel.it/passotile/ws), non '$Relay'"
}
if ($Stanza -lt 41000) {
    Write-Warning ("Stanza {0}: meglio un numero alto e non ovvio (41000-65000), e' l'unica difesa del relay." -f $Stanza)
}

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)  # ...\overworld-link
$out  = Join-Path $root "build\pacchetto-amico"
$zip  = Join-Path $root "build\pacchetto-amico.zip"

# --- i pezzi, con verifica che esistano e che siano quelli giusti ------------
$mbstub   = Join-Path $root "hw\mbstub\build\mbstub.gba"
$firmware = Join-Path $root "hw\firmware\celio-f1f2b-f3-f4.uf2"
$sorgenti = @("client.py", "protocol.py", "usb_link.py", "ws_link.py", "mb_multi.py",
              "club_link.py", "finto_club.py", "pannello.py", "pannello.html",
              "mappa.html") |
    ForEach-Object { Join-Path $root "net\$_" }

foreach ($f in @($mbstub, $firmware) + $sorgenti) {
    if (-not (Test-Path $f)) { throw "manca $f - la catena non e' stata costruita?" }
}

# Lo stub DEVE contenere l'ultima versione del payload. Il confronto NON si fa
# con build\payload.bin - nell'ordine di build obbligato (WithSio -> mbstub ->
# p1) quel file viene sovrascritto per ultimo dalla build no-SIO, quindi e'
# SEMPRE piu' nuovo dello stub e la guardia griderebbe sempre. Si confronta con
# i SORGENTI: se uno di questi e' piu' recente di mbstub.gba, qualcuno ha
# corretto il codice senza rifare lo stub, e l'amico giocherebbe con la
# versione prima. E' il difetto classico, e va fermato qui.
$fonti = @("payload\main.c", "payload\sio.c", "payload\hook.S",
           "payload\game_syms_it.h", "hw\mbstub\main.c") |
    ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ }
$stubTime = (Get-Item $mbstub).LastWriteTime
foreach ($f in $fonti) {
    if ((Get-Item $f).LastWriteTime -gt $stubTime) {
        throw ("{0} e' PIU' RECENTE di mbstub.gba: ricostruisci nell'ordine " -f $f) +
              "obbligato (build.ps1 -Syms it -WithSio, poi hw\mbstub\build.ps1) " +
              "prima di impacchettare."
    }
}

if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

foreach ($f in $sorgenti) { Copy-Item $f $out }
# La testa del pannello e' fatta dai PNG in net\img\: i pixel della decomp,
# non risorse remote. Senza, la pagina resta un rettangolo verde vuoto -
# e il pannello deve funzionare col PC offline, cioe' proprio mentre si gioca.
$img = Join-Path $root "net\img"
if (-not (Test-Path (Join-Path $img "tile-erba-alta.png"))) {
    throw "manca $img - il pannello resterebbe senza insegna"
}
New-Item -ItemType Directory -Force (Join-Path $out "img") | Out-Null
Copy-Item (Join-Path $img "*.png") (Join-Path $out "img")
Copy-Item $mbstub   (Join-Path $out "mbstub.gba")
Copy-Item $firmware (Join-Path $out "celio.uf2")

# --- la mappa live (2026-08-21): i dati generati da tools\gen_mappa.py ------
# Senza, /mappa.html dice "dati non generati". Se manca qui, si genera
# PRIMA di impacchettare: e' la decomp, non un download.
$mappa = Join-Path $root "net\mappa"
if (-not (Test-Path (Join-Path $mappa "dati.json"))) {
    throw "manca net\mappa\dati.json: genera la mappa con tools\gen_mappa.py prima di impacchettare"
}
Copy-Item -Recurse $mappa (Join-Path $out "mappa")

# --- Python portabile ---------------------------------------------------------
# Il minimo che serve ai nostri script: interprete, stdlib zippata, i .pyd che
# usano (ctypes per pyusb, socket/select per il pannello e il client, _queue
# per i thread USB, unicodedata per il log), i tre pacchetti (usb,
# libusb_package con la sua libusb-1.0.dll, importlib_resources che
# libusb_package importa). NIENTE ssl/sqlite/tk: 7 MB inutili. Il file _pth e'
# quello che rende l'interprete autosufficiente: sys.path = solo quello che c'e'
# scritto, e `..` e' la cartella del pacchetto (gli script). Senza quella riga
# `import protocol` fallirebbe, perche' con un _pth la cartella dello script
# NON entra in sys.path da sola (e' il motivo per cui su questo PC il _pth e'
# disattivato, vedi CLAUDE.md).
if (-not $SenzaPython) {
    if (-not (Test-Path (Join-Path $PythonSorgente "python313.dll"))) {
        throw "Python portabile: $PythonSorgente non sembra un embeddable Python 3.13 (manca python313.dll)"
    }
    $pyOut = Join-Path $out "python"
    New-Item -ItemType Directory -Force $pyOut | Out-Null
    # _ssl.pyd + le due DLL di OpenSSL dal 2026-08-25: senza, `import ssl`
    # fallisce e con lui il relay via wss:// - cioe' l'unica strada che passa
    # da internet verso la VPS. La guardia qui sotto apre una connessione TLS
    # VERA prima di impacchettare: un pacchetto che non si collega e' peggio
    # di nessun pacchetto, e lo scoprirebbe l'amico a 300 km.
    $pyFiles = @("python.exe", "pythonw.exe", "python3.dll", "python313.dll",
                 "python313.zip", "vcruntime140.dll", "vcruntime140_1.dll",
                 "libffi-8.dll", "_ctypes.pyd", "_socket.pyd", "select.pyd",
                 "_queue.pyd", "unicodedata.pyd", "_ssl.pyd", "_hashlib.pyd",
                 "libssl-3.dll", "libcrypto-3.dll", "LICENSE.txt")
    foreach ($f in $pyFiles) {
        $src = Join-Path $PythonSorgente $f
        if (-not (Test-Path $src)) { throw "Python portabile: manca $src" }
        Copy-Item $src $pyOut
    }
    $sp = Join-Path $pyOut "Lib\site-packages"
    New-Item -ItemType Directory -Force $sp | Out-Null
    foreach ($pkg in @("usb", "libusb_package", "importlib_resources")) {
        $src = Join-Path $PythonSorgente "Lib\site-packages\$pkg"
        if (-not (Test-Path $src)) { throw "Python portabile: manca il pacchetto $pkg in $src (pip install pyusb libusb-package)" }
        Copy-Item -Recurse $src $sp
        Get-ChildItem -Recurse -Directory (Join-Path $sp $pkg) -Filter "__pycache__" |
            Remove-Item -Recurse -Force
    }
    $pth = "python313.zip`r`n.`r`nLib\site-packages`r`n..`r`n"
    [IO.File]::WriteAllText((Join-Path $pyOut "python313._pth"), $pth, [Text.Encoding]::ASCII)

    # LA GUARDIA: il Python copiato deve importare i nostri moduli E pyusb, dalla
    # cartella del pacchetto, prima di finire nello zip. Un pacchetto che non
    # parte e' peggio di nessun pacchetto: lo scopre l'amico a 300 km.
    $prova = & (Join-Path $pyOut "python.exe") -B -c "import sys; sys.path.insert(0, r'$out'); import usb, libusb_package, http.server, json, select, socket, struct, hashlib, threading, subprocess, queue, ssl, protocol, usb_link, ws_link, mb_multi, club_link, finto_club, client, pannello; print('PY-OK', sys.version.split()[0], libusb_package.get_library_path() is not None)" 2>&1
    if ($LASTEXITCODE -ne 0 -or ($prova -join ' ') -notmatch 'PY-OK') {
        throw "Python portabile: la prova di import e' fallita: $prova"
    }
    Write-Output ("Python portabile: {0}" -f ($prova -join ' '))

    # LA GUARDIA TLS. `import ssl` che riesce non basta: servono anche i
    # certificati di sistema, e su Windows arrivano dal negozio del sistema
    # operativo tramite _ssl. Qui si apre una connessione VERA verso il relay
    # se e' un URL wss:// (se e' UDP non c'e' niente da provare).
    if ($Relay -match "^wss://([A-Za-z0-9\.\-]+)") {
        $tlsHost = $matches[1]
        $tls = & (Join-Path $pyOut "python.exe") -B -c "import socket, ssl; c=ssl.create_default_context(); s=c.wrap_socket(socket.create_connection(('$tlsHost',443),timeout=10), server_hostname='$tlsHost'); print('TLS-OK', s.version()); s.close()" 2>&1
        if ($LASTEXITCODE -ne 0 -or ($tls -join ' ') -notmatch 'TLS-OK') {
            throw "Python portabile: la connessione TLS a $tlsHost e' fallita: $tls"
        }
        Write-Output ("TLS verso il relay: {0}" -f ($tls -join ' '))
    }
}
# La prova di import (e qualunque avvio di prova) lascia __pycache__ nella
# cartella: non deve finire nello zip.
Get-ChildItem -Recurse -Directory $out -Filter "__pycache__" | Remove-Item -Recurse -Force

# --- i tre .bat --------------------------------------------------------------
# %~dp0 = la cartella del .bat: funzionano ovunque l'amico scompatti lo zip.
#
# Il preambolo PY: sul PC dell'amico "python" sta sul PATH (installer con Add
# to PATH); sul PC di Lain NO - li' c'e' solo il segnaposto muto del Microsoft
# Store, e l'interprete vero sta su D: (regola: niente installazioni su C:).
# Il 2026-08-02 Lain ha lanciato un .bat del pacchetto sul proprio PC e ha
# preso "Python non e' stato trovato": con questo preambolo gli stessi .bat
# girano su entrambe le macchine.
$pyPreamble = @"
set PY=python
if exist "D:\Progettini\Python313\python.exe" set "PY=D:\Progettini\Python313\python.exe"
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
"@

$bat0 = @"
@echo off
rem Prova del canale USB+cavo+GBA, SENZA internet. Vedi LEGGIMI.md, passo 4.
cd /d "%~dp0"
$pyPreamble
"%PY%" usb_link.py --ascolta 30 --timing $UsbTiming
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

$bat2 = @"
@echo off
rem La partita. Da lanciare DOPO 1-multiboot e DOPO aver inserito la
rem cartuccia (schermo del GBA su Smeraldo).
cd /d "%~dp0"
$pyPreamble
"%PY%" client.py --transport usb --room $Stanza --peer-id $PeerId --usb-timing $UsbTiming --relay $Relay
pause
"@

# Il quarto .bat esiste per un caso REALE, non teorico: il 2026-08-02 l'amico
# ha preso "il comando 'Cancel' non e' partito ... endpoint USB inceppato"
# lanciando 2-gioca.bat, e il rimedio che il messaggio stesso suggerisce
# (usb_link.py --riavvia) non era lanciabile senza aprire un prompt a mano.
# Un rimedio che il pacchetto nomina ma non fornisce non e' un rimedio.
$bat3 = @"
@echo off
rem SBLOCCA l'adattatore quando 2-gioca.bat dice "endpoint USB del Pico
rem inceppato" oppure "il comando 'Cancel' non e' partito".
rem
rem NON SPEGNERE IL GBA: il programma vive nella RAM della console e
rem sopravvive a questo riavvio. Spegnendolo perderesti il multiboot e
rem dovresti rifare tutto da 1-multiboot.bat.
rem
rem Dopo che ha detto "ricomparso", rilancia 2-gioca.bat.
cd /d "%~dp0"
$pyPreamble
"%PY%" usb_link.py --riavvia
pause
"@

# I .bat vanno scritti in ASCII: cmd.exe legge i batch nella codepage OEM e un
# BOM UTF-8 in testa diventa un comando inesistente.
# IL PANNELLO (2026-08-09): e' la porta d'ingresso. I .bat numerati restano
# sotto come ripiego, ma l'amico deve poter lanciare UNA cosa sola.
$batPannello = @"
@echo off
rem ============================================================
rem  APRI QUESTO. E' l'unica cosa da lanciare.
rem  Si apre da solo nel browser: da li' si fa tutto.
rem  Lascia aperta questa finestra nera: e' il motore.
rem ============================================================
cd /d "%~dp0"
$pyPreamble
"%PY%" pannello.py
pause
"@
[IO.File]::WriteAllText((Join-Path $out "PANNELLO.bat"), $batPannello, [Text.Encoding]::ASCII)

# La configurazione gia' pronta: l'amico non deve digitare niente. Ruolo
# "amico" = non accende nessun relay, si collega a quello di Lain.
$configPannello = @{
    ruolo       = "amico"
    relay       = $Relay
    stanza      = $Stanza
    timing      = $UsbTiming
    cavo        = "auto"
    porta_relay = 9000
} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $out "pannello.config.json"), $configPannello,
                        (New-Object Text.UTF8Encoding $false))

[IO.File]::WriteAllText((Join-Path $out "0-prova-canale.bat"), $bat0, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $out "1-multiboot.bat"),    $bat1, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $out "2-gioca.bat"),        $bat2, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText((Join-Path $out "3-sblocca.bat"),      $bat3, [Text.Encoding]::ASCII)

# --- LEGGIMI.md --------------------------------------------------------------
# Here-string LETTERALE (@'...'@), obbligatoria: il testo contiene i backtick
# del markdown, che in una here-string interpolata (@"..."@) sono il carattere
# di escape di PowerShell - il primo giro `ricevuti` era diventato un
# carriage return + "icevuti". I valori variabili si inseriscono dopo, con
# -replace su segnaposto.
$leggimi = @'
# Pokemon Smeraldo — camminare insieme via internet

Questo pacchetto ti fa vedere Lain nel tuo overworld (e te nel suo), su GBA
vero e cartuccia vera, senza modificare niente: ne' la cartuccia ne' il
salvataggio. Il programma vive solo nella RAM del GBA: spegni, e sparisce.

Ti serve: il GBA, la cartuccia di **Smeraldo italiana**, l'adattatore
USB (la scheda col Pico), il **cavo link GBA** e un PC Windows.

---

## Parte 1 — Setup, si fa UNA volta sola (~15 minuti)

### 1. Python: niente da fare
Python e' **gia' dentro il pacchetto** (cartella `python\`): non devi
installare niente ne' dare comandi. Se per qualche motivo quella cartella
mancasse, il ripiego e' il vecchio metodo: scarica Python da
https://www.python.org/downloads/ con la spunta "Add Python to PATH" e poi
`pip install pyusb libusb-package` dal Prompt dei comandi.

### 2. Driver dell'adattatore (Zadig)
Windows da solo non sa parlare con l'adattatore. Serve **Zadig**:
https://zadig.akeo.ie/

1. Collega l'adattatore al PC via USB.
2. Apri Zadig → menu **Options → List All Devices**.
3. Nella tendina scegli il dispositivo con **USB ID 2FE3 000A**.
4. Come driver di destinazione scegli **WinUSB** e premi **Install Driver**.

Va fatto una volta sola. Se un giorno l'adattatore "sparisce", rifai questi
quattro passi.

### 3. Firmware dell'adattatore
Nel pacchetto c'e' `celio.uf2`. Per metterlo sul Pico:

1. Scollega l'adattatore dal PC.
2. Tieni premuto il **bottone BOOTSEL** sul Pico e, tenendolo premuto,
   ricollega l'USB. Compare un disco chiamato `RPI-RP2`.
3. Trascina `celio.uf2` dentro quel disco. Si riavvia da solo e il disco
   sparisce: fatto.

### 4. Prova del canale (senza internet)
Prepara l'hardware come per una partita (vedi la checklist sotto), poi fai
doppio clic su **`0-prova-canale.bat`**. Cammina un po' nel gioco.

**Criterio: nella finestra devono comparire eventi che salgono (almeno 15 in
30 secondi).** Se restano a zero, il problema e' tra cavo, verso del cavo e
driver — non c'entra internet. Manda a Lain la schermata.

---

## Parte 2 — La checklist hardware, a OGNI sessione

- L'interruttore **SW1 sulla scheda: su 3,3 V** (lato GBA).
- **Cavo link GBA** collegato **nel verso marcato** sulla scheda.
- **Slot cartuccia del GBA: VUOTO.**
- Prima il cavo, **poi** accendi il GBA.

## Parte 3 — Giocare (~1 minuto di avvio)

**Apri `PANNELLO.bat`.** E' l'unica cosa da lanciare: si apre da solo nel
browser ed e' gia' configurato (relay di Lain e stanza giusta). Lascia aperta
la finestra nera che compare: e' il motore.

Dal pannello, in ordine:

1. GBA acceso a slot vuoto (resta sul logo, e' giusto cosi').
2. Premi **1 · Carica il gioco nel GBA**. In ~15 secondi lo schermo del GBA
   diventa **rosso**.
   - Se dice `nessuna risposta`: premilo di nuovo, una volta su tre serve.
3. **Inserisci la cartuccia** nel GBA acceso (si', a caldo: e' voluto).
   Lo schermo passa **giallo → verde** e parte Smeraldo. Carica la partita e
   portati **all'aperto**.
4. Premi **2 · Gioca**. In cima al pannello compaiono la tua mappa e i
   contatori; quando Lain e' collegato, il suo allenatore compare sulla tua
   mappa. Camminate.

**Cosa puoi fare mentre giocate**
- Premi **A** davanti all'allenatore di Lain: vedi la sua **scheda
  allenatore**. B per chiudere.
- Andate insieme al **Centro Pokemon, piano di sopra**, e parlate con la
  signorina: **scambi e lotte funzionano**, senza toccare niente sul PC. Il
  pannello se ne accorge da solo e mostra "Cable Club". Quando uscite dalla
  saletta si torna a camminare da solo.

Per chiudere: chiudi la finestra nera e spegni il GBA. Fine, non resta niente.

> I vecchi file numerati (`1-multiboot.bat`, `2-gioca.bat`, ...) sono ancora
> qui e funzionano: servono solo se il pannello facesse i capricci.

## Se qualcosa non va

| Cosa vedi | Cosa vuol dire | Cosa fare |
|---|---|---|
| `endpoint USB del Pico inceppato` o `il comando 'Cancel' non e' partito` | L'adattatore e' rimasto imbambolato dopo il multiboot | Premi **Sblocca adattatore** nel pannello, poi di nuovo **2 · Gioca**. **NON spegnere il GBA**: il programma vive in RAM e sopravvive |
| Il browser non si apre da solo | Il PC non ha un browser di default impostato | Apri a mano **http://127.0.0.1:7411** |
| `nessuna risposta ... fase detect` nel multiboot | Il GBA non ha risposto all'aggancio | Rilancia `1-multiboot.bat`. Se insiste: GBA spento/acceso e ricontrolla cavo e SW1 |
| `DIREZIONE GBA->PC` nel client | Il PC non riceve dal GBA | Cavo nel verso sbagliato, o SW1 non su 3,3 V |
| `peer 2 e' SOLO nella stanza` sul relay di Lain | Sei collegato ma Lain no (o stanza diversa) | Aspetta Lain, o confrontate il numero di stanza |
| `ricevuti 0` che non sale mai in `2-gioca.bat` | Gli eventi di Lain non arrivano | Problema di rete: Lain controlla relay/port forwarding (procedura R-0) |
| Il gioco parte ma l'amico non si vede | Siete su mappe lontane | Andate nello stesso posto: vi vedete sulla stessa mappa o su quella adiacente |

Il tuo numero di giocatore e' **2** (Lain e' 1): il pannello lo sceglie da
solo dal ruolo "Mi collego a un amico", gia' impostato.
'@

$leggimiPath = Join-Path $out "LEGGIMI.md"
[IO.File]::WriteAllText($leggimiPath, $leggimi, (New-Object Text.UTF8Encoding $true))

# --- lo zip ------------------------------------------------------------------
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path (Join-Path $out "*") -DestinationPath $zip

Write-Output ""
Write-Output "Pacchetto pronto:"
Write-Output ("  cartella : {0}" -f $out)
Write-Output ("  zip      : {0}  ({1:N0} byte)" -f $zip, (Get-Item $zip).Length)
Write-Output ("  relay    : {0}   stanza {1}   peer-id {2}" -f $Relay, $Stanza, $PeerId)
Write-Output ""
Write-Output "Contenuto:"
Get-ChildItem $out | ForEach-Object {
    if ($_.PSIsContainer) {
        $n = (Get-ChildItem -Recurse -File $_.FullName | Measure-Object -Property Length -Sum)
        Write-Output ("  {0,-22} {1,8:N0} byte in {2} file (cartella)" -f $_.Name, $n.Sum, $n.Count)
    } else {
        Write-Output ("  {0,-22} {1,8:N0} byte" -f $_.Name, $_.Length)
    }
}
Write-Output ""
Write-Output "Da mandare all'amico: lo zip. Da dirgli a voce: 'apri LEGGIMI.md'."
