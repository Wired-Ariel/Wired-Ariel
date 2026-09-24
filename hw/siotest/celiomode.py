#!/usr/bin/env python3
"""
celiomode.py - mette l'adattatore Celio in modalita' raw relay e lo fa partire.

PERCHE' SERVE
-------------
Celio all'accensione **non e' in raw relay**: aspetta un comando USB che gli
dica cosa fare. Senza questo passaggio il cavo resta muto e il banco di prova
mostra la barra rossa per sempre - che non e' un difetto del GBA ne' del driver,
e' l'adattatore che non ha ancora ricevuto ordini.

COSA MANDA
----------
  0x00 0x04   SetMode(gbaPassthrough)  - il relay grezzo a parole di 16 bit,
                                         senza handshake Gen 3 ne' CRC.
                                         QUI il firmware rileva il tipo di cavo
                                         leggendo GP1: il capo del cavo GBA che
                                         tiene SI a massa deve stare LATO BOARD,
                                         e se giri il cavo devi rilanciare lo
                                         script (la rilevazione non si ripete)
  0x10        SetModeMaster            - l'adattatore fa da master, il GBA da
                                         child: e' l'assetto che payload/sio.c
                                         si aspetta
  0x12        StartHandshake           - IL PEZZO CHE MANCAVA: rawRelay.cpp
                                         resta in attesa di questo comando, e
                                         solo dopo attiva il PIO e clocca.
                                         Stati attesi in sequenza: DeviceReady,
                                         AwaitMode, HandshakeReceived,
                                         LinkConnected - e barra VERDE sul GBA

Protocollo USB, da src/layers/usbLayer.cpp del firmware: interfaccia 0, vendor,
quattro endpoint interrupt da 64 byte.
  EP 0x01 OUT  comandi
  EP 0x81 IN   stato (LinkStatus, 2 byte little-endian)
  EP 0x02 OUT  dati
  EP 0x82 IN   dati

Uso:
    python celiomode.py            # imposta e resta a mostrare lo stato
    python celiomode.py --once     # imposta ed esce
"""

import argparse
import time

VID = 0x2FE3
PID = 0x000A

EP_CMD_OUT = 0x01
EP_STATUS_IN = 0x81
EP_DATA_OUT = 0x02
EP_DATA_IN = 0x82


def frame_ping(seq, zero_in_payload=False):
    """Un frame di ping nel framing di payload/sio.c: SYNC, HEAD, dati, CKSUM.

    Dalla F-2 in poi (firmware celio-f2.uf2 e successivi) il canale USB e' a
    lunghezza variabile e SENZA padding: si mandano solo i byte veri, perche'
    0x0000 e' una parola di dati legittima e il firmware non filtra piu'.
    Col firmware vecchio questo frame passa comunque (non contiene zeri),
    ma --zero-in-payload richiede la F-2: senza, lo zero sparisce nel filtro
    e il checksum sul GBA fallisce. Che e' esattamente il difetto misurato.
    """
    head = 0x0302                       # tipo 3 (PING), 2 parole di dati
    d0 = 0x1000 | (seq & 0x0FFF) or 1   # mai zero: porta il numero di sequenza
    d1 = 0x0000 if zero_in_payload else 0xBEEF
    cksum = (-(head + d0 + d1)) & 0xFFFF
    words = [0xA55A, head, d0, d1, cksum]
    return b"".join(w.to_bytes(2, "little") for w in words)

CMD_SET_MODE = 0x00
# control.hpp: un SetMode mandato mentre un modulo gira viene solo ACCODATO e
# non fa niente finche' il modulo non muore. Percio' ogni run comincia con
# Cancel: chiude il modulo precedente, e il SetMode successivo riparte da zero
# rifacendo anche la rilevazione del cavo. Senza, solo il primo run dopo
# l'accensione del Pico fa qualcosa - misurato a lungo il 2026-08-01.
CMD_CANCEL = 0x01
MODE_GBA_PASSTHROUGH = 0x04
CMD_SET_MODE_MASTER = 0x10
# module/rawRelay.cpp: dopo SetModeMaster il modulo resta FERMO ad aspettare
# questo comando, e solo allora attiva il PIO e comincia a clockare. Senza,
# la barra resta rossa per sempre con DeviceReady a schermo - misurato il
# 2026-08-01 e poi letto nel sorgente (execute(), Step 2 -> Step 3).
CMD_START_HANDSHAKE = 0x12
# F-1 (firmware celio-f1f2 e successivi): [0x14][u32 LE] = periodo del master
# in ITERAZIONI PIO da ~540 ns, non microsecondi. 15370 ~= 8.3 ms (default),
# 7400 ~= 4 ms, 3700 ~= 2 ms, 1850 ~= 1 ms. Ogni SetMode riparte dal default:
# il comando va rimandato a ogni sessione. Il firmware vecchio lo ignora.
CMD_SET_RAW_TIMING = 0x14

# src/linkStatus.hpp
STATUS = {
    0xFF00: "GameboyConnected", 0xFF01: "GameboyDisconnected",
    0xFF02: "AwaitMode", 0xFF03: "HandshakeReceived",
    0xFF04: "HandshakeFinished", 0xFF05: "LinkConnected",
    0xFF06: "LinkReconnecting", 0xFF07: "LinkClosed",
    0xFF08: "DeviceReady", 0xFF09: "EmuTradeSessionFinished",
    0xFF0A: "GBModeActive", 0xFF0B: "GBPrinterModeActive",
    0xFF0C: "GBSessionFinished", 0xFFFF: "StatusDebug",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true",
                    help="imposta la modalita' ed esci, senza restare in ascolto")
    ap.add_argument("--sonda", type=float, default=None, metavar="SECONDI",
                    help="ascolta per N secondi, stampa il riassunto ed esci: "
                         "serve per i test automatici senza Ctrl-C")
    ap.add_argument("--solo-ascolto", action="store_true",
                    help="nessun comando: ascolta un modulo GIA' avviato. Utile "
                         "quando l'endpoint comandi e' in stallo ma il relay gira")
    ap.add_argument("--ping", action="store_true",
                    help="manda un frame di ping al GBA una volta al secondo: "
                         "il banco di prova lo fa suo (riga 4 sale) e lo rimanda "
                         "indietro - e' la misura del giro completo PC->GBA->PC")
    ap.add_argument("--timing", type=int, default=None, metavar="ITER",
                    help="periodo del master in iterazioni PIO (~540 ns l'una): "
                         "15370=8.3ms (default del firmware), 7400=4ms, "
                         "3700=2ms, 1850=1ms. Richiede il firmware con F-1")
    ap.add_argument("--zero-in-payload", action="store_true",
                    help="il ping porta una parola 0x0000 DENTRO i dati: e' il "
                         "test di accettazione della F-2. Col firmware vecchio "
                         "il frame arriva monco (riga 5 del GBA sale); con "
                         "celio-f2 arriva intero (riga 4 sale, riga 5 ferma) "
                         "e l'eco riporta indietro lo zero")
    args = ap.parse_args()

    import usb.core
    import usb.util

    # Su questa macchina pyusb non ha una libusb di sistema ("No backend
    # available"): la si prende dal pacchetto pip `libusb-package`, che la
    # porta con se'. Celio non ha alternative CDC: o questo, o niente.
    backend = None
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except ImportError:
        pass

    def trova():
        try:
            return usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        except usb.core.NoBackendError:
            raise SystemExit(
                "pyusb non ha un backend libusb, e libusb-package non e' installato.\n"
                "Rimedia con:\n"
                "    pip install libusb-package\n"
                "e rilancia questo script."
            )

    def apri():
        d = trova()
        if d is None:
            return None
        try:
            d.set_configuration()
        except Exception:
            pass  # gia' configurato: succede se un altro programma l'ha aperto prima
        return d

    dev = apri()
    if dev is None:
        raise SystemExit(
            f"nessun device {VID:04x}:{PID:04x}.\n"
            "Celio non e' collegato, oppure sul Pico c'e' ancora il firmware del\n"
            "multiboot (gb-link-firmware-reconfigurable, che si presenta CAFE:4011)."
        )
    print(f"device       : {VID:04x}:{PID:04x}")

    def manda_modi(d):
        d.write(EP_CMD_OUT, bytes([CMD_CANCEL]))
        time.sleep(0.6)   # il modulo precedente deve fare in tempo a chiudersi
        print("reset        : Cancel mandato, modulo precedente chiuso")
        d.write(EP_CMD_OUT, bytes([CMD_SET_MODE, MODE_GBA_PASSTHROUGH]))
        time.sleep(0.3)   # QUI il firmware rileva il tipo di cavo leggendo GP1
        print("modalita'    : raw relay (gbaPassthrough 0x04), cavo rilevato ora")
        d.write(EP_CMD_OUT, bytes([CMD_SET_MODE_MASTER]))
        time.sleep(0.2)
        print("ruolo        : adattatore MASTER, GBA child")
        if args.timing is not None:
            d.write(EP_CMD_OUT, bytes([CMD_SET_RAW_TIMING])
                    + args.timing.to_bytes(4, "little"))
            time.sleep(0.2)
            print(f"timing       : {args.timing} iterazioni PIO "
                  f"(~{args.timing * 540 / 1_000_000:.1f} ms per scambio)")
        d.write(EP_CMD_OUT, bytes([CMD_START_HANDSHAKE]))
        time.sleep(0.2)
        print("start        : StartHandshake mandato - da qui Celio clocca da solo")

    if args.solo_ascolto:
        print("solo ascolto : nessun comando, il modulo deve essere gia' avviato")
    # Il reset e' SOLO la mossa di recupero: fatto a ogni avvio manda il
    # device Zephyr in rienumerazione e i write successivi vanno in timeout
    # (provato il 2026-08-01). Prima si prova con le buone; se una sessione
    # precedente ha lasciato gli endpoint in stallo, si resetta e si riprova.
    try:
        if not args.solo_ascolto:
            manda_modi(dev)
    except usb.core.USBError as exc:
        print(f"  il device non risponde ({exc}): reset e riprovo...")
        try:
            dev.reset()
        except Exception:
            pass
        time.sleep(1.0)
        dev = None
        for _ in range(20):
            dev = apri()
            if dev is not None:
                break
            time.sleep(0.25)
        if dev is None:
            raise SystemExit("il device non e' tornato dopo il reset: scollega e "
                             "ricollega il Pico, poi rilancia.")
        try:
            manda_modi(dev)
        except usb.core.USBError:
            raise SystemExit(
                "ancora niente dopo il reset. A questo punto e' lo stato del Pico:\n"
                "scollegalo, ricollegalo e rilancia questo script.")

    if args.once:
        print("\nFatto. Guarda il GBA: la barra in alto dovrebbe diventare verde.")
        return

    print("\nIn ascolto su stato (EP 0x81) e dati (EP 0x82). Ctrl-C per uscire.")
    print("Se il relay clocca, le parole scorrono anche a GBA muto (0xffff).")
    print("Se 'dati' resta a 0, il relay NON sta clockando: e' il dato che serve.\n")
    parole = 0
    campione = {}
    totale = {}
    seq = 0
    ultimo = time.time()
    fine = (time.time() + args.sonda) if args.sonda else None
    try:
        while fine is None or time.time() < fine:
            try:
                raw = dev.read(EP_STATUS_IN, 64, timeout=20)
                if len(raw) >= 2:
                    value = raw[0] | (raw[1] << 8)
                    print(f"  stato {value:#06x}  {STATUS.get(value, 'sconosciuto')}")
            except Exception:
                pass
            try:
                dati = dev.read(EP_DATA_IN, 64, timeout=50)
                for i in range(len(dati) // 2):
                    w = dati[2 * i] | (dati[2 * i + 1] << 8)
                    # Dalla F-2 i pacchetti sono a lunghezza variabile e senza
                    # riempitivo: OGNI parola e' traffico, anche 0x0000.
                    campione[w] = campione.get(w, 0) + 1
                    totale[w] = totale.get(w, 0) + 1
                    parole += 1
            except Exception:
                pass
            adesso = time.time()
            if adesso - ultimo >= 1.0:
                if args.ping:
                    try:
                        dev.write(EP_DATA_OUT, frame_ping(seq, args.zero_in_payload))
                        seq += 1
                        print(f"  ping  #{seq} spedito (d0=0x{0x1000 | ((seq - 1) & 0x0FFF):04x})")
                    except Exception as exc:
                        print(f"  ping  NON spedito ({exc})")
                if campione:
                    top = ", ".join(f"{v:#06x} x{c}" for v, c in
                                    sorted(campione.items(), key=lambda kv: -kv[1])[:4])
                    print(f"  dati  {parole} parole/s   [{top}]")
                else:
                    print("  dati  0 parole/s   (il relay non clocca)")
                parole = 0
                campione = {}
                ultimo = adesso
    except KeyboardInterrupt:
        print("\ninterrotto")

    if args.sonda:
        print("\nriassunto della sonda:")
        if not totale:
            print("  nessuna parola: il relay non clocca")
        else:
            for v, c in sorted(totale.items(), key=lambda kv: -kv[1])[:8]:
                print(f"  {v:#06x}  x{c}")
            diversi = [v for v in totale if v != 0xFFFF]
            if 0xA55A in totale:
                print("  -> visto il SYNC 0xa55a del banco di prova: pin GIUSTO, il GBA parla")
            elif diversi:
                print(f"  -> parole diverse da 0xffff: {len(diversi)} valori, ma niente SYNC")
            else:
                print("  -> solo 0xffff: il relay clocca su un pin dove non c'e' nessuno")


if __name__ == "__main__":
    main()
