# Firmware Celio — le build di questo progetto

Gli UF2 si flashano tenendo **BOOTSEL** premuto mentre si collega il Pico, poi copiando il
file sul disco `RPI-RP2`. Il GBA può restare acceso: il programma vive in RAM e sopravvive
al riflash (M-4, provato il 2026-08-01).

> **Dal 2026-08-02 il Pico si flasha UNA VOLTA SOLA.** Il multiboot non ha più bisogno di
> `gbalink-mb.uf2`: passa in **MultiPlay 16 bit** (GBATEK `SWI 25h`, transfer mode 1), che è
> lo stesso modo del link in gioco e quindi gira su questo firmware e sul **cavo GBA**.
> Lo fa `net/mb_multi.py`. Il vecchio percorso (`gbalink-mb.uf2` + cavo DMG/GBC +
> `hw/siotest/mbsend.py --legacy`, Normal 32 bit) resta documentato come ripiego.

| File | Cosa contiene | Stato |
|---|---|---|
| `celio-f1f2b-f3-f4.uf2` | tutto il precedente + **F-4 (`0x43 0xA5` Reboot via software)** | ✅ 2026-08-02 sera, **provato sul fisico**: `mb_multi.py` lo riavvia da solo a fine caricamento (criterio «Pico sparito e ricomparso dal bus»), poi 160 eventi in 30 s senza mani sul cavo. sha1 `57b48f80…`, 154624 byte, versione firmware **2.0.5**. È il firmware del **pacchetto per l'amico** (`tools\prepara-pacchetto-amico.ps1`, copiato come `celio.uf2`) |
| `celio-f1f2b-f3.uf2` | F-1 + F-2 + back-pressure + **F-3 (`0x15 SetCableType`)** | 2026-08-02, sha1 `b8057d82…`. ✅ provato sul fisico (sedicesima sessione). Superset di `celio-f1f2b.uf2`: col cavo GBA la F-3 non serve (`auto` sceglie già GP3) ma non fa danno |
| `celio-f1f2b.uf2` | **F-1 + F-2 + back-pressure su `sendData`** | ✅ l'ultimo **provato sul fisico** (2026-08-02) — col **cavo GBA**. Va bene anche per il multiboot di `mb_multi.py`: quello non usa la F-3 |
| `celio-f1f2.uf2` | F-1 + F-2, senza back-pressure | ❌ direzione GBA→PC rotta: 0 frame validi |
| `celio-f2.uf2` | solo F-2 | idem, solo per il test di accettazione della F-2 |
| `celio-0b-baseline.uf2` | l'upstream così com'è | **bit-identico** a `celio-low-latency-rpi-pico.uf2` di `Celio-Ready-To-Test` |
| `celio-0a-head.uf2` | HEAD pulito `f67733c` | solo riferimento, mai provato |

`celio-f1f2b.patch` è il diff completo delle modifiche rispetto a `f67733c`, **salvato qui
perché in `repo-studio/Celio-Firmware` sono NON COMMITTATE**: un `git checkout` in quel repo
le cancellerebbe. Per rimetterle:

```
git -C ..\..\..\repo-studio\Celio-Firmware apply celio-f1f2b.patch
```

⚠️ **`celio-f1f2b-f3-f4.patch` (2026-08-02, sera) è la patch AGGIORNATA e sostituisce le
precedenti**: contiene tutto ciò che c'era in `celio-f1f2b-f3.patch` **più la F-4** (comando
hardware `0x43 0xA5` Reboot, `CONFIG_REBOOT=y` in `prj.conf`, versione 2.0.5). Verificata con
`git apply --check` su albero pulito. Applicare quella, non le vecchie.

## Come si ricompila (riprodotto il 2026-08-02)

Lo **Zephyr SDK non è installato**: si usa il toolchain ARM GNU generico via `gnuarmemb`.
`west` sta nel venv del progetto, non nel PATH.

```powershell
$env:ZEPHYR_TOOLCHAIN_VARIANT = "gnuarmemb"
$env:GNUARMEMB_TOOLCHAIN_PATH = "C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\14.3 rel1"
$env:ZEPHYR_BASE = "D:\Progettini\GBA-USB\repo-studio\zephyr"
D:\Progettini\GBA-USB\.venv\Scripts\west.exe build -b rpi_pico `
    -d D:\Progettini\GBA-USB\repo-studio\Celio-Ready-To-Test\firmware\build `
    D:\Progettini\GBA-USB\repo-studio\Celio-Firmware
```

L'UF2 esce in `…\build\zephyr\zephyr.uf2` e va copiato qui col nome della build. Controllo
che le modifiche ci siano davvero nel binario (non basta che compili):

```powershell
& "C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\14.3 rel1\bin\arm-none-eabi-nm.exe" `
    ...\build\zephyr\zephyr.elf | Select-String " link_"
```

`link_setCableType` deve comparire fra i simboli `T`.

Attenzione: la patch include anche le **3 modifiche di terzi** che erano già nel working tree
prima di noi (`usbLinkCommand.cpp`, `usbSection.hpp`, e i contatori di drop in
`rawRelaySection.cpp`) — sono quelle contenute anche nell'UF2 known-good del 07/05.

## Cosa abbiamo cambiato, e perché

- **F-2** (`rawRelaySection.cpp`): tolto il filtro che scartava le parole `0x0000` in TX e
  passati i pacchetti USB a **lunghezza variabile** nei due versi. Con il padding a 64 byte
  il ricevitore non poteva distinguere un riempitivo da uno zero di dati — e negli eventi
  del gioco gli zeri sono ovunque (coordinate, direzioni).
- **F-1** (`rawRelaySection.*`, `module/rawRelay.*`): comando USB `0x14 SetRawTiming`
  `[u32 LE]` per il periodo del master, in **iterazioni PIO da ~540 ns** (non microsecondi,
  malgrado il nome storico `timingUs`). Default 15370 ≈ 8,3 ms, ripristinato a ogni
  `SetMode`. Misurato: periodo reale = timing + ~0,4 ms.
- **F-3** (`linkLayer.h`, `linkLayer_pio.c`, `module/rawRelay.*`): comando USB
  `0x15 SetCableType` `[u8]` — `0` auto, `1` cablaggio GBA (SD su **GP3**), `2` GBC (SD su
  GP4). Il firmware sceglieva da solo leggendo GP1, e **su questa board sbaglia col cavo
  DMG/GBC**: `J1/J2/J3` sono in parallelo su GP0..GP3 e **GP4 non è cablato**, quindi il ramo
  GBC mette SD su un pin inesistente e il canale resta muto *senza un errore da nessuna
  parte*. Lo scavalco è **sticky** (sopravvive a `link_detectCableType`) e va mandato **fra
  SetMode e StartHandshake**: la rilevazione avviene al primo, il programma PIO si carica al
  secondo. Lato host: `usb_link.py --cable gba`, `client.py --usb-cable gba`.
- **Back-pressure** (`usbLayer.hpp`, `control.hpp`): `sendData` aspetta che l'host abbia
  letto il pacchetto precedente prima di preparare il successivo. Senza, il buffer condiviso
  veniva sovrascritto sotto un transfer in volo e ogni frame arrivava corrotto. È lo stesso
  fix che l'upstream aveva già applicato a `sendStatus` (commit `0532e8e`) dimenticando
  questo percorso — il semaforo era perfino già dichiarato e mai usato.
- **F-4** (`control.hpp`, `prj.conf`): comando hardware `0x43` + byte di conferma `0xA5` →
  `sys_reboot(SYS_REBOOT_WARM)`. Il Pico è alimentato da USB, quindi lo scollega/ricollega
  che serviva fra multiboot e sessione di link (endpoint inceppato, NOTES 2026-08-02) era in
  realtà un power cycle: questo comando fa lo stesso reset via software. È nella fascia dei
  comandi hardware (0x40-0x4F), quindi passa **anche a modulo attivo**. Il byte di conferma
  evita che un byte vagante su EP1 riavvii il Pico a metà sessione. Lato host:
  `usb_link.py --riavvia`, oppure automatico a fine `mb_multi.py` (si spegne con
  `--senza-riavvio`).

**Trappola da ricordare**: chi chiama `sendData` *dalla work queue USB* deve passare
`K_NO_WAIT`, altrimenti aspetta una callback che solo quel thread potrebbe eseguire. Oggi
riguarda `callGetFirmwareInfo` in `control.hpp`.
