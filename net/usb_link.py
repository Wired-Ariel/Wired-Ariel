#!/usr/bin/env python3
"""
usb_link.py - il transport USB verso il GBA fisico, via adattatore Celio.

E' il gemello hardware del socket TCP verso mGBA: il bridge (client.py) parla
solo NetEvent da 12 byte, e questo modulo li porta al GBA vero attraverso
Celio in raw relay. Due lezioni misurate sul banco di prova (2026-08-02)
governano il design:

1. **Thread di lettura dedicato.** Il lettore a polling nel loop principale
   perde ~2/3 dei pacchetti USB (flushRxBuffer del firmware scarica ~100
   pacchetti al secondo, un loop con select e sleep ne legge ~20). Qui un
   thread fa SOLO read bloccanti e accoda; chi consuma drena una Queue.

2. **FSM di de-framing con stato fra una read e l'altra.** Un frame EVENT
   (9 parole = 18 byte) a ~126 parole/s si spalma su molti flush USB da
   ~10 ms: il de-framer DEVE poter ricominciare a meta' frame. La FSM e'
   la stessa di payload/sio.c (WAIT_SYNC / HEAD / DATA / CKSUM), con gli
   stessi criteri prudenti di riaggancio.

Richiede il firmware con F-2 (celio-f2.uf2 o successivi): pacchetti USB a
lunghezza variabile, nessun filtro 0x0000, nessun padding. Con il firmware
vecchio gli zeri nei NetEvent sparirebbero nel filtro e ogni frame che li
contiene arriverebbe monco.

Framing (payload/sio.c):
    [SYNC 0xA55A][HEAD = tipo<<8 | n_parole][dati...][CKSUM]
    CKSUM = -(HEAD + somma dati) a 16 bit; il SYNC non entra nella somma.
    Tipi: 0x01 EVENT (6 parole = un NetEvent), 0x02 STATE, 0x03 PING.
    Idle del canale: 0x7FFF (GBA child senza niente da dire), 0xFFFF (linea
    a riposo / GBA assente): il de-framer li ignora in WAIT_SYNC.

Uso da solo, come attrezzo di diagnosi (il banco di prova fa eco dei PING):
    python usb_link.py --selftest 15          # 15 s di ping + eco
    python usb_link.py --selftest 15 --timing 7400
"""

import argparse
import queue
import struct
import threading
import time

VID = 0x2FE3
PID = 0x000A

EP_CMD_OUT = 0x01
EP_STATUS_IN = 0x81
EP_DATA_OUT = 0x02
EP_DATA_IN = 0x82

CMD_SET_MODE = 0x00
CMD_CANCEL = 0x01
MODE_GBA_PASSTHROUGH = 0x04
MODE_ONLINE_LINK = 0x01     # il modo LINK di Celio: il device fa da controparte
                            # del GBA al Cable Club (Consegna D)
CMD_SET_MODE_MASTER = 0x10
CMD_SET_MODE_SLAVE = 0x11
CMD_START_HANDSHAKE = 0x12
CMD_CONNECT_LINK = 0x13

# Gli stati che il firmware emette sull'endpoint di stato in modo onlineLink
# (Celio-Client, src/services/linkdevice.service.ts - sorgente letto il
# 2026-08-09). In passthrough lo stato ha altri significati: questi valgono
# SOLO fra club_enter() e club_exit().
LINK_ST_AWAIT_MODE = 0xFF02
LINK_ST_HANDSHAKE_RX = 0xFF03
LINK_ST_HANDSHAKE_OK = 0xFF04
LINK_ST_CONNECTED = 0xFF05
LINK_ST_RECONNECTING = 0xFF06
LINK_ST_CLOSED = 0xFF07
LINK_ST_READY = 0xFF08
LINK_ST_DEBUG = 0xFFFF

LINK_ST_NOMI = {
    LINK_ST_AWAIT_MODE: "attesa ruolo",
    LINK_ST_HANDSHAKE_RX: "handshake col GBA",
    LINK_ST_HANDSHAKE_OK: "handshake finito",
    LINK_ST_CONNECTED: "cavo collegato",
    LINK_ST_RECONNECTING: "riconnessione",
    LINK_ST_CLOSED: "link chiuso",
    LINK_ST_READY: "pronto",
    LINK_ST_DEBUG: "debug",
}
CMD_SET_RAW_TIMING = 0x14   # F-1: [0x14][u32 LE] iterazioni PIO (~540 ns)
CMD_SET_CABLE_TYPE = 0x15   # F-3: [0x15][u8] 0=auto 1=GBA(SD su GP3) 2=GBC(SD su GP4)
CMD_HW_REBOOT = 0x43        # F-4: [0x43][0xA5] sys_reboot del Pico (comando hardware,
REBOOT_MAGIC = 0xA5         #      passa anche a modulo attivo). Firmware >= 2.0.5.

# F-3, il perche' esiste. Il firmware sceglie il pin di SD da solo leggendo GP1:
# basso = cavo GBA (SD su GP3), alto = cavo GBC (SD su GP4). Sulla
# game-boy-pico-link-board quell'euristica sbaglia col cavo DMG/GBC, perche' i
# connettori J1/J2/J3 sono in PARALLELO sugli stessi GP0..GP3 e **GP4 non e'
# cablato**: il ramo GBC mette SD su un pin che non esiste e il canale resta
# muto senza un errore da nessuna parte. Con `--cable gba` si forza il ramo
# giusto e il cavo DMG/GBC funziona per tutta la sessione (multiboot + link),
# che e' il punto: un cavo solo, niente scambio a meta' partita.
CABLE_TYPES = {"auto": 0, "gba": 1, "gbc": 2}

SIO_SYNC = 0xA55A
SIO_IDLE = 0x7FFF
SIO_MAX_WORDS = 32

SIO_T_EVENT = 0x01
SIO_T_STATE = 0x02
SIO_T_DIAG = 0x04     # sonda pagina 2: registri seriali veri
SIO_T_PING = 0x03

EVENT_WORDS = 6             # un NetEvent: 12 byte = 6 parole da 16 bit


def _backend():
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except ImportError:
        return None


def _trova_device(required=True):
    import usb.core
    try:
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_backend())
    except usb.core.NoBackendError:
        raise SystemExit("pyusb senza backend libusb: pip install libusb-package")
    if dev is None and required:
        raise SystemExit(
            f"nessun device {VID:04x}:{PID:04x}: Celio non e' collegato, "
            "o sul Pico c'e' un altro firmware.")
    return dev


def riavvia_pico(dev=None, quiet=False):
    """F-4: riavvia il Pico via software e aspetta che rienumeri.

    E' il sostituto dello scollega/ricollega fisico che serviva fra il
    multiboot e la sessione di link (difetto dell'endpoint USB inceppato,
    NOTES 2026-08-02). Il Pico e' alimentato da USB, quindi staccarlo era un
    power cycle: sys_reboot fa lo stesso identico reset, e l'host vede la
    stessa sequenza — il device sparisce dal bus e ricompare.

    Ritorna (sparito, ricomparso). Il criterio di successo e' ricomparso=True;
    sparito=False con write riuscito significa quasi certamente che il
    firmware non ha la F-4 (i comandi hardware sconosciuti vengono ignorati
    in silenzio).
    """
    import usb.util
    if dev is None:
        dev = _trova_device()
    try:
        dev.write(EP_CMD_OUT, bytes([CMD_HW_REBOOT, REBOOT_MAGIC]))
    except Exception as e:
        usb.util.dispose_resources(dev)
        if not quiet:
            print(f"[usb ] comando di riavvio NON partito: {e}")
        return False, False
    usb.util.dispose_resources(dev)

    # Prima si deve VEDERE sparire: e' l'unica prova che il reset e' avvenuto.
    # Un device che resta li' tranquillo vuol dire firmware senza F-4.
    sparito = False
    fine = time.time() + 5.0
    while time.time() < fine:
        d = _trova_device(required=False)
        if d is None:
            sparito = True
            break
        usb.util.dispose_resources(d)
        time.sleep(0.15)

    ricomparso = False
    fine = time.time() + 10.0
    while time.time() < fine:
        d = _trova_device(required=False)
        if d is not None:
            usb.util.dispose_resources(d)
            ricomparso = True
            break
        time.sleep(0.25)
    if ricomparso:
        # Windows enumera in fretta ma il driver ci mette ancora un momento a
        # diventare apribile: senza questa pausa la open() subito dopo fallisce
        # una volta su tante, e sembrerebbe un difetto del riavvio.
        time.sleep(1.5)
    return sparito, ricomparso


def sio_frame(ftype, words):
    """Costruisce un frame nel framing di sio.c. Ritorna i byte da mandare
    a EP 0x02, SENZA padding (F-2: ogni parola spedita e' payload)."""
    if not 0 < ftype <= 0xFF:
        raise ValueError(f"tipo frame fuori range: {ftype}")
    if len(words) > SIO_MAX_WORDS:
        raise ValueError(f"troppe parole: {len(words)}")
    head = ((ftype & 0xFF) << 8) | len(words)
    cksum = (-(head + sum(words))) & 0xFFFF
    return b"".join(w.to_bytes(2, "little")
                    for w in [SIO_SYNC, head, *words, cksum])


class SioDeframer:
    """La FSM di ricezione di payload/sio.c, parola per parola.

    feed(data) accetta byte grezzi (i pacchetti USB, di qualunque lunghezza)
    e ritorna la lista dei frame completi e col checksum giusto: (tipo,
    [parole]). Lo stato sopravvive fra una feed e l'altra: e' il punto.
    """

    W_SYNC, W_HEAD, W_DATA, W_CKSUM = range(4)

    def __init__(self):
        self.state = self.W_SYNC
        self.ftype = 0
        self.count = 0
        self.data = []
        self.head = 0
        self._byte_pending = None   # un pacchetto puo' spezzare anche la parola
        # I contatori specchiano g_sio del GBA: stessi nomi, stessa semantica.
        self.words_rx = 0
        self.frames_rx = 0
        self.frame_err = 0
        self.resync = 0
        self.idle_words = 0
        # Che cosa arriva davvero, quando NON arriva un frame valido. Tre cause
        # diverse danno tre firme diverse, e senza questo istogramma sono
        # indistinguibili da "0 frame":
        #   0xFFFF/0x7FFF dominanti -> linea alta e viva, ma nessuno risponde
        #                              (il banco non gira, o e' il gioco senza payload)
        #   0x0000 dominante        -> linea tenuta a massa: pin sbagliato o cavo
        #   valori sparsi           -> qualcuno parla ma il framing non torna
        #                              (timing, o SD su un pin che raccoglie rumore)
        self.hist = {}

    def _drop(self):
        self.frame_err += 1
        self.resync += 1
        self.state = self.W_SYNC
        self.data = []

    def feed(self, data):
        out = []
        buf = bytes(data)
        if self._byte_pending is not None:
            buf = self._byte_pending + buf
            self._byte_pending = None
        if len(buf) % 2:
            self._byte_pending = buf[-1:]
            buf = buf[:-1]

        for (w,) in struct.iter_unpack("<H", buf):
            self.words_rx += 1
            if self.state == self.W_SYNC:
                # L'istogramma si riempie SOLO qui: fra un frame e l'altro. Se
                # il framing funziona non cresce quasi, e non costa niente; se
                # non funziona e' l'unica cosa che dice perche'.
                if len(self.hist) < 64 or w in self.hist:
                    self.hist[w] = self.hist.get(w, 0) + 1
                if w == SIO_SYNC:
                    self.state = self.W_HEAD
                elif w in (SIO_IDLE, 0xFFFF):
                    self.idle_words += 1
                # tutto il resto: rumore fra i frame, ignorato in silenzio
            elif self.state == self.W_HEAD:
                ftype, count = w >> 8, w & 0xFF
                # Il riaggancio prudente di sio.c: un SYNC da solo non basta,
                # anche la testa deve essere plausibile.
                if ftype == 0 or count > SIO_MAX_WORDS:
                    self._drop()
                    continue
                self.ftype, self.count, self.head = ftype, count, w
                self.data = []
                self.state = self.W_DATA if count else self.W_CKSUM
            elif self.state == self.W_DATA:
                self.data.append(w)
                if len(self.data) == self.count:
                    self.state = self.W_CKSUM
            else:  # W_CKSUM
                if (self.head + sum(self.data) + w) & 0xFFFF == 0:
                    self.frames_rx += 1
                    out.append((self.ftype, list(self.data)))
                    self.state = self.W_SYNC
                    self.data = []
                else:
                    self._drop()
        return out


class UsbLink:
    """Il canale verso il GBA fisico: setup di Celio, thread di lettura,
    frame in uscita. L'API che il bridge usa e' a 12 byte: send_event /
    poll_events. Tutto il resto e' diagnostica."""

    def __init__(self, timing=None, quiet=False, cable="auto", raw=False):
        self.timing = timing
        if cable not in CABLE_TYPES:
            raise ValueError(f"tipo di cavo sconosciuto: {cable}")
        self.cable = cable
        self.quiet = quiet
        # Modo parola grezza (mb_multi.py). Nel link in gioco 0x7FFF e 0xFFFF
        # sono idle da buttare; nel multiboot sono DATI - una parola cifrata
        # della ROM vale 0xFFFF quanto qualunque altra, e il conteggio delle
        # risposte non tollera buchi. Quindi qui il de-framer non entra
        # nemmeno in gioco: ogni parola ricevuta finisce in coda, in ordine.
        self.raw = raw
        self.dev = None
        self.deframer = SioDeframer()
        self.raw_words = queue.Queue()   # solo in modo raw: parole a 16 bit
        self.raw_rx = 0
        self.raw_tx = 0
        self._raw_odd = b""              # mezza parola a cavallo di due pacchetti
        self.events = queue.Queue()      # NetEvent da 12 byte, in arrivo
        # I PING del nodo arrivano 1/s anche quando nessuno li consuma (il
        # bridge non li usa): coda limitata, si scarta il piu' vecchio.
        self.pings = queue.Queue(maxsize=64)
        # La sonda del payload (SIO_T_STATE, 1/s + una a ogni ripresa della
        # porta): si tiene solo l'ULTIMO frame, con l'ora d'arrivo. Non e'
        # una coda: il valore sta nel confronto "quanto e' vecchio" e nei
        # contatori dentro, non nella storia.
        self.state_words = None          # tupla di 8 u16, o None
        self.state_stamp = 0.0           # time.time() dell'ultimo arrivo
        self.state_count = 0
        self.diag_words = None           # pagina 2: registri (8 u16)
        self.diag_stamp = 0.0
        self.frames_tx = 0
        self.tx_errors = 0
        self.packets_rx = 0
        self.last_status = None
        # Lo stato viaggia su EP 0x81, i dati su EP 0x82: due percorsi USB
        # distinti. Contarli separatamente e' cio' che distingue "l'endpoint
        # del Pico si e' inceppato" da "il canale verso il GBA si e' fermato",
        # che nel riassunto sono lo stesso identico "rx fermo".
        self.status_reads = 0
        self.status_timeouts = 0   # normali: il timeout e' 1 ms per scelta
        self.status_fails = 0      # questi si': errori USB veri
        self._rate_words = 0
        self._rate_since = None
        self._reader = None
        self._reader2 = None   # il secondo lettore del modo club (dati)
        self._stop = threading.Event()

    # -- setup ------------------------------------------------------------

    def open(self):
        dev = _trova_device()
        try:
            dev.set_configuration()
        except Exception:
            pass
        self.dev = dev

        # La sequenza misurata e messa a verbale il 2026-08-02: Cancel chiude
        # il modulo precedente (senza, il SetMode viene solo accodato e la
        # rilevazione del cavo non si ripete), poi modalita', ruolo, timing
        # opzionale (F-1), e StartHandshake che fa partire il PIO.
        self._cmd(bytes([CMD_CANCEL]), 0.6, "Cancel")
        self._cmd(bytes([CMD_SET_MODE, MODE_GBA_PASSTHROUGH]), 0.3,
                  "SetMode raw relay (rilevazione cavo ADESSO)")
        self._cmd(bytes([CMD_SET_MODE_MASTER]), 0.2, "master")
        # F-3: DEVE stare fra SetMode e StartHandshake. SetMode ha appena fatto
        # la rilevazione automatica; StartHandshake e' il momento in cui il
        # firmware carica il programma PIO leggendo il tipo di cavo. In mezzo
        # c'e' la finestra per correggerlo.
        if self.cable != "auto":
            self._cmd(bytes([CMD_SET_CABLE_TYPE, CABLE_TYPES[self.cable]]), 0.2,
                      f"cavo forzato: {self.cable} "
                      f"(SD su GP{3 if self.cable == 'gba' else 4})")
        if self.timing is not None:
            self._cmd(bytes([CMD_SET_RAW_TIMING])
                      + int(self.timing).to_bytes(4, "little"), 0.2,
                      f"timing {self.timing} iterazioni PIO")
        self._cmd(bytes([CMD_START_HANDSHAKE]), 0.2, "StartHandshake")

        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="usb_link-reader")
        self._reader.start()
        return self

    def _cmd(self, payload, pause, label):
        try:
            self.dev.write(EP_CMD_OUT, payload)
        except Exception as e:
            # Difetto noto e ricorrente, gia' a verbale il 2026-08-01: dopo una
            # sessione lunga l'endpoint del Pico resta inceppato e il comando
            # DOPO va in timeout. Non e' il firmware, non e' il cavo, non e' il
            # GBA - e senza questo messaggio si legge come un fallimento del
            # test che si stava per fare, che e' il modo migliore di perdere
            # mezz'ora dietro alla variabile sbagliata.
            raise SystemExit(
                f"\n[usb ] il comando '{label}' non e' partito: {e}\n"
                "[usb ] E' l'endpoint USB del Pico inceppato dopo la sessione\n"
                "[usb ] precedente, NON un difetto di cio' che stavi provando.\n"
                "[usb ] Rimedio: python usb_link.py --riavvia (F-4). Se anche\n"
                "[usb ] quello fallisce: SCOLLEGA e RICOLLEGA il Pico (senza\n"
                "[usb ] BOOTSEL, cosi' tiene il firmware), poi rilancia. Il\n"
                "[usb ] programma sul GBA vive in RAM e sopravvive: non\n"
                "[usb ] spegnere la console.")
        time.sleep(pause)
        if not self.quiet:
            print(f"[usb ] {label}")

    def close(self):
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._reader2 is not None:
            self._reader2.join(timeout=1.0)
            self._reader2 = None

    def reboot_pico(self):
        """F-4: riavvia il Pico. Dopo questa chiamata l'UsbLink e' da buttare
        (il device rienumera): per parlare ancora se ne apre uno nuovo.
        Ritorna (sparito, ricomparso), vedi riavvia_pico()."""
        self.close()
        dev, self.dev = self.dev, None
        return riavvia_pico(dev=dev, quiet=self.quiet)

    # -- il thread di lettura ---------------------------------------------

    def _read_loop(self):
        """Il ciclo CALDO: non fa altro che leggere i dati, subito e sempre.

        Non e' pignoleria. Su USB un endpoint interrupt IN viene interrogato
        dall'host SOLO se c'e' una richiesta di lettura pendente: ogni istante
        in cui questo thread sta facendo altro e' un istante in cui il firmware
        ha il pacchetto pronto e nessuno lo ritira - e con la back-pressure
        aggiunta al firmware, quel tempo blocca anche il pacchetto dopo.

        La lettura dello stato costava 1 ms a ogni giro e stava proprio qui in
        mezzo: era una fetta enorme del budget. Ora esce dal giro e si fa una
        volta al secondo, che e' la frequenza con cui lo stato cambia davvero.
        """
        import usb.core
        next_status = 0.0
        while not self._stop.is_set():
            adesso = time.time()
            if adesso >= next_status:
                next_status = adesso + 1.0
                try:
                    raw = self.dev.read(EP_STATUS_IN, 64, timeout=1)
                    if len(raw) >= 2:
                        self.last_status = raw[0] | (raw[1] << 8)
                        self.status_reads += 1
                except usb.core.USBTimeoutError:
                    # NON e' un guasto: il timeout e' di 1 ms *apposta*, per non
                    # rubare tempo al lettore dei dati, e lo stato cambia molto
                    # piu' di rado. Andare in timeout e' il caso normale.
                    # Contarlo come fallimento faceva sembrare rotto un canale
                    # perfettamente sano (2026-08-02: "falliti 29" su 30 s).
                    self.status_timeouts += 1
                except Exception:
                    self.status_fails += 1
            try:
                raw = self.dev.read(EP_DATA_IN, 64, timeout=500)
            except usb.core.USBTimeoutError:
                continue
            except Exception:
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue
            self.packets_rx += 1
            if self.raw:
                # Un pacchetto USB puo' spezzare una parola a meta' (stesso
                # motivo per cui il de-framer ha _byte_pending): qui i
                # pacchetti sono sempre di lunghezza pari perche' il firmware
                # scarica multipli di uint16_t, ma il dispari va comunque
                # gestito o si perde l'allineamento per il resto della corsa.
                buf = self._raw_odd + bytes(raw)
                self._raw_odd = buf[len(buf) & ~1:]
                for (w,) in struct.iter_unpack("<H", buf[:len(buf) & ~1]):
                    self.raw_rx += 1
                    self.raw_words.put(w)
                continue
            for ftype, words in self.deframer.feed(bytes(raw)):
                if ftype == SIO_T_EVENT and len(words) == EVENT_WORDS:
                    self.events.put(b"".join(w.to_bytes(2, "little")
                                             for w in words))
                elif ftype == SIO_T_STATE and len(words) == 8:
                    self.state_words = tuple(words)
                    self.state_stamp = time.time()
                    self.state_count += 1
                elif ftype == SIO_T_DIAG and len(words) == 8:
                    self.diag_words = tuple(words)
                    self.diag_stamp = time.time()
                elif ftype == SIO_T_PING:
                    try:
                        self.pings.put_nowait(words)
                    except queue.Full:
                        try:
                            self.pings.get_nowait()   # scarta il piu' vecchio
                            self.pings.put_nowait(words)
                        except queue.Empty:
                            pass
                # Tipi sconosciuti: solo contati dal deframer

    # -- l'API del bridge: 12 byte dentro, 12 byte fuori ------------------

    def send_event(self, event12):
        if len(event12) != 12:
            raise ValueError(f"un NetEvent e' 12 byte, non {len(event12)}")
        words = [w for (w,) in struct.iter_unpack("<H", event12)]
        self.send_frame(SIO_T_EVENT, words)

    def poll_events(self):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def send_frame(self, ftype, words):
        try:
            self.dev.write(EP_DATA_OUT, sio_frame(ftype, words))
            self.frames_tx += 1
        except Exception:
            self.tx_errors += 1
            raise

    # -- API a parola grezza (mb_multi.py) --------------------------------
    #
    # Il multiboot in modo MultiPlay 16 bit (GBATEK, SWI 25h transfer mode 1)
    # ha bisogno di una primitiva sola: manda una parola, leggi la parola con
    # cui risponde il child. E' esattamente cio' che RawRelaySection gia' fa -
    # transmitCallback pesca dalla coda TX, receiveCallback accoda in RX.
    #
    # L'UNICA cosa da sapere di questo canale, e da cui dipende tutto il
    # protocollo sopra: a coda TX VUOTA il firmware manda 0x7FFF e fa lo
    # scambio lo stesso (rawRelaySection.cpp:147). In una sessione di link e'
    # la scelta giusta (il master clocca da solo); in un multiboot e' una
    # parola spuria in mezzo al protocollo. Chi usa questa API deve tenere la
    # coda piena: si accodano blocchi interi, non parole singole.

    def send_words(self, words):
        """Accoda parole a 16 bit. La coda del firmware e' profonda 256
        (K_MSGQ_DEFINE in rawRelaySection.cpp): oltre, il firmware le SCARTA
        contando g_rawRelayTxQueueDrops, che da qui non si vede. Quindi il
        blocco si spezza e chi chiama regola quanto stare avanti."""
        if not self.raw:
            raise RuntimeError("send_words richiede UsbLink(raw=True)")
        data = b"".join((w & 0xFFFF).to_bytes(2, "little") for w in words)
        for i in range(0, len(data), 64):
            self.dev.write(EP_DATA_OUT, data[i:i + 64])
        self.raw_tx += len(words)

    def read_word(self, timeout=2.0):
        """Una parola dal GBA, o None se entro timeout non arriva niente."""
        try:
            return self.raw_words.get(timeout=timeout)
        except queue.Empty:
            return None

    def words_pending(self):
        return self.raw_words.qsize()

    def drain_rx(self, seconds=0.3):
        """Butta via tutto cio' che e' gia' in coda. Serve subito dopo
        StartHandshake: il master comincia a clockare con la coda TX vuota,
        quindi manda 0x7FFF e riceve risposte che non appartengono a niente."""
        fine = time.time() + seconds
        buttate = 0
        while time.time() < fine:
            if self.read_word(timeout=0.05) is None:
                continue
            buttate += 1
        return buttate

    def restart(self):
        """Rifa' la sequenza di setup senza rienumerare il device.

        Serve fra un tentativo di multiboot e l'altro: GBATEK dice che se il
        detect fallisce si aspetta 1/16 s e si RICOMINCIA la sessione, non che
        si continua a insistere. Cancel chiude il modulo e il SetMode che segue
        rifa' la rilevazione del cavo, cioe' riporta il PIO allo stato iniziale.
        Nota: NON si fa dev.reset(), che su un device Zephyr manda in
        rienumerazione e fa andare in timeout i write successivi.
        """
        self._cmd(bytes([CMD_CANCEL]), 0.4, "Cancel (riavvio sessione)")
        self._cmd(bytes([CMD_SET_MODE, MODE_GBA_PASSTHROUGH]), 0.3, "SetMode")
        self._cmd(bytes([CMD_SET_MODE_MASTER]), 0.2, "master")
        if self.cable != "auto":
            self._cmd(bytes([CMD_SET_CABLE_TYPE, CABLE_TYPES[self.cable]]), 0.2,
                      f"cavo forzato: {self.cable}")
        if self.timing is not None:
            self._cmd(bytes([CMD_SET_RAW_TIMING])
                      + int(self.timing).to_bytes(4, "little"), 0.2,
                      f"timing {self.timing}")
        self._cmd(bytes([CMD_START_HANDSHAKE]), 0.2, "StartHandshake")

    def set_timing(self, iterations, label=None):
        """F-1 a caldo. Il firmware applica il valore alla PROSSIMA parola che
        transmitCallback pesca, insieme a quella parola: e' la leva con cui si
        allunga il periodo del master senza fermarlo."""
        self._cmd(bytes([CMD_SET_RAW_TIMING])
                  + int(iterations).to_bytes(4, "little"), 0.0,
                  label or f"timing {iterations}")

    # -- il Cable Club (Consegna D) ---------------------------------------
    #
    # Fra club_enter() e club_exit() il Pico e' nel modo LINK originale di
    # Celio (0x01): fa da controparte del GBA al Cable Club, e sul filo USB
    # passano stati (2 byte su EP_STATUS) e blocchi dati da 64 byte = 32
    # parole (EP_DATA), come fa il client web su celi0.link. La logica di
    # sessione NON sta qui: sta in club_link.ClubSession. Qui solo il
    # trasporto.

    def club_enter(self):
        """Ferma il passthrough e mette il Pico in Online Link Mode."""
        self.close()   # ferma il lettore passthrough
        self.club_statuses = queue.Queue()
        self.club_blocks = queue.Queue()
        # SI LEGGE MENTRE SI SMONTA, NON DOPO (2026-08-17, seconda lezione).
        #
        # La prima versione mandava Cancel, dormiva, mandava SetMode, dormiva,
        # e SOLO POI si metteva a leggere l'endpoint di stato. Ma quel
        # endpoint ha UN buffer solo: sendStatus del firmware aspetta al
        # massimo 100 ms che il precedente sia stato letto e poi sovrascrive
        # (usbLayer.hpp: k_sem_take(&m_statusTransferDone, K_MSEC(100))).
        # Con nessuno in lettura per quasi un secondo, LinkClosed (l'addio
        # del passthrough) restava nel buffer e DeviceReady + AwaitMode - gli
        # stati che ci servono - andavano PERSI. Nel log del campo: `scartati
        # 1 stati residui: 0xFF07` e poi il nulla.
        #
        # Quindi: ogni attesa fra i comandi E' fatta di letture. Cosi' il
        # buffer e' sempre libero e AwaitMode - che LinkModule::execute()
        # manda per primo e una volta sola (module/link.cpp:13) - arriva.
        import usb.core

        def _leggi_stati(durata, fermati_su=None):
            fine = time.time() + durata
            visti = []
            while time.time() < fine:
                try:
                    raw = self.dev.read(EP_STATUS_IN, 64, timeout=100)
                except usb.core.USBTimeoutError:
                    continue
                except Exception:
                    break
                if len(raw) < 2:
                    continue
                st = raw[0] | (raw[1] << 8)
                visti.append(st)
                if fermati_su is not None and st == fermati_su:
                    break
            return visti

        self._cmd(bytes([CMD_CANCEL]), 0.0, "Cancel (fine passthrough)")
        vecchi = _leggi_stati(0.6)
        self._cmd(bytes([CMD_SET_MODE, MODE_ONLINE_LINK]), 0.0,
                  "SetMode onlineLink (modo Celio originale)")
        nuovi = _leggi_stati(3.0, fermati_su=LINK_ST_AWAIT_MODE)

        self.club_ready = bool(nuovi) and nuovi[-1] == LINK_ST_AWAIT_MODE
        if self.club_ready:
            self.last_status = LINK_ST_AWAIT_MODE
            self.club_statuses.put(LINK_ST_AWAIT_MODE)
        if not self.quiet:
            resti = vecchi + (nuovi[:-1] if self.club_ready else nuovi)
            if resti:
                print("[usb ] stati di transizione scartati: %s"
                      % ", ".join("0x%04X" % s for s in resti))
            print("[usb ] %s" % ("AwaitMode: il device e' pronto per il club"
                                 if self.club_ready else
                                 "!!! AwaitMode NON e' arrivato entro 3 s: il "
                                 "device non e' entrato in modo link"))

        self._stop.clear()
        # DUE lettori, uno per endpoint (2026-08-19). Il lettore unico che
        # alternava stato (timeout 1 ms) e dati lasciava l'endpoint dati
        # SENZA una richiesta IN pendente per tutta la durata della lettura
        # di stato - e su Windows quel millisecondo si arrotonda al tick di
        # ~16 ms. In quelle finestre cieche la sendData del firmware resta
        # appesa (back-pressure F-2), il thread di sezione del Pico perde il
        # round successivo (m_receivedCommand e' UN buffer solo) e un comando
        # del gioco sparisce. Lo scambio dei dati giocatore e' 7 comandi
        # SENZA NESSUN RITENTO (link.c: LinkCB_BlockSend): un comando perso =
        # linkup appeso per sempre, in silenzio. Misurato sul campo: master 6
        # comandi su 7, slave 5 su 6, sessione morta alla schermata di
        # conferma. Il client web di Celio non soffre perche' WebUSB tiene
        # una transferIn sempre pendente per endpoint: da oggi anche noi.
        self._reader = threading.Thread(target=self._club_status_loop,
                                        daemon=True, name="usb_link-club-st")
        self._reader2 = threading.Thread(target=self._club_data_loop,
                                         daemon=True, name="usb_link-club-dt")
        self._reader.start()
        self._reader2.start()

    def club_exit(self):
        """Chiude il modo club e rifa' TUTTA la sequenza passthrough
        (SetMode, master, cavo, timing F-1, StartHandshake): al ritorno il
        canale eventi e' quello di sempre."""
        self.close()   # ferma il lettore club
        self.restart()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="usb_link-reader")
        self._reader.start()

    def club_command(self, cmd, label):
        self._cmd(bytes([cmd]), 0.05, label)

    def club_send_block(self, block64):
        """Un blocco dati da 64 byte verso il device (32 parole del link)."""
        try:
            self.dev.write(EP_DATA_OUT, block64)
            return True
        except Exception:
            self.tx_errors += 1
            return False

    def _club_status_loop(self):
        """Il lettore degli STATI del modo club. Thread suo: cosi' la lettura
        e' sempre pendente e nessun LinkStatus va perso (un LinkStatus perso
        e' una sessione che non parte), senza rubare nemmeno un istante
        all'endpoint dati, che ha il suo thread gemello qui sotto."""
        import usb.core
        while not self._stop.is_set():
            try:
                raw = self.dev.read(EP_STATUS_IN, 64, timeout=250)
                if len(raw) >= 2:
                    st = raw[0] | (raw[1] << 8)
                    self.last_status = st
                    self.status_reads += 1
                    self.club_statuses.put(st)
            except usb.core.USBTimeoutError:
                self.status_timeouts += 1
            except Exception:
                if self._stop.is_set():
                    return
                self.status_fails += 1
                time.sleep(0.05)

    def _club_data_loop(self):
        """Il lettore dei DATI del modo club: non fa ALTRO che leggere.
        Come il ciclo caldo del passthrough, e per lo stesso motivo: ogni
        istante senza una richiesta IN pendente e' un istante in cui la
        sendData del firmware (back-pressure F-2) tiene fermo il thread di
        sezione del Pico, che nel frattempo perde i comandi del GBA."""
        import usb.core
        while not self._stop.is_set():
            try:
                raw = self.dev.read(EP_DATA_IN, 64, timeout=250)
            except usb.core.USBTimeoutError:
                continue
            except Exception:
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue
            if len(raw) == 64:
                self.packets_rx += 1
                self.club_blocks.put(bytes(raw))

    def stats(self):
        """Una riga sola, e dentro c'e' il numero che decide tutto: parole/s.

        Va confrontato con la riga 0 del GBA. Se il GBA ne manda 126 e qui ne
        arrivano 75, il canale sta perdendo il 40% e nessun frame sopravvive:
        e' esattamente il difetto misurato il 2026-08-02, e senza questo
        confronto lo si legge solo come 'errori che salgono', senza capire da
        quale lato guardare.
        """
        d = self.deframer
        adesso = time.time()
        if self._rate_since is None:
            rate = "?"
            self._rate_since = adesso
            self._rate_words = d.words_rx
        else:
            trascorso = adesso - self._rate_since
            if trascorso < 0.5:
                # Due chiamate ravvicinate darebbero un rate di rumore (il
                # "372 parole/s" del riassunto del primo T-4 severo): sotto
                # il mezzo secondo si tiene la finestra aperta.
                rate = "~"
            else:
                rate = "%.0f" % ((d.words_rx - self._rate_words) / trascorso)
                self._rate_since = adesso
                self._rate_words = d.words_rx
        return (f"tx {self.frames_tx} frame ({self.tx_errors} errori) | "
                f"rx {d.frames_rx} frame, {rate} parole/s ({d.words_rx} tot, "
                f"{self.packets_rx} pacchetti), {d.frame_err} errori, "
                f"{d.resync} resync")

    def diagnosi_linea(self):
        """Che cosa c'e' sul filo quando NON arrivano frame. Serve a separare
        'nessuno risponde' da 'il pin e' sbagliato', che nel riassunto sono
        tutti e due 'rx 0 frame' e si confondono."""
        d = self.deframer
        if not d.hist:
            return "  linea                  : nessuna parola ricevuta"
        top = sorted(d.hist.items(), key=lambda kv: -kv[1])[:4]
        tot = sum(d.hist.values())
        elenco = ", ".join(f"0x{w:04X} x{n} ({100.0 * n / tot:.0f}%)" for w, n in top)
        primo, quante = top[0]
        if primo in (0xFFFF, SIO_IDLE) and quante > tot * 0.9:
            verdetto = ("linea ALTA e viva, ma nessuno risponde: il partner sul "
                        "GBA non sta trasmettendo (banco non in esecuzione, "
                        "oppure il gioco senza payload). NON e' il pin.")
        elif primo == 0x0000 and quante > tot * 0.9:
            verdetto = ("linea tenuta a MASSA: SD e' sul pin sbagliato, oppure "
                        "il cavo non porta quel filo.")
        else:
            verdetto = ("qualcuno parla ma il framing non torna: timing, "
                        "oppure SD su un pin che raccoglie rumore.")
        return (f"  parole fuori frame     : {elenco}\n"
                f"  -> {verdetto}")


def ascolta(args):
    """Bring-up del payload sul GBA FISICO: si sta zitti e si guarda se il
    gioco parla. Nessun ping, nessuna aspettativa di eco.

    Perche' esiste separato da --selftest: il banco di prova risponde ai ping,
    il payload dentro Smeraldo no - manda NetEvent per conto suo. Con
    --selftest un payload perfettamente funzionante darebbe 'echi tornati: 0',
    cioe' la riga che finora abbiamo letto come fallimento."""
    try:
        from protocol import describe_event
    except ImportError:
        describe_event = None

    link = UsbLink(timing=args.timing, cable=args.cable).open()
    eventi = 0
    fine = time.time() + args.ascolta
    ultimo = 0.0
    print(f"\nin ascolto per {args.ascolta:.0f} s. CAMMINA nell'overworld: il payload "
          "manda un PASSO per ogni tile\ne un SYNC circa al secondo. Se resti fermo "
          "devono arrivare comunque i SYNC.\n")
    try:
        while time.time() < fine:
            for ev in link.poll_events():
                eventi += 1
                testo = describe_event(ev) if describe_event else ev.hex()
                print(f"  [{eventi:4d}] {testo}")
            adesso = time.time()
            if adesso - ultimo >= 2.0:
                ultimo = adesso
                # Lo stato viene da EP 0x81, cioe' da un percorso USB DIVERSO
                # da quello dei dati (EP 0x82). E' l'unico modo di separare due
                # guasti che nel riassunto sono identici: se lo stato continua
                # ad aggiornarsi mentre i pacchetti dati sono fermi, l'USB e'
                # vivo e si e' fermato il canale (master o PIO); se si ferma
                # anche lo stato, e' l'endpoint del Pico.
                st = link.last_status
                st_txt = "mai letto" if st is None else f"0x{st:04X}"
                errori = (f", {link.status_fails} errori USB"
                          if link.status_fails else "")
                print(f"        ... {link.stats()} | status {st_txt}{errori}")
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\ninterrotto")
    finally:
        link.close()

    d = link.deframer
    print("\nriassunto dell'ascolto:")
    print(f"  eventi dal GBA         : {eventi}")
    print(f"  {link.stats()}")
    # Lo stato serve a UNA cosa sola: dire se l'USB era vivo mentre i dati non
    # arrivavano. Quindi si stampa solo cio' che discrimina - le letture
    # riuscite e gli errori VERI. I timeout sono il caso normale (1 ms) e si
    # stampano solo se non e' mai riuscita nessuna lettura.
    if link.status_reads == 0:
        print(f"  stato (EP 0x81)        : MAI letto in {link.status_timeouts} "
              "tentativi -> l'endpoint di stato non risponde")
    else:
        print(f"  stato (EP 0x81)        : 0x{link.last_status:04X}, "
              f"{link.status_reads} letture riuscite"
              + (f", {link.status_fails} errori USB" if link.status_fails
                 else ""))
    # Il payload manda un SYNC circa AL SECONDO anche da fermo: su una sessione
    # di N secondi ci si aspettano ~N eventi. "Almeno uno" non e' un criterio -
    # il 2026-08-02 ha scritto "bring-up passato" su UN evento in 30 s, cioe'
    # su un payload che aveva parlato una volta e poi era ammutolito. Un
    # criterio che passa quando non deve e' peggio di nessun criterio.
    attesi = max(3, int(args.ascolta * 0.5))
    if 0 < eventi < attesi:
        print(f"  -> ⚠ IL PAYLOAD SI E' ZITTITO: {eventi} eventi in "
              f"{args.ascolta:.0f} s, ne servivano almeno {attesi} (il SYNC "
              "parte ~1/s anche da fermo).")
        print("     Ha parlato e poi ha smesso: NON e' un problema di cavo o "
              "di pin.\n"
              "     Da guardare, in ordine: sei rimasto nell'overworld? il "
              "gioco gira\n"
              "     ancora normalmente (audio, animazioni)? Se l'audio e' "
              "distorto,\n"
              "     le due cose sono probabilmente lo stesso difetto.")
        print(link.diagnosi_linea())
    elif eventi >= attesi and d.frame_err == 0:
        print("  -> IL PAYLOAD PARLA, e il canale e' integro: bring-up passato")
    elif eventi > 0:
        print("  -> il payload parla ma il canale perde: guarda errori/resync")
    else:
        print("  -> nessun evento. Che cosa e' arrivato, invece:")
        print(link.diagnosi_linea())
        print("  Se dice 'nessuno risponde': il payload non sta trasmettendo.\n"
              "  Controlla di essere NELL'OVERWORLD (nei menu e in lotta il\n"
              "  payload molla la porta seriale apposta) e che il gioco sia\n"
              "  partito dallo stub multiboot, non da un'accensione normale.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", type=float, default=None, metavar="SECONDI",
                    help="manda un PING al secondo e conta gli echi del banco "
                         "di prova: e' il collaudo del transport, contatori "
                         "alla mano")
    ap.add_argument("--timing", type=int, default=None, metavar="ITER",
                    help="periodo del master in iterazioni PIO (F-1): "
                         "15370=8.3ms, 7400=4ms, 3700=2ms, 1850=1ms")
    ap.add_argument("--cable", choices=sorted(CABLE_TYPES), default="auto",
                    metavar="TIPO",
                    help="F-3: forza il cablaggio invece di lasciarlo indovinare "
                         "al firmware. 'gba' = SD su GP3 — E' QUESTO che serve "
                         "col cavo DMG/GBC su questa board, dove GP4 non e' "
                         "cablato e l'autodetect sbaglia in silenzio")
    ap.add_argument("--ascolta", type=float, default=None, metavar="SECONDI",
                    help="NON manda niente: sta in ascolto e stampa gli eventi "
                         "che arrivano dal GBA. E' il bring-up del payload sul "
                         "fisico: il payload NON fa eco ai ping (quello lo fa il "
                         "banco di prova), quindi --selftest direbbe 'nessun eco' "
                         "anche funzionando perfettamente")
    ap.add_argument("--riavvia", action="store_true",
                    help="F-4: riavvia il Pico via software e aspetta che "
                         "rienumeri. E' lo scollega/ricollega senza toccare il "
                         "filo (firmware >= 2.0.5)")
    args = ap.parse_args()

    if args.riavvia:
        print("[usb ] riavvio del Pico (F-4)...")
        sparito, ricomparso = riavvia_pico()
        if ricomparso:
            print("[usb ] sparito e ricomparso: riavvio avvenuto, canale pulito")
            return 0
        if not sparito:
            print("[usb ] il Pico NON e' mai sparito dal bus: il firmware non "
                  "ha la F-4.\n[usb ] Flasha hw/firmware/celio-f1f2b-f3-f4.uf2 "
                  "(BOOTSEL), oppure scollega\n[usb ] e ricollega a mano.")
        else:
            print("[usb ] sparito ma MAI ricomparso entro 10 s: scollega e "
                  "ricollega a mano.")
        return 1

    if args.selftest is None and args.ascolta is None:
        ap.error("serve --selftest (banco di prova) oppure --ascolta (payload nel gioco)")
    if args.selftest is not None and args.ascolta is not None:
        ap.error("--selftest e --ascolta si escludono: uno parla, l'altro sta zitto")

    if args.ascolta is not None:
        return ascolta(args)

    link = UsbLink(timing=args.timing, cable=args.cable).open()
    sent = 0
    echoed = 0
    seen_gba_pings = 0
    strani = 0
    in_attesa = set()      # i seq spediti e non ancora tornati: l'eco puo'
                           # arrivare anche DOPO la finestra del suo secondo
    fine = time.time() + args.selftest
    try:
        while time.time() < fine:
            seq = 0x1000 | (sent & 0x0FFF)
            link.send_frame(SIO_T_PING, [seq, 0xBEEF])
            in_attesa.add(seq)
            sent += 1
            time.sleep(1.0)
            while True:
                try:
                    words = link.pings.get_nowait()
                except queue.Empty:
                    break
                # I due tipi di PING si distinguono senza ambiguita' dalla
                # LUNGHEZZA: il nostro eco ha 2 parole [seq, 0xBEEF], il ping
                # autonomo del banco ne ha 4 [hi, lo, 0x1234, 0xABCD]. Il primo
                # selftest li distingueva solo dal seq DELL'ULTIMO secondo, e
                # un eco in ritardo finiva contato come "autonomo": due echi
                # dati per persi che persi non erano (misurato: 18/20 con
                # 0 errori di frame, cioe' canale integro).
                if len(words) == 4:
                    seen_gba_pings += 1
                elif len(words) == 2 and words[0] in in_attesa:
                    in_attesa.discard(words[0])
                    echoed += 1
                    print(f"  eco   0x{words[0]:04x} tornato ({link.stats()})")
                else:
                    strani += 1
    finally:
        link.close()

    print("\nriassunto del selftest:")
    print(f"  ping spediti           : {sent}")
    print(f"  echi tornati           : {echoed}")
    print(f"  ping autonomi del GBA  : {seen_gba_pings}")
    if strani:
        print(f"  frame ping non attesi  : {strani}")
    print(f"  {link.stats()}")
    d = link.deframer
    if d.frame_err == 0 and echoed == sent and sent > 0:
        print("  -> canale integro e TUTTI gli echi tornati: transport a posto")
    elif d.frame_err == 0 and echoed:
        print(f"  -> CANALE INTEGRO (0 errori di frame): i {sent - echoed} echi"
              " mancanti sono collisioni sul TX del GBA (buffer singolo, il suo"
              " ping e l'eco si contendono lo slot: txFull sul banco), non"
              " perdite del canale")
    elif echoed:
        print("  -> echi parziali E errori di frame: il canale perde, guarda"
              " parole/s contro la riga 0 del GBA")
    else:
        # "nessun eco" ha almeno tre cause diverse e da solo non ne distingue
        # nessuna: qui si guarda che cosa c'era davvero sul filo.
        print("  -> nessun eco. Che cosa e' arrivato, invece:")
        print(link.diagnosi_linea())


if __name__ == "__main__":
    main()
