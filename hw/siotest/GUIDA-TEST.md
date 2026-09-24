# Guida al banco di prova SIO — passo per passo

Serve a misurare, **su hardware vero**, quello che l'emulatore non può dire: quante parole al
secondo passano davvero sul cavo, se il framing regge, ogni quanto l'adattatore fa uno scambio.

> **Aggiornamento 2026-08-02**: il banco è diventato il **NODO GIOCATORE** — parla il
> protocollo NetEvent vero, adotta la mappa dal primo evento remoto e cammina in quadrato.
> La scaletta dei test di questa fase (T-1…T-5, con i criteri) sta in **NOTES.md, «Stato
> attuale»**: questa guida resta il *come si fa* di ogni passo (cavi, firmware, comandi).
> I passi 5-6 e M-5..M-7, «mai provati» nella prima stesura, **sono stati provati e passati**
> il 2026-08-01/02.

> **Aggiornamento 2026-08-02 (sedicesima sessione) — questa guida descrive la strada VECCHIA,
> a due cavi e due firmware.** Il multiboot non ha bisogno della SIO Normal 32 bit: `SWI 25h`
> ha un transfer mode `1 = 115 KHz, 16 bit, MultiPlay`, che è lo stesso modo di Celio e passa
> dal **cavo GBA**. Quindi oggi la strada da provare per prima è, con `celio-f1f2b-f3.uf2` sul
> Pico e il **solo cavo GBA**:
>
> ```
> cd D:\Progettini\GBA-USB\overworld-link\net
> python mb_multi.py ..\hw\siotest\build\siotest.gba
> ```
>
> Procedura completa U-0…U-4 in NOTES.md, «Stato attuale». **Se non aggancia**, tutto ciò che
> segue in questa guida resta il ripiego valido e misurato.

**Non c'è nessun rischio per il salvataggio**: questo programma gira *al posto* di Smeraldo e
pretende lo **slot cartuccia vuoto**. La cartuccia non va nemmeno inserita.

Tempo: circa mezz'ora la prima volta.

---

## 0. Cosa ti serve

| Cosa | Note |
|---|---|
| GBA SP | connettore link standard, va bene |
| **Cavo DMG/GBC** | **per il multiboot** — è quello che usavi per gli scambi con l'homebrew di lorenzooone |
| Cavo link GBA | **per la fase Celio** (passi 5-8) — quello che usi con Celio |
| Pico / board | una sola basta |
| UF2 **del multiboot** | **`%USERPROFILE%\Desktop\Trading GBA\gbusb.uf2`** (sha1 `c200792a…`, l'originale di lorenzooone) **con `mbsend.py --legacy`** — è la combinazione provata con *questo* `.gba`. **Correzione 2026-08-02**: la riga qui diceva che `gbalink-mb.uf2` «ha un difetto SPI ed è solo diagnostico», e non è più vero — con quello il blocco W′ ha multibootato `mbstub.gba` sull'hardware (T2 passato), usando `mbsend.py` **senza** `--legacy`. Il difetto SPI era del `gbusb.uf2` **ricompilato** in `trades\` (sha1 `b11c9bd4…`), non di `gbalink-mb`. Se una delle due strade non aggancia, l'altra è il ripiego documentato |
| UF2 **Celio** (per il test) | stanno in **`overworld-link\hw\firmware\`** (vedi il README lì): **`celio-f1f2b.uf2` è IL firmware** — quello della prova ufficiale passata. Gli altri servono solo a rifare i passi intermedi: `celio-f1f2.uf2` e `celio-f2.uf2` hanno la direzione GBA→PC rotta (nessun frame valido), `celio-0b-baseline.uf2` è l'upstream puro |
| Python con `pyusb` **o** `pyserial` | `pip install pyusb pyserial` |

### Perché DUE cavi (misurato il 2026-08-01, dopo tre giri a vuoto)

Il multiboot parla SIO **Normal 32-bit**, che viaggia sulla coppia incrociata `SO`↔`SI`. Il
cavo **GBA** che usi con Celio è un cavo di tipo *multiplayer*: un capo tiene `SI` a massa
(misurato: 0/0 in un verso, 1/1 nell'altro) e i dati di gioco passano su `SD` — per questo
Celio ci funziona. Ma la coppia `SO`↔`SI` **non ha percorso di ritorno**: 42 combinazioni di
pin e clock provate, e la linea di ingresso non si è mossa mai. Il multiboot su quel cavo non
può funzionare, **da nessun verso e con nessun firmware**.

Lo dice anche il README dell'homebrew con cui il multiboot ti ha sempre funzionato
(`Pokemon-Gen3-to-Gen-X`, riga 10): *«All communications need the DMG/GB/GBC Link Cable. The
software is not compatible with the GBA Link Cable»*.

Quindi: **multiboot col cavo DMG/GBC** (come hai sempre fatto, anche senza saperlo), poi per la
fase Celio **si cambia cavo** — il programma a quel punto è già in RAM del GBA e il cavo si può
scambiare a console accesa senza problemi.

### Due cose da controllare *prima* di collegare qualsiasi cosa

⚠️ **`SW1` (l'interruttore/jumper sulla board) deve stare sulla posizione GBA — 3,3 V, anche
se il cavo è quello DMG/GBC.** Quell'interruttore non sceglie il connettore: sceglie la
**tensione di logica** del level shifter, e i connettori sono in parallelo. Dall'altra parte
del cavo c'è comunque un GBA, che lavora a 3,3 V: sulla posizione DMG/GBC la board
piloterebbe le sue linee a **5 V**, ed è il modo in cui si rompe un GBA.

⚠️ **Lo slot cartuccia del GBA deve essere VUOTO.** Il BIOS entra in modalità multiboot solo a
slot vuoto: con una cartuccia valida inserita avvia quella e ignora il cavo.

Sul connettore invece puoi stare tranquillo: sullo schematico della board `J1 (GBA_Link)` e
`J2 (DMG_Link)` sono **in parallelo sugli stessi quattro segnali**. Usa quello in cui il tuo
cavo entra.

---

## 1. Costruire il programma di prova (PC, 5 secondi)

```
D:\Progettini\GBA-USB\overworld-link\hw\siotest\build.ps1
```

**Deve dire:**

```
siotest   : 2592 byte
header    : branch ok, logo copiato, checksum 0x06
```

Se dice `TROPPO GRANDE` o `i primi 4 byte non sono un branch ARM`, fermati e riporta: è un
difetto della build, non tuo.

File prodotto: `hw\siotest\build\siotest.gba`

---

## 2. Il firmware del multiboot (Pico)

Il multiboot usa SIO **Normal 32-bit**, che **Celio non implementa** — verificato: in
`Celio-Firmware/src/control.hpp` le modalità sono solo `gbaTradeEmu`, `gbaLink`, `gbLink`,
`gbPrinter`, `gbaPassthrough`, e di multiboot non c'è traccia nel sorgente. Serve un firmware
diverso, che poi al passo 5 verrà sostituito da Celio.

Il firmware giusto è **l'originale di lorenzooone**, che hai già:

```
%USERPROFILE%\Desktop\Trading GBA\gbusb.uf2
```

(sha1 `c200792a…` — verificato il 2026-08-01: handshake in 0,7 s, 2016 byte in 0,03 s.
Il `gbalink-mb.uf2` di `hw\gbalink-fw` **non va usato per caricare**: ha un difetto SPI, serve
solo per la diagnostica dei livelli con `--diag`. Lo script comunque ti dice quale firmware
vede: la riga `firmware sul Pico:` deve dire `originale`.)

**Flashalo:**

1. Scollega il Pico
2. Tieni premuto **BOOTSEL** e ricollegalo: compare il disco **`RPI-RP2`**
3. Copiaci sopra `gbusb.uf2` dal Desktop
4. Il disco sparisce da solo: è flashato

### Perché non i due `gbusb.uf2` che hai già sul disco

Sono due file **diversi**, e la differenza conta:

| File | sha1 | Cosa |
|---|---|---|
| `%USERPROFILE%\Desktop\Trading GBA\gbusb.uf2` | `c200792a…` | ottobre 2025, l'originale — probabilmente quello con cui il multiboot ti aveva funzionato |
| `D:\Progettini\trades\…\build\gbusb.uf2` | `b11c9bd4…` | gennaio 2026, **ricompilato con una patch** che sposta l'ingresso dati su GP3 (`SD`) |

In SIO Normal 32-bit il GBA risponde sul proprio `SO`, e su `SD` quella risposta non passa mai:
con la seconda build l'handshake **non può** tornare. La guida precedente ti mandava a flashare
proprio quella — errore mio.

Il firmware nuovo non sceglie: accetta i pin dal PC e li fa provare tutti (passo 4).

⚠️ Anche `pico-gba-link-bridge` ha lo **stesso VID/PID `CAFE:4011`** ma non fa multiboot. Se ce
l'hai flashato, il passo 4 fallisce con un messaggio esplicito.

---

## 3. Collegare e accendere (GBA)

**In quest'ordine, e conta:**

1. Verifica **`SW1` su GBA (3,3 V)** — sì, anche col cavo DMG/GBC: vedi il punto 0
2. GBA **spento**, **slot cartuccia vuoto**
3. Collega il **cavo DMG/GBC**: un capo nel connettore **DMG** della board, l'altro nel GBA
   (entra nello stesso identico connettore del GBA SP — è così che facevi gli scambi)
4. **Accendi il GBA**

Il **cavo GBA resta nel cassetto fino al passo 5**: per il multiboot non funziona — non è
un'opinione, sono le 42 prove del 2026-08-01 più la riga 10 del README di lorenzooone.

**Deve restare fermo sulla schermata `GAME BOY`, e non deve succedere altro.** È lo stato
giusto: a slot vuoto il BIOS entra in **modalità multiboot slave** e aspetta che qualcuno gli
mandi un programma dal cavo. Non parte da solo — tocca al PC, al passo 4.

Se invece parte un gioco, la cartuccia è inserita: spegni, toglila, riaccendi.

---

## 4. Misurare le linee e caricare (PC)

```
python D:\Progettini\GBA-USB\overworld-link\hw\siotest\mbsend.py D:\Progettini\GBA-USB\overworld-link\hw\siotest\build\siotest.gba --legacy
```

`--legacy` è il percorso ufficiale: puro protocollo di lorenzooone sul firmware originale, con
in più il controllo di quale firmware c'è sul Pico e la sonda dell'handshake che dice dopo
quanti secondi aggancia (atteso: sotto il secondo).

**Deve dire, in quest'ordine:**

```
file         : ...siotest.gba (2008 byte)
multiboot.py : %USERPROFILE%\Desktop\PokemonGB_Online_Trades-main\multiboot.py
trasporto    : USB vendor (pyusb)        <- oppure "porta : COMx"
configuro il firmware...
  firmware riconosciuto

livelli delle linee:
  pin  linea  pull-up  pull-down  stato
  GP0     SC        1          0  libera (nessuno la pilota)
  ...

cerco i pin (il GBA deve essere acceso, slot vuoto, schermata GAME BOY)
  SC=0 SO=2 SI=1 -> RISPONDE 0x7202
pin trovati  : SC=0 SO=2 SI=1   <- annota questa riga in NOTES.md

invio...
Handshake successful!
DONE!
fatto
```

### Come si legge la tabella dei livelli

Ogni linea viene letta due volte, una col pull-up e una col pull-down. Le due letture insieme
dicono chi la sta tenendo:

| pull-up | pull-down | Significa |
|---|---|---|
| 1 | 0 | **libera** — nessuno la pilota (cavo scollegato, o pin non usato) |
| 0 | 0 | **tenuta a massa** — dal cavo, o da chi sta dall'altra parte |
| 1 | 1 | **tenuta alta** |

È la misura che chiude una domanda rimasta aperta per tutta la sessione: se il cavo GBA
cortocircuiti davvero `SI` a massa, come sostiene la rilevazione del cavo dentro Celio
(`linkLayer_pio.c:87-91`). **Riportamela comunque, anche se il resto funziona**: vale per il
firmware di Fase 5, non solo per questo test.

### Se nessuna combinazione risponde

Lo script prova, da solo e in quest'ordine: le 6 combinazioni plausibili, poi **tutte le 24
permutazioni su GP0..GP3**, poi di nuovo le 6 plausibili a **clock via via più lento**
(~250 kHz, ~62 kHz, ~15 kHz). **42 prove**, circa un minuto.

Il clock c'entra perché il valore dell'upstream è ~1 MHz e il segnale passa da un **BOB-12009**,
uno shifter a MOSFET con pull-up da 10 kΩ: sopra qualche centinaio di kHz i fronti di salita si
arrotondano. «Pin sbagliati» e «troppo veloce» danno lo stesso sintomo, e così si separano.

Se non risponde nessuna delle 42, allora **né i pin né il clock sono la causa** — e a quel
punto servono le due prove qui sotto, in quest'ordine.

#### Prova L — chi tiene `SI` a massa: il cavo o il GBA?

Nella misura del 2026-08-01 `GP1 (SI)` risultava **tenuto a massa**, mentre `SC`, `SO` e `SD`
erano alti. Sapere *chi* lo tiene giù decide la strada, e si scopre con tre `--diag`, un minuto
in tutto:

```
python D:\Progettini\GBA-USB\overworld-link\hw\siotest\mbsend.py --diag
```

| # | Come stai | Guarda `GP1` |
|---|---|---|
| **L-1** | cavo link **scollegato dalla board** | dovrebbe risultare **alto** come gli altri |
| **L-2** | cavo nella board, **l'altro capo per aria** | se è già a massa → **è il cavo** |
| **L-3** | cavo in entrambi, **GBA spento** | se passa a massa solo qui → è il **GBA** |

**Perché conta**: se a mettere `SI` a massa è il cavo, la board sta tenendo il connettore
"parent" di un cavo multiplayer GBA — e su quel lato la SIO Normal 32 bit **non ha percorso di
ritorno**. Il multiboot con quel cavo sarebbe impossibile per costruzione, e quello che ti
funzionava in passato passava da un cavo DMG/GBC, che è un incrocio `SO`↔`SI` semplice.
Se invece è il GBA che tiene basso il proprio `SO` da fermo, il cavo va bene e il problema è
altrove.

#### Prova C — il controllo: il multiboot funziona ancora, di per sé?

Serve a separare «ho rotto qualcosa io» da «non funziona comunque». Flasha il `gbusb.uf2`
**originale** — `%USERPROFILE%\Desktop\Trading GBA\gbusb.uf2`, sha1 `c200792a…` — e lancia:

```
python D:\Progettini\GBA-USB\overworld-link\hw\siotest\mbsend.py D:\Progettini\GBA-USB\overworld-link\hw\siotest\build\siotest.gba --legacy
```

`--legacy` non manda **nessun** comando mio: è esattamente il protocollo di `usb_trading.py`.

| Esito | Cosa significa |
|---|---|
| `Handshake successful!` poi `DONE!` | il multiboot va: il difetto è nel firmware o nei pin che ho scelto io |
| `Failed handshake!` | non è colpa mia: il problema è il cavo, il connettore o lo stato del GBA |

**Criterio del passo**: sul GBA compare una schermata scura con una **barra in alto** e nove
righe di numeri. Da qui in poi il GBA **non va più spento**.

---

## 5. Passare a Celio (Pico), col GBA acceso

Il programma è ormai in RAM: il Pico può essere riflashato sotto, e il cavo cambiato.

1. **Non spegnere il GBA**
2. Scollega il **cavo DMG/GBC** dal GBA e dalla board, e collega il **cavo GBA** (quello di
   Celio) **nel verso marcato col nastro** — la rilevazione del cavo avviene al SetMode, e
   nel verso sbagliato il master legge solo `0xffff` con l'errore SIOCNT su ogni parola.
   Cambiare cavo a console accesa è normale
3. Scollega il Pico dal PC, BOOTSEL + ricollega → disco `RPI-RP2`
4. Copiaci **`celio-f1f2b.uf2`** (da `overworld-link\hw\firmware\`). Gli altri UF2 lì
   dentro servono solo a ricostruire i passi intermedi: `f1f2` e `f2` hanno la direzione
   GBA→PC rotta, ed è un difetto del firmware, non del tuo cavo

Il cavo GBA qui va benissimo, ed è anzi quello giusto: la fase Celio parla **Multi-Player
16 bit**, i cui dati viaggiano su `SD` — che su quel cavo è cablato (è il motivo per cui Celio
ci ha sempre funzionato).

**Criterio**: durante e dopo il riflash la schermata del GBA **resta lì e non si pianta**. La
barra in alto diventa rossa (nessun dato) e va bene: è il canale che tace.

Questo è il punto più incerto della procedura, perché è ragionato e mai provato. Se il GBA si
pianta, fermati e riportalo: significa che il programma non tollera il silenzio, ed è una cosa
da correggere nel codice.

---

## 6. Svegliare Celio (PC)

**Senza questo passo la barra resta rossa per sempre**, e non è un difetto: Celio all'accensione
aspetta un comando USB che gli dica cosa fare, e finché non lo riceve il cavo non trasporta
niente.

```
python D:\Progettini\GBA-USB\overworld-link\hw\siotest\celiomode.py
```

**Deve dire:**

```
device       : 2fe3:000a
modalita'    : raw relay (gbaPassthrough 0x04)
ruolo        : adattatore MASTER, GBA child
```

e poi restare in ascolto stampando lo stato. Se il GBA è acceso e collegato deve comparire
**`GameboyConnected`**.

Se dice `nessun device 2fe3:000a`, sul Pico c'è ancora il firmware del multiboot: il passo 5
non è andato a buon fine.

**Criterio del passo**: sul GBA la barra in alto diventa **verde**.

---

## 7. Leggere i numeri (schermo del NODO GIOCATORE, dal 2026-08-02)

Le righe hanno l'etichetta in esadecimale a sinistra e il valore a destra. **Il layout è
cambiato rispetto alle foto di ieri** — 11 righe:

| Riga | Cosa | Cosa vuoi vedere |
|---|---|---|
| **0** | parole al secondo | **> 0 e stabile** (≈ `0x7E` a timing default; scala con `--timing`) |
| **1** | interrupt seriali al secondo | uguale alla riga 0 |
| **2** | frame ricevuti interi | sale quando arrivano messaggi interi |
| **3** | **errori di frame** | **0** — rosso se sale |
| **4** | resync | sale solo quando si perde e ritrova l'aggancio |
| **5** | `zeroWords` | **NON è più rossa**: dopo F-2 gli zeri sono dati legittimi. Col nodo adottato che cammina **DEVE salire** — ferma a 0 sarebbe lei il difetto |
| **6** | errori di `SIOCNT` | **0** — rosso se sale |
| **7** | frame trasmessi | sale coi passi, i SYNC e i ping |
| **8** | eventi remoti ricevuti | sale coi passi del giocatore mGBA |
| **9** | remoto: x, y, direzione | **cambia a ogni passo del giocatore mGBA** |
| **A** | noi (blu): x, y, mappa adottata | si popola all'adozione |

**La barra in alto**: verde = nell'ultimo secondo sono arrivati dati, rossa = silenzio.
**Il tratto blu in fondo a destra della barra** = adozione avvenuta (il primo evento remoto
ci ha detto su quale mappa camminare).

---

## 8. Le prove

**La scaletta con i criteri, aggiornata, è T-1…T-5 in NOTES.md, «Stato attuale».** In breve:
T-1 multiboot del nodo; T-2 `celio-f2.uf2` + `celiomode.py --ping --zero-in-payload`
(accettazione F-2); T-3 `celio-f1f2.uf2` + sweep `--timing`; T-4
`net\usb_link.py --selftest 20`; T-5 la catena completa con relay, due client e mGBA.

---

## 9. Cosa riportarmi

Anche solo una foto dello schermo per ogni test va benissimo. Se preferisci scrivere:

- riga **0** (parole/s) per ogni valore di `--timing` provato
- riga **3** (errori di frame) e riga **6** (errori SIOCNT) — devono restare 0
- riga **5** (`zeroWords`): deve SALIRE durante la camminata
- righe **8**, **9**, **A** durante la prova ufficiale
- il riassunto stampato da `usb_link.py --selftest`
- se il tratto blu si è acceso, e se in mGBA hai visto l'allenatore camminare in quadrato

---

## Se qualcosa non va

| Sintomo | Probabile causa |
|---|---|
| `Bytes written mismatch 36 vs 0`, o `risposta 0` da un firmware che prima era riconosciuto | l'endpoint USB del Pico si è inceppato (succede dopo una sessione lunga): **scollega e ricollega il Pico**, poi rilancia |
| il GBA non resta su `GAME BOY` ma parte un gioco | cartuccia inserita: spegni, toglila, riaccendi |
| `firmware non riconosciuto` | è flashato Celio o `pico-gba-link-bridge`, non `gbalink-mb.uf2` |
| `nessun device CAFE:4011` | Pico non collegato, o driver mancante: prova `--port COMx` |
| nessuna combinazione di pin risponde | vedi il passo 4: `SW1`, slot vuoto, cavo a fondo |
| tutte le linee «libere» nella tabella | il cavo non sta arrivando alla board |
| barra sempre rossa dopo il passo 5 | manca il passo 6, oppure Celio non è in raw relay |
| il GBA si pianta al riflash | il programma non tollera il silenzio — difetto mio, riportalo |

---

## Cosa in questa guida non è mai stato provato

Aggiornato al 2026-08-02, **dopo la prova ufficiale passata**: multiboot, riflash a caldo,
M-5..M-7, F-1, F-2, il nodo giocatore, il transport USB e **la catena completa fino
all'allenatore che cammina in mGBA** sono tutti verificati su hardware. Restano non provati:

- la **tenuta lunga** della catena completa (oltre qualche minuto) e lo stacco/riattacco del
  cavo *durante* una sessione di rete (M-7 è passato sul banco, non con relay e client attivi);
- il timing sotto **1850 iterazioni** (~1 ms/scambio): terreno nuovo per design del PIO, un
  fallimento lì è il pavimento fisico e non un difetto;
- il caso «il giocatore mGBA **cambia mappa** dopo che il nodo ha adottato la sua»: il nodo
  resta sulla mappa vecchia e sparisce dalla vista. È previsto, non è un bug;
- **due GBA reali** collegati fra loro: serve l'hardware dell'amico.
