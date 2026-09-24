# Componenti di terze parti

> 🇬🇧 English version: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

Il codice di questo repository è rilasciato sotto **GPL-3.0** (`LICENSE`). Alcune parti derivano da
progetti di altri, con le licenze qui sotto: tutte compatibili con GPL-3.0.

## Codice incluso o derivato

| Dove | Origine | Licenza |
|---|---|---|
| `hw/firmware/*.patch` (e il `celio.uf2` delle Releases) | [Celio-Link/Celio-Firmware](https://github.com/Celio-Link/Celio-Firmware), patch sul commit `f67733c` | GPL-3.0 |
| `net/club_link.py`, `net/protocol.py`, `net/usb_link.py`, `net/club_mgba.py`, `web/js/club.js`, `web/js/device.js` | logica di sessione e protocollo del device portata da [Celio-Server](https://github.com/Celio-Link/Celio-Server) e [Celio-Client](https://github.com/Celio-Link/Celio-Client) | GPL-3.0 |
| `mgba/websocket/` | presa da [Celio-mGBA-Link](https://github.com/Celio-Link/Celio-mGBA-Link) (GPL-3.0), che a sua volta usa i file sotto | vedi sotto |
| `mgba/websocket/frame.lua`, `handshake.lua`, `server.lua`, `server_client.lua`, `tools.lua` | [lipp/lua-websockets](https://github.com/lipp/lua-websockets) — Copyright (c) 2012 Gerhard Lipp | MIT |
| `mgba/websocket/base64.lua` | [iskolbin/lbase64](https://github.com/iskolbin/lbase64) v1.5.3 | pubblico dominio / MIT |
| `mgba/websocket/sha1.lua` | [Jeffrey Friedl, SHA-1 in pure Lua](https://regex.info/blog/lua/sha1) — Copyright 2009 Jeffrey Friedl («Feel free to use it as you like»; poi ridistribuito sotto MIT) | libera / MIT |
| `hw/gbalink-fw/` | [Lorenzooone/gb-link-firmware-reconfigurable](https://github.com/Lorenzooone/gb-link-firmware-reconfigurable) | GPL-3.0 (`hw/gbalink-fw/LICENSE`) |
| `hw/gbalink-fw/main.c`, `tusb_config.h`, `usb_descriptors.*` | [TinyUSB](https://github.com/hathach/tinyusb) — Copyright (c) 2019 Ha Thach | MIT (intestazioni nei file) |
| `hw/gbalink-fw/pio/` | [Raspberry Pi pico-examples](https://github.com/raspberrypi/pico-examples) — Copyright (c) 2020 Raspberry Pi (Trading) Ltd. | BSD-3-Clause (intestazioni nei file) |

## Riferimenti usati per scrivere codice nostro (nessun file copiato)

- [afska/gba-link-connection](https://github.com/afska/gba-link-connection) (MIT) e
  [Lorenzooone/PokemonGB_Online_Trades_and_Battles](https://github.com/Lorenzooone/PokemonGB_Online_Trades_and_Battles) (MIT):
  il protocollo multiboot (`net/mb_multi.py`, `web/js/multiboot.js`). `hw/siotest/mbsend.py` *importa* `multiboot.py`
  di Lorenzooone da una copia locale, se presente: non è incluso qui.
- [GBATEK](https://problemkaputt.de/gbatek.htm) di Martin Korth: documentazione.
- [pret/pokeemerald](https://github.com/pret/pokeemerald): indirizzi, offset delle struct e costanti del gioco
  (`payload/game_types.h`, `payload/game_syms*.h`, i Lua). Sono **fatti sull'interfaccia del gioco**, non codice copiato;
  pokeemerald non ha una licenza.

## Dipendenze che si installano a parte (non incluse)

| Pacchetto | Serve a | Licenza |
|---|---|---|
| [pyusb](https://github.com/pyusb/pyusb) | pannello per PC, USB | BSD-3-Clause |
| [libusb-package](https://github.com/pyocd/libusb-package) | pannello per PC, porta con sé libusb (LGPL-2.1) | Apache-2.0 |
| [lupa](https://github.com/scoder/lupa) | solo alcuni test | MIT |
| [mGBA](https://mgba.io/) | emulatore, per chi gioca da PC | MPL-2.0 |
| [Zadig](https://zadig.akeo.ie/) | driver WinUSB su Windows | GPL-3.0 |

## Materiale Nintendo / Game Freak

Il repository **non contiene ROM né salvataggi**. Contiene però **quattro piccole icone derivate dalla grafica del
gioco** (`web/img/` e `net/img/`: il ciuffo d'erba alta, le orme di Treecko e Torchic e la favicon), e la
**Mappa live** usa dati e sprite che `tools/gen_mappa.py` estrae dalla propria copia di pokeemerald, più le GIF
di Nero/Bianco che `tools/sprite_bw.py` scarica a parte: quel materiale non è nel repository.

Lo stub multiboot (`mbstub.gba` nelle Releases) contiene nell'intestazione il **logo Nintendo** di 156 byte,
che il BIOS del GBA pretende per avviare qualsiasi programma: nel sorgente non c'è, `hw/mbstub/build.ps1`
lo copia al momento della build da una ROM indicata da chi compila. È la prassi di tutto l'homebrew per GBA.

Pokémon, Game Boy Advance e i nomi correlati sono marchi di Nintendo, Creatures Inc. e GAME FREAK inc.

## Testo della licenza MIT di lua-websockets

```
Copyright (c) 2012 by Gerhard Lipp <gelipp@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
