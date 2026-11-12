"""Collaudo completo contro il finto MadMapper: niente hardware, niente MadMapper.
  python test_offline.py
"""
import json
import os
import random
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
s = socket.socket()
s.bind(("127.0.0.1", 0))
PORT = s.getsockname()[1]
s.close()
os.environ.update(MADMAPPER_HOST="127.0.0.1", MADMAPPER_QUERY_PORT=str(PORT))
sys.path.insert(0, str(HERE))

from fake_madmapper import Fake  # noqa: E402
import mm  # noqa: E402
import madmapper_auto as auto  # noqa: E402
import madmapper_mcp as srv  # noqa: E402

fake = Fake(PORT).start()
V = lambda p: mm.first(fake.find(p)["VALUE"])  # noqa: E731
wait = lambda: time.sleep(0.15)  # noqa: E731


def until(cond, secs=8.0):
    """UDP e processi staccati non hanno tempi fissi: si aspetta l'esito, con un tetto."""
    t = time.time()
    while not cond():
        assert time.time() - t < secs, "tempo scaduto"
        time.sleep(0.05)
    return round(time.time() - t, 2)

# --- lettura
st = srv.get_state()
assert [x["name"] for x in st["surfaces"]] == ["Quad-1", "Quad-2"], st
assert st["bpm"] == 120.0 and st["medias"] == ["TestCard", "Bar_Code", "Clouds"], st
assert any(p["address"] == "/surfaces/Quad-1/opacity" for p in srv.find_parameters("opacity"))

# --- scrittura con tipi giusti e verifica
srv.set_parameter("/surfaces/Quad-1/opacity", "0.25")
assert abs(V("/surfaces/Quad-1/opacity") - 0.25) < 1e-6
srv.surface_set("*", "visible", False); wait()
assert V("/surfaces/Quad-1/visible") == 0 and V("/surfaces/Quad-2/visible") == 0
assert fake.log[-1][1] == [0]                # MadMapper vuole i booleani come int (TYPE 'i')
srv.set_bpm(128)
srv.tap(); wait()
assert fake.log[-1] == ("/master/Global_BPM/TAP", [1])
srv.cue(1, 1); wait(); assert fake.log[-1] == ("/cues/Bank-1/cues/by_cell/col_1/row_1", [])
srv.blackout(True); wait(); assert V("/master/master_level") == 0.0
srv.blackout(False); wait(); assert V("/master/master_level") == 1.0
srv.assign_media("Bar_Code", ["Quad-1"]); assert V("/surfaces/Quad-1/visual/name") == "Bar Code"  # nome della lista, non la chiave
srv.assign_media("TestCard"); wait(); assert V("/surfaces/Quad-2/visual/name") == "TestCard"
try:
    mm.write("/surfaces/Quad-1/nonesiste", 1, verify=True)
    raise AssertionError("doveva fallire")
except RuntimeError:
    pass

# --- automapping: omografia identita', proiettore 1000x500
(HERE / "calibrazione.json").write_text(json.dumps(
    {"H_camera_verso_proiettore": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
     "dimensioni": {"proiettore": [1000, 500]}}))
out = srv.automap_points({"Quad-1": [[500, 250], [0, 0], [500, 0], [0, 250]]})  # ordine a caso
assert "ok" in out, out
H = "/surfaces/Quad-1/output/handles"
# handle 3 = alto-sx, 1 = basso-dx; tela 1920x1080
assert (V(f"{H}/3/x"), V(f"{H}/3/y")) == (0.0, 0.0)
assert (V(f"{H}/1/x"), V(f"{H}/1/y")) == (960.0, 540.0)
assert (V(f"{H}/0/x"), V(f"{H}/0/y")) == (0.0, 540.0)
assert "alto-sx [0.0, 0.0]" in srv.automap_points({"Quad-2": [[0, 0], [1, 0], [1, 1], [0, 1]]}, dry_run=True)

# --- effetti: deterministici e mai la stessa superficie due volte di fila
cfg = {"mix": {"surfaces": ["a", "b", "c"], "beats": 1, "seed": 7}}
seq = [auto.mix_index(cfg["mix"], i) for i in range(50)]
assert seq == [auto.mix_index({"surfaces": ["a", "b", "c"], "seed": 7}, i) for i in range(50)]
assert all(x != y for x, y in zip(seq, seq[1:]))
lv = auto.fx_levels({"chase": {"surfaces": ["a", "b"], "beats": 1},
                     "strobe": {"surfaces": ["a", "b"], "beats": 1, "duty": 0.5}}, 0.25)
assert lv == {"a": 1.0, "b": 0.0}, lv
assert auto.fx_levels({"strobe": {"surfaces": ["a"], "beats": 1, "duty": 0.5}}, 0.75) == {"a": 0.0}

# --- scaletta a blocchi: il blocco cambia ogni 'beats' battute, poi ricomincia
sc = {"sections": {"beats": 4, "seq": [{"chase": {"surfaces": ["a"], "beats": 1}}, {}]}}
assert "chase" in auto.blocco(sc, 1) and auto.blocco(sc, 5) == {} and "chase" in auto.blocco(sc, 9)

# --- auto_show: media a rotazione sfalsata + scaletta di effetti
fake.log.clear()
print(srv.auto_show("alta", seed=3))
until(lambda: any(a.endswith("/visual/name") for a, _ in fake.log))
time.sleep(1.5)
nomi = {v[0] for a, v in fake.log if a.endswith("/visual/name")}
assert "Bar Code" in nomi, nomi
assert "TestCard" not in nomi, nomi
assert any(a.endswith("/opacity") for a, _ in fake.log)
print(srv.fx_stop_all()); wait()
assert V("/surfaces/Quad-2/opacity") == 1.0

# --- scene (processo separato, come dal vivo)
srv.set_parameter("/surfaces/Quad-1/opacity", 0.8)
print(srv.scene_save("prova"))
scena = json.loads((HERE / "scenes/prova.json").read_text())
assert not any(a.startswith(("/application", "/outputs", "/cues")) or a.endswith("/rgba") for a in scena), scena
assert "/surfaces/Quad-1/output/handles/0/x" in scena
srv.set_parameter("/surfaces/Quad-1/opacity", 0.1)
print(srv.scene_recall("prova")); wait()
assert abs(V("/surfaces/Quad-1/opacity") - 0.8) < 1e-6

# --- pilota con strobo: gira, poi lo stop rimette le opacita'
print(srv.fx_strobe(beats=0.25, duty=0.5))
assert "in corso" in srv.job_status()
time.sleep(1.0)
spediti = [a for a, v in fake.log if a == "/surfaces/Quad-2/opacity"]
assert len(spediti) > 3, spediti
print(srv.fx_stop_all())
wait()
assert abs(V("/surfaces/Quad-1/opacity") - 0.8) < 1e-6 and V("/surfaces/Quad-2/opacity") == 1.0

# --- registra e riproduci
print(srv.job_start("record", "prova.jsonl")); time.sleep(0.6)
srv.set_parameter("/surfaces/Quad-2/opacity", 0.3); time.sleep(0.6)
print(srv.job_stop())
righe = (HERE / "prova.jsonl").read_text().splitlines()
assert any("Quad-2/opacity" in r for r in righe[1:]), righe
srv.set_parameter("/surfaces/Quad-2/opacity", 1.0)
print(srv.job_start("play", "prova.jsonl"))
print("play arrivato in", until(lambda: abs(V("/surfaces/Quad-2/opacity") - 0.3) < 1e-6), "s")
t = time.time(); [mm.value("/master/master_level") for _ in range(10)]
print(f"latenza OSCQuery: {(time.time() - t) * 100:.0f} ms a lettura")

# --- riapertura: lo stato salvato torna sulle superfici che esistono ancora
(HERE / "riapri.json").write_text(json.dumps({"stato": {
    "/surfaces/Quad-2/opacity": 0.42, "/surfaces/Sparita/opacity": 0.1}, "fullscreen": False}))
auto.riapri(HERE / "riapri.json", wait=5); wait()
assert abs(V("/surfaces/Quad-2/opacity") - 0.42) < 1e-6
assert not any(a.startswith("/surfaces/Sparita") for a, _ in fake.log)

for f in ("riapri.json", "calibrazione.json", "prova.jsonl", "fx.json", "job.json", "stop.flag",
          "job_auto.log", "job_auto.err", "job_record.log", "job_record.err",
          "job_play.log", "job_play.err", "scenes/prova.json"):
    (HERE / f).unlink(missing_ok=True)
fake.stop()
print("TUTTO OK")
