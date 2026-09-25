# Running the relay on a VPS

> 🇮🇹 Versione italiana: [RELAY-VPS.it.md](RELAY-VPS.it.md)

A relay on your home PC with a UDP port forwarded on the router is fine for two friends, not for
"anyone who wants to try". A VPS for a few euros a month makes it stable and independent from your
home PC. `relay.py` is pure standard library: nothing to install besides Python 3.

The relay does NOT know the 12-byte format and does NOT filter by map:
it manages UDP rooms and nothing else (room = game). It stays "opaque" on the VPS too.

## 1. The VPS

Any Linux with a public IPv4 (Debian/Ubuntu; the smallest size is enough:
the relay moves a few tens of KB/s per game). Open **UDP 9000** (or whatever port you choose)
in the provider's firewall.

## 2. Copy and start it by hand (test)

```bash
sudo apt-get install -y python3
mkdir -p ~/gba-relay && cd ~/gba-relay
# copy here, from your PC: net/relay.py and net/protocol.py (scp, WinSCP, whatever you like)
python3 relay.py --port 9000
```

On the PC, in the panel: role "amico" (join a friend), relay `VPS-IP:9000`,
same room for everyone. The relay log must show `peer 1 -> stanza N` and
`peer 2 -> stanza N`: that's the success criterion.

## 3. As a service (restarts by itself)

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
sudo journalctl -u gba-relay -f        # the relay log, live
```

## 4. Packages pointing to the VPS

```powershell
.\tools\prepara-pacchetto-amico.ps1 -Relay VPS-IP:9000 -Stanza 4242
```

To play with other friends you only change the **room** in the panel
(the "Stanza" field): the relay keeps rooms independent. The room is the only
"password": use high, non-obvious numbers.

## 5. With the web panel: relay_ws.py + Caddy (or nginx)

Browsers speak WebSocket, and the page is https, so the relay must
answer on `wss://`. On the VPS you add two things: `relay_ws.py` (the
WebSocket front end in front of `relay.py`, stdlib only) and **Caddy**, which does
https/wss with its own certificate and also serves the static page. You need
a DNS name pointing to the VPS IP (a free subdomain is fine).

```bash
sudo cp relay_ws.py /opt/gba-relay/          # next to relay.py and protocol.py
sudo apt-get install -y caddy                # https://caddyserver.com/docs/install
sudo mkdir -p /var/www/gen3-poke-multiplayer             # put the content of build/sito-web here (prepara-sito-web.ps1)
```

`/etc/systemd/system/gba-relay-ws.service`:

```ini
[Unit]
Description=GBA overworld-link relay, WebSocket front end
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

`/etc/caddy/Caddyfile` (replace the name):

```
relay.example.com {
    root * /var/www/gen3-poke-multiplayer
    file_server
    reverse_proxy /ws 127.0.0.1:9001
}
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gba-relay-ws
sudo systemctl reload caddy
curl https://relay.example.com/ws          # {"relay_ws": true, ...}: the front end answers behind Caddy
```

In the web panel: relay `wss://relay.example.com/ws`, same room for
everyone. `relay_ws.py --bind 127.0.0.1` on purpose: from outside you can ONLY get in through
Caddy (https). UDP 9000 stays open for people playing from mGBA/GBA with
`client.py`: they share the same rooms as the browsers.

**With nginx instead of Caddy** (this is what the project's public relay uses): include
[`nginx-gen3pm.conf`](nginx-gen3pm.conf) in a vhost that already has a certificate. It also
limits each IP to 8 relay connections (`limit_conn`), so nobody can clog the relay for everyone else.

**The mGBA script has no TLS**: it speaks plain `ws://`. If emulator players use your relay, expose the
WebSocket path in plain HTTP on port 80 too (without redirecting it to https), otherwise mGBA
fails the handshake with `HTTP/1.1 301`.

First run check: the `curl` above, then `bridge_test.html?relay=wss://relay.example.com/ws`
must pass all its tests.

## What this recipe does NOT do

- No authentication: the relay sees 12 opaque bytes per event, no personal data, and the room is
  the only protection. For a public relay, add a per-IP limit (as in `nginx-gen3pm.conf`):
  `relay.py` itself has none.
- No IPv6, no dynamic DNS: a VPS has a fixed IP, and that's its strength.
