# Fase 6 — rete

```
mGBA  --TCP 127.0.0.1-->  client.py  --UDP-->  relay.py  --UDP-->  client.py  --TCP-->  mGBA
```

Un `client.py` **per giocatore**. È il pezzo destinato a restare: in Fase 5, al posto del socket
TCP verso mGBA ci sarà l'USB verso l'adattatore Celio e il resto non cambia. Per questo è anche
l'unico punto che conosce il formato dei 12 byte del gioco — il relay sopra e il payload sotto
non ne sanno niente.

**Il payload non è stato toccato.** Il punto di contatto resta la mailbox, ed è esattamente il
motivo per cui è stata messa lì.

## Avvio

```
.\run-local.ps1                                # senza simulatore
.\run-local.ps1 -Delay 60 -Jitter 15 -Loss 2   # condizioni realistiche
```

Lo script compila **anche i due script Lua** (`inject.p1.lua` sulla porta 8201 e
`inject.p2.lua` sulla 8202) e avvia relay e bridge. Poi, con entrambe le istanze di mGBA **già
in partita sulla stessa mappa**, si carica `p1` nella prima e `p2` nella seconda.

Compilarli a mano non serve, ed è proprio da lì che è nato il primo test fallito: un `-LinkRole
server` scritto per abitudine, e quell'emulatore non ha mai parlato col relay. Entrambe le
istanze devono essere `client`; il ruolo `server` è solo per la prova diretta senza relay, e ora
lo dice a schermo.

### Le porte

I bridge stanno su **8201/8202**, lontano da **8123** che è il default della modalità diretta.
Non è pignoleria: vedi sotto.

### Perché il bridge controlla la porta prima di partire

Su Windows, un bind su `127.0.0.1:P` **riesce anche quando un altro processo ha già legato
`0.0.0.0:P`** — misurato, e nessuna opzione di socket lo impedisce, `SO_EXCLUSIVEADDRUSE`
compreso. Le due socket convivono e quella con l'indirizzo più specifico si prende le
connessioni in arrivo.

È così che il primo test è fallito: mGBA in modalità diretta aveva la 8123 in wildcard, il
bridge ci si è legato sopra, e la connessione del secondo emulatore è finita sul bridge
sbagliato senza un errore da nessuna parte. Per questo il bridge ora fa una verifica
**positiva** — prova a collegarsi alla porta e si ferma se qualcuno risponde — invece di fidarsi
del `bind`.

## Il simulatore, e perché serve

`--delay` / `--jitter` / `--loss` si applicano a ciò che **esce** da ciascun client, quindi
`--delay 60` sono 60 ms di sola andata per direzione e 120 ms di round trip.

Serve perché un amico sulla stessa wifi darebbe 5 ms e nasconderebbe ogni difetto, mentre
CLAUDE.md si è dato ~50 ms come limite dichiarato. Con questo si verifica sul serio, da soli.

## Perdite, duplicati, riordino

Il protocollo del payload è nato su un trasporto ordinato e affidabile (il socket TCP del Lua).
Su UDP:

- **duplicati e riordino sono un problema vero** — un `PASSO` duplicato fa fare al remoto un
  passo in più, uno fuori ordine glielo fa sbagliare. Il client li scarta con una finestra sui
  numeri di sequenza (che sono a un byte e riavvolgono, quindi si confronta la distanza in
  modulo 256, non con `>`);
- **le perdite invece vanno bene**: un passo perso si riassorbe al primo `SYNC`, perché il
  payload riallinea per qualsiasi scostamento quando il remoto è fermo. È quella correzione del
  2026-07-30 che rende UDP accettabile — non era ovvio, ed è la ragione per cui questa
  architettura sta in piedi.

## Il despawn pulito (chiuso: EV_LEAVE)

(Fino al 2026-08-23 questo paragrafo diceva che mancava, ed era vecchio di settimane.)
Quando un peer sparisce, il relay manda `BYE`; `client.py` lo trasforma in un evento di tipo 4
(`make_leave_event`, con la mappa dell'ultimo evento di quel peer) e il payload lo consuma
(`EVENT_LEAVE` in `ConsumeRemoteEvents`: `DoorAbort` + `ForgetRemote` + resync). Il contatore
è `VIA ricevuti` nel rapporto Lua.

## Il frontale WebSocket (`relay_ws.py`, 2026-08-23)

I browser non fanno UDP: il pannello web (`web/`) parla con `relay_ws.py`, che mette ogni
connessione WebSocket su una socket UDP verso `relay.py`. Niente secondo relay: stanze, BYE e
timeout restano in `relay.py`, e browser ed emulatori/GBA con `client.py` stanno nelle stesse
stanze. Test: `python net\test_relay_ws.py`. In locale: `avvia-relay-ws.bat` (TCP 9001) accanto
ad `avvia-relay.bat`. Per l'amico dal browser serve `wss://` (vedi `RELAY-VPS.md` e `web/README.md`).

## Perché non si usa Celio-Server

`repo-studio/Celio-Server` è un coordinatore di sessioni per il protocollo del **cavo link**
(`SetModeMaster`, `StartHandshake`, `LinkStatus`): 767 righe di macchina a stati che noi non
attraversiamo mai. Ci serve un router stupido di eventi da 12 byte. Ne sono state prese le idee
su stanze e riconnessione, non il codice.
