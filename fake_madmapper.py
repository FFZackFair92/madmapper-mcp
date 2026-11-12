"""Un finto MadMapper: OSCQuery su HTTP + OSC su UDP, stessa porta.

Serve a provare il server MCP e il pilota automatico senza MadMapper aperto:
  python fake_madmapper.py            (ascolta su 127.0.0.1:8010)
I messaggi OSC ricevuti aggiornano l'albero e finiscono in 'log'.
"""
import json
import socket
import struct
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def p(path, typ, val, lo=None, hi=None):
    n = {"FULL_PATH": path, "TYPE": typ, "ACCESS": 3, "VALUE": val if isinstance(val, list) else [val]}
    if lo is not None:
        n["RANGE"] = [{"MIN": lo, "MAX": hi}]
    return n


def trig(path):
    return {"FULL_PATH": path, "TYPE": "N", "ACCESS": 2}


def node(path, **children):
    return {"FULL_PATH": path, "CONTENTS": {k.replace("__", " "): v for k, v in children.items()}}


def surface(name, x0, media):
    """Come in MadMapper 5.0.7: handle in pixel della tela 1920x1080, y in giu',
    ordine basso-sx, basso-dx, alto-dx, alto-sx. I booleani sono 'i'."""
    b = f"/surfaces/{name}"
    pts = [(x0, 1080), (x0 + 800, 1080), (x0 + 800, 0), (x0, 0)]
    hs = {str(i): node(f"{b}/output/handles/{i}", x=p(f"{b}/output/handles/{i}/x", "f", float(x)),
                       y=p(f"{b}/output/handles/{i}/y", "f", float(y)))
          for i, (x, y) in enumerate(pts)}
    return node(b, opacity=p(f"{b}/opacity", "f", 1.0, 0, 1),
                visible=p(f"{b}/visible", "i", True, 0, 1),
                select=trig(f"{b}/select"),
                color=node(f"{b}/color", rgba=p(f"{b}/color/rgba", "r", "#FFFFFFFF")),
                visual=node(f"{b}/visual", name=p(f"{b}/visual/name", "s", media)),
                output=node(f"{b}/output", handles=node(f"{b}/output/handles", **hs)))


def media(name):
    b = f"/medias/{name}"
    return {**node(b, Speed=p(f"{b}/Speed", "f", 0.5), select=trig(f"{b}/select"),
                   assign=trig(f"{b}/assign"),
                   assign_to_all_surfaces=trig(f"{b}/assign_to_all_surfaces")),
            "DESCRIPTION": name.replace("_", " ")}


def build():
    bank = "/cues/Bank-1/cues"
    return node("/",
                surfaces=node("/surfaces", **{"Quad-1": surface("Quad-1", 100, "TestCard"),
                                              "Quad-2": surface("Quad-2", 1000, "Bar_Code")}),
                medias=node("/medias", **{"TestCard": media("TestCard"), "Bar_Code": media("Bar_Code"),
                                          "Clouds": media("Clouds")},
                            select_by_name=p("/medias/select_by_name", "s", "")),
                master=node("/master",
                            master_level=p("/master/master_level", "f", 1.0, 0, 1),
                            master_video_level=p("/master/master_video_level", "f", 1.0, 0, 1),
                            freeze_engine=p("/master/freeze_engine", "i", False),
                            Global_BPM=node("/master/Global_BPM",
                                            BPM=p("/master/Global_BPM/BPM", "f", 120.0, 20, 300),
                                            TAP=p("/master/Global_BPM/TAP", "i", False))),
                cues=node("/cues", active_bank=p("/cues/active_bank", "s", "Bank-1"),
                          **{"Bank-1": node("/cues/Bank-1", cues=node(
                              bank, start_next=trig(f"{bank}/start_next"),
                              by_cell=node(f"{bank}/by_cell", col_1=node(
                                  f"{bank}/by_cell/col_1", row_1=trig(f"{bank}/by_cell/col_1/row_1")))))}),
                application=node("/application", medias=node(
                    "/application/medias", add=p("/application/medias/add", "s", "~/image.jpg"))),
                outputs=node("/outputs", **{"Video-Output-1": node(
                    "/outputs/Video-Output-1",
                    enabled=p("/outputs/Video-Output-1/enabled", "i", True))}))


class Fake:
    def __init__(self, port=8010):
        self.root, self.log, self.port = build(), [], port
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(u.path)
                if u.query == "HOST_INFO":
                    body = {"NAME": "fake madmapper", "OSC_PORT": fake.port, "OSC_TRANSPORT": "UDP"}
                else:
                    n = fake.find(path)
                    if n is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = {"VALUE": n.get("VALUE")} if u.query == "VALUE" else n
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        ThreadingHTTPServer.request_queue_size = 64
        self.http = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", port))

    def find(self, path):
        n = self.root
        for seg in [s for s in path.split("/") if s]:
            n = (n.get("CONTENTS") or {}).get(seg)
            if n is None:
                return None
        return n

    def nodes(self, pattern):
        """Tutti i nodi che matchano un indirizzo, '*' compreso (come fa OSC)."""
        out = [self.root]
        for seg in [s for s in pattern.split("/") if s]:
            nxt = []
            for n in out:
                c = n.get("CONTENTS") or {}
                nxt += list(c.values()) if seg == "*" else ([c[seg]] if seg in c else [])
            out = nxt
        return out

    def osc_loop(self):
        while True:
            try:
                data, _ = self.udp.recvfrom(65536)
            except OSError:
                return          # socket chiuso da stop()
            addr, args = decode(data)
            self.log.append((addr, args))
            if addr.endswith("/assign_to_all_surfaces"):
                nome = addr.split("/")[2]
                for n in self.nodes("/surfaces/*/visual/name"):
                    n["VALUE"] = [nome]
            for n in self.nodes(addr):
                if "VALUE" in n and args:
                    n["VALUE"] = args

    def start(self):
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self.osc_loop, daemon=True).start()
        return self

    def stop(self):
        self.http.shutdown()
        self.udp.close()


def decode(data: bytes):
    def s(i):
        j = data.index(b"\0", i)
        return data[i:j].decode(), (j + 4) & ~3
    addr, i = s(0)
    tags, i = s(i)
    args = []
    for t in tags[1:]:
        if t == "f":
            args.append(struct.unpack(">f", data[i:i + 4])[0]); i += 4
        elif t == "i":
            args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
        elif t == "s":
            v, i = s(i); args.append(v)
        elif t in "TF":
            args.append(t == "T")
    return addr, args


if __name__ == "__main__":
    f = Fake(int(sys.argv[1]) if len(sys.argv) > 1 else 8010).start()
    print(f"finto MadMapper su 127.0.0.1:{f.port} (Ctrl+C per uscire)")
    threading.Event().wait()
