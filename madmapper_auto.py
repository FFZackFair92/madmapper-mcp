"""Motore autonomo per MadMapper: gira come processo separato, anche a Claude chiuso.

  python madmapper_auto.py snapshot scenes/nome.json   fotografa lo stato
  python madmapper_auto.py restore  scenes/nome.json   lo rimette
  python madmapper_auto.py record set.jsonl            registra ogni cambiamento
  python madmapper_auto.py play set.jsonl [--loop]     lo rifa' con gli stessi tempi
  python madmapper_auto.py auto auto.json              pilota automatico + effetti
  python madmapper_auto.py riapri riapri.json          dopo project_open: rimette lo stato

Lo stato del job sta in job.json (riscritto a ogni giro), lo stop e' pulito:
basta creare stop.flag. Il tempo si conta in battute del BPM di MadMapper,
riletto ogni 2 secondi, quindi un tap tempo dal vivo viene seguito.
"""
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mm  # noqa: E402

JOB, STOP = HERE / "job.json", HERE / "stop.flag"
BPM = "/master/Global_BPM/BPM"
# in una scena entra solo cio' che descrive il look: niente /application
# (add/remove media!), /outputs, /cues, e niente azioni o selezioni
ROOTS = ("/surfaces/", "/medias/", "/master/", "/modules/", "/fixtures/", "/laser_surfaces/")
SKIP = re.compile(r"(/select[^/]*$|/selected$|/assign[^/]*$|/TAP$|/Resync$|/Beat$|/Peers$|"
                  r"freeze|test_pattern|cursor)", re.I)


# ------------------------------------------------------------------ stato

def state() -> dict:
    """{address: valore} di ogni parametro leggibile e scrivibile."""
    out = {}
    for a, n in mm.walk(mm.tree()):
        if a.startswith(ROOTS) and n.get("ACCESS", 3) == 3 and "VALUE" in n \
                and n.get("TYPE") not in mm.TRIGGER_TYPES + ("r",) and not SKIP.search(a):
            out[a] = mm.first(n["VALUE"])
    return out


def apply(values: dict) -> int:
    for a, v in values.items():
        mm.write(a, v)
    return len(values)


# ------------------------------------------------------------------ job

class Job:
    def __init__(self, mode: str):
        self.mode = mode
        STOP.unlink(missing_ok=True)

    _last = 0.0

    def beat(self, note: str = "", force: bool = False) -> bool:
        """Battito per job_status (al massimo 2 volte al secondo). False quando
        e' stato chiesto lo stop."""
        now = time.time()
        if force or now - self._last > 0.5:
            self._last = now
            tmp = JOB.with_suffix(".tmp")
            tmp.write_text(json.dumps({"mode": self.mode, "note": note, "pid": os.getpid(),
                                       "last_tick": now}), encoding="utf-8")
            try:
                os.replace(tmp, JOB)
            except PermissionError:
                pass  # Windows: job_status lo sta leggendo proprio ora, si salta un battito
        return not STOP.exists()


def record(file: Path) -> None:
    job = Job("record")
    t0, prev = time.time(), state()
    with file.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"t": 0.0, "snapshot": prev}) + "\n")
        n = 0
        while job.beat(f"{n} cambiamenti in {file.name}"):
            time.sleep(0.25)
            now = state()
            diff = {a: v for a, v in now.items() if prev.get(a) != v}
            if diff:
                f.write(json.dumps({"t": round(time.time() - t0, 3), "set": diff}) + "\n")
                f.flush()
                n += len(diff)
            prev = now


def play(file: Path, loop: bool = False) -> None:
    job = Job("play")
    righe = [json.loads(r) for r in file.read_text(encoding="utf-8-sig").splitlines() if r.strip()]
    giro = 0
    while True:
        giro += 1
        t0 = time.time()
        for i, r in enumerate(righe):
            while time.time() - t0 < r["t"]:
                if not job.beat(f"giro {giro}, evento {i}/{len(righe)}"):
                    return
                time.sleep(min(0.05, max(0.0, r["t"] - (time.time() - t0))))
            apply(r.get("snapshot") or r.get("set") or {})
        if not loop:
            job.beat("finito", force=True)
            return


# ------------------------------------------------------------------ effetti

def _step(beat: float, beats: float) -> int:
    return int(beat // beats)


def mix_index(cfg: dict, step: int) -> int:
    """Sequenza pseudo-casuale col seme: stesso seed = stessi salti, ogni sera.
    Mai due volte di fila la stessa superficie."""
    n = len(cfg["surfaces"])
    seq = cfg.setdefault("_seq", [])
    rng = cfg.setdefault("_rng", random.Random(cfg.get("seed", 0)))
    while len(seq) <= step:
        c = rng.randrange(n)
        if n > 1 and seq and c == seq[-1]:
            c = (c + 1) % n
        seq.append(c)
    return seq[step]


def fx_levels(cfg: dict, beat: float) -> dict:
    """Opacita' di ogni superficie toccata dagli effetti, a una data battuta.
    chase e mix decidono QUALE superficie e' accesa; lo strobo moltiplica."""
    lv: dict[str, float] = {}
    for k in ("chase", "mix"):
        c = cfg.get(k)
        if not c or not c.get("surfaces"):
            continue
        s = _step(beat, c["beats"])
        acc = c["surfaces"][s % len(c["surfaces"])] if k == "chase" else c["surfaces"][mix_index(c, s)]
        for name in c["surfaces"]:
            on = name == acc
            lv[name] = max(lv.get(name, 0.0), c.get("on", 1.0) if on else c.get("off", 0.0))
    st = cfg.get("strobe")
    if st and st.get("surfaces"):
        fase = (beat / st["beats"]) % 1.0
        gate = st.get("on", 1.0) if fase < st.get("duty", 0.4) else st.get("off", 0.0)
        for name in st["surfaces"]:
            lv[name] = lv.get(name, 1.0) * gate
    return lv


def blocco(cfg: dict, beat: float) -> dict:
    """Gli effetti attivi a quella battuta: quelli fissi del cfg, o il blocco
    della scaletta 'sections' ({"beats": 32, "seq": [{...}, {...}]})."""
    sec = cfg.get("sections")
    if not sec or not sec.get("seq"):
        return cfg
    return sec["seq"][_step(beat, sec["beats"]) % len(sec["seq"])]


def modulate(m: dict, beat: float) -> float:
    fase = (beat / m["beats"]) % 1.0
    lo, hi = m.get("min", 0.0), m.get("max", 1.0)
    if m.get("shape") == "ramp":
        k = fase
    elif m.get("shape") == "random":
        k = random.Random(f"{m['path']}:{_step(beat, m['beats'])}").random()
    else:
        k = 0.5 - 0.5 * math.cos(2 * math.pi * fase)
    return lo + (hi - lo) * k


def auto(file: Path) -> None:
    job = Job("auto")
    cfg = json.loads(file.read_text(encoding="utf-8-sig"))
    tick_s = cfg.get("tick", 0.05)
    tutte = sorted(mm.children("/surfaces"))
    # 'sections': una scaletta di blocchi di effetti che si alternano ogni N battute
    blocchi = [cfg] + list((cfg.get("sections") or {}).get("seq", []))
    for b in blocchi:
        for k in ("strobe", "chase", "mix"):
            if b.get(k) is not None and not b[k].get("surfaces"):
                b[k]["surfaces"] = tutte
    toccate = {s for b in blocchi for k in ("strobe", "chase", "mix")
               for s in (b.get(k) or {}).get("surfaces", [])}
    originali = {s: mm.value(f"/surfaces/{s}/opacity") for s in toccate}
    rng = random.Random(cfg.get("seed", 0))
    ultimo_media: dict[str, str] = {}
    inviati: dict[str, object] = {}

    def manda(a, v):
        if inviati.get(a) != v:
            mm.write(a, v)
            inviati[a] = v

    def leggi_bpm():
        try:
            return float(mm.value(BPM) or 120.0)
        except (RuntimeError, TypeError, ValueError):
            return 120.0

    st = {"bpm": leggi_bpm(), "beat": 0.0, "t_prev": time.time(), "t_bpm": time.time()}

    def tick(now):
        st["beat"] += (now - st["t_prev"]) * st["bpm"] / 60.0
        st["t_prev"] = now
        if now - st["t_bpm"] > 2:
            st["bpm"], st["t_bpm"] = leggi_bpm(), now
        beat = st["beat"]
        for m in cfg.get("media", []):
            s = _step(beat + m.get("offset", 0), m["beats"])
            if m.get("_step") != s:
                m["_step"] = s
                scelte = [c for c in m["clips"] if c != ultimo_media.get(m["surface"])] or m["clips"]
                ultimo_media[m["surface"]] = rng.choice(scelte)
                manda(f"/surfaces/{m['surface']}/visual/name", ultimo_media[m["surface"]])
        for m in cfg.get("modulate", []):
            manda(m["path"], round(modulate(m, beat), 4))
        lv = fx_levels(blocco(cfg, beat), beat)
        for s in toccate:
            # una superficie che l'effetto del blocco non tocca torna com'era
            v = lv.get(s, originali.get(s) if originali.get(s) is not None else 1.0)
            manda(f"/surfaces/{s}/opacity", round(v, 3))

    try:
        errore = ""
        while job.beat(f"bpm {st['bpm']:.1f}, battuta {st['beat']:.1f}{errore}"):
            now = time.time()
            try:
                tick(now)
                errore = ""
            except RuntimeError as e:
                # dal vivo un intoppo di rete non deve fermare lo show: si segnala e si continua
                errore = f" — ERRORE: {str(e)[:120]}"
                print(time.strftime("%H:%M:%S"), e, flush=True)
            time.sleep(max(0.0, tick_s - (time.time() - now)))
    finally:
        # niente superfici lasciate spente o a meta' quando si ferma il pilota
        for s, v in originali.items():
            if v is not None:
                mm.write(f"/surfaces/{s}/opacity", v)
        job.beat("fermo, opacita' originali rimesse", force=True)


def _mm_vivo() -> bool:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MadMapper.exe", "/NH"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return "MadMapper.exe" in (r.stdout or "")


def riapri(file: Path, wait: float = 240) -> None:
    """Dopo project_open: aspetta che MadMapper risponda, rimette lo stato
    salvato sulle superfici che esistono ancora e l'uscita a tutto schermo."""
    job = Job("riapri")
    d = json.loads(file.read_text(encoding="utf-8-sig"))
    stato, full = d.get("stato", {}), d.get("fullscreen", True)
    t0 = lancio = time.time()
    tentativi = 1
    while True:
        try:
            superfici = set(mm.children("/surfaces"))
            break
        except RuntimeError:
            if time.time() - t0 > wait:
                job.beat(f"MadMapper non risponde dopo {wait:.0f}s", force=True)
                return
            # MadMapper a volte va in crash se riparte subito dopo essere stato
            # chiuso (0xC0000005, verificato): se il processo non c'e' piu' lo si
            # rilancia, fino a 3 volte
            if os.name == "nt" and time.time() - lancio > 20 and tentativi < 3 and not _mm_vivo():
                time.sleep(5)
                subprocess.run(["schtasks", "/Run", "/TN", "MadMapperMCP"],
                               capture_output=True, stdin=subprocess.DEVNULL)
                lancio, tentativi = time.time(), tentativi + 1
            if not job.beat(f"aspetto MadMapper ({time.time() - t0:.0f}s, avvio {tentativi})"):
                return
            time.sleep(1)
    time.sleep(2)  # appena risponde l'albero non e' ancora completo
    vivi = {a: v for a, v in stato.items()
            if not a.startswith("/surfaces/") or a.split("/")[2] in superfici}
    n = apply(vivi)
    if full:
        mm.write("/application/view/fullscreen", 1)
    job.beat(f"fatto in {time.time() - t0:.0f}s: superfici {sorted(superfici)}, "
             f"{n} parametri rimessi", force=True)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd, file = argv[0], Path(argv[1])
    if cmd == "snapshot":
        file.parent.mkdir(parents=True, exist_ok=True)
        s = state()
        file.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{len(s)} parametri salvati in {file.name}")
    elif cmd == "restore":
        print(f"{apply(json.loads(file.read_text(encoding='utf-8-sig')))} parametri rimessi da {file.name}")
    elif cmd == "record":
        record(file)
    elif cmd == "play":
        play(file, "--loop" in argv)
    elif cmd == "auto":
        auto(file)
    elif cmd == "riapri":
        riapri(file)
    else:
        print(f"comando sconosciuto: {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
