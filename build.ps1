# =============================================================================
# build.ps1 - compila il payload e genera lo script di iniezione per mGBA
# =============================================================================
#
# NOTA: questo file va tenuto in ASCII puro. PowerShell 5.1 legge gli .ps1 come
# ANSI e i caratteri accentati o i trattini lunghi rompono il parser.
#
# Usa arm-none-eabi-gcc di Windows: devkitARM NON serve per il payload
# (serve solo a compilare pokeemerald, e quello si fa in WSL).
#
# Uso:
#   .\build.ps1
#   .\build.ps1 -PayloadBase 0x02032000
#
# Produce:
#   build\payload.elf / payload.bin / payload.map
#   mgba\inject.generated.lua   <- questo e' il file da caricare in mGBA

# Coda di EWRAM libera nella build matching di Smeraldo USA: la sezione ewram del
# gioco finisce a 0x0203CF64 (dal .map: ewram 0x02000000 0x3CF64), e la EWRAM
# arriva a 0x02040000. Restano circa 12 KB liberi da 0x0203D000 in su.
param(
    # 0x0203CF80. Il confine e' un FATTO DEL LINKER, non una misura: la sezione
    # `ewram` del gioco finisce a 0x0203CF64 (nel .map di pokeemerald non esiste
    # un simbolo sopra: l'ultimo e' ewram_data di rayquaza_scene.o a 0x0203CF60),
    # e il gioco non ha crescita dinamica - tutti e sette i punti che chiamano
    # InitHeap passano gHeap/HEAP_SIZE, un array statico dentro la sezione.
    # Verificato anche sull'italiana in modo esaustivo da tools\ewram_bound.py:
    # il massimo indirizzo EWRAM materializzato dal codice e' 0x0203CF60 su
    # ENTRAMBE le ROM.
    # I 28 byte fra 0x0203CF64 e 0x0203CF80 sono guardia, non necessita'.
    [string]$PayloadBase = "0x0203CF80",

    # Modalita' di collegamento dell'harness Lua:
    #   loopback  i tuoi eventi rientrano da te dopo un ritardo (test con UN emulatore)
    #   server    apre LinkPort e aspetta l'altra istanza
    #   client    si collega a LinkHost:LinkPort (avvia PRIMA il server)
    #   off       nessuna rete, solo diagnostica
    [ValidateSet("loopback", "server", "client", "relay", "off")]
    [string]$LinkRole = "loopback",

    # Ruolo "relay" (2026-08-25): il client sta DENTRO il Lua e parla
    # WebSocket IN CHIARO col frontale relay_ws (il Lua di mGBA non ha
    # TLS). Serve a chi non puo' eseguire Python accanto all'emulatore
    # (Trimui/Knulli e simili). ws:// soltanto, niente wss.
    [string]$RelayUrl = "",
    [int]$RelayRoom = 0,
    [int]$RelayPeer = 0,
    [string]$LinkHost = "127.0.0.1",
    [int]$LinkPort = 8123,

    # Nome dello script generato, senza estensione. Serve a net\run-local.ps1 per
    # produrre due file distinti (inject.p1 / inject.p2) invece di due script
    # chiamati entrambi "client" che si sovrascriverebbero a vicenda.
    [string]$OutName = "",

    # Quale tabella di indirizzi del gioco usare:
    #   usa  payload\game_syms.h    - generato da tools\gen_syms.py dal .map della
    #                                 build pokeemerald (Smeraldo USA, BPEE)
    #   it   payload\game_syms_it.h - generato da tools\port_syms.py cercando gli
    #                                 stessi simboli nel dump della cartuccia
    #                                 italiana (BPEI)
    # I due header hanno lo STESSO include guard (GAME_SYMS_H): passando quello
    # italiano con -include, il successivo #include "game_syms.h" dentro main.c
    # non fa niente. Cosi' la scelta della ROM non tocca una riga di sorgente.
    [ValidateSet("usa", "it")]
    [string]$Syms = "usa",

    # Compila e linka payload\sio.c dentro il payload: e' la build per il GBA
    # FISICO (cavo, Celio, multiboot). Senza, il payload parla solo con
    # l'harness Lua di mGBA (la mailbox la drena lo script).
    #
    # Resta dietro a un interruttore, e NON di default, per costruzione: le due
    # build vanno in due posti diversi (mGBA rifiuta quella col driver, vedi
    # PAYLOAD_HAS_SIO piu' sotto) e una build sbagliata nel posto sbagliato e'
    # gia' costata una Torre Lotta piantata (2026-08-02) e un mbstub muto sul
    # cavo (2026-08-22). Regola: `-WithSio` SUBITO PRIMA di hw\mbstub\build.ps1.
    [switch]$WithSio,

    # Permette all'iniettore Lua di caricare la build -WithSio in EMULATORE,
    # cosa che la guardia altrimenti rifiuta (e' la build che ha piantato la
    # Torre Lotta il 2026-08-02, quando ci e' finita per sbaglio).
    #
    # Serve a UNA cosa sola: eseguire il prendi/molla della porta seriale
    # (SioInit/SioShutdown legati all'overworld) dove c'e' il log, prima che la
    # sua prima esecuzione avvenga sul GBA fisico dove non c'e' niente. La riga
    # [sio] del rapporto e' il criterio: prese - mollate = 1 dentro l'overworld,
    # 0 fuori. Ha senso solo insieme a -WithSio.
    [switch]$AllowSioInEmu
)

$ErrorActionPreference = "Stop"

if ($LinkRole -eq "relay") {
    if (-not $RelayUrl.StartsWith("ws://")) { throw "-LinkRole relay vuole -RelayUrl ws://... (niente wss: il Lua di mGBA non ha TLS)" }
    # -RelayPeer 0 = "lo sorteggia il Lua all'avvio" (2026-08-26): e' il
    # default giusto, perche' il peer deve solo essere unico e nessuno deve
    # coordinarsi dei numeri. La stanza invece va detta: senza, i giocatori
    # non si trovano.
    if ($RelayRoom -lt 1 -or $RelayRoom -gt 65535) { throw "-LinkRole relay vuole -RelayRoom (1..65535)" }
    if ($RelayPeer -lt 0 -or $RelayPeer -gt 65534) { throw "-RelayPeer fuori range (0 = sorteggiato dal Lua, oppure 1..65534)" }
}

if ($AllowSioInEmu -and -not $WithSio) {
    Write-Warning "-AllowSioInEmu senza -WithSio non fa niente: la guardia scatta solo sulle build col driver SIO dentro."
}

$root  = $PSScriptRoot
$build = Join-Path $root "build"

# --- toolchain ---------------------------------------------------------------
# La toolchain sta sull'HDD (regola fissa: niente installazioni sull'SSD di
# sistema - la copia in Program Files e' sparita il 2026-08-02 insieme a
# Python). Il percorso D: si prova per primo; PATH e vecchio percorso restano
# come ripieghi.
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
if (-not (Test-Path $objcopy)) { throw "arm-none-eabi-objcopy non trovato accanto a gcc" }

# --- tabella degli indirizzi del gioco ---------------------------------------
if ($Syms -eq "it") {
    $symsHeader = Join-Path $root "payload\game_syms_it.h"
    if (-not (Test-Path $symsHeader)) {
        throw "manca payload\game_syms_it.h: generalo con tools\port_syms.py"
    }
    $symsLabel = "italiana (BPEI, da tools\port_syms.py)"
} else {
    $symsHeader = Join-Path $root "payload\game_syms.h"
    $symsLabel = "USA (BPEE, dal .map di pokeemerald)"
}

Write-Output "toolchain : $gcc"
Write-Output "base      : $PayloadBase"
Write-Output "simboli   : $symsLabel"
Write-Output "rete      : $LinkRole ($LinkHost`:$LinkPort)"

if (-not (Test-Path $build)) { New-Item -ItemType Directory -Path $build | Out-Null }

# --- compilazione ------------------------------------------------------------
# arm7tdmi, interworking ARM/Thumb: hook.S e' ARM, il C e' Thumb.
$common = @("-mcpu=arm7tdmi", "-mthumb-interwork", "-ffreestanding", "-fno-builtin",
            "-fno-strict-aliasing", "-O2", "-Wall", "-Wextra")

& $gcc @common -marm  -c (Join-Path $root "payload\hook.S") -o (Join-Path $build "hook.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di hook.S fallita" }

$mainDefs = @()
if ($WithSio) { $mainDefs += "-DPAYLOAD_WITH_SIO" }

# main.c SI COMPILA A -Os, non a -O2. Il 2026-08-02, aggiungendo il buffer di
# riproduzione, il margine in coda alla EWRAM (40 byte) e' finito: 96 byte
# oltre. Le vie d'uscita elencate nell'errore della guardia - TX/RX_SLOTS,
# SIO_MAX_WORDS, lo stack - sono tutte tagli FUNZIONALI: meno slot vuol dire
# eventi persi, meno stack vuol dire fidarsi di una misura al posto di un
# limite (la stessa cosa che payload.ld vieta per la IWRAM). -Os no: e' lo
# stesso programma scritto piu' corto.
#
# Misurato: main.o passa da 9808 a 8280 byte di testo, cioe' 1528 byte
# recuperati - il margine torna a oltre 1,5 KB invece di 40, e le prossime
# correzioni smettono di doversi comprare 8 byte alla volta.
#
# (sio.c e' rimasto a -O2 fino al 2026-08-23 "perche' e' il percorso caldo";
# ora e' a -Os anche lui, vedi il commento alla sua riga di compilazione.)
# -fno-jump-tables E' OBBLIGATORIO INSIEME A -Os, non e' una rifinitura: a -Os
# GCC compila gli switch densi come tabelle di salto Thumb-1, che passano da
# __gnu_thumb1_case_uqi - una funzione di libgcc. Il payload si linka con
# -nostdlib, quindi il link e' fallito su MapRemoteToLocal e DoorTick. Il
# sintomo e' rumoroso (undefined reference), ma la causa e' questa riga: se un
# giorno qualcuno toglie il flag, tornera' identico.
#
# I SETTE -fno-* QUI SOTTO SONO MISURATI, NON STIMATI (2026-08-23). Sono
# passi di ottimizzazione di GCC che su QUESTO sorgente, a -Os, producono
# codice Thumb PIU' LUNGO: spenti uno alla volta (125 flag provati) e poi
# combinati in modo greedy, main.o passa da 9508 a 9132 byte di testo, cioe'
# 376 byte di EWRAM. Spegnere un passo di ottimizzazione non puo' cambiare
# la semantica del programma (ogni passo la deve conservare, accesi o spenti
# sono entrambi configurazioni supportate); cambia solo la forma del codice.
# -fno-dce e -fno-sched-pressure valgono 20 byte in due: se un giorno
# infastidiscono, sono i primi da togliere. Il metodo per rimisurare e' in
# NOTES (blocco del 2026-08-23): e' un ciclo di 30 righe di shell, non
# un'arte.
# -fstack-usage e -fcallgraph-info=su producono build\main.su / main.ci, da
# cui la guardia dello stack qui sotto calcola il LIMITE statico del percorso
# IRQ: non toccano il codice generato.
# QUESTO INSIEME E' STATO RIFATTO DA ZERO IL 2026-08-28, e la lezione conta piu'
# della lista: l'ottimo dei flag DIPENDE DAL CODICE. Con l'arrivo di Bump() e di
# OeAt()/SprAt() in main.c (che tolgono ~560 byte da soli), sette dei diciannove
# -fno-* di prima erano diventati DANNOSI - togliendoli si guadagna. La ricerca
# greedy (rimozioni + aggiunte, due giri fino a convergenza) e' passata da 9120 a
# 9032 byte di testo.
#
# La ricerca precedente aveva anche un buco di metodo: provava solo a SPEGNERE
# ottimizzazioni. I 104 flag che a -Os sono gia' spenti non erano mai stati
# provati ACCESI, e uno di loro (-fira-loop-pressure) e' nell'insieme vincente.
#
# Regola: non si tramanda questa lista. Dopo una modifica seria a main.c si
# rilancia la greedy (il ciclo di shell sta in NOTES, blocco del 2026-08-28) e si
# prende quello che dice. Spegnere o accendere un passo di ottimizzazione non
# cambia la semantica del programma: entrambe le configurazioni sono supportate
# da GCC, cambia solo la forma del codice generato.
$sizeFlags = @("-fno-tree-forwprop", "-fno-reorder-blocks", "-fno-guess-branch-probability",
               "-fno-move-loop-invariants", "-fno-sched-pressure", "-fno-tree-dominator-opts",
               "-fno-tree-sink", "-fno-tree-vrp", "-fno-tree-loop-im",
               "-fno-tree-loop-ivcanon", "-fno-tree-fre", "-fno-ipa-reference",
               "-fno-sched-critical-path-heuristic", "-fno-tree-bit-ccp",
               # L'unico ACCESO: e' una strategia di allocazione dei registri,
               # spenta di default a -Os. Vale 12 byte.
               "-fira-loop-pressure")
$stackFlags = @("-fstack-usage", "-fcallgraph-info=su")
& $gcc @common -Os -fno-jump-tables @sizeFlags @stackFlags @mainDefs -mthumb -include $symsHeader -c (Join-Path $root "payload\main.c") -o (Join-Path $build "main.o")
if ($LASTEXITCODE -ne 0) { throw "compilazione di main.c fallita" }

$objs = @((Join-Path $build "hook.o"), (Join-Path $build "main.o"))

if ($WithSio) {
    # sio.c: -Os -fno-jump-tables dal 2026-08-23 (era -O2 "perche' e' il
    # percorso caldo"). Misurato: 988 -> 936 byte di testo, 52 byte di EWRAM.
    # Sul percorso caldo non si perde niente, e la ragione e' fisica: il
    # payload gira in EWRAM, bus a 16 bit con 2 wait state, quindi OGNI
    # istruzione Thumb costa 3 cicli solo per essere letta - il codice piu'
    # corto e' anche quello che il GBA esegue prima. SioOnSerialIrq ha ~2400
    # cicli fra un IRQ e l'altro (6800/s nel caso peggiore) e ne usa una
    # frazione. I sette -fno-* di main.c qui NON si applicano: valevano 4
    # byte e il percorso caldo si tiene con le scelte standard di -Os.
    # -fno-jump-tables per la stessa ragione di main.c (SioRxWord ha uno
    # switch denso: senza il flag il link cade su __gnu_thumb1_case_uqi).
    # I DUE QUI SOTTO SI APPLICANO (2026-08-28, ricerca greedy sui 260 flag):
    # 876 -> 868 byte. Otto byte non sono molti, ma a questo punto il margine
    # si compra otto byte alla volta.
    & $gcc @common -Os -fno-jump-tables -fno-guess-branch-probability -fno-move-loop-invariants @stackFlags -mthumb -c (Join-Path $root "payload\sio.c") -o (Join-Path $build "sio.o")
    if ($LASTEXITCODE -ne 0) { throw "compilazione di sio.c fallita" }
    $objs += (Join-Path $build "sio.o")
}

# --- link --------------------------------------------------------------------
& $gcc @common -nostdlib -nostartfiles `
    -T (Join-Path $root "payload\payload.ld") `
    "-Wl,--defsym,PAYLOAD_BASE=$PayloadBase" `
    "-Wl,-Map,$(Join-Path $build 'payload.map')" `
    -o (Join-Path $build "payload.elf") `
    @objs
if ($LASTEXITCODE -ne 0) { throw "link fallito" }

& $objcopy -O binary (Join-Path $build "payload.elf") (Join-Path $build "payload.bin")
if ($LASTEXITCODE -ne 0) { throw "objcopy fallito" }

$bytes = [System.IO.File]::ReadAllBytes((Join-Path $build "payload.bin"))
Write-Output "payload   : $($bytes.Length) byte"

# Regione da riservare e azzerare in EWRAM: codice + dati + bss. Il valore
# esatto lo dice il simbolo payload_end prodotto dal linker. Lo STACK non c'e'
# piu' dentro (2026-08-25): sta a parte, in cima alla EWRAM, e non va azzerato
# (si scrive prima di leggersi).
$nm = Join-Path $binDir "arm-none-eabi-nm.exe"
$endLine = & $nm (Join-Path $build "payload.elf") | Select-String -Pattern "payload_end" | Select-Object -First 1
if (-not $endLine) { throw "simbolo payload_end assente dall'ELF" }
$payloadEnd = [Convert]::ToUInt32(($endLine.Line -split "\s+")[0], 16)
$baseVal    = [Convert]::ToUInt32($PayloadBase.Replace("0x",""), 16)
$reserved   = $payloadEnd - $baseVal
Write-Output "riservato : $reserved byte (codice + dati + bss; stack a parte)"

# --- LA GUARDIA: IL PAYLOAD NON PUO' SCENDERE SOTTO IL CONFINE DEL GIOCO -----
# Fine della sezione `ewram` di Smeraldo, dal .map di pokeemerald. Sotto questo
# indirizzo c'e' memoria del gioco: scriverci significa corromperlo, e il
# sintomo sarebbe lontano dalla causa.
$EWRAM_SECTION_END = 0x0203CF64
if ($baseVal -lt $EWRAM_SECTION_END) {
    throw ("PAYLOAD_BASE 0x{0:X8} e' SOTTO la fine della sezione ewram del gioco (0x{1:X8}). " -f $baseVal, $EWRAM_SECTION_END) +
          "Quella memoria e' sua. Se serve spazio si recupera dentro il payload, non si scende."
}

# --- LA GUARDIA: NIENTE DEL PAYLOAD PUO' STARE IN IWRAM ----------------------
#
# Sopra la sezione `iwram` del gioco (0x030078AC) ci sono i suoi STACK, e il
# gioco e' autorizzato a scendere fin li'. Una misura di quanto scende davvero e'
# un'osservazione, non un limite: il 2026-08-02 il .bss del driver ci era stato
# messo sulla base di 684 byte visti liberi, ed e' stato tolto per questo.
#
# Qui non si misura niente: si VIETA. Se un simbolo del payload finisce in
# IWRAM - per una riga sbagliata nel linker script, per un attributo di sezione,
# per una svista futura quando manchera' un byte - la build si ferma.
$iwramSyms = & $nm -n (Join-Path $build "payload.elf") |
    Select-String -Pattern "^0300[0-7][0-9a-fA-F]{3}\s"
if ($iwramSyms) {
    $names = ($iwramSyms | ForEach-Object { ($_.Line -split "\s+")[-1] }) -join ", "
    throw "SIMBOLI DEL PAYLOAD IN IWRAM: $names`n" +
          "Sopra 0x030078AC ci sono gli stack del gioco. Nessuna misura di quanto " +
          "scendono e' un limite: la memoria e' sua. Il payload sta in EWRAM, dove il " +
          "confine e' un fatto del linker (vedi tools\ewram_bound.py)."
}

# LA GUARDIA: codice + dati + bss devono stare SOTTO HANDOFF_BASE (0x0203FE00).
#
# Fino al 2026-08-25 il tetto era la fine della EWRAM (0x02040000) e lo stack
# veniva dopo la bss: funzionava perche' tutto insieme restava comunque sotto
# HANDOFF_BASE. Con i 4 giocatori non ci sta piu', e il tetto VERO e' emerso:
# da 0x0203FE00 in su mbstub copia handoff.S DOPO l'azzeramento, quindi una
# bss che sconfina li' si ritrova piena di machine code invece che di zeri.
# Lo STACK invece ci abita apposta (payload.ld): handoff gira una volta al
# boot e poi e' codice morto, e uno stack non ha bisogno di init.
$HANDOFF_BASE = 0x0203FE00
if ($payloadEnd -gt $HANDOFF_BASE) {
    $e = "0x{0:X8}" -f $payloadEnd
    $over = $payloadEnd - $HANDOFF_BASE
    throw "payload_end $e OLTRE HANDOFF_BASE (0x0203FE00) di $over byte: la bss " +
          "finirebbe sotto la copia di handoff.S e ne uscirebbe piena di codice. " +
          "Recupera spazio (vedi NOTES: SIO_MAX_WORDS, TX/RX/RQ_SLOTS)."
}
$headroom = $HANDOFF_BASE - $payloadEnd
Write-Output "margine   : $headroom byte liberi sotto HANDOFF_BASE (stack a parte, in cima)"

# --- LA GUARDIA DELLO STACK PRIVATO: un LIMITE, non una misura (2026-08-23) --
#
# Sullo stack privato (payload.ld) gira SOLO il percorso IRQ: hook.S ->
# payload_frame -> payload_drain, tutto codice nostro, senza chiamate al gioco.
# GCC sa quanto stack usa ogni funzione (-fstack-usage) e chi chiama chi
# (-fcallgraph-info=su): qui si somma lungo il grafo e si ottiene il caso
# peggiore POSSIBILE, non quello visto in una sessione. Tre cose fanno fallire
# la build: (1) una chiamata INDIRETTA raggiungibile dall'IRQ - cioe' una
# funzione del gioco, di cui lo stack non e' calcolabile: va in payload_cb1;
# (2) ricorsione o frame non statici; (3) il limite oltre META' dello stack.
# Il canary di mGBA (inject_body.lua, [stack]) resta come controprova sul
# campo, con lo stesso criterio della meta'.
$ciFiles = @((Join-Path $build "main.ci"))
if ($WithSio) { $ciFiles += (Join-Path $build "sio.ci") }
$stackOf = @{}
$callees = @{}
foreach ($ci in $ciFiles) {
    if (-not (Test-Path $ci)) { throw "manca ${ci}: il compilatore non ha prodotto il grafo delle chiamate (-fcallgraph-info=su)" }
    foreach ($line in Get-Content $ci) {
        if ($line -match '^node: \{ title: "([^"]+)" label: "([^"]*)"') {
            $fname = ($matches[1] -split ':')[-1]
            $label = $matches[2]
            if ($label -match '(\d+) bytes \((static|dynamic|bounded)\)') {
                if ($matches[2] -ne 'static') { throw "stack di $fname non statico ($($matches[2])): alloca o array a dimensione variabile nel payload" }
                $stackOf[$fname] = [int]$matches[1]
            } elseif (-not $stackOf.ContainsKey($fname)) {
                $stackOf[$fname] = 0
            }
        } elseif ($line -match '^edge: \{ sourcename: "([^"]+)" targetname: "([^"]+)"') {
            $s = ($matches[1] -split ':')[-1]
            $t = ($matches[2] -split ':')[-1]
            if (-not $callees.ContainsKey($s)) { $callees[$s] = New-Object System.Collections.Generic.HashSet[string] }
            [void]$callees[$s].Add($t)
        }
    }
}
function Get-IrqStackDepth([string]$f, [string[]]$trail) {
    if ($trail -contains $f) { throw "ricorsione nel percorso IRQ: $($trail -join ' > ') > $f" }
    if ($f -eq '__indirect_call') {
        throw ("CHIAMATA INDIRETTA nel percorso IRQ ({0}): una funzione del gioco chiamata dall'IRQ ha uno stack che nessuno puo' calcolare. " -f ($trail -join ' > ')) +
              "Va spostata in payload_cb1 (main loop) - vedi payload.ld e la regola del 2026-08-21 in CLAUDE.md."
    }
    if (-not $stackOf.ContainsKey($f)) { throw "funzione sconosciuta nel percorso IRQ: $f (chiamata da $($trail -join ' > '))" }
    $own = $stackOf[$f]
    $best = 0
    $bestPath = "$f($own)"
    if ($callees.ContainsKey($f)) {
        foreach ($c in $callees[$f]) {
            $r = Get-IrqStackDepth $c ($trail + $f)
            if ($r.Depth -gt $best -or ($r.Depth -eq $best -and $best -gt 0)) {
                $best = $r.Depth
                $bestPath = "$f($own) > " + $r.Path
            }
        }
    }
    return @{ Depth = ($own + $best); Path = $bestPath }
}
$HOOK_S_PUSH = 8   # hook.S: stmfd sp!, {r1, lr} sul NOSTRO stack prima di payload_frame
$worst = @{ Depth = 0; Path = "" }
foreach ($rootFn in @("payload_frame", "payload_drain")) {
    $r = Get-IrqStackDepth $rootFn @()
    if ($r.Depth -gt $worst.Depth) { $worst = $r }
}
$irqBound = $HOOK_S_PUSH + $worst.Depth
$stackSyms = & $nm (Join-Path $build "payload.elf")
$stkLo = $stackSyms | Select-String -Pattern " payload_stack_bottom$" | Select-Object -First 1
$stkHi = $stackSyms | Select-String -Pattern " payload_stack_top$" | Select-Object -First 1
if (-not $stkLo -or -not $stkHi) { throw "simboli payload_stack_bottom/top assenti dall'ELF" }
$stackSize = [Convert]::ToUInt32(($stkHi.Line -split "\s+")[0], 16) - [Convert]::ToUInt32(($stkLo.Line -split "\s+")[0], 16)
Write-Output ("stack     : limite statico del percorso IRQ {0} byte su {1} ({2}%) - hook.S {3} + {4}" -f $irqBound, $stackSize, [math]::Floor($irqBound * 100 / $stackSize), $HOOK_S_PUSH, $worst.Path)
if ($irqBound * 2 -gt $stackSize) {
    throw ("STACK PRIVATO INSUFFICIENTE: il percorso IRQ puo' usare {0} byte su {1}, piu' della meta'. " -f $irqBound, $stackSize) +
          "Si ALLARGA lo stack in payload.ld (e si ricontrolla il margine), non si toglie la guardia."
}

# --- LA GUARDIA DELLA REGIONE DI HANDOFF (2026-08-25) ------------------------
# Nella regione 0x0203FE00-0x02040000 convivono, dal basso: .lateclear (le code
# per-slot, azzerate da RemotesInitOnce al primo VBlank) e lo stack privato in
# cima. Se .lateclear cresce (RQ_SLOTS, N_REMOTES) fino a toccare lo stack, il
# primo IRQ scriverebbe dentro le code: qui la collisione si ferma in build,
# dove il numero e' leggibile.
$lcEnd = $stackSyms | Select-String -Pattern " lateclear_end$" | Select-Object -First 1
if ($lcEnd) {
    $lcEndVal = [Convert]::ToUInt32(($lcEnd.Line -split "\s+")[0], 16)
    $stkLoVal = [Convert]::ToUInt32(($stkLo.Line -split "\s+")[0], 16)
    if ($lcEndVal -gt $stkLoVal) {
        throw (".lateclear finisce a 0x{0:X8}, DENTRO lo stack privato (parte a 0x{1:X8}). " -f $lcEndVal, $stkLoVal) +
              "Riduci RQ_SLOTS o sposta lo stack: la regione di handoff e' 512 byte in tutto."
    }
    Write-Output ("handoff   : .lateclear fino a 0x{0:X8}, stack da 0x{1:X8} - {2} byte d'aria" -f $lcEndVal, $stkLoVal, ($stkLoVal - $lcEndVal))
}

# Sanity check sul layout che hook.S e l'iniettore danno per scontato:
# +0x08 deve contenere il magic HOOK (0x4B4F4F48, little endian).
if ($bytes.Length -lt 0x14) { throw "payload troppo corto: layout non valido" }
$magic = [BitConverter]::ToUInt32($bytes, 0x08)
if ($magic -ne 0x4B4F4F48) {
    $m = "0x{0:X8}" -f $magic
    throw "magic errato a +0x08: $m (atteso 0x4B4F4F48), layout del linker script rotto"
}
$stateMagic = [BitConverter]::ToUInt32($bytes, 0x10)
if ($stateMagic -ne 0x53544154) {
    $m = "0x{0:X8}" -f $stateMagic
    throw "magic di stato errato a +0x10: $m (atteso 0x53544154)"
}
# --- dove sta la mailbox ------------------------------------------------------
# Non e' piu' inchiodata a +0x200: segue PayloadState (payload.ld). L'offset si
# LEGGE dall'ELF e diventa l'unica fonte per tutti - lo script Lua generato e i
# controlli qui sotto. Cablarlo a mano in due posti e' come stava prima, ed e'
# il motivo per cui c'erano 152 byte di buco che nessuno rivedeva.
$symsAll = & $nm -S (Join-Path $build "payload.elf")

$mboxLine = $symsAll | Select-String -Pattern " g_mailbox$" | Select-Object -First 1
if (-not $mboxLine) { throw "simbolo g_mailbox assente dall'ELF" }
$offMailbox = [Convert]::ToUInt32(($mboxLine.Line -split "\s+")[0], 16) - $baseVal

if ($bytes.Length -lt $offMailbox + 4) { throw "payload troppo corto: manca la mailbox" }
$mboxMagic = [BitConverter]::ToUInt32($bytes, $offMailbox)
if ($mboxMagic -ne 0x584F424D) {
    $m = "0x{0:X8}" -f $mboxMagic
    throw ("magic mailbox errato a +0x{0:X}: {1} (atteso 0x584F424D)" -f $offMailbox, $m)
}
Write-Output ("layout    : magic HOOK, STAT e MBOX al posto giusto (mailbox a +0x{0:X})" -f $offMailbox)

$stateLine = $symsAll | Select-String -Pattern " g_state$" | Select-Object -First 1
if ($stateLine) {
    $stateSize = [Convert]::ToUInt32(($stateLine.Line -split "\s+")[1], 16)
    $gap = $offMailbox - (0x10 + $stateSize)
    Write-Output "stato     : $stateSize byte, $gap byte di allineamento prima della mailbox"
    # Ora PayloadState puo' crescere quanto vuole: la mailbox si sposta da sola.
    # Quello che NON deve succedere e' che il buco torni a essere grande: se
    # cresce oltre l'allineamento, qualcuno ha rimesso un offset fisso.
    if ($gap -ge 8) {
        Write-Warning "fra PayloadState e la mailbox ci sono $gap byte: piu' del semplice allineamento. Qualcuno ha rimesso un offset fisso in payload.ld?"
    }

    # --- IL CONTRATTO struct PayloadState <-> tabella S di inject_body.lua ----
    # Si toccano insieme, dice il commento nel Lua; dal 2026-08-23 lo controlla
    # la build. Un campo aggiunto di la' e non di qua da' "attempt to perform
    # arithmetic on a nil value" per ogni riga di stato in mGBA (gia' successo
    # il 2026-07-29). Qui: i campi sono tutti u32/s32 (4 byte, niente
    # padding), quindi l'offset del campo i-esimo e' 4*i; si pretende che la
    # tabella S abbia ESATTAMENTE quegli offset, tanti quanti i campi, e che
    # ogni nome C compaia (irqCount/vblankCount stanno nel Lua come irq/vblank,
    # dal 2026-07-29).
    $mainLines = Get-Content (Join-Path $root "payload\main.c")
    $inStruct = $false
    $stFields = @()
    foreach ($ln in $mainLines) {
        if (-not $inStruct) { if ($ln -match '^struct PayloadState\s*$') { $inStruct = $true }; continue }
        if ($ln -match '^\};') { break }
        if ($ln -match '^\s+(u32|s32)\s+(\w+);') { $stFields += $matches[2] }
    }
    if ($stFields.Count * 4 -ne $stateSize) {
        throw ("PayloadState: letti {0} campi in main.c ({1} byte) ma g_state nell'ELF e' di {2} byte. Un campo non e' u32/s32 su una riga sola?" -f $stFields.Count, ($stFields.Count * 4), $stateSize)
    }
    $luaText = Get-Content -Raw (Join-Path $root "mgba\inject_body.lua")
    $sMatch = [regex]::Match($luaText, '(?s)\nlocal S = \{(.*?)\n\}')
    if (-not $sMatch.Success) { throw "tabella S non trovata in mgba\inject_body.lua" }
    $sEntries = @{}
    # Via i commenti Lua prima di cercare le voci: "Bitmap completa = 0x3FFF"
    # in un commento conterebbe come un campo.
    $sBody = [regex]::Replace($sMatch.Groups[1].Value, '--[^
]*', '')
    foreach ($m in [regex]::Matches($sBody, '(\w+)\s*=\s*0x([0-9A-Fa-f]+)')) {
        $sEntries[$m.Groups[1].Value] = [Convert]::ToInt32($m.Groups[2].Value, 16)
    }
    $alias = @{ irqCount = "irq"; vblankCount = "vblank" }
    $problems = @()
    for ($i = 0; $i -lt $stFields.Count; $i++) {
        $cName = $stFields[$i]
        $lName = if ($alias.ContainsKey($cName)) { $alias[$cName] } else { $cName }
        if (-not $sEntries.ContainsKey($lName)) { $problems += "manca nel Lua: $cName (+0x{0:X})" -f (4 * $i); continue }
        if ($sEntries[$lName] -ne 4 * $i) { $problems += ("offset diverso: {0} e' +0x{1:X} in main.c, 0x{2:X} nel Lua" -f $cName, (4 * $i), $sEntries[$lName]) }
    }
    if ($sEntries.Count -ne $stFields.Count) { $problems += ("la tabella S ha {0} voci, la struct {1} campi" -f $sEntries.Count, $stFields.Count) }
    if ($problems.Count -gt 0) {
        throw "CONTRATTO PayloadState/tabella S ROTTO:`n  " + ($problems -join "`n  ") + "`nSi toccano insieme: main.c e mgba\inject_body.lua."
    }
    Write-Output ("contratto : struct PayloadState ({0} campi) e tabella S del Lua concordano" -f $stFields.Count)

    # --- E CHIUNQUE ALTRO SI SCRIVA GLI OFFSET IN CASA -----------------------
    # La guardia qui sopra copre inject_body.lua, che era l'unico a conoscere
    # PayloadState. Non lo e' piu': i banchi di prova sotto mgba\ hanno la loro
    # copia degli offset, e il 2026-08-30 e' successo esattamente il guaio
    # previsto - tolto un campo in mezzo alla struct, banco_avatar.lua ha
    # continuato a leggere gli indirizzi vecchi e ha riportato NUMERI PLAUSIBILI
    # MA SBAGLIATI (i respawn contati come scrub, e uno zero letto oltre la fine
    # della struct). Un banco che mente e' peggio di un banco rotto.
    #
    # La convenzione e' la dichiarazione: `local PS_NOMECAMPO = 0x...`. Chi la
    # segue viene controllato; chi indirizza a mano (STAT + 0x18) resta fuori,
    # e va bene cosi' - la guardia copre la CLASSE, non ogni singolo caso.
    $offProblemi = @()
    $offMap = @{}
    for ($i = 0; $i -lt $stFields.Count; $i++) { $offMap[$stFields[$i].ToUpper()] = 4 * $i }
    $nOff = 0
    foreach ($f in (Get-ChildItem (Join-Path $root 'mgba') -Filter '*.lua')) {
        foreach ($ln in (Get-Content $f.FullName)) {
            if ($ln -match '^\s*local PS_(\w+)\s*=\s*0x([0-9A-Fa-f]+)') {
                $nome = $matches[1].ToUpper()
                $val  = [Convert]::ToInt32($matches[2], 16)
                $nOff++
                if (-not $offMap.ContainsKey($nome)) {
                    $offProblemi += ('{0}: PS_{1} non e'' un campo di PayloadState' -f $f.Name, $matches[1])
                } elseif ($offMap[$nome] -ne $val) {
                    $offProblemi += ('{0}: PS_{1} dice 0x{2:X}, la struct dice +0x{3:X}' -f $f.Name, $matches[1], $val, $offMap[$nome])
                }
            }
        }
    }
    if ($offProblemi.Count -gt 0) {
        throw "OFFSET DI PayloadState FUORI POSTO:`n  " + ($offProblemi -join "`n  ") + "`nChi si copia gli offset li aggiorna quando la struct cambia."
    }
    Write-Output ('offset    : {0} PS_* nei banchi Lua, tutti allineati alla struct' -f $nOff)
}

# --- generazione dello script Lua -------------------------------------------
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("-- GENERATO DA build.ps1 - non modificare a mano.")
[void]$sb.AppendLine("-- Modifica payload\main.c oppure mgba\inject_body.lua e ricompila.")
[void]$sb.AppendLine("PAYLOAD_BASE = $PayloadBase")
[void]$sb.AppendLine("PAYLOAD_RESERVED = $reserved")
# Letto dall'ELF, non scritto a mano: e' l'unica fonte dell'offset della mailbox.
[void]$sb.AppendLine(("PAYLOAD_OFF_MAILBOX = 0x{0:X}" -f $offMailbox))

# LA BANDIERA CHE IMPEDISCE IL DISASTRO DEL 2026-08-02.
#
# La build -WithSio scrive nello STESSO file inject.<ruolo>.lua di quella per
# l'emulatore. Compilando -WithSio per ultima, l'harness Lua si e' ritrovato in
# mano un payload che al primo VBlank chiama SioInit() e riconfigura REG_RCNT,
# REG_SIOCNT e REG_IE in modalita' Multi-Player con IRQ - dentro mGBA, dove non
# c'e' nessun partner e dove il codice link del gioco gira comunque a ogni
# VBlank. Il gioco si e' piantato alla Torre Lotta.
#
# L'#ifdef PAYLOAD_WITH_SIO impediva ai DUE CODICI di convivere, non ai due
# ARTEFATTI di scambiarsi di posto. Questa bandiera chiude il secondo buco:
# inject_body.lua si rifiuta di iniettare un payload marcato cosi'.
[void]$sb.AppendLine("PAYLOAD_HAS_SIO = " + $(if ($WithSio) { "true" } else { "false" }))

# Lo scavalco ESPLICITO della guardia di cui sopra: serve alla prova del
# prendi/molla della porta seriale in emulatore (vedi il parametro in testa).
# Emesso sempre, cosi' inject_body.lua non dipende da un nil.
[void]$sb.AppendLine("PAYLOAD_SIO_EMU_OK = " + $(if ($WithSio -and $AllowSioInEmu) { "true" } else { "false" }))

# I contatori del driver SIO (g_sio in sio.c), perche' la riga [sio] del
# rapporto possa leggerli: senza, i contatori esistono nel payload ma nessun
# log li dice - che per la regola del progetto e' un difetto, non un dettaglio.
if ($WithSio) {
    $l = $symsAll | Select-String -Pattern " g_sio`$" | Select-Object -First 1
    if (-not $l) { throw "simbolo g_sio assente dall'ELF di una build -WithSio" }
    $v = [Convert]::ToUInt32(($l.Line -split "\s+")[0], 16)
    [void]$sb.AppendLine(("PAYLOAD_G_SIO = 0x{0:X8}" -f $v))
}

# Confini dello stack privato del payload, per la misura del watermark (W''-4).
#
# Perche' emetterli invece di misurare dal payload: il riempimento del motivo lo
# fa il CARICATORE e il calcolo lo fa lo SCRIPT, quindi la misura non costa un
# solo byte della coda di EWRAM - che e' esattamente la risorsa scarsa.
foreach ($s in @("payload_stack_bottom", "payload_stack_top")) {
    $l = $symsAll | Select-String -Pattern " $s`$" | Select-Object -First 1
    if (-not $l) { throw "simbolo $s assente dall'ELF" }
    $v = [Convert]::ToUInt32(($l.Line -split "\s+")[0], 16)
    [void]$sb.AppendLine(("{0} = 0x{1:X8}" -f $s.ToUpper(), $v))
}
[void]$sb.AppendLine("LINK_ROLE = `"$LinkRole`"")
[void]$sb.AppendLine("LINK_HOST = `"$LinkHost`"")
[void]$sb.AppendLine("LINK_PORT = $LinkPort")
[void]$sb.AppendLine("RELAY_URL = `"$RelayUrl`"")
[void]$sb.AppendLine("RELAY_ROOM = $RelayRoom")
[void]$sb.AppendLine("RELAY_PEER = $RelayPeer")

# Impronta della ROM attesa dall'iniettore. Senza questo, con -Syms it il payload
# avrebbe gli indirizzi italiani ma l'iniettore rifiuterebbe la cartuccia
# italiana, perche' i valori USA sono cablati in inject_body.lua.
if ($Syms -eq "it") {
    $profilePath = Join-Path $root "payload\rom_profile_it.txt"
    if (-not (Test-Path $profilePath)) {
        throw "manca payload\rom_profile_it.txt: rigenera con tools\port_syms.py"
    }
    foreach ($line in Get-Content $profilePath) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*(\w+)\s*=\s*(.+?)\s*$') {
            $key = $matches[1]; $val = $matches[2]
            if ($val -match '^0x[0-9A-Fa-f]+$') {
                [void]$sb.AppendLine("ROM_$key = $val")
            } else {
                [void]$sb.AppendLine("ROM_$key = `"$val`"")
            }
        }
    }
    Write-Output "impronta  : letta da payload\rom_profile_it.txt"
}
# I byte del payload NON vanno emessi come catena di `..`.
#
# Il parser di Lua annida a destra le concatenazioni, e la ricorsione sfonda
# LUAI_MAXCCALLS, che vale 200. Misurato il 2026-07-30: a 166 pezzi lo script
# compilava, a 213 no. E il modo in cui fallisce e' il peggiore possibile: mGBA
# non stampa NIENTE, nemmeno un errore, perche' il file non arriva a essere
# eseguito - quindi sembra che l'iniettore sia muto invece che rotto.
#
# Con una tabella la profondita' di annidamento e' costante qualunque sia la
# dimensione del payload, quindi il difetto non puo' ripresentarsi crescendo.
[void]$sb.AppendLine("local __chunks = {}")

$perLine = 64
for ($i = 0; $i -lt $bytes.Length; $i += $perLine) {
    $chunk = ""
    for ($j = $i; ($j -lt $i + $perLine) -and ($j -lt $bytes.Length); $j++) {
        $chunk += "\" + $bytes[$j].ToString()
    }
    [void]$sb.AppendLine('__chunks[#__chunks + 1] = "' + $chunk + '"')
}
[void]$sb.AppendLine("PAYLOAD_BYTES = table.concat(__chunks)")
[void]$sb.AppendLine("")
# Il club (mgba\club_lua.lua) va PRIMA del corpo: definisce la globale ClubLua
# che inject_body.lua usa. E' un file a parte perche' e' un pezzo a se':
# se cambia il protocollo del club si tocca quello e net\club_link.py.
[void]$sb.AppendLine((Get-Content -Raw (Join-Path $root "mgba\club_lua.lua")))
[void]$sb.AppendLine("")
[void]$sb.AppendLine((Get-Content -Raw (Join-Path $root "mgba\inject_body.lua")))

# Il nome porta il ruolo: per il test a due istanze servono DUE script diversi
# contemporaneamente, e un nome fisso significa che compilare il client
# sovrascrive quello del server. Con -OutName si sceglie esplicitamente.
$leaf = if ($OutName) { $OutName } else { $LinkRole }
$outLua = Join-Path $root "mgba\inject.$leaf.lua"
[System.IO.File]::WriteAllText($outLua, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
Write-Output "lua       : $outLua"

# Lo script viene COMPILATO davvero prima di dichiararlo pronto. Uno script che
# non compila, in mGBA, non da' nessun errore: da' silenzio, e sembra un difetto
# del payload. Vedi tools\check_lua.py.
# L'interprete sta sull'HDD (regola fissa: niente installazioni sull'SSD di
# sistema) e NON e' sul PATH. Sul PATH c'e' solo il segnaposto del Microsoft
# Store, che esce in silenzio con codice 9009: Get-Command lo TROVA, quindi
# senza questo controllo il ramo "python trovato" partirebbe e non farebbe
# niente. Percorso pieno prima, PATH solo come ripiego.
$python = if (Test-Path 'D:\Progettini\Python313\python.exe') {
    'D:\Progettini\Python313\python.exe'
} else {
    $p = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($p -and $p -notmatch 'WindowsApps') { $p } else { $null }
}
if ($python) {
    & $python (Join-Path $root "tools\check_lua.py") $outLua (Join-Path $build "payload.bin")
    if ($LASTEXITCODE -ne 0) { throw "lo script Lua generato non e' valido: vedi sopra" }
} else {
    Write-Warning "python non trovato: lo script Lua NON e' stato compilato per verifica"
}

Write-Output ""
Write-Output "Fatto. In mGBA: Tools > Scripting > Load script > mgba\inject.$leaf.lua"
Write-Output ""
Write-Output "IMPORTANTE: carica lo script solo QUANDO SEI GIA' IN PARTITA e stai"
Write-Output "camminando. Sul titolo o nei menu il payload dorme e non manda niente."
if ($LinkRole -eq "server" -or $LinkRole -eq "client") {
    Write-Output "Ordine per il test a due istanze: prima il server, poi il client"
    Write-Output "(socket.connect di mGBA e' bloccante)."
}
