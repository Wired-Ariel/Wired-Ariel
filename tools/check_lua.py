#!/usr/bin/env python3
"""check_lua.py - compila davvero lo script generato per mGBA.

Perche' esiste: il 2026-07-30 lo script ha smesso di compilare perche' il
payload, crescendo, ha superato i 200 pezzi nella catena di concatenazione
`..` e ha sfondato LUAI_MAXCCALLS nel parser di Lua. Il modo in cui fallisce e'
il peggiore possibile: **mGBA non stampa niente**, nemmeno un errore, perche' il
file non arriva a essere eseguito. Dai log sembra un iniettore muto, non un
iniettore rotto, e si perde un test intero a cercare la causa altrove.

Fa due cose, ed entrambe sono test POSITIVI:
  1. compila l'intero file (senza eseguirlo): se il parser si rifiuta, lo dice
     qui invece che con il silenzio in mGBA;
  2. esegue SOLO l'intestazione generata (le costanti e i byte del payload, che
     non hanno effetti collaterali) e confronta PAYLOAD_BYTES **byte per byte**
     con payload.bin. Cosi' si accorge anche di un escape sbagliato o di un byte
     perso, non solo di un errore di sintassi.

Serve `lupa` (pip install lupa). Se manca, lo dice e non fallisce: e' un
controllo in piu', non una dipendenza della build.

Uso:
    python tools/check_lua.py <script.lua> [payload.bin]
"""

import sys
from pathlib import Path

MARKER = "PAYLOAD_BYTES = table.concat(__chunks)"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    path = Path(sys.argv[1])
    binary = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not path.is_file():
        print("ERRORE: %s non esiste" % path, file=sys.stderr)
        return 1

    try:
        import lupa
    except ImportError:
        print("  (lupa non installato: salto la compilazione dello script Lua.")
        print("   'pip install lupa' per attivare questo controllo)")
        return 0

    src = path.read_bytes()
    # encoding=None: i byte del payload NON sono testo, e con la decodifica
    # automatica lupa si strozza sul primo byte non UTF-8.
    lua = lupa.LuaRuntime(encoding=None)

    try:
        lua.compile(src)
    except Exception as exc:
        # Con encoding=None il messaggio di Lua arriva come bytes.
        msg = exc.args[0] if exc.args else exc
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", errors="replace")
        print("ERRORE: %s NON COMPILA" % path.name, file=sys.stderr)
        print("        %s" % msg, file=sys.stderr)
        print("        In mGBA questo si presenta come silenzio totale: nessun log,",
              file=sys.stderr)
        print("        nemmeno un errore. Non e' l'iniettore muto, e' il file rotto.",
              file=sys.stderr)
        return 1

    text = src.decode("utf-8", errors="replace")
    cut = text.find(MARKER)
    if cut < 0:
        print("  compila, ma l'intestazione non ha il formato atteso:")
        print("  '%s' non trovato, salto il controllo sulla lunghezza" % MARKER)
        return 0

    header = text[:cut + len(MARKER)]
    try:
        lua.execute(header.encode("utf-8"))
        embedded = bytes(lua.eval(b"PAYLOAD_BYTES"))
    except Exception as exc:
        print("ERRORE: l'intestazione generata non si esegue: %s" % exc, file=sys.stderr)
        return 1

    if binary is not None and binary.is_file():
        want = binary.read_bytes()
        if embedded != want:
            print("ERRORE: i byte incorporati nello script non corrispondono a %s"
                  % binary.name, file=sys.stderr)
            print("        script %d byte, bin %d byte"
                  % (len(embedded), len(want)), file=sys.stderr)
            for i, (a, b) in enumerate(zip(embedded, want)):
                if a != b:
                    print("        prima differenza a +0x%X: %d invece di %d"
                          % (i, a, b), file=sys.stderr)
                    break
            return 1
        print("  script Lua: compila, payload %d byte identici a %s"
              % (len(embedded), binary.name))
        return 0

    print("  script Lua: compila, PAYLOAD_BYTES %d byte" % len(embedded))
    return 0


if __name__ == "__main__":
    sys.exit(main())
