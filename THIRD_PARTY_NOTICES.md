# Third-party components

> 🇮🇹 Versione italiana: [THIRD_PARTY_NOTICES.it.md](THIRD_PARTY_NOTICES.it.md)

The code in this repository is released under **GPL-3.0** (`LICENSE`). Some parts derive from
other people's projects, under the licenses below: all compatible with GPL-3.0.

## Included or derived code

| Where | Origin | License |
|---|---|---|
| `hw/firmware/*.patch` (and `celio.uf2` in the Releases) | [Celio-Link/Celio-Firmware](https://github.com/Celio-Link/Celio-Firmware), patch on commit `f67733c` | GPL-3.0 |
| `net/club_link.py`, `net/protocol.py`, `net/usb_link.py`, `net/club_mgba.py`, `web/js/club.js`, `web/js/device.js` | session logic and device protocol ported from [Celio-Server](https://github.com/Celio-Link/Celio-Server) and [Celio-Client](https://github.com/Celio-Link/Celio-Client) | GPL-3.0 |
| `mgba/websocket/` | taken from [Celio-mGBA-Link](https://github.com/Celio-Link/Celio-mGBA-Link) (GPL-3.0), which in turn uses the files below | see below |
| `mgba/websocket/frame.lua`, `handshake.lua`, `server.lua`, `server_client.lua`, `tools.lua` | [lipp/lua-websockets](https://github.com/lipp/lua-websockets) — Copyright (c) 2012 Gerhard Lipp | MIT |
| `mgba/websocket/base64.lua` | [iskolbin/lbase64](https://github.com/iskolbin/lbase64) v1.5.3 | public domain / MIT |
| `mgba/websocket/sha1.lua` | [Jeffrey Friedl, SHA-1 in pure Lua](https://regex.info/blog/lua/sha1) — Copyright 2009 Jeffrey Friedl («Feel free to use it as you like»; later redistributed under MIT) | free / MIT |
| `hw/gbalink-fw/` | [Lorenzooone/gb-link-firmware-reconfigurable](https://github.com/Lorenzooone/gb-link-firmware-reconfigurable) | GPL-3.0 (`hw/gbalink-fw/LICENSE`) |
| `hw/gbalink-fw/main.c`, `tusb_config.h`, `usb_descriptors.*` | [TinyUSB](https://github.com/hathach/tinyusb) — Copyright (c) 2019 Ha Thach | MIT (headers in the files) |
| `hw/gbalink-fw/pio/` | [Raspberry Pi pico-examples](https://github.com/raspberrypi/pico-examples) — Copyright (c) 2020 Raspberry Pi (Trading) Ltd. | BSD-3-Clause (headers in the files) |

## References used to write our own code (no files copied)

- [afska/gba-link-connection](https://github.com/afska/gba-link-connection) (MIT) and
  [Lorenzooone/PokemonGB_Online_Trades_and_Battles](https://github.com/Lorenzooone/PokemonGB_Online_Trades_and_Battles) (MIT):
  the multiboot protocol (`net/mb_multi.py`, `web/js/multiboot.js`). `hw/siotest/mbsend.py` *imports* Lorenzooone's
  `multiboot.py` from a local copy, if present: it is not included here.
- [GBATEK](https://problemkaputt.de/gbatek.htm) by Martin Korth: documentation.
- [pret/pokeemerald](https://github.com/pret/pokeemerald): the game's addresses, struct offsets and constants
  (`payload/game_types.h`, `payload/game_syms*.h`, the Lua scripts). These are **facts about the game's interface**, not copied
  code; pokeemerald has no license.

## Dependencies installed separately (not included)

| Package | Used for | License |
|---|---|---|
| [pyusb](https://github.com/pyusb/pyusb) | PC panel, USB | BSD-3-Clause |
| [libusb-package](https://github.com/pyocd/libusb-package) | PC panel, bundles libusb (LGPL-2.1) | Apache-2.0 |
| [lupa](https://github.com/scoder/lupa) | some tests only | MIT |
| [mGBA](https://mgba.io/) | emulator, for people playing on PC | MPL-2.0 |
| [Zadig](https://zadig.akeo.ie/) | WinUSB driver on Windows | GPL-3.0 |

## Nintendo / Game Freak material

This repository **contains no ROMs or save files**. It does contain **four small icons derived from the game's
graphics** (`web/img/` and `net/img/`: the tall-grass tile, Treecko's and Torchic's footprints, and the favicon), and the
**Live map** uses data and sprites that `tools/gen_mappa.py` extracts from your own copy of pokeemerald, plus the
Black/White GIFs that `tools/sprite_bw.py` downloads separately: that material is not in the repository.

The multiboot stub (`mbstub.gba` in the Releases) contains the 156-byte **Nintendo logo** in its header,
which the GBA BIOS requires to boot any program: it is not in the source code, `hw/mbstub/build.ps1`
copies it at build time from a ROM supplied by whoever builds it. This is standard practice for all GBA homebrew.

Pokémon, Game Boy Advance and related names are trademarks of Nintendo, Creatures Inc. and GAME FREAK inc.

## lua-websockets MIT license text

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
