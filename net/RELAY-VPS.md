# Il relay su un VPS (ricetta, 2026-08-21)

Oggi il relay gira sul PC di Lain con una porta UDP aperta sul router: va bene
per due amici, non per "chi vuole provare". Un VPS da pochi euro al mese lo
rende stabile e indipendente dal PC di casa. `relay.py` e' libreria standard
pura: niente da installare oltre Python 3.

Il relay NON conosce il formato dei 12 byte e NON fa nessun filtro per mappa:
fa stanze UDP e basta (stanza = partita). Resta "opaco" anche sul VPS.

## 1. Il VPS

Qualunque Linux con IPv4 pubblico (Debian/Ubuntu, il taglio piu' piccolo basta:
il relay muove poche decine di KB/s per partita). Aprire nel firewall del
provider **UDP 9000** (o la porta che si sceglie).

## 2. Copia e avvio a mano (prova)

```bash
sudo apt-get install -y python3
mkdir -p ~/gba-relay && cd ~/gba-relay
# copiare qui, dal PC: net/relay.py e net/protocol.py (scp, WinSCP, quello che si vuole)
python3 relay.py --port 9000
```

Sul PC, nel pannello: ruolo "Mi collego a un amico", relay `IP-DEL-VPS:9000`,
stessa stanza per tutti. Il log del relay deve mostrare `peer 1 -> stanza N` e
`peer 2 -> stanza N`: e' il criterio.

## 3. Come servizio (si riavvia da solo)

`/etc/systemd/system/gba-relay.service`:

```ini
[Unit]
Description=GBA overworld-link relay (UDP)
After=network.target

[Service]
User=nobody
WorkingDirectory=/opt/gba-relay
ExecStart=/usr/bin/python3 /opt/gba-relay/relay.py --port 9000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /opt/gba-relay && sudo cp relay.py protocol.py /opt/gba-relay/
sudo systemctl daemon-reload
sudo systemctl enable --now gba-relay
sudo systemctl status gba-relay        # "active (running)"
sudo journalctl -u gba-relay -f        # il log del relay, dal vivo
```

## 4. Pacchetti che puntano al VPS

```powershell
.\tools\prepara-pacchetto-amico.ps1 -Relay IP-DEL-VPS:9000 -Stanza 4242
```

Chi vuole provare con un altro amico cambia solo la **stanza** dal pannello
(campo "Stanza"): il relay tiene stanze indipendenti. La stanza e' l'unica
"password": numeri alti e non ovvi.

## 5. Con il pannello web (2026-08-23): relay_ws.py + Caddy

I browser parlano WebSocket, e la pagina e' https, quindi il relay deve
rispondere in `wss://`. Sul VPS si aggiungono due cose: `relay_ws.py` (il
frontale WebSocket davanti a `relay.py`, solo stdlib) e **Caddy**, che fa
https/wss con il certificato da solo e serve anche la pagina statica. Serve
un nome DNS che punti all'IP del VPS (un sottodominio gratuito va bene).

```bash
sudo cp relay_ws.py /opt/gba-relay/          # accanto a relay.py e protocol.py
sudo apt-get install -y caddy                # https://caddyserver.com/docs/install
sudo mkdir -p /var/www/passotile             # qui il contenuto di build/sito-web (prepara-sito-web.ps1)
```

`/etc/systemd/system/gba-relay-ws.service`:

```ini
[Unit]
Description=GBA overworld-link relay, frontale WebSocket
After=network.target gba-relay.service

[Service]
User=nobody
WorkingDirectory=/opt/gba-relay
ExecStart=/usr/bin/python3 /opt/gba-relay/relay_ws.py --port 9001 --bind 127.0.0.1 --relay 127.0.0.1:9000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/caddy/Caddyfile` (sostituire il nome):

```
passotile.esempio.it {
    root * /var/www/passotile
    file_server
    reverse_proxy /ws 127.0.0.1:9001
}
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gba-relay-ws
sudo systemctl reload caddy
curl https://passotile.esempio.it/ws          # {"relay_ws": true, ...}: il frontale risponde dietro Caddy
```

Nel pannello web: relay `wss://passotile.esempio.it/ws`, stessa stanza per
tutti. `relay_ws.py --bind 127.0.0.1` apposta: dall'esterno si entra SOLO da
Caddy (https). La 9000 UDP resta aperta per chi gioca da mGBA/GBA con
`client.py`: stanno nelle stesse stanze dei browser.

Non eseguito su un VPS vero (2026-08-23): provato in locale (`net/test_relay_ws.py`
e la pagina contro `ws://127.0.0.1:9001`). La prima volta: `curl` qui sopra,
poi `bridge_test.html?relay=wss://passotile.esempio.it/ws` deve dare 32 ok.

## Cosa NON fa questa ricetta

- Niente TLS/autenticazione: il relay vede 12 byte opachi per evento, non dati
  personali, e la stanza e' l'unica difesa. Se un giorno il relay diventa
  pubblico per davvero, serve almeno un rate-limit per IP (relay.py non ce l'ha).
- Niente IPv6, niente DNS dinamico: il VPS ha un IP fisso, e' il suo pregio.
- Non e' stata eseguita su un VPS vero: e' la procedura, da provare la prima
  volta con `relay.py --port 9000` a mano e i due client che si vedono.
