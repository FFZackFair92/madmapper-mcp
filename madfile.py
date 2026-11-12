"""Leggere e scrivere i progetti MadMapper (.mad).

Formato (ricavato da un .mad di MadMapper 5.0.7): Qt QDataStream big-endian.
  quint32 0x0BADBABE, quint32 versione, poi una QVariantMap "nuda"
  (quint32 n, n volte: QString chiave + QVariant valore).
QVariant = quint32 tipo, quint8 isNull, valore. I tipi utente (127) portano
il nome come QByteArray ('MadColor', 'FilePath') e un payload loro.

Il lettore conserva l'ordine delle chiavi e il tipo esatto di ogni valore,
cosi' riscrivendo esce lo stesso file byte per byte (verifica: __main__).
"""
import struct
import sys
from pathlib import Path

MAGIC = 0x0BADBABE


class V:
    """Un QVariant: tipo Qt + valore Python (+ nome se tipo utente)."""
    __slots__ = ("t", "v", "user", "null")

    def __init__(self, t, v, user=None, null=0):
        self.t, self.v, self.user, self.null = t, v, user, null

    def __repr__(self):
        return f"V({self.user or self.t}, {self.v!r})"


class Reader:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def take(self, n):
        b = self.d[self.i:self.i + n]
        if len(b) != n:
            raise ValueError(f"file troncato a {self.i}")
        self.i += n
        return b

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def string(self):
        n = self.u32()
        return None if n == 0xFFFFFFFF else self.take(n).decode("utf-16-be")

    def bytearray(self):
        n = self.u32()
        return None if n == 0xFFFFFFFF else self.take(n)

    def vmap(self):
        return [(self.string(), self.variant()) for _ in range(self.u32())]

    def variant(self) -> V:
        t = self.u32()
        null = self.take(1)[0]
        if t == 127:
            name = self.bytearray()
            return V(t, self.user(name), name, null)
        return V(t, self.value(t), None, null)

    def user(self, name: bytes):
        n = name.rstrip(b"\0")
        if n == b"MadColor":
            return (self.string(), struct.unpack(">4d", self.take(32)))
        if n == b"FilePath":
            return self.string()
        raise ValueError(f"tipo utente sconosciuto {n!r} a {self.i}")

    def qimage(self):
        """QImage (miniature): MadMapper le salva vuote, cioe' solo un qint32 0."""
        if struct.unpack(">i", self.take(4))[0] != 0:
            raise ValueError(f"QImage non vuota a {self.i}: non gestita")
        return None

    def value(self, t):
        f = {0: self.string,          # QVariant non valido: Qt scrive una QString nulla
             1: lambda: bool(self.take(1)[0]),
             2: lambda: struct.unpack(">i", self.take(4))[0],
             3: lambda: struct.unpack(">I", self.take(4))[0],
             4: lambda: struct.unpack(">q", self.take(8))[0],
             5: lambda: struct.unpack(">Q", self.take(8))[0],
             6: lambda: struct.unpack(">d", self.take(8))[0],
             135: lambda: struct.unpack(">d", self.take(8))[0],   # double dei parametri modulo
             8: self.vmap,
             9: lambda: [self.variant() for _ in range(self.u32())],
             10: self.string,
             11: lambda: [self.string() for _ in range(self.u32())],
             12: self.bytearray,
             19: lambda: struct.unpack(">4i", self.take(16)),
             20: lambda: struct.unpack(">4d", self.take(32)),
             21: lambda: struct.unpack(">2i", self.take(8)),
             22: lambda: struct.unpack(">2d", self.take(16)),
             25: lambda: struct.unpack(">2i", self.take(8)),
             26: lambda: struct.unpack(">2d", self.take(16)),
             38: lambda: struct.unpack(">f", self.take(4))[0],
             70: self.qimage}.get(t)
        if f is None:
            raise ValueError(f"tipo QVariant {t} non gestito a {self.i}")
        return f()


class Writer:
    def __init__(self):
        self.out = bytearray()

    def u32(self, n):
        self.out += struct.pack(">I", n)

    def string(self, s):
        if s is None:
            return self.u32(0xFFFFFFFF)
        b = s.encode("utf-16-be")
        self.u32(len(b))
        self.out += b

    def bytearray(self, b):
        if b is None:
            return self.u32(0xFFFFFFFF)
        self.u32(len(b))
        self.out += b

    def vmap(self, m):
        self.u32(len(m))
        for k, v in m:
            self.string(k)
            self.variant(v)

    def variant(self, x: V):
        self.u32(x.t)
        self.out.append(x.null)
        if x.t == 127:
            self.bytearray(x.user)
            n = x.user.rstrip(b"\0")
            if n == b"MadColor":
                self.string(x.v[0])
                self.out += struct.pack(">4d", *x.v[1])
            else:
                self.string(x.v)
            return
        t, v = x.t, x.v
        if t == 0:
            self.string(v)
        elif t == 1:
            self.out.append(1 if v else 0)
        elif t == 2:
            self.out += struct.pack(">i", v)
        elif t == 3:
            self.out += struct.pack(">I", v)
        elif t == 4:
            self.out += struct.pack(">q", v)
        elif t == 5:
            self.out += struct.pack(">Q", v)
        elif t in (6, 135):
            self.out += struct.pack(">d", v)
        elif t == 8:
            self.vmap(v)
        elif t == 9:
            self.u32(len(v))
            for e in v:
                self.variant(e)
        elif t == 10:
            self.string(v)
        elif t == 11:
            self.u32(len(v))
            for s in v:
                self.string(s)
        elif t == 12:
            self.bytearray(v)
        elif t in (19, 21, 25):
            self.out += struct.pack(f">{len(v)}i", *v)
        elif t in (20, 22, 26):
            self.out += struct.pack(f">{len(v)}d", *v)
        elif t == 38:
            self.out += struct.pack(">f", v)
        elif t == 70:
            self.out += struct.pack(">i", 0)
        else:
            raise ValueError(f"tipo {t} non scrivibile")


def load(path) -> tuple[int, list]:
    return load_bytes(Path(path).read_bytes())


def load_bytes(data: bytes) -> tuple[int, list]:
    r = Reader(data)
    if r.u32() != MAGIC:
        raise ValueError("non e' un progetto MadMapper (.mad)")
    ver = r.u32()
    root = r.vmap()
    if r.i != len(r.d):
        raise ValueError(f"avanzano {len(r.d) - r.i} byte: formato non capito del tutto")
    return ver, root


def dump(ver: int, root: list) -> bytes:
    w = Writer()
    w.u32(MAGIC)
    w.u32(ver)
    w.vmap(root)
    return bytes(w.out)


def get(m: list, key: str):
    """Valore (V) di una chiave in una mappa [(k, V)]."""
    return next((v for k, v in m if k == key), None)


def surfaces(root: list) -> list[V]:
    return get(root, "surfaces").v


def add_quads(root: list, names: list[str]) -> list[str]:
    """Aggiunge una Quad per nome, clonando la prima Quad del progetto.
    La geometria resta quella della copia: le posizioni vere si scrivono dopo,
    dal vivo, via OSC (gli handle in pixel sono verificati, il sistema di
    coordinate del file no). Nomi gia' presenti vengono saltati."""
    import copy
    lista = surfaces(root)
    quads = [s for s in lista if get(s.v, "type").v == "quad"]
    if not quads:
        raise ValueError("nel progetto non c'e' nessuna Quad da usare come modello: creane una a mano")
    esistenti = {get(s.v, "name").v for s in lista}
    prossimo = max(get(s.v, "surfaceId").v for s in lista) + 1
    rel = get(root, "surfacesRelations")
    fatti = []
    for n in names:
        if n in esistenti:
            continue
        q = copy.deepcopy(quads[0])
        for k, v in q.v:
            if k == "name":
                v.v = n
            elif k == "surfaceId":
                v.v = prossimo
        prossimo += 1
        lista.append(q)
        if rel is not None:
            rel.v.append(V(2, -1))
        esistenti.add(n)
        fatti.append(n)
    return fatti


def remove_quads(root: list, names: list[str]) -> list[str]:
    """Toglie le superfici con quei nomi, con la loro voce in surfacesRelations
    (allineata per indice) e i riferimenti '/surfaces/<id>' nei gruppi di anteprima."""
    lista = surfaces(root)
    rel = get(root, "surfacesRelations")
    via, ids = [], set()
    for i in range(len(lista) - 1, -1, -1):
        nome = get(lista[i].v, "name").v
        if nome in names:
            ids.add(f"/surfaces/{get(lista[i].v, 'surfaceId').v}")
            del lista[i]
            if rel is not None and i < len(rel.v):
                del rel.v[i]
            via.append(nome)
    gruppi = get(root, "previewSurfaceGroups")
    for g in (gruppi.v if gruppi else []):
        sl = get(g.v, "surfaces")
        if sl is not None and sl.t == 11:
            sl.v = [x for x in sl.v if x not in ids]
    return via[::-1]


def visuals(root: list) -> list[V]:
    return get(get(root, "visuals").v, "visuals").v


def add_material(root: list, name: str, surfaces_for: list[str] | None = None) -> int:
    """Aggiunge ai media del progetto un materiale della libreria (anche uno
    utente, da Documenti/MadMapper/Materials/<name>/) e, se chiesto, lo mette
    sulle superfici indicate. Clona la voce di un materiale gia' presente
    tenendo solo gli attributi generici 'Material/...': gli altri tornano ai
    default del materiale. Ritorna l'id del media (gia' presente = quello)."""
    import copy
    vis = visuals(root)
    for v in vis:
        if get(v.v, "url").v == f"/materials/{name}/0":
            vid = get(v.v, "id").v
            break
    else:
        modelli = [v for v in vis if get(v.v, "mmtype").v == "Materials"]
        if not modelli:
            raise ValueError("nel progetto non c'e' nessun materiale da usare come modello")
        nuovo = copy.deepcopy(modelli[0])
        vid = max(get(v.v, "id").v for v in vis) + 1
        pos = max(get(v.v, "gridPos").v for v in modelli) + 1
        for k, x in nuovo.v:
            if k == "url":
                x.v = f"/materials/{name}/0"
            elif k == "name":
                x.v = name
            elif k == "id":
                x.v = vid
            elif k == "gridPos":
                x.v = pos
            elif k == "attributes":
                x.v = [(a, b) for a, b in x.v if a.startswith("Material/")]
        vis.append(nuovo)
    for s in surfaces(root):
        if surfaces_for and get(s.v, "name").v in surfaces_for:
            get(s.v, "visualId").v = vid
    return vid


if __name__ == "__main__":
    p = Path(sys.argv[1])
    ver, root = load(p)
    ok = dump(ver, root) == p.read_bytes()
    print(f"versione {ver}, {len(root)} chiavi, andata e ritorno identico: {ok}")
    n = len(surfaces(root))
    add_quads(root, ["__prova__"])
    _, r2 = load_bytes(dump(ver, root))
    nomi = [get(s.v, "name").v for s in surfaces(r2)]
    ok2 = len(nomi) == n + 1 and nomi[-1] == "__prova__"
    print(f"aggiunta di una Quad e rilettura: {ok2} {nomi}")
    remove_quads(root, ["__prova__"])
    ok3 = dump(ver, root) == p.read_bytes()
    print(f"e togliendola torna identico all'originale: {ok3}")
    sys.exit(0 if ok and ok2 and ok3 else 1)
