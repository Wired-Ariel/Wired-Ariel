# gen3-poke-multiplayer

> 🇬🇧 English version: [README.md](README.md)

**Vedi i tuoi amici camminare nel tuo Pokémon Smeraldo. Su Game Boy Advance vero, con la cartuccia originale, attraverso internet.**

Nessuna ROM modificata, nessun salvataggio toccato, nessun trucco da fare in partita:
colleghi il cavo, accendi, giochi — e gli altri giocatori compaiono nella tua mappa e ci
camminano con l'animazione vera del gioco.

🌐 **Prova subito dal browser:** https://gbcatrade.wired-ariel.it/
📦 **Binari pronti (firmware, stub multiboot, script per mGBA):** pagina *Releases* di questo repository

| Giocatore 1 | Giocatore 2 | Giocatore 3 |
|---|---|---|
| ![p1](docs/img/2026-08-25-tre-giocatori-p1.png) | ![p2](docs/img/2026-08-25-tre-giocatori-p2.png) | ![p3](docs/img/2026-08-25-tre-giocatori-p3.png) |

*La stessa scena vista da tre partite diverse: ognuno vede gli altri due.*

| Si cammina insieme | Cable Club via internet | Scambio completato |
|---|---|---|
| ![insieme](docs/img/foto-p3-73.png) | ![club](docs/img/2026-08-27-club-due-emulatori-saletta.png) | ![scambio](docs/img/2026-08-27-SCAMBIO-FATTO-p1.png) |

> *English summary:* gen3-poke-multiplayer brings **free-roaming overworld multiplayer to Pokémon Emerald on
> real GBA hardware with an unmodified cartridge**. A tiny payload is loaded into RAM via
> multiboot *before* the cartridge boots, hooks the IRQ vector, and draws remote players as
> native object events driven by the game's own movement actions. Transport: GBA link cable →
> Raspberry Pi Pico (patched Celio firmware) → USB/WebUSB → UDP/WebSocket relay. Up to 4
> players, mixed hardware + mGBA emulator + browser. Italian and English (USA/Europe) Emerald; English cartridge on real GBA not yet hardware-tested.
> The software was written by **Claude (Anthropic)** in guided sessions, with all hardware
> testing done by Lain.

> ⚠️ **A tuo rischio.** Il programma non scrive mai il salvataggio, ma esegue codice dentro il gioco sulla tua
> cartuccia originale, e **non ha nessuna garanzia** (GPL-3.0). Se puoi, **fai prima un backup del salvataggio**
> (con un dumper di cartucce o una flash cart; in mGBA basta copiare il file `.sav`).

---

## Indice

1. [Cosa fa (e cosa no)](#cosa-fa-e-cosa-no)
2. [Cosa ti serve](#cosa-ti-serve)
3. [Come si gioca — tre modi](#come-si-gioca--tre-modi)
4. [Come funziona davvero](#come-funziona-davvero)
5. [Architettura del repository](#architettura-del-repository)
6. [Compilare da sorgente](#compilare-da-sorgente)
7. [Test](#test)
8. [Ospitare il proprio relay](#ospitare-il-proprio-relay)
9. [Limiti noti](#limiti-noti)
10. [Crediti e fonti](#crediti-e-fonti)
11. [Note legali](#note-legali)

---

## Cosa fa (e cosa no)

**Fa:**
- mostra **fino a 3 amici** (4 giocatori in tutto) che camminano, corrono, vanno in bici e fanno
  surf nella tua mappa, con animazione, passi e interpolazione **del motore originale**;
- segue i cambi mappa: quando un amico attraversa il confine di una route lo vedi arrivare,
  quando entra in una casa sparisce e ricompare quando esce;
- si mette a dormire da solo in lotta, nei menu e nelle schermate speciali, e si risveglia al
  ritorno nell'overworld;
- **scambi e lotte via internet** attraverso il Cable Club del gioco (provato sul fisico, dal
  sito e dal pannello per PC);
- fa giocare insieme, **nella stessa stanza**, GBA veri, emulatori mGBA e spettatori dal browser;
- ha una **Mappa live** di Hoenn dove vedi dove sono tutti.

**Non fa:**
- non modifica la cartuccia (è ROM a maschera: fisicamente impossibile) e **non scrive il
  salvataggio**;
- non distribuisce il gioco né parti di esso: **serve la tua cartuccia** (o il tuo dump, per l'emulatore);
- non richiede nessuna azione dentro il gioco: niente glitch, niente box del PC, niente
  sequenze di tasti.

---

## Cosa ti serve

### Per giocare su GBA vero
- un **Game Boy Advance** (o GBA SP) e una cartuccia originale di **Pokémon Smeraldo**, **italiana** o
  **inglese (USA/Europa)** *(inglese: nuova, vedi Limiti noti)*;
- un **Raspberry Pi Pico (RP2040)** con una scheda link (es.
  [game-boy-pico-link-board](https://github.com/agtbaskara/game-boy-pico-link-board)) e un
  **cavo link GBA** a 5 contatti;
- il firmware **Celio esteso da questo progetto** (`celio.uf2` nelle Releases — si flasha una volta
  sola: tieni premuto BOOTSEL, collega il Pico, trascina il file sul disco `RPI-RP2`);
- **Chrome o Edge** (serve WebUSB). Su **Windows**, una volta sola, il driver **WinUSB** per il
  Pico con [Zadig](https://zadig.akeo.ie/).

### Per giocare in emulatore
- **mGBA 0.10 o successivo** (con scripting Lua) e il **tuo** dump di Pokémon Smeraldo, **italiano o inglese (USA/Europa)**;
- lo script per mGBA adatto alla tua ROM (lo scarichi dal sito già configurato con la tua stanza, o dalle Releases:
  `gen3-poke-multiplayer-emulatore.lua` per la ROM italiana, `gen3-poke-multiplayer-emulatore-usa.lua` per quella inglese).

### Per guardare e basta
- un browser qualsiasi, anche Firefox: modalità **spettatore** + Mappa live.

---

## Come si gioca — tre modi

Tutti e tre finiscono **nella stessa stanza**: scegliete un numero fra 1 e 65535 e usatelo tutti.

### A. GBA vero, dal browser (il modo consigliato)
1. Apri il sito, scegli la **versione del gioco** (italiana o inglese), premi **«Collega il Pico»** e scegli il dispositivo.
2. Accendi il GBA **senza cartuccia**, col cavo collegato. Premi **«Carica il gioco nel GBA»**:
   lo schermo diventa **rosso** (~15 s, il programma viaggia nel cavo).
3. Quando il sito lo dice, **inserisci la cartuccia a console accesa**: schermo **giallo**, poi
   **verde**, e Smeraldo parte normalmente — con il nostro programma già dentro.
4. Carica la partita, scrivi la **stanza** e premi **«Gioca»**. Fatto.

### B. GBA vero, dal pannello per PC
```bash
pip install pyusb libusb-package
```
```bash
net\PANNELLO.bat
```
Si apre `http://127.0.0.1:7411`: stessi passi del sito (relay, multiboot, partita, sblocco),
più i log completi. Di default il pannello **ospita un relay sul tuo PC** (ruolo «ospite»): per
collegarti al relay pubblico scegli il ruolo **«amico»** nelle impostazioni e come relay
`wss://gbcatrade.wired-ariel.it/ws`. Ripiego a riga di comando: `net\1-multiboot.bat` poi `net\2-gioca-internet.bat`. Il pannello carica lo stub
italiano: con una **cartuccia inglese** carica `mbstub-usa.gba` (dalle Releases) con
`python net\mb_multi.py mbstub-usa.gba`, oppure usa il sito.

### C. Emulatore mGBA
1. Sul sito scrivi la stanza, scegli la tua ROM (Smeraldo **italiano** o Emerald **inglese**) e premi
   **«Scarica lo script per l'emulatore»**.
2. In mGBA carica la tua ROM di Smeraldo, arriva nell'overworld, poi *Tools → Scripting → File →
   Load script* → lo script scaricato (`gen3-poke-multiplayer-stanzaN-ita.lua` o `-eng.lua`).
3. Stanza e peer si cambiano anche dentro il gioco: **L+R+B** apre un pannellino (aprilo da fermo).

---

## Come funziona davvero

### Il vincolo che governa tutto
Il cavo link non può aggiungere funzioni a un gioco: può solo parlare il protocollo che la ROM già
conosce. E in Gen 3 **non esiste nessuna routine** che disegni un giocatore remoto in una route.
Quindi, per avere l'overworld condiviso, bisogna **eseguire codice nostro dentro il gioco**.
Il codice di Smeraldo gira dalla ROM (non scrivibile), ma il gioco si guida attraverso
**puntatori in RAM** — e quelli sì che si possono riscrivere.

### Il vettore d'ingresso: il multiboot *residente*
Il BIOS del GBA entra in modalità multiboot solo **a slot vuoto**. Il trucco è sfruttare l'ordine
delle cose:

1. slot vuoto → il PC/browser carica via cavo uno **stub** (`hw/mbstub`) in modalità MultiPlay 16 bit
   (lo stesso modo del Cable Club, quindi **stesso cavo e stesso firmware** del gioco);
2. lo stub copia il **payload** in cima alla EWRAM (`0x0203CF80`, un angolo che Smeraldo non usa)
   e aspetta la cartuccia;
3. inserita la cartuccia, lo stub salta dentro `AgbMain` **dopo** l'azzeramento della memoria
   (`0x080003CE`, dopo `InitIntrHandlers`): in Smeraldo retail `crt0` non azzera niente, quindi
   il payload sopravvive;
4. il payload aggancia il **vettore IRQ a `0x03007FFC`** e concatena l'handler originale: da lì
   gira a ogni fotogramma, per sempre, qualunque scena il gioco carichi.

Allo spegnimento non resta nulla: è tutto in RAM.

### Le decisioni che contano
1. **Movement action, non coordinate.** Al personaggio remoto non si scrive x/y (teletrasporterebbe
   di tile in tile): gli si mette in coda `MOVEMENT_ACTION_WALK_NORMAL_DOWN` & co. Il motore regala
   animazione, sub-pixel e timing. È indistinguibile da un NPC vero.
2. **Eventi, non stati.** Non si manda la posizione a ogni frame: si manda «ho iniziato un passo,
   direzione D, dal tile T» (**12 byte**), più una posizione assoluta ogni tanto come correzione.
   100 ms di ping = l'amico mezzo passo indietro, invisibile.
3. **Agganciare l'IRQ, non il callback di scena.** `gMain.vblankCallback` viene riscritto a ogni cambio
   di scena; il vettore IRQ no.
4. **Il cambio mappa è un evento del protocollo.** Attraversare il bordo di una route non è un warp e
   il gioco aggiorna mappa e coordinate in due momenti diversi: al cambio mappa si riallinea e si
   manda una posizione assoluta, mai un «passo» (un passo è un tile, per definizione).
5. **Dall'IRQ non si tocca il gioco.** L'IRQ fa solo I/O seriale; tutto ciò che crea o muove oggetti
   gira dal main loop (`gMain.callback1`), dove il gioco se lo aspetta.
6. **Ridondanza contro la perdita.** Il tratto USB→SIO perde qualche frame e nessuno lo ricuce: ogni
   PASSO/GIRA esce due volte verso il gioco e il payload scarta la copia sul numero di sequenza.

### Niente da reimplementare: le decomp
[pokeemerald](https://github.com/pret/pokeemerald) è una decompilazione **combaciante**: dal `.map`
della build si conosce l'indirizzo di ogni funzione. Il payload chiama direttamente le routine
originali per creare, muovere e animare gli object event, e riusa gli sprite già in ROM.
Gli indirizzi della ROM **italiana** sono stati ritrovati per firma (`tools/port_syms.py`, 42/42).

### La catena
```
GBA ─cavo link─ Pico (Celio F-1…F-4) ─USB─ browser (WebUSB) o client.py
    ─WebSocket 443 / UDP─ relay ─ … ─ e al contrario fino al GBA dell'amico
```
- **Il relay non capisce niente**: fa stanze e consegna datagrammi. La «stanza» è la partita, non la mappa.
- **Solo `net/client.py` e `web/js/bridge.js` conoscono il formato dei 12 byte** (slot del mittente
  nel nibble alto del tipo): dedup, riordino, ricucitura, copie ×2.
- **Il payload parla via mailbox** in RAM: in emulatore la mailbox la legge il Lua di mGBA, sul
  fisico il driver SIO (`payload/sio.c`) in modalità Multi-Player 16 bit.

### Il firmware del Pico
Celio esteso con un modo **passthrough** (`0x04`) e quattro modifiche (patch in `hw/firmware/`):
**F-1** timing di scambio configurabile a runtime, **F-2** niente filtro sulle parole `0x0000`
(più back-pressure su `sendData`), **F-3** `SetCableType`, **F-4** riavvio software
(`0x43 0xA5`) usato a fine multiboot. Periodo di scambio misurato: ~4 ms → **226 parole/s**.

### Lo spazio: ~11 KB, contati a byte
Il payload vive in ~11,5 KB di EWRAM, stack IRQ compreso. Per farci stare 4 giocatori e il driver
seriale: `-Os` su `main.c`, flag GCC scelti con una **ricerca greedy bidirezionale** rifatta dopo
ogni modifica seria, helper `noinline` per pagare una volta sola i literal pool di Thumb-1, e
guardie in `build.ps1` che **falliscono la build** se il margine, lo stack IRQ statico o il contratto
C↔Lua (`PayloadState` ↔ tabella `S`) non tornano.

---

## Architettura del repository

| Cartella | Cosa |
|---|---|
| `payload/` | il codice che gira **dentro** Smeraldo: hook IRQ (`hook.S`), logica (`main.c`), driver SIO (`sio.c`), simboli della ROM (`game_syms*.h`, generati) |
| `hw/mbstub/` | lo stub multiboot che carica il payload e salta nella cartuccia |
| `hw/firmware/` | patch del firmware Celio (F-1…F-4) e istruzioni per ricompilarlo |
| `hw/gbalink-fw/` | firmware link alternativo per RP2040 (GPL-3.0) |
| `hw/siotest/` | banco di prova SIO su hardware (usa lo stesso `sio.c` del payload) |
| `mgba/` | lato emulatore: `inject_body.lua` (iniettore + rete), `club_lua.lua`, libreria WebSocket, autopilota per le prove automatiche |
| `net/` | lato PC: `client.py`, `relay.py`, `relay_ws.py`, `mb_multi.py` (multiboot), `usb_link.py`, `club_link.py` (Cable Club via internet), `pannello.py` + `pannello.html`, `mappa.html`, test |
| `web/` | il sito: pannello nel browser (WebUSB + multiboot + relay WebSocket + Cable Club), spiegazione, pagine di test |
| `tools/` | generatori (simboli, mappa, nomi italiani dalla ROM), preparazione pacchetti e sito, simulatore, test |
| `build.ps1` | la build del payload e degli script Lua, con tutte le guardie |

---

## Compilare da sorgente

Strumenti: **Windows + PowerShell 5.1**, [Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads)
(`arm-none-eabi-gcc` sul PATH), **Python 3.11+**. Per rigenerare i simboli o la Mappa live serve
anche una build di [pokeemerald](https://github.com/pret/pokeemerald) (devkitARM, in WSL:
`tools/build-pokeemerald.sh`).

```powershell
.\build.ps1 -Syms it -WithSio          # payload per GBA vero (ROM italiana + driver SIO)
.\hw\mbstub\build.ps1                  # stub multiboot: ingloba il payload APPENA costruito
.\build.ps1 -Syms usa -WithSio         # payload per la cartuccia inglese (USA/Europa)...
.\hw\mbstub\build.ps1 -Syms usa        # ...e il suo stub, mbstub-usa.gba (rifiuta un payload della versione sbagliata)
.\build.ps1 -LinkRole relay -Syms it -OutName emulatore   # script per mGBA, ROM italiana
.\build.ps1 -LinkRole relay -Syms usa -OutName emulatore-usa   # script per mGBA, ROM inglese (USA/Europa)
python tools\gen_mappa.py              # dati della Mappa live (dalla decomp + nomi IT dalla ROM)
.\tools\prepara-sito-web.ps1 -Relay wss://tuo.host/ws -Stanza 0     # assembla il sito
```

L'ordine conta: lo stub ingloba l'ultimo `payload.bin`. Ogni build stampa il **margine EWRAM**,
lo **stack** statico dell'IRQ e il **contratto** C↔Lua: se uno dei tre non torna, la build si ferma.

I file generati (`mgba/inject.*.lua`, `payload/game_syms*.h`, `mbstub.gba`) **non si modificano a mano**.

---

## Test

Unittest in sola libreria standard, si lanciano singolarmente:

```bash
python net/test_tre_giocatori.py
```

Gli altri: `net/test_deframer.py`, `test_bridge_tcp.py`, `test_relay_rebind.py`, `test_club.py`,
`test_pannello.py`, `test_relay_ws.py`, `test_slot.py`, `test_relay_cap.py`, `test_client_ws.py`,
`test_spettatore.py`, `test_presenza.py`, e in `tools/` `test_ruolo_relay.py`, `test_club_lua.py`
(richiedono `pip install lupa`). Il sito ha i suoi autotest nel browser: `web/test.html`,
`web/mb_test.html`, `web/bridge_test.html`.

Prova end-to-end in emulatore, senza mani: `.\tools\prova-in-tre.ps1 -Rom <smeraldo-ita.gba> -Giocatori 3`
avvia tre mGBA pilotati da `mgba/autopilota.lua`, li fa camminare e **fotografa** ogni schermo
accanto ai contatori del payload.

Audio in lotta sulla build da GBA vero: dopo `.\build.ps1 -Syms it -WithSio` e `.\hw\mbstub\build.ps1`,
`mgba/banco_audio.lua` carica il payload con lo stesso ingresso in `AgbMain` dello stub multiboot
(`mgba/handoff_test.lua`), fa N lotte vere e controlla a ogni frame che la maschera degli interrupt del
gioco (`sRegIE`) non venga mai ridotta in `REG_IE` - la causa della musica rotta in lotta corretta il
2026-08-30. Parametri e criteri in testa al file.

Animazione delle MN fuori lotta: `.\tools\prova-in-tre.ps1 -Rom <smeraldo-ita.gba> -Giocatori 2 -Banco <...>\mgba\banco_mn.lua`
fa saltare sul posto l'avatar dell'amico 20 volte e controlla a ogni frame che `gFieldEffectArguments`
(dove la MN tiene il posto in squadra del Pokémon che la usa) non venga mai sovrascritta per conto
dell'amico - la causa del Pokémon buggato / «in negativo» / MissingNo nell'animazione delle MN,
corretta il 2026-09-26. Verdetto in `build\prova-in-tre\banco_mn.txt`.

---

## Ospitare il proprio relay

Il relay è Python puro (`net/relay.py`, UDP) più un frontale WebSocket (`net/relay_ws.py`) per i
browser. Guida completa per una VPS con nginx/Caddy e HTTPS: [`net/RELAY-VPS.it.md`](net/RELAY-VPS.it.md);
blocco nginx pronto: [`net/nginx-gen3pm.conf`](net/nginx-gen3pm.conf).

```bash
python net/relay.py --port 9000
```
```bash
python net/relay_ws.py --port 9001 --relay 127.0.0.1:9000
```

Massimo 4 giocatori per stanza (il quinto viene rifiutato); gli spettatori non contano.

---

## Limiti noti

- **Cartuccia inglese su GBA vero: nuova, non ancora provata sul fisico.** `mbstub-usa.gba` differisce dallo
  stub italiano (provato sul fisico) solo in cinque costanti (codice del gioco + quattro firme della ROM, tutte
  misurate sulla ROM inglese); il codice di avvio è identico byte per byte. In emulatore il payload inglese
  passa il banco di handoff completo, e partite italiane e inglesi si vedono sul relay pubblico. Manca una
  prova su un GBA vero con cartuccia inglese: se ne hai una, segnalacelo negli Issues. Uno stub sbagliato non
  avvia mai il gioco a metà: resta rosso (codice del gioco sbagliato) o diventa blu (corpo della ROM diverso).
  Rosso Fuoco/Verde Foglia sono il prossimo passo naturale (anche `pokefirered` è decompilato).
- **16 object event per mappa**: su una mappa affollata ci stanno 2-3 amici. Il quarto amico in su
  non ha avatar ma resta sulla Mappa live.
- **Mappe non adiacenti**: l'amico si vede sulla tua mappa e nella striscia della mappa connessa
  che il gioco tiene caricata (~7 tile). Oltre, non esiste terreno da disegnare.
- **L'amico blocca la vista degli allenatori**: nel gioco la vista *è* una collisione. Scelta
  consapevole; col d-pad ci passi attraverso, così non ti blocca nelle porte.
- **Firefox e Safari** non hanno WebUSB: da lì si può solo guardare (spettatore).
- **L'emulatore non entra nel Cable Club** (mGBA non fa da partner del link da script). Scambi e
  lotte funzionano fra GBA veri.
- Latenza: sotto ~50 ms di ping è perfetto; oltre si vede un po' di rincorsa.

---

## Crediti e fonti

### Chi ha scritto il codice
**La fonte principale di questo progetto è [Claude](https://www.anthropic.com/claude), di Anthropic.**
Payload in C/Thumb e assembly, stub multiboot, estensioni del firmware del Pico, driver SIO, protocollo
di rete, client, relay, frontale WebSocket, porting del multiboot e del Cable Club in JavaScript,
pannelli, Mappa live, strumenti di analisi della ROM e test: tutto il software di questo repository è
stato scritto da **Claude (Claude Code)**, in decine di sessioni di lavoro guidate da Lain.

**Lain** ha avuto l'idea, messo l'hardware, fissato i vincoli («nessun rituale nel gioco, mai»,
«cartuccia italiana», «non si tocca la ROM») e fatto **tutte le prove sul campo** — con
**[Hacke](https://github.com/SickPick97)** dall'altra parte del relay, il primo amico ad averlo provato via internet. Il metodo di lavoro è stato: codice → Lain prova sul fisico → misura →
correzione, con ogni modifica accompagnata da una procedura di test e un contatore che deve salire.

### Su cosa si appoggia
- **[pret](https://github.com/pret)** — le decompilazioni combacianti `pokeemerald` e `pokefirered`:
  la mappa degli indirizzi senza cui niente di questo sarebbe stato possibile. Anche i dati della Mappa live vengono da lì.
- **Progetto Celio** — il firmware RP2040 dell'adattatore USB-GBA, base di tutto lo strato hardware.
- **smashstacking** — il progetto dell'adattatore GBA-USB su cui Celio è tarato.
- **agtbaskara** — [game-boy-pico-link-board](https://github.com/agtbaskara/game-boy-pico-link-board).
- **weimanc** — game-boy-zero-link-board.
- **endrift e il progetto [mGBA](https://mgba.io/)** — emulatore, debugger e scripting Lua usati per tutto lo sviluppo.
- **Martin Korth** — [GBATEK](https://problemkaputt.de/gbatek.htm), la documentazione del GBA (SIO, multiboot, timing).
- **Raspberry Pi**, **Zephyr RTOS**, **devkitPro**, **Arm GNU Toolchain**.
- **La comunità Celio-Link su GBAtemp**, che ha sciolto per prima lo strato hardware più ostico.
- **Pokémon Showdown** e **Pokémon Database** — gli sprite animati di Nero/Bianco 2 della Mappa live (scaricati a parte da `tools/sprite_bw.py`, non inclusi qui).

---

## Note legali

Pokémon, Game Boy Advance e i nomi correlati sono marchi di Nintendo, Creatures Inc. e GAME FREAK inc.
Questo è un progetto amatoriale senza fini di lucro, **non affiliato né approvato** da loro.
Il repository **non contiene** ROM né salvataggi: serve la propria cartuccia originale. Contiene solo quattro piccole
icone derivate dalla grafica del gioco, elencate con tutto il resto in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.it.md).

Codice rilasciato sotto **GPL-3.0** (vedi [`LICENSE`](LICENSE)), come i progetti Celio da cui deriva in parte.
Il firmware `celio.uf2` delle Releases è Celio-Firmware (GPL-3.0) al commit `f67733c` più la patch in
`hw/firmware/`: quello è il suo sorgente completo. Componenti di terze parti e licenze: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.it.md).
