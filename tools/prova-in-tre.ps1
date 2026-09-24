# =============================================================================
# prova-in-tre.ps1 - la partita a TRE in emulatore, SENZA MANI (2026-08-25)
# =============================================================================
#
# NOTA: ASCII puro, come gli altri .ps1.
#
# PERCHE'. La procedura Q chiede tre istanze di mGBA guidate a mano. Questo
# script fa la stessa cosa da solo: mGBA 0.11 ha --script, e l'autopilota
# (mgba\autopilota.lua) sa premere i tasti con emu:setKeys e fotografare con
# emu:screenshot. Serve a vedere i PIXEL senza dipendere da chi ha il pad -
# e le foto restano come prova.
#
# Uso:
#   .\tools\prova-in-tre.ps1 -Rom <smeraldo-ita.gba> [-Giocatori 3] [-Secondi 90]
#
# Cosa fa, in ordine:
#   1. compila i Lua di iniezione (uno per giocatore, porte 8201/8202/8203)
#   2. avvia relay + un client.py per giocatore
#   3. avvia un mGBA per giocatore con l'autopilota, che entra in partita,
#      inietta il payload e cammina
#   4. dopo -Secondi raccoglie foto e rapporti e chiude tutto
#
# I file finiscono in build\prova-in-tre\.
param(
    [Parameter(Mandatory = $true)]
    [string]$Rom,
    [ValidateRange(2, 3)]
    [int]$Giocatori = 3,
    [int]$Secondi = 90,
    [string]$Mgba = "D:\WinDS PRO\emu\mGBA\mGBA.exe",
    [int]$RelayPort = 19040,
    [int]$Room = 47050,
    # I simboli del gioco. "it" e' la cartuccia italiana dumpata (il caso
    # normale); "usa" serve per provare con la ROM costruita dalla decomp
    # (repo-studio\pokeemerald\pokeemerald.gba), che sta gia' sul disco e non
    # chiede di avere la cartuccia sottomano. La ROM passata con -Rom e i
    # simboli DEVONO essere la stessa versione, o l'iniettore rifiuta.
    [ValidateSet("it", "usa")]
    [string]$Syms = "it",
    # Perdita simulata sul tratto verso il relay, in percentuale: serve a far
    # scattare le correzioni di posizione, che con un canale perfetto non si
    # vedono mai.
    [double]$Loss = 0,
    # Un secondo script Lua caricato dopo l'iniettore, nel PRIMO giocatore:
    # serve ai banchi che devono PROVOCARE un caso invece di aspettarlo
    # (mgba/banco_subpixel.lua).
    [string]$Banco = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$net  = Join-Path $root "net"
$mg   = Join-Path $root "mgba"
$out  = Join-Path $root "build\prova-in-tre"
$py   = "D:\Progettini\Python313\python.exe"

# I simboli che l'autopilota legge per sapere dov'e' il giocatore: devono
# essere gli stessi con cui il payload e' stato compilato, o l'iniettore
# rifiuta e a schermo non succede niente.
$symsFile = if ($Syms -eq "usa") { "game_syms.h" } else { "game_syms_it.h" }

if (-not (Test-Path $Rom))  { throw "ROM non trovata: $Rom" }
if (-not (Test-Path $Mgba)) { throw "mGBA non trovato: $Mgba" }
if (-not (Test-Path $py))   { throw "python non trovato: $py" }

if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

# --- 1. i Lua di iniezione ---------------------------------------------------
# Uno per giocatore, ognuno col suo ponte TCP. I simboli li sceglie -Syms:
# "it" per la cartuccia italiana dumpata (il caso normale), "usa" per la ROM
# costruita dalla decomp.
if (-not $SkipBuild) {
    # build.ps1 si lancia in un PROCESSO A PARTE, e non e' pignoleria: il
    # linker scrive un warning su stderr (LOAD segment RWX) e build.ps1 ha il
    # suo ErrorActionPreference=Stop, quindi in-process quel warning diventa
    # un'eccezione che ferma tutto. In un processo separato l'esito lo dice
    # l'ExitCode, che e' l'unica cosa che conta davvero.
    for ($i = 1; $i -le $Giocatori; $i++) {
        $porta = 8200 + $i
        $b = Start-Process powershell -PassThru -Wait -WindowStyle Hidden `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            (Join-Path $root "build.ps1"),
                            "-LinkRole", "client", "-LinkPort", $porta,
                            "-OutName", ("q" + $i), "-Syms", $Syms)
        if ($b.ExitCode -ne 0) { throw "build del Lua del giocatore $i fallita (exit $($b.ExitCode))" }
        if (-not (Test-Path (Join-Path $mg ("inject.q{0}.lua" -f $i)))) {
            throw "build finita ma manca mgba\inject.q$i.lua"
        }
    }
    Write-Output "Lua di iniezione: q1..q$Giocatori (porte 8201..)"
}

# --- 2. relay e client -------------------------------------------------------
$procs = @()
$procs += Start-Process $py -ArgumentList @((Join-Path $net "relay.py"), "--port", $RelayPort, "--verbose") `
    -WorkingDirectory $net -PassThru -WindowStyle Minimized `
    -RedirectStandardOutput (Join-Path $out "relay.log") -RedirectStandardError (Join-Path $out "relay.err")
Start-Sleep -Milliseconds 800

for ($i = 1; $i -le $Giocatori; $i++) {
    $procs += Start-Process $py -ArgumentList @((Join-Path $net "client.py"),
        "--listen", (8200 + $i), "--relay", "127.0.0.1:$RelayPort",
        "--peer-id", $i, "--room", $Room, "--loss", $Loss) `
        -WorkingDirectory $net -PassThru -WindowStyle Minimized `
        -RedirectStandardOutput (Join-Path $out "client$i.log") `
        -RedirectStandardError (Join-Path $out "client$i.err")
    Start-Sleep -Milliseconds 400
}
Write-Output "relay + $Giocatori client avviati (stanza $Room)"

# --- 3. gli emulatori --------------------------------------------------------
# Ogni istanza carica un file di tre righe: le variabili dell'autopilota, poi
# l'autopilota, che a sua volta carichera l'iniettore quando sara il momento.
# Il salvataggio (.srm) va copiato accanto a ogni copia della ROM, o le tre
# istanze si contenderebbero lo stesso file.
$asse = @("verticale", "orizzontale", "verticale")
for ($i = 1; $i -le $Giocatori; $i++) {
    $romCopia = Join-Path $out ("gioco$i.gba")
    Copy-Item $Rom $romCopia
    # mGBA cerca il salvataggio accanto alla ROM con estensione .SAV: un
    # .srm (il nome di RetroArch/VBA) lo ignora e parte una partita NUOVA -
    # il primo tentativo del 2026-08-25 e' finito sull'intro di Birch. Si
    # accetta l'uno o l'altro come sorgente e si scrive sempre .sav.
    $save = $null
    foreach ($est in @(".sav", ".srm")) {
        $cand = [IO.Path]::ChangeExtension($Rom, $est)
        if (Test-Path $cand) { $save = $cand; break }
    }
    if ($save) {
        Copy-Item $save (Join-Path $out ("gioco$i.sav"))
    } else {
        Write-Warning ("nessun salvataggio accanto a {0}: le istanze partiranno da una partita nuova" -f $Rom)
    }

    $boot = Join-Path $out ("boot$i.lua")
    $testo = @"
AUTO_NOME = "p$i"
AUTO_DIR = [[$($out -replace '\\','/')]]
AUTO_INJECT = [[$(($mg -replace '\\','/'))/inject.q$i.lua]]
AUTO_PASSI = "$($asse[$i-1])"
AUTO_BANCO = $(if ($Banco -and $i -eq 1) { "[[" + ($Banco.Replace("\","/")) + "]]" } else { "nil" })
AUTO_SYMS = [[$(($root -replace '\\','/'))/payload/$symsFile]]
dofile([[$(($mg -replace '\\','/'))/autopilota.lua]])
"@
    [IO.File]::WriteAllText($boot, $testo, [Text.Encoding]::ASCII)

    $procs += Start-Process $Mgba -ArgumentList @("--script", $boot, $romCopia) -PassThru
    Start-Sleep -Milliseconds 1200
}
Write-Output "$Giocatori istanze di mGBA avviate con l'autopilota"

# --- 4. si aspetta e si raccoglie -------------------------------------------
Write-Output "in corso per $Secondi secondi..."
Start-Sleep -Seconds $Secondi

foreach ($p in $procs) {
    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
}
Start-Sleep -Seconds 2

Write-Output ""
Write-Output "RACCOLTO in $out :"
Get-ChildItem $out -Filter "foto-*.png" | ForEach-Object {
    Write-Output ("  {0,-24} {1,7} byte" -f $_.Name, $_.Length)
}
foreach ($f in (Get-ChildItem $out -Filter "autopilota-*.txt")) {
    Write-Output ""
    Write-Output ("--- {0}" -f $f.Name)
    Get-Content $f.FullName | Select-Object -Last 6 | ForEach-Object { Write-Output ("    " + $_) }
}
