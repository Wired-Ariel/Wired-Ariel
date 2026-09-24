#!/usr/bin/env python3
"""
test_ruoli_club.py - i ruoli del club combaciano fra sito, pannello ed emulatore.

PERCHE' ESISTE (2026-08-27)
---------------------------
Nel Cable Club ci sono DUE scale di ruoli, e sono invertite:

  * il ruolo del DEVICE (chi comanda il Pico: master o slave del cavo). Lo
    decidono `ruoloMaster` in web/js/club.js e la stessa regola in
    net/club_link.py, guardando i peer-id.
  * il ruolo nel GIOCO. Il device master rende il suo GBA lo SLAVE del gioco;
    il device slave rende il suo GBA il MASTER.

L'emulatore non ha un device: il suo Lua (mgba/club_lua.lua) deve dedurre il
proprio ruolo nel gioco da quello del DEVICE dell'amico, con la formula vista
dalla parte dell'amico. Sbagliarla significa due slave che si aspettano per
sempre - o due master che parlano insieme.

Questo test rifa' le due funzioni in Python e pretende che per OGNI coppia di
peer-id ci sia esattamente UN master del gioco. Il sito sorteggia i peer
(47001 e simili), quindi le coppie strane sono la norma, non l'eccezione.

IL TERZO ATTORE, che fino al 2026-08-28 mancava. L'intestazione qui sopra
diceva "la stessa regola in net/club_link.py", e non era vero: il client
Python decideva `master = (peer_id == 1)`, cioe' guardando solo il proprio
numero. Con una partita a 3-4, o con un peer scelto a mano, quel lato era
SEMPRE slave e l'amico poteva concludere slave anche lui: nessuno apriva le
danze. Adesso `ruolo_master` esiste davvero in club_link.py e questo test la
importa e la confronta con le altre due invece di fidarsi del commento.

    python tools/test_ruoli_club.py
"""

import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "net"))
from club_link import ruolo_master as ruolo_master_python  # noqa: E402


def ruolo_master_device(mio, suo):
    """web/js/club.js:196 (ruoloMaster) - true = IO sono device master."""
    if mio == 1:
        return True
    if mio == 2:
        return False
    if suo is None:
        return None
    if suo == 1:
        return False
    if suo == 2:
        return True
    return mio < suo


def lua_e_master_del_gioco(mio, suo):
    """mgba/club_lua.lua (partnerEDeviceMaster): l'emulatore fa il master del
    gioco se e solo se l'AMICO e' device master."""
    if suo == 1:
        return True
    if suo == 2:
        return False
    if mio == 1:
        return False
    if mio == 2:
        return True
    return suo < mio


def main():
    peers = [1, 2, 3, 7, 47001, 51002, 65001]
    errori = []
    provate = 0

    for lua, sito in itertools.permutations(peers, 2):
        provate += 1
        sito_device_master = ruolo_master_device(sito, lua)
        # Il GBA del sito e' master del gioco quando il suo device e' SLAVE.
        sito_gioco_master = not sito_device_master
        lua_gioco_master = lua_e_master_del_gioco(lua, sito)

        if lua_gioco_master == sito_gioco_master:
            errori.append(
                "peer lua %d / sito %d: sono tutti e due %s del gioco"
                % (lua, sito, "MASTER" if lua_gioco_master else "slave"))

    # E la simmetria: la formula del Lua DEVE essere quella del sito vista
    # dall'altra parte. Se un giorno cambia una delle due, qui si rompe.
    for a, b in itertools.permutations(peers, 2):
        provate += 1
        if lua_e_master_del_gioco(a, b) != ruolo_master_device(b, a):
            errori.append("formule divergenti per la coppia %d/%d" % (a, b))

    # IL PANNELLO PYTHON, con la funzione VERA importata da club_link.py: deve
    # dare lo stesso verdetto del sito, coppia per coppia, e due pannelli
    # Python fra loro devono risultare complementari (uno master, uno slave).
    for a, b in itertools.permutations(peers, 2):
        provate += 1
        if ruolo_master_python(a, b) != ruolo_master_device(a, b):
            errori.append("python e sito divergono per la coppia %d/%d: "
                          "%s contro %s" % (a, b, ruolo_master_python(a, b),
                                            ruolo_master_device(a, b)))
        if ruolo_master_python(a, b) == ruolo_master_python(b, a):
            errori.append("due pannelli Python con peer %d e %d sono tutti e "
                          "due %s del device" % (a, b, ruolo_master_python(a, b)))

    # Partner ancora ignoto: si deve poter rispondere "non lo so" invece di
    # tirare a indovinare - tranne per i peer 1 e 2, che il protocollo fissa.
    # E' il caso di chi arriva al bancone per primo: se qui uscisse un ruolo,
    # lo manderebbe al Pico contro nessuno e la sessione nascerebbe storta.
    provate += 3
    if ruolo_master_python(47001, None) is not None:
        errori.append("con partner ignoto il Python decide lo stesso il ruolo")
    if ruolo_master_python(1, None) is not True:
        errori.append("il peer 1 deve essere master anche senza partner")
    if ruolo_master_python(2, None) is not False:
        errori.append("il peer 2 deve essere slave anche senza partner")

    if errori:
        print("RUOLI ROTTI (%d casi su %d):" % (len(errori), provate))
        for e in errori:
            print("  " + e)
        return 1

    print("ruoli del club: %d coppie provate, sempre UN solo master del gioco" % provate)
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
