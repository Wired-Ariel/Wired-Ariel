"""
relay.py - il router degli eventi. Solo stdlib: deve poter girare su un VPS
senza installare niente.

Cosa fa: tiene un elenco di peer, li raggruppa in STANZE (la stanza e' l'ID
mappa, come da CLAUDE.md) e inoltra ogni evento agli altri membri della stessa
stanza. Non guarda dentro i 12 byte dell'evento.

Cosa NON fa, deliberatamente:
  - non deduplica e non riordina: lo fa il client, che e' l'unico che deve
    consegnare in ordine al gioco. Qui servirebbe tenere stato per mittente e
    non aggiungerebbe niente;
  - non conosce il protocollo di gioco. Se domani l'evento diventa di 16 byte,
    il relay non cambia.

Uso:
    python relay.py [--port 9000] [--timeout 10] [--verbose]
"""

import argparse
import socket
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

from protocol import (  # noqa: E402
    HEADER_SIZE, T_BYE, T_CLUB, T_EVENT, T_HELLO, T_PING, T_PONG, T_WATCH,
    TYPE_NAMES, pack, unpack,
)


class Peer:
    def __init__(self, addr, peer_id):
        self.addr = addr
        self.peer_id = peer_id
        self.room = None
        self.last_seen = time.monotonic()
        self.alone_since = self.last_seen
        self.rx = 0
        self.tx = 0
        # Stanza per cui questo peer e' gia' stato rifiutato (tetto dei 4
        # giocatori): serve a dirlo nel log una volta, non a ogni evento.
        self.refused_room = None
        # SPETTATORE (2026-08-26, T_WATCH): riceve i broadcast della stanza ma
        # non conta nel tetto, non manda eventi e la sua uscita non produce un
        # T_BYE. Il ruolo e' dell'INDIRIZZO, non del peer_id: lo stesso numero
        # puo' stare in stanza due volte - da giocatore col client Lua e da
        # spettatore col browser - ed e' esattamente il caso d'uso (uno guarda
        # sulla mappa il proprio avatar). Vedi il rientro-NAT piu' sotto.
        self.observer = False
        # Eventi rifiutati perche' arrivati da uno spettatore: si dice una
        # volta sola, come per refused_room.
        self.event_warned = False
        # L'ultimo tipo di pacchetto ricevuto: lo stampa lo sfratto per
        # silenzio, dove "chi taceva dopo un EVENT" e "chi taceva dopo un
        # PING" sono due guasti diversi (vedi sweep).
        self.last_kind = None


class Relay:
    def __init__(self, port, timeout, verbose):
        self.timeout = timeout
        self.verbose = verbose
        self.peers = {}      # addr -> Peer
        self.rooms = {}      # room_id -> set(addr)
        self.seq = 0
        self.rebinds = 0     # rientri in stanza da un indirizzo nuovo (NAT)
        self.eventi_da_spettatori = 0   # DEVE restare 0: vedi il ramo T_EVENT
        # Il ping-pong dei peer-id duplicati: vedi _forse_ping_pong.
        self._rientri = {}   # peer_id -> [istanti degli ultimi rientri]
        self._ping_pong_detto = set()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # NIENTE SO_REUSEADDR: su Windows vuol dire "ruba la porta a chi ce l'ha
        # gia'", e due relay accesi per sbaglio si spartirebbero i pacchetti in
        # silenzio. Su UDP non serviva comunque (il TIME_WAIT non esiste).
        # SO_EXCLUSIVEADDRUSE serve perche' su Windows togliere SO_REUSEADDR non
        # basta: un bind su un indirizzo specifico riesce lo stesso sopra un
        # bind wildcard sulla stessa porta.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            self.sock.bind(("", port))
        except OSError as exc:
            raise SystemExit(
                "[relay] porta UDP %d gia' occupata (%s): c'e' gia' un relay acceso?"
                % (port, exc))
        self.sock.settimeout(0.5)
        self.log("in ascolto su UDP %d, timeout peer %ds" % (port, timeout))

    def log(self, msg):
        print("[relay] %s" % msg, flush=True)

    # --- stanze -----------------------------------------------------------

    def giocatori_in(self, room_id):
        """Quanti GIOCATORI (non spettatori) ci sono in una stanza.

        Sta in un posto solo perche' i lettori sono due - il tetto dei 4 e
        l'avviso "e' solo nella stanza" - e sono esattamente il tipo di
        predicato condiviso che, sdoppiato, si scorda di essere aggiornato
        (regola del progetto: quando si tocca un predicato condiviso, si
        controlla chi altro lo legge)."""
        n = 0
        for addr in self.rooms.get(room_id, ()):
            other = self.peers.get(addr)
            if other is not None and not other.observer:
                n += 1
        return n

    def _forse_ping_pong(self, peer, other_addr):
        """DUE CLIENT CON LO STESSO PEER-ID si espellono a vicenda, per sempre.

        Il rientro qui sopra e' pensato per il NAT: stesso giocatore, porta
        nuova. Ma se DUE persone diverse partono con lo stesso peer-id - ed e'
        facilissimo, i .bat avevano `--peer-id 1` scritto dentro - allora ogni
        battito dell'uno butta fuori l'altro, un secondo dopo il contrario, e
        nessuno dei due vede mai niente. Il sintomo ("non ci troviamo") e'
        identico a dieci altre cause e non c'e' nessun errore da nessuna parte.

        La firma che distingue i due casi e' l'ALTERNANZA: un rebinding NAT
        vero e' un evento isolato, il conflitto rimbalza fra due indirizzi
        piu' volte in pochi secondi. Si dice UNA volta per peer: e' una
        diagnosi, non un allarme da ripetere.
        """
        adesso = time.monotonic()
        storia = self._rientri.setdefault(peer.peer_id, [])
        storia.append((adesso, other_addr, peer.addr))
        del storia[:-6]
        recenti = [r for r in storia if adesso - r[0] <= 10.0]
        if len(recenti) < 3 or peer.peer_id in self._ping_pong_detto:
            return
        # Alternanza vera: fra i rientri recenti compaiono al massimo due
        # indirizzi distinti, e ciascuno e' stato sia quello che entra sia
        # quello che viene buttato fuori.
        indirizzi = set()
        for _, vecchio, nuovo in recenti:
            indirizzi.add(vecchio)
            indirizzi.add(nuovo)
        if len(indirizzi) != 2:
            return
        self._ping_pong_detto.add(peer.peer_id)
        a, b = sorted(indirizzi)
        self.log("!!! peer %d RIMBALZA fra %s:%d e %s:%d (%d rientri in 10 s): "
                 "DUE CLIENT CON LO STESSO PEER-ID? Si buttano fuori a "
                 "vicenda a ogni battito e non si vedranno MAI. Dai a ognuno "
                 "un --peer-id diverso."
                 % (peer.peer_id, a[0], a[1], b[0], b[1], len(recenti)))

    def move_to_room(self, peer, room_id):
        """Sposta un peer di stanza, avvisando chi lascia.

        E' il caso normale quando un giocatore cambia mappa: i vecchi compagni
        devono sapere che se n'e' andato, altrimenti resterebbero ad aspettare
        eventi che non arriveranno mai.

        La stanza 0 non esiste: e' il valore che il client usa finche' non ha
        visto il primo evento e non sa ancora su che mappa siamo. Mettercisi
        creerebbe una stanza fittizia in cui due giocatori si "vedono" senza
        essere davvero sulla stessa mappa.
        """
        if room_id == 0 or peer.room == room_id:
            return

        if peer.room is not None:
            self.leave_room(peer, notify=True)

        members = self.rooms.setdefault(room_id, set())

        # RIENTRO DA UN INDIRIZZO NUOVO (2026-08-02, per la partita via
        # internet). Su internet il NAT di casa puo' riciclare la porta
        # sorgente: stesso giocatore, indirizzo diverso. Per il relay, che
        # indicizza per (IP, porta), e' un peer nuovo - e quello VECCHIO
        # resterebbe nella stanza fino al timeout, con broadcast sprecati verso
        # un indirizzo morto e il conteggio dei membri sballato. Qui si
        # riconosce il caso (stesso peer_id, altro indirizzo, stessa stanza),
        # si rimuove il fantasma SENZA T_BYE - il giocatore non se n'e' mai
        # andato, un BYE farebbe despawnare il suo avatar per niente - e lo si
        # GRIDA nel log: un rebinding e' un fatto della rete che va visto,
        # non un dettaglio da lasciar scadere in silenzio.
        for other_addr in list(members):
            other = self.peers.get(other_addr)
            # ...MA SOLO FRA PARI RUOLO (2026-08-26). Chi gioca in emulatore
            # apre il sito da spettatore, e la cosa piu' naturale del mondo e'
            # che ci metta lo stesso numero di peer del suo client Lua. Senza
            # questo controllo lo spettatore espellerebbe il proprio giocatore
            # (e il PING del giocatore, un attimo dopo, espellerebbe lo
            # spettatore): i due si butterebbero fuori a vicenda per sempre.
            if (other is not None and other.peer_id == peer.peer_id
                    and other.observer == peer.observer):
                members.discard(other_addr)
                self.peers.pop(other_addr, None)
                self.rebinds += 1
                self.log("peer %d RIENTRA da %s:%d (prima %s:%d): rebinding "
                         "NAT o riavvio del client - rientro n. %d"
                         % (peer.peer_id, peer.addr[0], peer.addr[1],
                            other_addr[0], other_addr[1], self.rebinds))
                self._forse_ping_pong(peer, other_addr)

        # IL TETTO DELLA STANZA: 4 GIOCATORI (2026-08-25). Non e' un limite
        # del relay - per lui le stanze restano id opachi - ma del GIOCO: il
        # payload disegna al massimo 3 remoti (16 object event per mappa, slot
        # 0 il giocatore, il resto agli NPC), e un quinto peer produrrebbe
        # solo traffico che nessuno puo' disegnare. Il quinto resta FUORI
        # dalla stanza (i suoi eventi non vengono inoltrati e non riceve
        # niente) e il suo client lo mostra col contatore `ricevuti` fermo;
        # qui lo si dice nel log UNA volta per peer (a transizione: ogni suo
        # evento riprova, e un rigo ogni decimo di secondo sarebbe rumore).
        # Il rientro NAT non c'entra: lo stesso peer_id e' gia' stato
        # ripulito dal ciclo qui sopra.
        #
        # GLI SPETTATORI NON CONTANO (2026-08-26). Il conteggio si fa OGNI
        # VOLTA sui membri, invece di fidarsi di un numero deciso all'ingresso:
        # e' cio' che rende il tetto autoriparante dopo un riavvio del relay.
        # Li' lo stato si perde, e il primo pacchetto di uno spettatore e'
        # quasi sempre il suo PING (1/s) invece del WATCH (1/5s): per qualche
        # secondo rientra come giocatore e occupa un posto. Col conteggio
        # dinamico, appena arriva il WATCH il posto torna libero da solo, e il
        # quinto giocatore vero entra al suo tentativo successivo.
        if not peer.observer and self.giocatori_in(room_id) >= 4:
            if peer.refused_room != room_id:
                peer.refused_room = room_id
                self.log("stanza %d PIENA (4 giocatori): peer %d resta fuori"
                         % (room_id, peer.peer_id))
            return
        peer.refused_room = None
        peer.room = room_id
        members.add(peer.addr)
        # La stanza e' la PARTITA, non la mappa (decisione del 2026-07-30): stamparla
        # come "gruppo.numero" era un residuo dell'epoca in cui era un ID mappa, e
        # faceva leggere "stanza 0.1" dove il client dice "stanza 1".
        giocatori = self.giocatori_in(room_id)
        spettatori = len(members) - giocatori
        if peer.observer:
            self.log("peer %d GUARDA la stanza %d (spettatore; %d giocatori "
                     "+ %d spettatori)"
                     % (peer.peer_id, room_id, giocatori, spettatori))
        else:
            self.log("peer %d entra nella stanza %d (%d giocatori%s)"
                     % (peer.peer_id, room_id, giocatori,
                        (" + %d spettatori" % spettatori) if spettatori else ""))

    def leave_room(self, peer, notify):
        members = self.rooms.get(peer.room)
        if members is None:
            return

        members.discard(peer.addr)
        # NIENTE T_BYE PER UNO SPETTATORE: non e' mai stato nel gioco degli
        # altri, quindi non c'e' nessun avatar da far sparire, e il client che
        # lo ricevesse stamperebbe un despawn per un peer che non ha mai
        # camminato. La guardia sta QUI e non nei chiamanti perche' cosi'
        # copre in un colpo tutte e tre le uscite: il timeout dello sweep, il
        # cambio stanza, e il T_BYE che relay_ws sintetizza alla chiusura
        # della scheda - che per uno spettatore browser e' l'uscita NORMALE.
        if notify and not peer.observer:
            self.broadcast(peer, pack(T_BYE, peer.peer_id, peer.room or 0, 0))
        if not members:
            del self.rooms[peer.room]

    def broadcast(self, sender, datagram):
        """A tutti i membri della stanza tranne il mittente."""
        for addr in self.rooms.get(sender.room, ()):
            if addr == sender.addr:
                continue
            try:
                self.sock.sendto(datagram, addr)
                sender.tx += 1
            except OSError as exc:
                self.log("invio a %s fallito: %s" % (addr, exc))

    # --- ciclo principale -------------------------------------------------

    def handle(self, data, addr):
        parsed = unpack(data)
        if parsed is None:
            # Su UDP arriva di tutto. Si ignora e si tira avanti.
            return

        kind, peer_id, room_id, seq, body = parsed

        peer = self.peers.get(addr)
        if peer is None:
            peer = Peer(addr, peer_id)
            self.peers[addr] = peer
            self.log("nuovo peer %d da %s:%d" % (peer_id, addr[0], addr[1]))

        peer.last_seen = time.monotonic()
        peer.peer_id = peer_id
        peer.last_kind = kind     # per il log dello sfratto: vedi sweep()
        peer.rx += 1

        if kind == T_PING:
            # ANCHE IL PING RIMETTE IN STANZA (2026-08-02). Prima lo facevano
            # solo T_HELLO e T_EVENT: dopo un rebinding NAT il peer "nuovo"
            # restava fuori dalla stanza finche' il gioco non emetteva
            # qualcosa - da fermo, fino al prossimo SYNC. Il PING parte 1 al
            # secondo INCONDIZIONATAMENTE e porta gia' la stanza nell'header:
            # e' il battito giusto a cui agganciare il rientro, e rende il
            # buco impossibile per costruzione invece che raro per fortuna.
            self.move_to_room(peer, room_id)
            # Si rimanda indietro il corpo intatto: dentro c'e' il timestamp del
            # client, che e' l'unico a doverlo interpretare.
            self.sock.sendto(pack(T_PONG, peer_id, room_id, seq, body), addr)
            return

        if kind == T_HELLO:
            # Il verbo dichiara il ruolo, sempre: chi saluta e' un GIOCATORE.
            # Cosi' una pagina che passa da "guarda soltanto" a partita vera
            # (stesso NAT, stessa porta) torna giocatore subito, invece di
            # restare spettatore fino al timeout.
            peer.observer = False
            self.move_to_room(peer, room_id)
            return

        if kind == T_WATCH:
            # "VOGLIO GUARDARE" (2026-08-26): entra in stanza come l'HELLO, ma
            # da spettatore - riceve i broadcast, non conta nel tetto dei 4.
            # Il flag si alza PRIMA di move_to_room, che lo legge sia per il
            # tetto sia per il rientro-NAT.
            peer.observer = True
            self.move_to_room(peer, room_id)
            return

        if kind == T_EVENT or kind == T_CLUB:
            # T_CLUB (Cable Club, Consegna D): stesso trattamento di un evento.
            # Il relay non guarda dentro nemmeno qui: la sessione del club vive
            # nei client, come da disciplina (stanza = partita e basta).
            self.move_to_room(peer, room_id)
            if peer.observer:
                # UNO SPETTATORE NON PARLA. Il bridge del sito in modo
                # spettatore non ha device e non produce eventi, quindi questo
                # contatore DEVE restare a zero: e' una guardia contro un
                # client rotto o vecchio, ed e' proprio per questo che quando
                # sale lo si dice invece di sommarlo in silenzio.
                self.eventi_da_spettatori += 1
                if not peer.event_warned:
                    peer.event_warned = True
                    self.log("peer %d e' SPETTATORE ma manda eventi: non li "
                             "inoltro (client vecchio o rotto?)" % peer.peer_id)
                return
            self.broadcast(peer, data)
            if self.verbose:
                # La stanza e' un numero, NON una mappa: stamparla come "%d.%d"
                # la faceva leggere come group.number (4242 -> "16.146") e
                # il 2026-08-02 e' costato mezz'ora di sospetto sulla stanza
                # sbagliata, mentre era giusta. Formato uguale a quello che
                # l'utente scrive in --room.
                self.log("peer %d -> stanza %d, %d byte"
                         % (peer_id, room_id, len(body)))
            return

        if kind == T_BYE:
            self.drop(peer, "ha salutato")
            return

        self.log("tipo sconosciuto %s da peer %d"
                 % (TYPE_NAMES.get(kind, kind), peer_id))

    def drop(self, peer, reason):
        self.leave_room(peer, notify=True)
        self.peers.pop(peer.addr, None)
        self.log("peer %d rimosso (%s), rx %d tx %d"
                 % (peer.peer_id, reason, peer.rx, peer.tx))

    def sweep(self):
        now = time.monotonic()
        for peer in list(self.peers.values()):
            if now - peer.last_seen > self.timeout:
                # QUALE pacchetto e' stato l'ultimo, non solo "silenzio"
                # (2026-08-30). Sfrattare chi taceva dopo un EVENT e sfrattare
                # chi taceva dopo un PING sono due guasti diversi: il primo e'
                # un client che ha smesso di battere (pagina strozzata, o
                # adattatore staccato), il secondo e' rete che si e' chiusa
                # sotto. Nel registro del va-e-vieni del 30/08 questa riga non
                # c'era, e quella distinzione e' rimasta indovinabile per ore.
                ultimo = TYPE_NAMES.get(peer.last_kind, "niente")
                self.drop(peer, "silenzio da %ds (ultimo pacchetto: %s)"
                          % (self.timeout, ultimo))
                continue

            # "tx 0" nei log del primo test voleva dire esattamente questo, ma
            # bisognava saperlo leggere. Meglio dirlo.
            # I GIOCATORI, non i membri (2026-08-26): con un giocatore e il
            # suo spettatore i membri sono due, e questo avviso - che serve a
            # dire "l'altro GIOCATORE non c'e'" - sparirebbe proprio nel caso
            # d'uso nuovo (chi gioca in emulatore e si guarda sulla mappa).
            # Per uno spettatore l'avviso non ha senso e non si stampa: e'
            # normale che guardi una stanza con un solo giocatore.
            if (peer.room is not None and not peer.observer
                    and self.giocatori_in(peer.room) == 1):
                if now - peer.alone_since > 5.0:
                    peer.alone_since = now
                    self.log("peer %d e' SOLO nella stanza %d: l'altro non e' "
                             "collegato, o e' su un'altra mappa"
                             % (peer.peer_id, peer.room))
            else:
                peer.alone_since = now

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                self.sweep()
                continue
            except KeyboardInterrupt:
                self.log("chiusura")
                return
            except ConnectionResetError:
                # Su Windows, quando un peer chiude, l'ICMP "porta non
                # raggiungibile" torna come eccezione su recvfrom di un socket
                # UDP. Senza questo ramo il relay muore appena un giocatore
                # chiude l'emulatore, portandosi dietro la partita di tutti gli
                # altri. Trovato dalla prova end-to-end, non in produzione.
                continue
            except OSError as exc:
                self.log("errore di ricezione: %r" % exc)
                self.sweep()
                continue

            try:
                self.handle(data, addr)
            except Exception as exc:   # un peer malformato non deve fermare tutto
                self.log("errore gestendo %s: %r" % (addr, exc))

            self.sweep()


def main():
    ap = argparse.ArgumentParser(description="Relay per l'overworld link")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--timeout", type=int, default=10,
                    help="secondi di silenzio dopo cui un peer viene tolto")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        Relay(args.port, args.timeout, args.verbose).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
