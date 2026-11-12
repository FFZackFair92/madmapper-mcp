"""Nucleo condiviso: parlare con MadMapper. Solo libreria standard.

Lettura: OSCQuery, cioe' HTTP GET sull'albero JSON dei parametri
         (GET /  -> tutto l'albero, GET /path?VALUE -> un valore, GET /?HOST_INFO).
Scrittura: OSC su UDP. MadMapper non risponde ai messaggi OSC, quindi quando
           conta si rilegge il valore via OSCQuery (set(..., verify=True)).

Config da variabili d'ambiente:
  MADMAPPER_HOST         default 127.0.0.1
  MADMAPPER_QUERY_PORT   porta HTTP di OSCQuery, default 8010
  MADMAPPER_OSC_PORT     porta OSC in ingresso; se manca la chiede a HOST_INFO
"""
import fnmatch
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("MADMAPPER_HOST", "127.0.0.1")
QPORT = int(os.environ.get("MADMAPPER_QUERY_PORT", "8010"))
_osc_port: int | None = int(os.environ["MADMAPPER_OSC_PORT"]) if os.environ.get("MADMAPPER_OSC_PORT") else None
_types: dict[str, str] = {}          # address -> TYPE di OSCQuery, per spedire il tipo giusto
_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

TRIGGER_TYPES = ("N", "I", "")       # niente argomenti: si spedisce l'indirizzo e basta


# ------------------------------------------------------------------ OSCQuery

def http(path: str = "/", query: str = ""):
    url = f"http://{HOST}:{QPORT}{urllib.parse.quote(path)}" + (f"?{query}" if query else "")
    try:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                body = r.read()
        except (ConnectionResetError, ConnectionAbortedError):
            time.sleep(0.05)     # Windows: un reset isolato sotto carico, si riprova una volta
            with urllib.request.urlopen(url, timeout=5) as r:
                body = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OSCQuery {path}: HTTP {e.code} (indirizzo inesistente?)") from None
    except OSError as e:
        raise RuntimeError(
            f"MadMapper non raggiungibile su {HOST}:{QPORT} ({e}). In MadMapper: "
            f"Preferences > OSC/MIDI, attiva OSC Query e controlla la porta "
            f"(variabile MADMAPPER_QUERY_PORT).") from None
    return json.loads(body) if body.strip() else None


def tree(path: str = "/") -> dict:
    return http(path) or {}


def osc_port() -> int:
    global _osc_port
    if _osc_port is None:
        info = http("/", "HOST_INFO") or {}
        _osc_port = int(info.get("OSC_PORT") or QPORT)
    return _osc_port


def walk(node: dict, path: str = ""):
    """(address, nodo) per ogni parametro (nodo con TYPE) dell'albero."""
    fp = node.get("FULL_PATH", path)
    if "TYPE" in node:
        yield fp, node
    for k, child in (node.get("CONTENTS") or {}).items():
        yield from walk(child, f"{fp.rstrip('/')}/{k}")


def first(v):
    return v[0] if isinstance(v, list) and len(v) == 1 else v


def value(address: str):
    return first((http(address, "VALUE") or {}).get("VALUE"))


def children(path: str) -> dict:
    """I figli diretti di un nodo che sono a loro volta contenitori (superfici, media...)."""
    return {k: v for k, v in (tree(path).get("CONTENTS") or {}).items() if "CONTENTS" in v}


# ------------------------------------------------------------------ OSC

def _pad(b: bytes) -> bytes:
    return b + b"\0" * (4 - len(b) % 4)


def encode(address: str, args: list, types: str = "") -> bytes:
    """Messaggio OSC 1.0. 'types' (dal TYPE di OSCQuery) decide il tipo di ogni
    argomento; senza, lo si deduce dal tipo Python (bool -> int 0/1, come
    vuole MadMapper per i BOOL)."""
    tags, data = ",", b""
    for i, a in enumerate(args):
        t = types[i] if i < len(types) else ""
        if t == "r":                       # colore OSC: '#RRGGBBAA' -> 4 byte
            tags += "r"
            data += bytes.fromhex(str(a).lstrip("#").ljust(8, "F")[:8])
        elif t in ("T", "F"):
            tags += "T" if _truthy(a) else "F"
        elif t == "s" or (not t and isinstance(a, str)):
            tags += "s"
            data += _pad(str(a).encode())
        elif t in ("i", "h") or (not t and isinstance(a, (bool, int))):
            tags += "i"
            data += struct.pack(">i", int(_num(a)))
        else:
            tags += "f"
            data += struct.pack(">f", float(_num(a)))
    return _pad(address.encode()) + _pad(tags.encode()) + data


def _truthy(a) -> bool:
    return a.strip().lower() in ("1", "true", "on", "yes") if isinstance(a, str) else bool(a)


def _num(a):
    if isinstance(a, str):
        if a.strip().lower() in ("true", "false", "on", "off"):
            return 1 if _truthy(a) else 0
        return float(a)
    return a


def send(address: str, *args, types: str = "") -> None:
    _sock.sendto(encode(address, list(args), types), (HOST, osc_port()))


def type_of(address: str) -> str:
    """TYPE del parametro (cache). Con le wildcard si prende quello del primo
    parametro che corrisponde. Indirizzi sconosciuti -> ''."""
    if address not in _types:
        t = ""
        try:
            if "*" in address or "?" in address:
                base = address.split("*")[0].split("?")[0].rsplit("/", 1)[0] or "/"
                t = next((n.get("TYPE", "") for a, n in walk(tree(base), base)
                          if fnmatch.fnmatchcase(a, address)), "")
            else:
                t = (http(address) or {}).get("TYPE", "")
        except RuntimeError:
            pass
        _types[address] = t
    return _types[address]


def write(address: str, val=None, verify: bool = False):
    """Scrive un parametro col tipo che MadMapper si aspetta.
    val lista -> piu' argomenti (es. una posizione 'ff'). None -> trigger.
    verify=True rilegge da OSCQuery e solleva errore se non torna."""
    t = type_of(address)
    args = [] if (val is None or t in ("N", "I")) else (list(val) if isinstance(val, (list, tuple)) else [val])
    send(address, *args, types=t)
    if not verify or not args or "*" in address:
        return val
    time.sleep(0.08)
    letto = value(address)
    if not _same(letto, val):
        raise RuntimeError(f"{address}: scritto {val!r} ma MadMapper ora dice {letto!r} "
                           f"(fuori range, sola lettura, o indirizzo sbagliato?)")
    return letto


def _same(a, b) -> bool:
    if isinstance(a, list) or isinstance(b, (list, tuple)):
        a, b = list(a if isinstance(a, list) else [a]), list(b if isinstance(b, (list, tuple)) else [b])
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    try:
        return abs(float(_num(a)) - float(_num(b))) < 1e-3
    except (TypeError, ValueError):
        return str(a) == str(b)


# ------------------------------------------------------------------ geometria

CANVAS = tuple(int(v) for v in os.environ.get("MADMAPPER_CANVAS", "1920x1080").lower().split("x"))


def to_canvas(x: float, y: float, proj_w: float, proj_h: float, canvas=CANVAS) -> list[float]:
    """Pixel di proiettore -> pixel della tela di uscita di MadMapper.
    Verificato su MadMapper 5.0.7: gli handle sono in pixel della tela (default
    1920x1080), origine in alto a sinistra, y verso il basso, e la tela viene
    stirata a tutto schermo sul proiettore. Se la tela e' un'altra, MADMAPPER_CANVAS."""
    return [round(x * canvas[0] / proj_w, 2), round(y * canvas[1] / proj_h, 2)]


def homography(H, pts):
    out = []
    for x, y in pts:
        q = [H[r][0] * x + H[r][1] * y + H[r][2] for r in range(3)]
        out.append([q[0] / q[2], q[1] / q[2]])
    return out


def order_quad(pts):
    """Angoli in ordine alto-sx, alto-dx, basso-dx, basso-sx (in pixel, y in giu')."""
    s = sorted(pts, key=lambda p: p[0] + p[1])
    d = sorted(pts, key=lambda p: p[0] - p[1])
    return [s[0], d[-1], s[-1], d[0]]


# handle di una Quad: 0 basso-sx, 1 basso-dx, 2 alto-dx, 3 alto-sx (verificato
# con una cattura del proiettore). order_quad da' alto-sx, alto-dx, basso-dx, basso-sx.
QUAD_HANDLE = [3, 2, 1, 0]


def handles(surface: str) -> list[tuple[str, str]]:
    """(address x, address y) di ogni handle di uscita della superficie, in ordine."""
    base = f"/surfaces/{surface}/output/handles"
    try:
        ks = sorted(children(base), key=lambda k: int(k) if k.isdigit() else 1 << 30)
    except RuntimeError:
        return []
    return [(f"{base}/{k}/x", f"{base}/{k}/y") for k in ks]
