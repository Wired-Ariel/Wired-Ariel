# `web/` — gen3-poke-multiplayer nel browser (stato al 2026-08-23)

Obiettivo (richiesta di Lain): chi vuole provare **apre una pagina**, scrive
la stanza e fa TUTTO dal browser — multiboot compreso — senza scaricare
nulla. Quello che resta fisicamente fuori dalla portata di qualunque sito:
**il firmware sul Pico** (UF2 drag&drop, una volta) e, su Windows, **il
driver WinUSB** (Zadig, una volta). Tutto il resto ora vive nella pagina.

## Cosa c'e' OGGI

| File | Cosa | Gemello Python | Provato |
|---|---|---|---|
| `index.html` + `js/app.js` | **il pannello gen3-poke-multiplayer** (stessa faccia di `net/pannello.html`): 0 Collega il Pico, 1 Carica il gioco nel GBA, 2 Gioca; schede stato (tuo GBA / amici / canale), impostazioni in localStorage, «Prova il relay», registro con le righe di `client.py` | `pannello.py` + `pannello.html` | nel browser (senza Pico): impostazioni, prova del relay (PONG 2 ms), errori giusti, immagini, niente overflow; **col Pico NO** |
| `js/device.js` | il Pico via WebUSB (apertura misurata, `transferIn` sempre pendente, F-4) | `usb_link.py` | **NO: mai collegato a un Pico** |
| `js/multiboot.js` | il multiboot MultiPlay 16 bit, riga per riga da `mb_multi.py` | `mb_multi.py` | `mb_test.html`: lo stesso autotest, tutto passato |
| `js/sio.js` | framing SYNC/HEAD/DATI/CKSUM, de-framer FSM, decodifica eventi e sonda | `usb_link.py`, `protocol.py` | `test.html` 12/12 |
| `js/relay.js` | header OWL1 (pack/unpack) + `RelayLink`: WebSocket verso il relay, riconnessione automatica | `protocol.py` | `bridge_test.html` |
| `js/bridge.js` | **il porting di `client.py`**: dedup/riordino sul seq, ricucitura in linea retta, copie x2 con secchiello, VIA dal T_BYE, RTT, righe di log identiche | `client.py` | `bridge_test.html`: 73 casi (ricucitura + header + dedup + VIA + spettatore + **club**) **+ 4 INTEGRATI** attraverso `relay_ws.py` + `relay.py` veri |
| `../net/relay_ws.py` | **il frontale WebSocket del relay**: ogni browser diventa una socket UDP verso `relay.py`; stanze, BYE, timeout restano la'. Browser ed emulatori/GBA+client.py nella STESSA stanza | — (stdlib) | `net/test_relay_ws.py`: 8 test, compreso browser↔UDP e il VIA alla chiusura |
| `relay-worker/` | il relay su Cloudflare Workers + Durable Objects (opzione "zero server", mondo browser-only) | `relay.py` | **NO: scritto, mai eseguito** (niente account in sessione) |
| `../tools/prepara-sito-web.ps1` | assembla `build/sito-web/` (pagina + js + img + `mbstub.gba` + `config.js` coi default), con la guardia anti-stub-vecchio | `prepara-pacchetto-amico.ps1` | eseguito: 15 file, 56 KB |

| `mappa.html` + `mappa/` | **la mappa live**: LO STESSO file del pannello Python, non una copia. Prende le posizioni da `api/posizioni` se c'e' un server dietro, e via **BroadcastChannel** dal pannello web se no. `prepara-sito-web.ps1` ci mette anche i ~11 MB di dati generati da `gen_mappa.py` | `pannello.py` (`/mappa.html`, `/api/posizioni`) | provata in locale E in produzione: 518 mappe, 160 porte, 49/49 immagini, 2 giocatori finti disegnati con nomi e stati giusti |

**Il Cable Club c'e' anche dal browser (2026-08-26).** `js/club.js` e' il
porting riga per riga di `club_link.py` (epoche, sequenze, riannunci, giri del
link, watchdog: la stessa macchina a stati, stesso protocollo T_CLUB byte per
byte — verificato contro `protocol.py` in tutte e due le direzioni);
l'orchestrazione sta in `bridge.js` (specchio della parte club di `client.py`:
quarantena, versioni, pompa) e il trasporto in `device.js`
(`clubEnter`/`clubExit`: il Pico in modo LINK via WebUSB, con la transferIn
sempre pendente per endpoint — il motivo per cui il doppio thread di
`usb_link.py` qui non serve). Differenza sostanziale, I RUOLI: peer 1 =
master e peer 2 = slave come in Python, ma fra due peer sorteggiati vince il
piu' basso, e il peer dell'amico si legge dall'header del primo T_CLUB (il
comando di ruolo al device aspetta quel pacchetto). Il pannello Python
riconosce il client web dall'impronta riservata `IMPRONTA_WEB`
(`protocol.py`) e dice «l'amico gioca dal browser» invece di urlare al
pacchetto vecchio. Test: `bridge_test.html` 73/73 (i 25 casi di
`net/test_club.py` portati + ruoli sorteggiati + orchestrazione), piu' la
sessione end-to-end browser↔browser sopra `relay_ws.py`+`relay.py` veri e la
MISTA browser↔`finto_club.py` (il vero `ClubSession` Python): scala completa,
7 comandi Gen-3 in ordine, dedup dei rilanci. **NON ancora provato col Pico e
i GBA veri.** Durante uno scambio la scheda va tenuta in PRIMO PIANO: in
background il browser strozza i timer (rilanci e riannunci), anche se il
percorso dati e' a eventi e sopravvive. Manca ancora: il simulatore di rete.

Aggiunte del 2026-08-25 (riscontri di Lain dopo la prova in produzione):
`progetto.html` (come funziona il progetto + crediti completi, linkata dal
footer), guide sotto OGNI campo delle impostazioni, errori WebUSB tradotti con
il rimedio (chooser vuoto/Zadig/interfaccia occupata), registro con **buffer
completo** e bottone «Scarica il log completo», e le **posizioni per la Mappa
live pubblicate a ogni evento di rete** (bridge `onPos`) invece che dal
`setInterval` — che in una scheda in background il browser rallenta fino a un
tick al minuto ed era il «movimento che si blocca» visto sul sito.

## Partita MISTA (GBA + emulatori + browser)

Funziona gia', non serve niente di nuovo: **stesso relay, stessa stanza,
numeri di peer tutti diversi**. In pratica:

- **browser + Pico + GBA** → questa pagina (peer sorteggiato a caso, va bene);
- **pannello Python + Pico + GBA** → relay `host:9000` (UDP), stessa stanza; in
  tre o piu' compilare il campo **peer** nelle impostazioni (0 = automatico
  ospite→1/amico→2, che in tre collide);
- **emulatore mGBA** → `client.py --transport tcp` + Lua (pacchetto emulatore),
  `--room` uguale e `--peer-id` diverso.

L'unico vincolo di infrastruttura: il server deve esporre SIA la porta UDP di
`relay.py` (per Python/mGBA) SIA il `wss://` di `relay_ws.py` (per i browser).
La macchina Oracle li ha entrambi. Il Worker Cloudflare invece e' browser-only.

## Firefox e Safari: perche' NO, e cosa si puo' fare lo stesso

**WebUSB non esiste su Firefox e non esistera'**: Mozilla l'ha marcata
**"harmful"** nella sua standards position (fingerprinting, esposizione
dell'hardware USB, modello di consenso debole). Non c'e' un flag in
`about:config`. Safari idem: WebKit e' dichiaratamente contrario.

**E WebSerial?** Firefox 151 (maggio 2026) l'ha aggiunta davvero — ma **non
ci serve**, e la ragione e' l'hardware, non il browser: il Pico si presenta
come device **vendor-class con quattro endpoint** (comandi 0x01, stato 0x81,
dati 0x02/0x82, vedi `usb_link.py`). WebSerial da' UNA sola pipe seriale
bidirezionale, e non puo' reclamare un'interfaccia vendor. Per passare di
li' bisognerebbe **rifare il protocollo del firmware RP2040** su CDC, su
entrambi i lati. E' un progetto, non un adattamento.

**Cosa funziona su Firefox: la modalita' SPETTATORE.** Il bottone «Guarda
soltanto» avvia il bridge con `device = null`: ci si collega al relay, si
ricevono le posizioni degli amici e si guardano sulla Mappa live. Non si
manda niente (non c'e' un GBA da leggere), ma non e' poco - serve anche a
chi il GBA ce l'ha in un'altra stanza. L'avviso in cima alla pagina lo
spiega, cosi' chi arriva con Firefox non pensa che il sito sia rotto.

## Come funziona

```
browser A (WebUSB -> Pico -> GBA)  --wss-->  relay_ws.py  --UDP-->  relay.py  <--UDP--  client.py (mGBA / GBA) 
browser B (WebUSB -> Pico -> GBA)  --wss-->  relay_ws.py  --UDP-->    "
```

Il messaggio WebSocket (binario) e' il datagramma OWL1 di `protocol.py`,
byte per byte: il frontale non interpreta niente. L'unica cosa che inventa
e' un `T_BYE` a nome del peer quando la sua socket si chiude, cosi' l'amico
vede il VIA entro un secondo invece che al timeout. La stanza viaggia anche
nell'URL (`/ws?stanza=N`): a `relay_ws.py` non serve, al Worker si'.

## Dove ospitare: la raccomandazione

Tre requisiti fissi: (1) la pagina su **https** (WebUSB lo pretende; vale
anche `http://localhost`), (2) il relay raggiungibile in **wss://** (una
pagina https non puo' aprire `ws://`), (3) firmware + Zadig restano
comunque. Tre strade, in ordine di quando usarle:

1. **Per provare SUBITO, gratis, senza aprire porte: Cloudflare Tunnel sul PC
   di Lain.** `cloudflared` (un eseguibile) pubblica un servizio locale su un
   URL https/wss di `trycloudflare.com`, WebSocket compresi, senza account:

   ```
   cloudflared tunnel --url http://127.0.0.1:7413     # la pagina (python -m http.server nella cartella del sito)
   cloudflared tunnel --url http://127.0.0.1:9001     # relay_ws.py (con relay.py acceso accanto)
   ```

   Ognuno stampa un URL `https://<parole-a-caso>.trycloudflare.com`. Il primo
   lo apre l'amico nel browser; il secondo, come `wss://<parole>.trycloudflare.com/ws`,
   va nel campo Relay (o in `config.js` via `prepara-sito-web.ps1 -Relay ...`).
   Gli URL cambiano a ogni avvio: va bene per le prove, non per «dare un
   indirizzo agli amici».
2. **Per il definitivo: un VPS piccolo (4-5 euro/mese) con Caddy** — e' la
   strada consigliata, perche' tiene **un mondo solo**: browser, mGBA e GBA
   con `client.py` nella stessa stanza (il Worker no). Caddy fa https e il
   certificato da solo; serve un nome DNS (anche gratuito). Ricetta in
   `net/RELAY-VPS.md`, sezione «Con il pannello web».
3. **Zero server: Cloudflare Pages + Workers** (`relay-worker/`). Gratis,
   wss incluso, niente da mantenere, edge vicino. Svantaggi: mondo
   browser-only, account Cloudflare + `wrangler` (npm), e **non l'ho mai
   eseguito**: la prima prova e' `wrangler dev` + `bridge_test.html?relay=ws://127.0.0.1:8787/ws`.

Chi pubblica la pagina da' agli amici **un URL e una stanza**. Fine.

## Come si prova (procedura W-web)

Tutto in ordine; ogni punto ha il suo criterio. I punti 1-2 non toccano
l'hardware e sono gia' stati eseguiti (2026-08-23, nel browser della
sessione).

1. **Autotest** (nessun hardware): `test.html` → 12/12; `mb_test.html` → TUTTO
   PASSATO; `bridge_test.html` → 28 ok. Con `relay.py` e `relay_ws.py` accesi,
   `bridge_test.html?relay=ws://127.0.0.1:9001` → **32 ok** (i 4 INTEGRATI:
   A→B ricucito e con le copie, B→A, PONG, VIA alla chiusura).
2. **Pannello in locale**, senza Pico:
   ```
   cd overworld-link
   D:\Progettini\Python313\python.exe net\relay.py --port 9000
   D:\Progettini\Python313\python.exe net\relay_ws.py --port 9001 --bind 127.0.0.1
   D:\Progettini\Python313\python.exe -m http.server 7413 --bind 127.0.0.1
   ```
   Chrome/Edge su `http://127.0.0.1:7413/web/`. Relay `ws://127.0.0.1:9001`,
   stanza a piacere, Salva, **Prova il relay** → «relay raggiungibile: PONG in
   N ms». Gioca senza Pico → «prima collega il Pico (passo 0)».
3. **Col Pico e il GBA** (Lain, ~10 minuti). Il pannello Python NON deve
   essere collegato al Pico nello stesso momento.
   - **Collega il Pico**: finestra del browser, scegli 2FE3:000A → targa
     «Pico: ...» verde, nel registro `Cancel`, `SetMode`, `master`, `StartHandshake`.
     Se fallisce qui e `0-prova-canale.bat` funziona, il sospetto e' `device.js`
     (riarmo delle letture): incollare il registro.
   - **Carica il gioco nel GBA** (slot vuoto, acceso dopo il cavo): registro
     `client 1 rilevato`, `handshake ok`, `trasferimento N%`, `DONE! CRC`,
     schermo ROSSO; poi `Pico riavviato (F-4)` e `canale riaperto dopo il
     multiboot` da solo (se no: Collega il Pico). Tempi: ~14 s a 3700,
     ~26 s a 7400.
   - cartuccia dentro (rosso → giallo → verde → gioco); in overworld la
     diagnostica mostra `sonda #1` entro ~2 s e la scheda «Il tuo GBA» passa
     a «Programma caricato».
   - **Gioca**: relay e stanza impostati, LED relay verde, RTT nella scheda
     canale; camminando 30 s **Inviati ≥ 15**, «mappa g.n (x,y)» nella scheda.
     Con un amico (altro browser, o il pannello Python con `client.py`)
     nella stessa stanza: «1 collegato» con la sua mappa, **Ricevuti** che
     sale, e in gioco ci si vede come sempre. Chiudendo la scheda
     dell'amico, il suo avatar sparisce entro ~2 s (VIA).
4. **Pubblico** (tunnel o VPS): stesso punto 3 con l'URL https della pagina e
   il relay wss://. La prova del relay deve dare un RTT coerente con il ping
   verso il server (+ qualche ms).

**Cosa NON e' verificato** (2026-08-23): tutto il punto 3 e 4 (nessun Pico
collegato a un browser, mai); il Worker Cloudflare; `relay_ws.py` dietro
Caddy/tunnel (provato solo in locale e nei test). La logica sotto (sio,
multiboot, bridge, frontale) e' provata dagli autotest e dai test Python.
