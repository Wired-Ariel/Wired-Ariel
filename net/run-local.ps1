# =============================================================================
# run-local.ps1 - prepara TUTTO per la prova a due istanze passando dal relay
# =============================================================================
#
# NOTA: file da tenere in ASCII puro. PowerShell 5.1 legge gli .ps1 come ANSI e
# un carattere accentato rompe il parser (gia' successo una volta).
#
# Questo script compila anche i due script Lua, e non e' un lusso: al primo test
# della Fase 6 i comandi di build li abbiamo scritti a mano, e' passato un
# -LinkRole server per abitudine, e quell'emulatore non ha mai parlato col relay.
# Meno decisioni ci sono da prendere, meno se ne sbagliano.
#
# Uso:
#   .\run-local.ps1
#   .\run-local.ps1 -Delay 60 -Jitter 15 -Loss 2
#
# Il ritardo si applica a cio' che ESCE da ciascun bridge, quindi -Delay 60
# significa 60 ms di sola andata per direzione, 120 ms di andata e ritorno.

param(
    [int]$RelayPort = 9000,

    # 8201/8202, LONTANO da 8123 che e' il default della modalita' diretta fra
    # due emulatori: cosi' i due mondi non possono piu' collidere per sbaglio.
    [int]$Port1 = 8201,
    [int]$Port2 = 8202,
    [int]$Port3 = 8203,

    # Quante istanze di mGBA (2026-08-25, fino a 4 giocatori): con 3 si prova
    # la partita a tre TUTTA in emulatore - terzo bridge su Port3, script
    # inject.p3.lua, peer-id 3.
    [ValidateRange(2, 3)]
    [int]$Players = 2,

    # La stanza e' la PARTITA, non la mappa (2026-07-30): i due bridge devono
    # usare lo stesso numero, altrimenti il relay non inoltra niente. Lo 0 non
    # si usa, il relay lo ignora apposta.
    [int]$Room = 1,

    # Simulatore di rete, applicato a ENTRAMBI i bridge.
    [double]$Delay  = 0,
    [double]$Jitter = 0,
    [double]$Loss   = 0,

    [switch]$SkipBuild,

    # Quale tabella di indirizzi del gioco: "usa" per la ROM compilata da noi
    # (pokeemerald.gba), "it" per il dump della cartuccia italiana. Va passato a
    # ENTRAMBE le build, o un'istanza avrebbe gli indirizzi di un'altra ROM.
    [ValidateSet("usa", "it")]
    [string]$Syms = "usa"
)

$ErrorActionPreference = "Stop"
$net  = $PSScriptRoot
$root = Split-Path $net -Parent

# Percorso pieno prima: l'interprete sta sull'HDD e non e' sul PATH, dove c'e'
# solo il segnaposto del Microsoft Store (che Get-Command trova e che esce in
# silenzio con 9009 - il difetto del 2026-08-02, tre finestre che si aprivano
# e si chiudevano senza dire niente).
$python = if (Test-Path 'D:\Progettini\Python313\python.exe') {
    'D:\Progettini\Python313\python.exe'
} else {
    $p = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($p -and $p -notmatch 'WindowsApps') { $p } else { $null }
}
if (-not $python) { throw "python non trovato: atteso D:\Progettini\Python313\python.exe" }

# --- 1. i due script Lua, costruiti qui per non poterli sbagliare -----------

if (-not $SkipBuild) {
    Write-Output "Compilo i due script Lua... (simboli: $Syms)"
    & (Join-Path $root "build.ps1") -LinkRole client -LinkPort $Port1 -OutName "p1" -Syms $Syms | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "build dello script del giocatore 1 fallita" }
    & (Join-Path $root "build.ps1") -LinkRole client -LinkPort $Port2 -OutName "p2" -Syms $Syms | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "build dello script del giocatore 2 fallita" }
    if ($Players -ge 3) {
        & (Join-Path $root "build.ps1") -LinkRole client -LinkPort $Port3 -OutName "p3" -Syms $Syms | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "build dello script del giocatore 3 fallita" }
    }
    Write-Output "  mgba\inject.p1.lua  (porta $Port1)"
    Write-Output "  mgba\inject.p2.lua  (porta $Port2)"
    if ($Players -ge 3) { Write-Output "  mgba\inject.p3.lua  (porta $Port3)" }
    Write-Output ""
}

# --- 2. relay e bridge ------------------------------------------------------

$sim = @("--delay", $Delay, "--jitter", $Jitter, "--loss", $Loss)

Write-Output "relay     : UDP $RelayPort"
Write-Output "bridge 1  : TCP 127.0.0.1:$Port1"
Write-Output "bridge 2  : TCP 127.0.0.1:$Port2"
Write-Output "stanza    : $Room (la stanza e' la PARTITA, non la mappa)"
if ($Delay -gt 0 -or $Jitter -gt 0 -or $Loss -gt 0) {
    Write-Output "simulatore: ritardo $Delay ms, jitter $Jitter ms, perdita $Loss%"
    Write-Output "            (sola andata per direzione: l'andata e ritorno e' il doppio)"
} else {
    Write-Output "simulatore: spento - usare -Delay/-Jitter/-Loss per le condizioni vere"
}
Write-Output ""

Start-Process -FilePath $python `
    -ArgumentList (@((Join-Path $net "relay.py"), "--port", $RelayPort)) `
    -WorkingDirectory $net
Start-Sleep -Milliseconds 500

Start-Process -FilePath $python `
    -ArgumentList (@((Join-Path $net "client.py"), "--listen", $Port1,
                     "--relay", "127.0.0.1:$RelayPort", "--peer-id", "1",
                     "--room", $Room) + $sim) `
    -WorkingDirectory $net

Start-Process -FilePath $python `
    -ArgumentList (@((Join-Path $net "client.py"), "--listen", $Port2,
                     "--relay", "127.0.0.1:$RelayPort", "--peer-id", "2",
                     "--room", $Room) + $sim) `
    -WorkingDirectory $net

if ($Players -ge 3) {
    Start-Process -FilePath $python `
        -ArgumentList (@((Join-Path $net "client.py"), "--listen", $Port3,
                         "--relay", "127.0.0.1:$RelayPort", "--peer-id", "3",
                         "--room", $Room) + $sim) `
        -WorkingDirectory $net
}

Write-Output ("Avviati in {0} finestre separate." -f (1 + $Players))
Write-Output ""
Write-Output "ORA, con entrambe le istanze di mGBA GIA' IN PARTITA sulla stessa mappa:"
Write-Output "  1a istanza -> Tools > Scripting > Load script > mgba\inject.p1.lua"
Write-Output "  2a istanza -> Tools > Scripting > Load script > mgba\inject.p2.lua"
Write-Output ""
Write-Output "Va bene: nella finestra del relay compare 'entra nella stanza' DUE volte,"
Write-Output "         con '2 in totale', e i due bridge dicono 'gioco collegato'."
Write-Output "Va male: un bridge ripete 'NESSUN EMULATORE COLLEGATO', oppure il relay"
Write-Output "         dice 'e' SOLO nella stanza'. In quel caso hai caricato due volte"
Write-Output "         lo stesso script."
Write-Output ""
Write-Output "NOTA: il relay stampa ancora la stanza in formato mappa (la stanza 1 si"
Write-Output "      legge '0.1'). E' solo cosmetico: da questa versione la stanza e' la"
Write-Output "      partita, e i due giocatori NON devono piu' stare sulla stessa mappa"
Write-Output "      perche' il relay inoltri."
