#!/usr/bin/env python3
"""
mbsend.py - carica un .gba sul GBA via multiboot, usando l'adattatore Pico.

PERCHE' ESISTE
--------------
`multiboot.py` del repo di lorenzooone implementa il protocollo ed e' ottimo,
ma **non e' uno script standalone**: non ha `main`, non apre nessuna porta, e
l'unico chiamante (`usb_trading.py`) ha il percorso del .gba cablato dentro e
passa da un menu interattivo. Qui si riusa quel modulo cosi' com'e' e gli si
mette davanti quello che gli manca per accettare un file qualunque.

IL PROBLEMA DEI PIN, E PERCHE' QUI SI MISURA INVECE DI ASSUMERE
--------------------------------------------------------------
Sulla game-boy-pico-link-board i connettori J1 (GBA) e J2 (DMG) sono
**elettricamente in parallelo**: il firmware non "sceglie il cavo", usa sempre
le stesse GPIO. Cio' che cambia davvero e' SW1 (5 V per GBC, 3,3 V per GBA) e
come e' cablato dentro il cavo che usi.

Su chi sia GP1 e chi GP2 le fonti si contraddicono: lo schematico etichetta i
pin del connettore SO/SI/SD/SC senza dire da quale punto di vista, e i firmware
in giro fanno scelte diverse (l'upstream legge l'ingresso su GP1, la copia
ricompilata in D:\\Progettini\\trades lo legge su GP3 = SD, dove in SIO Normal
32 bit la risposta del GBA non passa mai).

Invece di scommettere, il firmware `hw/gbalink-fw` accetta i pin a runtime e
qui li si prova a uno a uno finche' il GBA risponde 0x7202 all'handshake.
L'esito e' una misura, non un'opinione, e va annotato in NOTES.md.

FIRMWARE: SERVE QUELLO DI hw/gbalink-fw (O IL RICONFIGURABILE UPSTREAM)
----------------------------------------------------------------------
Il multiboot usa SIO **Normal 32-bit**, che il firmware Celio non implementa
(verificato: zero occorrenze di "multiboot" nel suo sorgente, e in
`src/control.hpp` le modalita' sono solo gbaTradeEmu/gbaLink/gbLink/gbPrinter/
gbaPassthrough). Serve `hw/gbalink-fw/build/gbalink-mb.uf2`.

Col firmware upstream la selezione dei pin non c'e': lo script se ne accorge,
lo dice, e prosegue con i pin che il firmware ha di suo.

Attenzione a un tranello: anche `pico-gba-link-bridge` di gba-online-link-lab
usa lo **stesso VID/PID CAFE:4011**, ma non risponde al magic. Se il device si
apre e la configurazione fallisce, e' quello.

PRIMA DI LANCIARLO
------------------
  1. **SW1 sulla posizione GBA (3,3 V)** - con 5 V la board pilota gli
     ingressi a 3,3 V del GBA, ed e' cosi' che si rompe un GBA
  2. Pico col firmware di hw/gbalink-fw
  3. **GBA con lo SLOT CARTUCCIA VUOTO** - il BIOS entra in modalita' multiboot
     solo a slot vuoto, con una cartuccia valida avvia quella e ignora il cavo
  4. cavo link collegato, poi si accende il GBA: resta sulla schermata
     `GAME BOY` e non succede altro. E' lo stato giusto.

Uso:
    python mbsend.py <file.gba>                # diagnostica + ricerca pin + invio
    python mbsend.py <file.gba> --pins 0,2,1   # pin imposti a mano, nessuna ricerca
    python mbsend.py --diag                    # solo la misura delle linee
"""

import argparse
import importlib.util
import time
from pathlib import Path

VID = 0xCAFE
PID = 0x4011

# Comandi del pacchetto di configurazione esteso (40 byte), vedi hw/gbalink-fw/main.c
PINCFG_CMD_SET_PINS = 0x01
PINCFG_CMD_READ_LEVELS = 0x02
PINCFG_REPLY_PINS = 0xA1
PINCFG_REPLY_LEVELS = 0xA2

LINK_PIN_COUNT = 6

# Nome della linea sul connettore, secondo lo schematico della board.
NOMI_PIN = {0: "SC", 1: "SI", 2: "SO", 3: "SD", 4: "-", 5: "-"}

# (sck, mosi, miso). L'ordine e' per probabilita' decrescente:
#   (0,2,1) upstream del firmware riconfigurabile, e la build con cui il
#           multiboot ha storicamente funzionato
#   (0,1,2) le etichette del connettore lette dal punto di vista del GBA
#   (0,2,3) la copia ricompilata in D:\Progettini\trades (ingresso su SD)
#
# GP4 e GP5 sono fuori: la diagnostica del 2026-08-01 li ha letti 1/0, cioe'
# solo i pull interni del Pico, quindi al connettore non arrivano. Provarli
# sarebbe tempo buttato.
CANDIDATI = [(0, 2, 1), (0, 1, 2), (0, 2, 3), (0, 1, 3), (0, 3, 1), (0, 3, 2)]

# Dove cercare multiboot.py. Si importa invece di copiarlo: se lorenzooone lo
# corregge, la correzione arriva anche qui.
MULTIBOOT_CANDIDATES = [
    Path.home().joinpath(r"Desktop\PokemonGB_Online_Trades-main\multiboot.py"),
    Path.home().joinpath(r"Desktop\PokemonGB_Online_Trades-main - Copia\multiboot.py"),
    Path(r"D:\Progettini\GB Link\PokemonGB_Online_Trades-main\multiboot.py"),
]


def load_multiboot():
    for path in MULTIBOOT_CANDIDATES:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("multiboot", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            print(f"multiboot.py : {path}")
            return mod
    raise SystemExit(
        "multiboot.py non trovato. Percorsi provati:\n  "
        + "\n  ".join(str(p) for p in MULTIBOOT_CANDIDATES)
        + "\nPassa il repo di lorenzooone o aggiungi il percorso a MULTIBOOT_CANDIDATES."
    )


class UsbLink:
    """Endpoint bulk del firmware (interfaccia vendor), via libusb/pyusb."""

    max_chunk = 64

    def __init__(self):
        import usb.core
        import usb.util

        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise RuntimeError("nessun device CAFE:4011")
        dev.reset()          # come fa usb_trading.py: senza, una sessione morta resta appesa
        dev.set_configuration()
        cfg = dev.get_active_configuration()
        intf = cfg[(2, 0)]          # interfaccia vendor, come fa usb_trading.py
        self.out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT)
        self.inp = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN)
        # Abilita il canale seriale WebUSB, altrimenti il device resta muto.
        dev.ctrl_transfer(1, 0x22, wIndex=2, wValue=1)
        self.dev = dev

    def read(self):
        try:
            return bytes(self.inp.read(self.inp.wMaxPacketSize, timeout=100))
        except Exception:
            return b""

    def write(self, data):
        self.out.write(bytes(data))


def _read_win(p, size=None):
    """Lettura da WinUSB CDC.

    E' la riscrittura che sta in usb_trading.py di lorenzooone (`read_win`):
    la `read` di serie di winusbcdc e' troppo lenta per un protocollo a tempo.
    Riportata qui perche' e' il percorso che su Windows funziona davvero.
    """
    from winusbcdc import ComPort

    if not p.is_open:
        return b""
    rx = [p._rxremaining]
    length = len(p._rxremaining)
    p._rxremaining = b''
    end_timeout = time.time() + (p.timeout or 0.2)
    if size:
        super(ComPort, p).set_timeout(p._ep_in, (p.timeout or 0.2) * 10)
        while length < size:
            c = super(ComPort, p).read(p._ep_in, size - length)
            if c is not None and len(c):
                rx.append(c)
                length += len(c)
                if len(c) == p.maximum_packet_size:
                    end_timeout += (p.timeout or 0.2)
            if time.time() > end_timeout:
                break
    else:
        super(ComPort, p).set_timeout(p._ep_in, (p.timeout or 0.2))
        while True:
            c = super(ComPort, p).read(p._ep_in, p.maximum_packet_size)
            if c is not None and len(c):
                rx.append(c)
                length += len(c)
                if len(c) == p.maximum_packet_size:
                    end_timeout += (p.timeout or 0.2)
                else:
                    break
            else:
                break
            if time.time() > end_timeout:
                break
    chunk = b''.join(rx)
    if size and len(chunk) >= size:
        p._rxremaining = chunk[size:] + p._rxremaining
        chunk = chunk[0:size]
    return chunk


class WinUsbLink:
    """WinUSB CDC: e' il trasporto che usa usb_trading.py su Windows quando
    pyusb non ha un backend libusb, cioe' esattamente il nostro caso."""

    # usb_trading.py taglia i blocchi a 0x3C con il commento "Why? Idk. But it
    # fixes it". Non e' spiegato, ma e' la configurazione con cui il multiboot
    # ha funzionato davvero: la si rispetta.
    max_chunk = 0x3C

    def __init__(self):
        from winusbcdc import ComPort

        p = ComPort(vid=VID, pid=PID)
        if not p.is_open:
            raise RuntimeError("winusbcdc non riesce ad aprire CAFE:4011")
        p.settimeout(0.1)
        self.p = p

    def read(self):
        return _read_win(self.p) or b""

    def write(self, data):
        self.p.write(bytes(data))


class SerialLink:
    """Ultima spiaggia: lo stesso firmware espone anche una CDC classica.

    lorenzooone avvisa esplicitamente che su questo percorso servono firmware
    che non alterino l'output. Se si finisce qui, e' il caso di dirlo.
    """

    max_chunk = 64

    def __init__(self, port):
        import serial
        import serial.tools.list_ports

        if not port:
            for p in serial.tools.list_ports.comports():
                if p.vid == VID and p.pid == PID:
                    port = p.device
                    break
        if not port:
            raise RuntimeError("nessuna porta CDC CAFE:4011")
        # Nessun baudrate: e' una CDC virtuale, il baud non significa niente.
        self.ser = serial.Serial(port=port, bytesize=8, timeout=0.1, write_timeout=5)
        print(f"porta        : {port}")

    def read(self):
        return self.ser.read(64)

    def write(self, data):
        self.ser.write(bytes(data))


def open_link(port, forzato=None):
    """Stesso ordine di usb_trading.py: libusb, poi WinUSB CDC, poi seriale.

    L'ordine non e' un dettaglio: su questa macchina pyusb non ha backend
    libusb, e saltare winusbcdc significa finire sulla seriale - il percorso
    che lorenzooone stesso sconsiglia, e che qui si e' gia' rivelato instabile
    (`PermissionError 31` all'apertura di COM4 al secondo tentativo).
    """
    tentativi = []
    if forzato in (None, "libusb"):
        tentativi.append(("libusb (pyusb)", lambda: UsbLink()))
    if forzato in (None, "winusb"):
        tentativi.append(("WinUSB CDC (winusbcdc)", lambda: WinUsbLink()))
    if forzato in (None, "seriale"):
        tentativi.append(("porta seriale (pyserial)", lambda: SerialLink(port)))

    for nome, apri in tentativi:
        try:
            link = apri()
            print(f"trasporto    : {nome}")
            if nome.startswith("porta seriale"):
                print("  ATTENZIONE: e' l'ultima spiaggia. Se hai winusbcdc installato e")
                print("  finisci qui, il device era occupato: scollega e ricollega il Pico.")
            return link
        except Exception as exc:
            print(f"  {nome}: non utilizzabile ({exc})")

    raise SystemExit(
        "Nessun trasporto disponibile verso CAFE:4011.\n"
        "Scollega e ricollega il Pico, poi riprova. Se persiste:\n"
        "  pip install winusbcdc pyserial")


def read_bytes(link, timeout=0.4):
    """Legge una risposta corta (i comandi PINCFG rispondono 4 byte)."""
    out = b""
    scadenza = time.time() + timeout
    while time.time() < scadenza:
        data = link.read()
        if data:
            out += data
            if len(data) < 64:
                break
        elif out:
            break
    return out


# Preset del clock SPI del firmware: base x1, x4, x16, x64. Il valore
# dell'upstream (~1 MHz) attraversa un BOB-12009, che e' uno shifter a MOSFET
# con pull-up da 10 kOhm: sopra qualche centinaio di kHz i fronti si
# arrotondano. "Pin sbagliati" e "troppo veloce per lo shifter" danno lo stesso
# sintomo, quindi vanno separati.
CLOCK_HZ = ["~1 MHz", "~250 kHz", "~62 kHz", "~15 kHz"]


def pincfg(link, mb, sck, mosi, miso, cmd, us=36, nbytes=4, clock=0):
    """Pacchetto di configurazione esteso: 32 byte di magic + 8 di coda.

    L'ultimo byte porta il comando nel nibble basso e il preset di clock in
    quello alto, cosi' la lunghezza del pacchetto non cambia.
    """
    coda = bytes([sck, mosi, miso, (cmd & 0x0F) | ((clock & 0x0F) << 4)])
    link.write(bytes(mb.get_configure_list(us, nbytes)) + coda)
    return read_bytes(link)


def identifica_firmware(link, mb):
    """Dice QUALE firmware c'e' sul Pico, invece di presumerlo.

    Entrambi i firmware rispondono 1 al magic di configurazione, quindi da
    fuori sono indistinguibili - ed e' esattamente l'ambiguita' che ha
    inquinato i test del 2026-08-01. Il pacchetto da 40 byte li separa:
    gbalink-mb risponde 0xA2 + livelli; l'originale non lo riconosce, lo
    tratta da dati e rimanda indietro 40 byte di eco SPI.
    """
    pkt = bytes(mb.get_configure_list(1000, 4)) + bytes([0, 0, 0, PINCFG_CMD_READ_LEVELS])
    link.write(pkt)
    r = read_bytes(link)
    if len(r) >= 3 and r[0] == PINCFG_REPLY_LEVELS:
        return "gbalink-mb"
    return "originale"


def diagnostica(link, mb):
    """Legge i livelli di GP0..GP5 col pull-up e col pull-down.

    E' la misura che dice, senza interpretare documenti, quali linee sono
    libere, quali sono tenute a massa dal cavo, e quali sta pilotando il GBA.
    """
    risposta = pincfg(link, mb, 0, 0, 0, PINCFG_CMD_READ_LEVELS)
    if len(risposta) < 3 or risposta[0] != PINCFG_REPLY_LEVELS:
        print("  il firmware non risponde alla diagnostica: non e' quello di")
        print("  hw/gbalink-fw. Si prosegue lo stesso, ma senza misura.")
        return None

    up, down = risposta[1], risposta[2]
    print("  pin  linea  pull-up  pull-down  stato")
    for p in range(LINK_PIN_COUNT):
        u = (up >> p) & 1
        d = (down >> p) & 1
        if u == 1 and d == 0:
            stato = "libera (nessuno la pilota)"
        elif u == 0 and d == 0:
            stato = "TENUTA A MASSA"
        elif u == 1 and d == 1:
            stato = "tenuta ALTA"
        else:
            stato = "incoerente (rumore?)"
        print(f"  GP{p}  {NOMI_PIN[p]:>5}  {u:>7}  {d:>9}  {stato}")
    return (up, down)


def sonda_handshake(link, mb, tentativi=6):
    """Manda 0x6202 e guarda se torna 0x7202: e' l'handshake del BIOS.

    Se i pin sono sbagliati il GBA non riceve niente di sensato e resta dov'e',
    quindi provare una combinazione dopo l'altra e' ripetibile. Se il GBA
    dovesse comunque impuntarsi, basta spegnerlo e riaccenderlo: lo slot e'
    vuoto, non c'e' nessun salvataggio in gioco.
    """
    def receiver():
        return link.read()

    def sender(value, num_bytes):
        link.write(value.to_bytes(num_bytes, byteorder="big"))

    visto = None
    for _ in range(tentativi):
        sender(0x6202, 4)
        recv = mb.read_all(receiver)
        if visto is None:
            visto = recv
        if (recv >> 16) == 0x7202:
            return True, recv
    return False, visto


def tutte_le_permutazioni(gia_provate):
    """Tutte le assegnazioni distinte di (sck, mosi, miso) su GP0..GP3.

    Serve come rete: se nemmeno una delle 24 fa rispondere il GBA, il problema
    NON e' l'assegnazione dei pin, ed e' una conclusione che vale la pena
    pagare trenta secondi per averla.
    """
    fuori = []
    for sck in range(4):
        for mosi in range(4):
            for miso in range(4):
                if len({sck, mosi, miso}) != 3:
                    continue
                if (sck, mosi, miso) in gia_provate:
                    continue
                fuori.append((sck, mosi, miso))
    return fuori


def cerca_pin(link, mb, candidati, clock_fisso=None):
    print("cerco i pin (il GBA deve essere acceso, slot vuoto, schermata GAME BOY)")
    print("  la colonna a destra e' quello che TORNA davvero dal cavo:")
    print("  0x00000000 = linea a massa, 0xffffffff = linea alta e muta,")
    print("  qualunque altra cosa = dall'altra parte c'e' qualcuno che parla")

    def prova(elenco, clock):
        for sck, mosi, miso in elenco:
            risposta = pincfg(link, mb, sck, mosi, miso, PINCFG_CMD_SET_PINS, clock=clock)
            if len(risposta) < 4 or risposta[0] != PINCFG_REPLY_PINS:
                print("  il firmware non accetta la scelta dei pin: non e' quello di")
                print("  hw/gbalink-fw. Proseguo coi pin che ha di suo.")
                return None
            if (risposta[1], risposta[2], risposta[3]) != (sck, mosi, miso):
                print(f"  SC={sck} SO={mosi} SI={miso} -> rifiutati dal firmware, salto")
                continue
            if len(risposta) >= 5 and risposta[4] != clock:
                print(f"  clock {clock} non applicato (firmware senza preset): mi fermo qui")
                return None
            ok, visto = sonda_handshake(link, mb)
            eco = "----------" if visto is None else f"0x{visto & 0xFFFFFFFF:08x}"
            esito = "RISPONDE 0x7202" if ok else "muto"
            print(f"  SC={sck} SO={mosi} SI={miso} -> {esito:>15}   eco {eco}")
            if ok:
                return (sck, mosi, miso, clock)
        return False

    if clock_fisso is not None:
        print(f"  clock imposto: preset {clock_fisso} ({CLOCK_HZ[clock_fisso]})")
        return prova(candidati + tutte_le_permutazioni(set(candidati)), clock_fisso)

    print(f"  clock preset 0 ({CLOCK_HZ[0]})")
    esito = prova(candidati, 0)
    if esito is not False:
        return esito

    resto = tutte_le_permutazioni(set(candidati))
    print(f"\n  nessuna delle prime {len(candidati)} risponde: provo le altre"
          f" {len(resto)} permutazioni su GP0..GP3")
    esito = prova(resto, 0)
    if esito is not False:
        return esito

    # Se nessun pin risponde a ~1 MHz, il sospetto si sposta sullo shifter.
    # Si ripetono solo i candidati plausibili, sempre piu' piano.
    for clock in range(1, len(CLOCK_HZ)):
        print(f"\n  niente a {CLOCK_HZ[clock - 1]}: riprovo i primi {len(candidati)}"
              f" a {CLOCK_HZ[clock]} (preset {clock})")
        esito = prova(candidati, clock)
        if esito is not False:
            return esito
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom", nargs="?", help="il .gba multiboot da caricare")
    ap.add_argument("--port", default=None, help="porta seriale, se l'autodetect sbaglia")
    ap.add_argument("--pins", default=None,
                    help="pin imposti a mano come sck,mosi,miso (es. 0,2,1): niente ricerca")
    ap.add_argument("--diag", action="store_true",
                    help="misura le linee ed esci, senza caricare niente")
    ap.add_argument("--legacy", action="store_true",
                    help="nessun comando nostro: si comporta come usb_trading.py. "
                         "Serve per la prova di controllo col firmware originale")
    ap.add_argument("--transport", choices=["libusb", "winusb", "seriale"], default=None,
                    help="forza il trasporto invece di provarli in ordine")
    ap.add_argument("--clock", type=int, choices=range(len(CLOCK_HZ)), default=None,
                    help="preset di clock SPI: 0=~1MHz (default upstream), 1=~250kHz, "
                         "2=~62kHz, 3=~15kHz. Senza, li prova in scala")
    args = ap.parse_args()

    if not args.diag and not args.rom:
        raise SystemExit("serve il file .gba da caricare (oppure --diag)")

    rom = None
    if args.rom:
        rom = Path(args.rom)
        if not rom.is_file():
            raise SystemExit(f"ERRORE: {rom} non esiste")
        size = rom.stat().st_size
        if size > 0x3FF40:
            raise SystemExit(f"ERRORE: {size} byte, il limite multiboot e' {0x3FF40}")
        print(f"file         : {rom} ({size} byte)")

    mb = load_multiboot()
    link = open_link(args.port, args.transport)

    def receiver():
        return link.read()

    def sender(value, num_bytes):
        link.write(value.to_bytes(num_bytes, byteorder="big"))

    def list_sender(data, chunk_size=64):
        data = bytes(data)
        # Ogni trasporto ha il suo massimo: winusbcdc vuole blocchi da 0x3C.
        chunk_size = min(chunk_size, link.max_chunk)
        for i in range(0, len(data), chunk_size):
            link.write(data[i:i + chunk_size])

    if args.legacy:
        print("\nmodalita' legacy: nessun comando nostro, solo il protocollo di")
        print("lorenzooone. E' la prova di controllo - dice se il multiboot")
        print("funziona ancora oggi, indipendentemente da quello che ho aggiunto io.")

        # Scarico l'endpoint finche' non e' vuoto, come fa usb_trading.py
        # prima di configurare: dati vecchi rimasti in coda disallineano tutto.
        scaricati = 0
        while mb.read_all(receiver) != 0:
            scaricati += 1
        if scaricati:
            print(f"  scaricati {scaricati} pacchetti vecchi dall'endpoint")

        cfg = mb.get_configure_list(1000, 4)
        list_sender(cfg, chunk_size=len(cfg))
        time.sleep(0.1)
        ret = mb.read_all(receiver)
        print(f"  risposta alla configurazione: {ret!r} (1 = firmware riconosciuto)")

        fw = identifica_firmware(link, mb)
        print(f"  firmware sul Pico: {fw}")
        if fw == "gbalink-mb":
            print("  ATTENZIONE: per la prova di controllo serve il firmware ORIGINALE")
            print("  (Desktop\\Trading GBA\\gbusb.uf2). Con gbalink-mb la prova non")
            print("  distingue niente: flashalo e rilancia.")
        # il pacchetto di identificazione ha sporcato la configurazione
        # sull'originale (l'ha trasferita via SPI): la si rimanda pulita
        list_sender(cfg, chunk_size=len(cfg))
        time.sleep(0.1)
        mb.read_all(receiver)

        # L'handshake di multiboot.py e' un loop infinito e muto: se il GBA
        # non risponde, lo script "sta fermo" senza dire niente. Qui lo si
        # sonda PRIMA, stampando cosa torna davvero. Il BIOS tollera handshake
        # ripetuti (risponde 0x7202 a ogni 0x6202 finche' non arriva 0x6102),
        # quindi sondare e poi lasciar fare a mb.multiboot non disturba.
        print("sondo l'handshake: mando 0x6202, aspetto 0x7202 nella meta' alta...")
        cfg36 = mb.get_configure_list(36, 4)
        list_sender(cfg36, chunk_size=len(cfg36))
        time.sleep(0.05)
        mb.read_all(receiver)
        # A tempo, non a tentativi: il BIOS del GBA cicla fra le modalita' di
        # ascolto e l'aggancio in Normal mode puo' arrivare dopo parecchi
        # secondi. Il loop di usb_trading.py e' infinito per questo motivo:
        # una sonda che molla dopo dieci secondi dichiara morto un GBA che
        # stava solo guardando dall'altra parte.
        DURATA = 60
        print(f"  insisto fino a {DURATA} secondi: e' normale che ci metta un po'...")
        visti = {}
        ok = False
        tentativi = 0
        inizio = time.time()
        while time.time() - inizio < DURATA:
            sender(0x6202, 4)
            recv = mb.read_all(receiver)
            tentativi += 1
            visti[recv] = visti.get(recv, 0) + 1
            if (recv >> 16) == 0x7202:
                ok = True
                print(f"  AGGANCIATO dopo {time.time() - inizio:.1f} s"
                      f" ({tentativi} tentativi)   <- annota questo tempo")
                break
            if tentativi % 100 == 0:
                print(f"  {tentativi} tentativi, {time.time() - inizio:.0f} s,"
                      f" ultimo eco {recv:#012x}")
        for valore, volte in sorted(visti.items(), key=lambda kv: -kv[1]):
            print(f"  tornato {valore:#012x}  x{volte}")
        if not ok:
            raise SystemExit(
                f"\nIl GBA non ha risposto in {DURATA} secondi di insistenza.\n"
                "  0xffffffff ripetuto = la linea di risposta e' alta e muta\n"
                "  0x00000000 ripetuto = la linea di risposta e' a massa\n"
                "  0 (zero secco)     = dall'USB non torna proprio niente\n"
                "A questo punto rifai C1 (usb_trading.py, tasto m) e CRONOMETRA il\n"
                "silenzio fra 'Data preloaded...' e 'Lets do this thing!': se e' piu'\n"
                "lungo di cosi', alzo il limite; se C1 aggancia in pochi secondi e\n"
                "questo no, la differenza e' vera e ho dove cercare.")
        print("  handshake OK: il GBA risponde. Procedo col multiboot vero.")

        print("\ninvio...")
        mb.multiboot(receiver, sender, list_sender, str(rom))
        return

    # Il magic di configurazione a 36 byte: e' anche il modo in cui si riconosce
    # il firmware giusto. Risponde 1; qualunque altra cosa significa firmware
    # sbagliato, e il multiboot fallirebbe piu' avanti in modo molto meno
    # leggibile.
    print("configuro il firmware...")
    cfg = mb.get_configure_list(1000, 4)
    list_sender(cfg, chunk_size=len(cfg))
    time.sleep(0.1)
    ret = mb.read_all(receiver)
    if ret != 1:
        print(f"  ATTENZIONE: risposta {ret!r} invece di 1.")
        print("  Il firmware non e' ne' quello di hw/gbalink-fw ne' il riconfigurabile.")
        print("  Se hai Celio o pico-gba-link-bridge flashato, e' quello: hanno lo")
        print("  stesso VID/PID ma non fanno multiboot. Riflasha e riprova.")
        raise SystemExit(2)
    print("  firmware riconosciuto")

    print("\nlivelli delle linee:")
    diagnostica(link, mb)

    if args.diag:
        print("\nSolo diagnostica: non carico niente.")
        return

    print()
    if args.pins:
        try:
            sck, mosi, miso = (int(x) for x in args.pins.split(","))
        except ValueError:
            raise SystemExit("--pins vuole tre numeri separati da virgola, es. 0,2,1")
        risposta = pincfg(link, mb, sck, mosi, miso, PINCFG_CMD_SET_PINS,
                          clock=args.clock or 0)
        if len(risposta) >= 4 and risposta[0] == PINCFG_REPLY_PINS:
            print(f"pin imposti  : SC={risposta[1]} SO={risposta[2]} SI={risposta[3]}")
        else:
            print("pin imposti  : il firmware non ha confermato, proseguo lo stesso")
    else:
        trovati = cerca_pin(link, mb, CANDIDATI, args.clock)
        if trovati is False:
            raise SystemExit(
                "\nNessuna combinazione di pin, a nessuna velocita', fa rispondere il GBA.\n"
                "A questo punto NON e' l'assegnazione dei pin ne' il clock: sono stati\n"
                "provati tutti. Guarda la colonna 'eco' qui sopra.\n"
                "  Tutte 0xffffffff  -> dall'altra parte non c'e' nessuno che pilota:\n"
                "                       il cavo non sta portando il segnale, oppure il\n"
                "                       GBA non e' in attesa di multiboot.\n"
                "  Qualche 0x00000000 -> una linea e' tenuta a massa: c'e' contatto, ma\n"
                "                       il verso del cavo e' probabilmente sbagliato.\n"
                "Rifai la prova L della guida (tre --diag: cavo staccato, cavo solo nella\n"
                "board, cavo in entrambi) e la prova C col firmware originale e --legacy.")
        if trovati:
            sck, mosi, miso, clock = trovati
            print(f"pin trovati  : SC={sck} SO={mosi} SI={miso} clock={clock}"
                  f" ({CLOCK_HZ[clock]})   <- annota questa riga in NOTES.md")

    print("\ninvio...")
    mb.multiboot(receiver, sender, list_sender, str(rom))
    print("fatto: se il GBA mostra la schermata del test, il multiboot e' riuscito.")


if __name__ == "__main__":
    main()
