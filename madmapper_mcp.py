# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "pillow>=10"]
# ///
"""MCP server per MadMapper (OSC + OSCQuery).

In MadMapper: Preferences > OSC/MIDI -> attiva OSC Query (porta di default 8010).
Config: MADMAPPER_HOST, MADMAPPER_QUERY_PORT, MADMAPPER_OSC_PORT (vedi mm.py).

Lettura via OSCQuery (l'albero JSON di tutti i parametri), scrittura via OSC.
Gli indirizzi sono quelli di MadMapper: '/surfaces/Quad-1/opacity',
'/master/Global_BPM/BPM'... find_parameters li trova, set_parameter li scrive.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:  # mcp SDK 2.x
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP, Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mm  # noqa: E402

AUTO, JOB, STOP = HERE / "madmapper_auto.py", HERE / "job.json", HERE / "stop.flag"
SCENES, FX, AUTOMAP = HERE / "scenes", HERE / "fx.json", HERE / "automap.json"
CALIB = HERE / "calibrazione.json"

mcp = FastMCP("madmapper")
_blackout_level: float | None = None


def _dentro(base: Path, nome: str, suffisso: str = "") -> Path:
    """Percorso di `nome` dentro `base`. Rifiuta nomi vuoti e percorsi che escono
    dalla cartella (es. '../x', percorsi assoluti): i nomi arrivano dal modello."""
    if not nome or not nome.strip():
        raise ValueError("nome vuoto")
    radice = base.resolve()
    p = (radice / f"{nome}{suffisso}").resolve()
    if radice != p.parent and radice not in p.parents:
        raise ValueError(f"'{nome}' esce dalla cartella {base.name}/: usa un nome semplice")
    return p


def _surfaces() -> list[str]:
    return sorted(mm.children("/surfaces"))


def _media(name: str) -> str:
    """Address di un media dal nome della lista ('Bar Code') o dalla sua chiave ('Bar_Code')."""
    medias = mm.children("/medias")
    for k, n in medias.items():
        if name in (k, n.get("DESCRIPTION")) or name.replace(" ", "_") == k:
            return f"/medias/{k}"
    raise ValueError(f"media '{name}' inesistente. Ci sono: {sorted(medias)}")


def _display(name: str) -> str:
    """Il nome che va scritto in /surfaces/X/visual/name: quello della lista di
    MadMapper ('Bar Code'), non la chiave OSC ('Bar_Code'). Verificato dal vivo:
    con la chiave il nome viene accettato ma il media non cambia."""
    key = _media(name).rsplit("/", 1)[-1]
    return mm.children("/medias")[key].get("DESCRIPTION") or key


def _v(address: str, default=None):
    try:
        return mm.value(address)
    except RuntimeError:
        return default


# ---------------------------------------------------------------- lettura

@mcp.tool()
def get_state() -> dict:
    """Panoramica di MadMapper: superfici (opacita', visibilita', media assegnato),
    media caricati, master, BPM, uscite video. Usalo sempre per primo."""
    root = mm.tree()
    c = root.get("CONTENTS") or {}

    def val(node, *keys):
        for k in keys:
            node = (node.get("CONTENTS") or {}).get(k) or {}
        return mm.first(node.get("VALUE"))

    surf = []
    for name, n in (c.get("surfaces", {}).get("CONTENTS") or {}).items():
        if "CONTENTS" in n:
            surf.append({"name": name, "opacity": val(n, "opacity"), "visible": val(n, "visible"),
                         "media": val(n, "visual", "name")})
    master = c.get("master", {})
    return {
        "surfaces": surf,
        "medias": [k for k, n in (c.get("medias", {}).get("CONTENTS") or {}).items()
                   if "assign" in (n.get("CONTENTS") or {})],
        "cue_bank": val(c.get("cues", {}), "active_bank"),
        "master_level": val(master, "master_level"),
        "master_video_level": val(master, "master_video_level"),
        "bpm": val(master, "Global_BPM", "BPM"),
        "outputs": {k: val(n, "enabled") for k, n in (c.get("outputs", {}).get("CONTENTS") or {}).items()},
        "osc": f"{mm.HOST}:{mm.osc_port()} (query {mm.QPORT})",
    }


@mcp.tool()
def find_parameters(query: str, limit: int = 40) -> list[dict]:
    """Cerca parametri nell'albero di MadMapper e restituisce il loro 'address'.
    Passalo a set_parameter per cambiare QUALSIASI cosa: opacita', posizione,
    parametri dei media e dei moduli, fixture DMX, uscite.
    Query di esempio: 'opacity', 'Quad-1', 'BPM', 'geometry', 'speed'."""
    q, out = query.lower(), []
    for a, n in mm.walk(mm.tree()):
        if q in a.lower() or q in str(n.get("DESCRIPTION", "")).lower():
            item = {"address": a, "type": n.get("TYPE"), "value": mm.first(n.get("VALUE"))}
            if n.get("RANGE"):
                item["range"] = n["RANGE"]
            if n.get("DESCRIPTION"):
                item["description"] = n["DESCRIPTION"]
            out.append(item)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- scrittura

@mcp.tool()
def set_parameter(address: str, value: Any = None, verify: bool = True) -> str:
    """Scrive un parametro dato l'address (da find_parameters).
    Esempi:
      set_parameter('/surfaces/Quad-1/opacity', 0.5)
      set_parameter('/surfaces/*/visible', False)        wildcard: tutte le superfici
      set_parameter('/master/Global_BPM/TAP')            senza valore = trigger
      set_parameter('/surfaces/Quad-1/geometry/x', [0.1, 0.2])   piu' valori
    verify rilegge il valore da MadMapper (non possibile con le wildcard)."""
    letto = mm.write(address, value, verify=verify)
    return f"{address} = {letto!r}"


@mcp.tool()
def osc_send(address: str, args: list | None = None) -> str:
    """Messaggio OSC grezzo, senza controlli: per tutto quello che non e' nell'albero."""
    mm.send(address, *(args or []))
    return f"inviato {address} {args or ''}"


@mcp.tool()
def master(level: float | None = None, video: float | None = None,
           dmx: float | None = None, laser: float | None = None) -> str:
    """Livelli master, da 0.0 a 1.0. Passa solo quelli da cambiare."""
    fatti = []
    for k, v in (("master_level", level), ("master_video_level", video),
                 ("master_dmx_level", dmx), ("master_laser_level", laser)):
        if v is not None:
            mm.write(f"/master/{k}", float(v))
            fatti.append(f"{k}={v}")
    return ", ".join(fatti) or "nessuna modifica"


@mcp.tool()
def blackout(on: bool = True) -> str:
    """Blackout istantaneo (master a 0). on=False rimette il livello di prima."""
    global _blackout_level
    if on:
        cur = _v("/master/master_level", 1.0)
        if cur:
            _blackout_level = float(cur)
        mm.write("/master/master_level", 0.0)
        return "blackout"
    mm.write("/master/master_level", _blackout_level if _blackout_level is not None else 1.0)
    return f"luce: master a {_blackout_level if _blackout_level is not None else 1.0}"


@mcp.tool()
def freeze(engine: bool | None = None, output: bool | None = None) -> str:
    """Congela il motore (tutto fermo) o solo l'uscita video (l'immagine resta,
    intanto dietro puoi preparare il cambio)."""
    if engine is not None:
        mm.write("/master/freeze_engine", engine)
    if output is not None:
        mm.write("/master/freeze_video_output", output)
    return f"freeze engine={engine} output={output}"


@mcp.tool()
def set_bpm(bpm: float) -> str:
    """BPM globale di MadMapper (lo seguono anche gli effetti a battute)."""
    mm.write("/master/Global_BPM/BPM", float(bpm), verify=True)
    return f"bpm = {bpm}"


@mcp.tool()
def tap() -> str:
    """Un colpo di tap tempo."""
    mm.write("/master/Global_BPM/TAP", 1)
    return "tap"


@mcp.tool()
def output_enable(enabled: bool, output: int = 1) -> str:
    """Accende/spegne un'uscita video (Video-Output-N)."""
    mm.write(f"/outputs/Video-Output-{output}/enabled", enabled)
    return f"Video-Output-{output} enabled={enabled}"


# ---------------------------------------------------------------- superfici e media

@mcp.tool()
def surface_set(surface: str, param: str, value: Any) -> str:
    """Un parametro di una superficie: surface_set('Quad-1', 'opacity', 0.3).
    surface='*' agisce su tutte. Per vedere cosa ha una superficie:
    find_parameters('/surfaces/Quad-1')."""
    return set_parameter(f"/surfaces/{surface}/{param}", value, verify=surface != "*")


@mcp.tool()
def assign_media(media: str, surfaces: list[str] | None = None) -> str:
    """Manda un media su una o piu' superfici. Senza 'surfaces' lo manda su tutte.
    Il nome e' quello di get_state()['medias'] o quello della lista di MadMapper."""
    if not surfaces:
        mm.write(f"{_media(media)}/assign_to_all_surfaces")
        return f"'{media}' su tutte le superfici"
    nome = _display(media)
    for s in _targets(surfaces):
        mm.write(f"/surfaces/{s}/visual/name", nome, verify=True)
    return f"'{nome}' su {', '.join(surfaces)}"


@mcp.tool()
def select_media(name: str) -> str:
    """Seleziona un media per nome (come cliccarlo nella lista media)."""
    mm.write(f"{_media(name)}/select")
    return f"media selezionato: {name}"


@mcp.tool()
def cue(column: int, row: int, bank: int = 1) -> str:
    """Lancia la cue nella griglia del Cue Editor: colonna 1-16, riga 1-7."""
    mm.write(f"/cues/Bank-{bank}/cues/by_cell/col_{column}/row_{row}")
    return f"cue bank {bank} colonna {column} riga {row}"


@mcp.tool()
def cue_scene(column: int, bank: int = 1) -> str:
    """Lancia la scena (colonna intera) del Cue Editor, 1-16."""
    mm.write(f"/cues/Bank-{bank}/scenes/by_cell/col_{column}")
    return f"scena bank {bank} colonna {column}"


@mcp.tool()
def cue_go(action: str = "next", bank: int = 1) -> str:
    """Scorre le cue: 'next', 'previous', 'selected', oppure un numero di cue."""
    b = f"/cues/Bank-{bank}/cues"
    if action.isdigit():
        mm.write(f"{b}/start_by_number", int(action))
    elif action in ("next", "previous", "selected"):
        mm.write(f"{b}/start_{action}")
    else:
        raise ValueError("action: next, previous, selected o un numero")
    return f"cue {action} (bank {bank})"


# ---------------------------------------------------------------- automapping

def _uv() -> str:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")
    if not Path(uv).exists() and not shutil.which("uv"):
        raise RuntimeError("uv non trovato: serve per lanciare automap.py (opencv)")
    return uv


@mcp.tool()
def calibrate(screen: int = 1, camera: int = 0, dots: bool = False) -> str:
    """Calibrazione + ricerca delle superfici, in un colpo solo (~15 s).

    Spegne l'uscita di MadMapper, proietta scacchiera / bianco / nero sul
    proiettore (screen 1 = il secondo display), scatta con la webcam, calcola
    l'omografia e trova le superfici fisiche illuminate. Poi riaccende l'uscita.
    dots=True usa i dischi al posto della scacchiera (superfici gia' dipinte).
    Il risultato va in automap.json; poi automap() lo scrive in MadMapper.
    Guarda automap_debug.jpg per vedere cosa ha trovato."""
    out_addr = "/outputs/Video-Output-1/enabled"
    era_acceso = _v(out_addr)
    if era_acceso:
        mm.write(out_addr, False)
    try:
        cmd = [_uv(), "run", "--script", str(HERE / "automap.py"), "auto",
               f"--schermo={screen}", f"--camera={camera}"] + (["--punti"] if dots else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=55,
                           stdin=subprocess.DEVNULL, cwd=str(HERE))
    finally:
        if era_acceso:
            mm.write(out_addr, True)
    righe = [x for x in (r.stdout or "").splitlines() if x.startswith("{")]
    if not righe:
        raise RuntimeError(f"automap.py non ha risposto: {(r.stderr or r.stdout)[-600:]}")
    res = json.loads(righe[-1])
    if "errore" in res:
        raise RuntimeError(res["errore"])
    AUTOMAP.write_text(json.dumps(res, indent=1), encoding="utf-8")
    return (f"calibrazione: {res['misura']}\n{len(res['quad'])} superfici trovate "
            f"(da sinistra a destra), proiettore {res['proiettore']}.\n"
            f"Controlla {HERE / res['debug']}, poi automap(). Se ne mancano o sono "
            f"sbagliate, automap_points() con i punti presi da {res['foto']}.")


def _canvas(canvas: str) -> tuple[int, int]:
    if not canvas:
        return mm.CANVAS
    w, h = canvas.lower().split("x")
    return int(w), int(h)


def _write_quads(quads_px: list, size: list, surfaces: list[str] | None,
                 canvas: str, dry_run: bool) -> str:
    targets = surfaces or _surfaces()
    tela, righe = _canvas(canvas), []
    for s, q in zip(targets, quads_px):
        tl_tr_br_bl = [mm.to_canvas(x, y, size[0], size[1], tela) for x, y in mm.order_quad(q)]
        punti = [tl_tr_br_bl[i] for i in mm.QUAD_HANDLE]
        if dry_run:
            righe.append(f"{s}: alto-sx {tl_tr_br_bl[0]} alto-dx {tl_tr_br_bl[1]} "
                         f"basso-dx {tl_tr_br_bl[2]} basso-sx {tl_tr_br_bl[3]}")
            continue
        hs = mm.handles(s)
        if len(hs) != 4:
            raise RuntimeError(f"'{s}' ha {len(hs)} handle: serve una Quad (4 angoli). "
                               f"Superfici presenti: {_surfaces()}")
        for (ax, ay), (x, y) in zip(hs, punti):
            mm.write(ax, x, verify=True)
            mm.write(ay, y, verify=True)
        righe.append(f"{s}: ok")
    extra = ""
    if len(quads_px) != len(targets):
        extra = (f"\nattenzione: {len(quads_px)} forme e {len(targets)} superfici, "
                 f"mappate le prime {min(len(quads_px), len(targets))}")
    return "\n".join(righe) + extra


@mcp.tool()
def automap(surfaces: list[str] | None = None, canvas: str = "",
            dry_run: bool = False) -> str:
    """Scrive in MadMapper le superfici trovate da calibrate().

    Le forme vanno da sinistra a destra e finiscono, in ordine, sulle superfici
    'surfaces' (default: tutte, in ordine alfabetico). Le superfici devono gia'
    esistere in MadMapper (una Quad per oggetto).
    canvas: risoluzione della tela di uscita di MadMapper, es. '1920x1080'
    (default MADMAPPER_CANVAS). dry_run=True mostra senza scrivere.
    Poi guarda il risultato con output_view()."""
    if not AUTOMAP.exists():
        raise RuntimeError("prima calibrate()")
    res = json.loads(AUTOMAP.read_text(encoding="utf-8-sig"))
    return _write_quads(res["quad"], res["proiettore"], surfaces, canvas, dry_run)


@mcp.tool()
def automap_points(points: dict[str, list[list[float]]], canvas: str = "",
                   dry_run: bool = False) -> str:
    """Mappatura a mano dalla foto: {superficie: [[x,y] x4]} in pixel della foto
    della webcam (automap_bianco.jpg), in qualsiasi ordine. Li converte con
    l'omografia di calibrate() e li scrive. Serve quando il rilevatore sbaglia."""
    if not CALIB.exists():
        raise RuntimeError("prima calibrate(): serve l'omografia")
    c = json.loads(CALIB.read_text(encoding="utf-8-sig"))
    H, size = c["H_camera_verso_proiettore"], c["dimensioni"]["proiettore"]
    nomi = list(points)
    quads = [mm.homography(H, points[n]) for n in nomi]
    return _write_quads(quads, size, nomi, canvas, dry_run)


@mcp.tool()
def output_view(display: str = "") -> Image:
    """Cattura quello che sta uscendo sul proiettore (lo schermo non primario,
    o 'display' tipo 'Display 2'). Per controllare mappatura ed effetti senza
    essere davanti al palco."""
    import ctypes
    import io
    import displays
    from PIL import ImageGrab
    if os.name == "nt":
        ctypes.windll.user32.SetProcessDPIAware()
    ds = [d for d in displays.list_displays() if d.get("active")]
    d = next((d for d in ds if d["name"] == display), None) if display else \
        next((d for d in ds if not d.get("primary")), None)
    if d is None:
        raise RuntimeError(f"display non trovato: {[x['name'] for x in ds]}")
    img = ImageGrab.grab(bbox=(d["x"], d["y"], d["x"] + d["width"], d["y"] + d["height"]),
                         all_screens=True)
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return Image(data=buf.getvalue(), format="jpeg")


# ---------------------------------------------------------------- progetti (.mad)
#
# Via OSC non si creano superfici e non si aprono progetti. Ma il .mad e' un
# QDataStream Qt che madfile.py legge e riscrive identico: si aggiungono le
# Quad nel file e si fa ripartire MadMapper con quel file.

EXE_FILE = HERE / "madmapper_exe.txt"


def _ps(cmd: str, timeout: int = 30) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                       text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return (r.stdout or "").strip()


def _exe() -> str:
    """Dove sta MadMapper.exe: dal processo in esecuzione (e lo ricorda), poi
    da MADMAPPER_EXE, poi dall'ultima volta che l'ha visto."""
    p = _ps("(Get-Process MadMapper -ErrorAction SilentlyContinue | Select -First 1).Path")
    if p:
        EXE_FILE.write_text(p, encoding="utf-8")
        return p
    for c in (os.environ.get("MADMAPPER_EXE", ""),
              EXE_FILE.read_text(encoding="utf-8-sig").strip() if EXE_FILE.exists() else ""):
        if c and Path(c).exists():
            return c
    raise RuntimeError("non so dove sta MadMapper.exe: aprilo una volta (lo ricordo) "
                       "oppure imposta MADMAPPER_EXE")


def _running() -> bool:
    return bool(_ps("(Get-Process MadMapper -ErrorAction SilentlyContinue).Id"))


@mcp.tool()
def project_open(path: str, fullscreen: bool = True, keep_state: bool = True,
                 file_media_for: list[str] | None = None) -> str:
    """Apre un progetto .mad: chiude MadMapper e lo riapre con quel file.
    keep_state=True fotografa prima lo stato dal vivo (angoli delle superfici,
    media, opacita', parametri) e lo rimette dopo, sulle superfici che esistono
    ancora: la mappatura fatta via OSC e non salvata non si perde.
    Ritorna subito: MadMapper ci mette ~30-40 s a ripartire, un job staccato
    ('riapri') aspetta, rimette lo stato e l'uscita a tutto schermo.
    file_media_for: superfici il cui media va preso dal file e non dallo stato
    di prima (serve quando il file assegna un media nuovo).
    Seguilo con job_status(). Uso in allestimento, non durante lo show."""
    f = Path(path)
    if not f.is_absolute():
        f = HERE / f
    if not f.exists():
        raise RuntimeError(f"progetto inesistente: {f}")
    if "in corso" in job_status():
        job_stop()
    exe = _exe()
    stato = {}
    if keep_state and _running():
        import madmapper_auto
        try:
            stato = madmapper_auto.state()
        except RuntimeError:
            pass
    if file_media_for:
        # tutto /visual/ (nome, id, tipo, numero): se no l'id vecchio rimette il media vecchio
        stato = {a: v for a, v in stato.items()
                 if not ("/visual/" in a and a.split("/")[2] in file_media_for)}
    SCENES.mkdir(exist_ok=True)
    salvataggio = SCENES / "_prima_di_project_open.json"
    salvataggio.write_text(json.dumps(stato, ensure_ascii=False), encoding="utf-8")
    riapri = HERE / "riapri.json"
    riapri.write_text(json.dumps({"stato": stato, "fullscreen": fullscreen},
                                 ensure_ascii=False), encoding="utf-8")
    if _running():
        # chiusura, poi si aspetta che il processo sia davvero sparito e si da'
        # qualche secondo in piu': ripartire subito lo manda in crash
        _ps("Get-Process MadMapper | % { $_.CloseMainWindow() | Out-Null }; Start-Sleep 3; "
            "Get-Process MadMapper -ErrorAction SilentlyContinue | Stop-Process -Force; "
            "Wait-Process -Name MadMapper -Timeout 15 -ErrorAction SilentlyContinue; Start-Sleep 5")
    q = lambda x: str(x).replace("'", "''")  # noqa: E731  (apici per PowerShell)
    # Un'attivita' pianificata parte nella sessione dell'utente e non e' figlia
    # di Claude: MadMapper resta aperto anche se Claude si chiude o si riavvia.
    # (WMI lo avvia senza desktop e MadMapper muore; un figlio diretto morirebbe
    # con Claude.) Le opzioni batteria servono: di default su un portatile a
    # batteria l'attivita' resta 'in coda' e non parte.
    out = _ps(f"$a = New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '\"{q(f)}\"' "
              f"-WorkingDirectory '{q(Path(exe).parent)}'; "
              "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
              "-DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew; "
              "Register-ScheduledTask -TaskName 'MadMapperMCP' -Action $a -Settings $s -Force "
              "| Out-Null; Start-ScheduledTask -TaskName 'MadMapperMCP'; 'ok'")
    if not out.endswith("ok"):
        raise RuntimeError(f"avvio di MadMapper fallito: {out[-300:]}")
    stato_job = _run(["riapri", str(riapri)], detach=True)
    return (f"MadMapper in riapertura con {f.name} ({len(stato)} parametri da rimettere). "
            f"Ci mette ~30-40 s: segui con job_status(). Se qualcosa va storto lo stato di "
            f"prima e' in scene_recall('_prima_di_project_open').\n{stato_job}")


@mcp.tool()
def project_add_quads(project: str, names: list[str], open: bool = True,
                      output: str = "") -> str:
    """Aggiunge Quad a un progetto .mad SALVATO, clonando la prima Quad che c'e'
    (in MadMapper salva prima con Ctrl+S). Scrive un nuovo file
    ('<nome>_mcp.mad' accanto all'originale, o 'output'): l'originale non si
    tocca; un file che e' gia' un '_mcp.mad' viene aggiornato.
    open=True lo apre subito in MadMapper (vedi project_open: chiude e riapre).
    Poi le posizioni si danno con automap / automap_points."""
    import madfile
    src = Path(project)
    if not src.is_absolute():
        src = HERE / src
    ver, root = madfile.load(src)
    fatti = madfile.add_quads(root, names)
    dest = Path(output) if output else (src if src.stem.endswith("_mcp")
                                         else src.with_name(f"{src.stem}_mcp.mad"))
    if not dest.is_absolute():
        dest = src.parent / dest
    dest.write_bytes(madfile.dump(ver, root))
    tutte = [madfile.get(s.v, "name").v for s in madfile.surfaces(root)]
    msg = f"aggiunte {fatti or 'nessuna (gia presenti)'} -> {dest}\nsuperfici nel file: {tutte}"
    if open:
        msg += "\n" + project_open(str(dest))
    return msg


@mcp.tool()
def project_remove_quads(project: str, names: list[str], open: bool = True,
                         output: str = "") -> str:
    """Toglie superfici da un progetto .mad SALVATO (Ctrl+S in MadMapper).
    Scrive '<nome>_mcp.mad' (o 'output'), l'originale non si tocca; open=True
    lo apre subito (chiude e riapre MadMapper, la mappatura dal vivo delle
    superfici rimaste viene rimessa)."""
    import madfile
    src = Path(project)
    if not src.is_absolute():
        src = HERE / src
    ver, root = madfile.load(src)
    via = madfile.remove_quads(root, names)
    if not via:
        tutte = [madfile.get(s.v, "name").v for s in madfile.surfaces(root)]
        raise ValueError(f"nessuna di {names} e' nel file. Ci sono: {tutte}")
    dest = Path(output) if output else (src if src.stem.endswith("_mcp")
                                         else src.with_name(f"{src.stem}_mcp.mad"))
    if not dest.is_absolute():
        dest = src.parent / dest
    dest.write_bytes(madfile.dump(ver, root))
    tutte = [madfile.get(s.v, "name").v for s in madfile.surfaces(root)]
    msg = f"tolte {via} -> {dest}\nsuperfici rimaste: {tutte}"
    if open:
        msg += "\n" + project_open(str(dest))
    return msg


@mcp.tool()
def project_add_material(project: str, material: str, surfaces: list[str] | None = None,
                         open: bool = True, output: str = "") -> str:
    """Mette nel progetto un materiale della libreria di MadMapper, anche uno
    tuo in Documenti/MadMapper/Materials/<nome>/<nome>.fs (formato materiale
    MadMapper: funzione materialColorForPixel), e lo assegna alle 'surfaces'.
    Via OSC non si possono aggiungere media: si passa dal file .mad salvato,
    come project_add_quads (scrive '<nome>_mcp.mad' e lo riapre)."""
    import madfile
    src = Path(project)
    if not src.is_absolute():
        src = HERE / src
    ver, root = madfile.load(src)
    vid = madfile.add_material(root, material, surfaces)
    dest = Path(output) if output else (src if src.stem.endswith("_mcp")
                                         else src.with_name(f"{src.stem}_mcp.mad"))
    if not dest.is_absolute():
        dest = src.parent / dest
    dest.write_bytes(madfile.dump(ver, root))
    msg = f"materiale '{material}' (id {vid}) nel progetto {dest.name}, su {surfaces or 'nessuna superficie'}"
    if open:
        msg += "\n" + project_open(str(dest), file_media_for=surfaces)
    return msg


@mcp.tool()
def output_fullscreen(on: bool = True) -> str:
    """Uscita video a tutto schermo sul proiettore (o in finestra con on=False)."""
    mm.write("/application/view/fullscreen", 1 if on else 0)
    return f"fullscreen {'on' if on else 'off'}"


# ---------------------------------------------------------------- job

def _run(args: list[str], detach: bool) -> str:
    """Lancia madmapper_auto.py. detach=True: sopravvive alla chiusura di Claude."""
    cmd = [sys.executable, str(AUTO)] + args
    env = {**os.environ, "MADMAPPER_HOST": mm.HOST, "MADMAPPER_QUERY_PORT": str(mm.QPORT)}
    if not detach:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env,
                           stdin=subprocess.DEVNULL, cwd=str(HERE))
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout)[-400:])
        return (r.stdout or "ok").strip()
    log = (HERE / f"job_{args[0]}.log").open("w", encoding="utf-8")
    err = (HERE / f"job_{args[0]}.err").open("w", encoding="utf-8")
    kw = ({"creationflags": 0x00000008 | 0x00000200} if os.name == "nt"
          else {"start_new_session": True})
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=log, stderr=err,
                            stdin=subprocess.DEVNULL, env=env, **kw)
    # si aspetta il primo battito invece di un tempo fisso: su Windows un Python
    # appena installato da uv (antivirus) puo' metterci parecchi secondi a partire
    while time.time() - t0 < 15:
        if JOB.exists() and JOB.stat().st_mtime >= t0 - 1:
            return job_status()
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    errore = (HERE / f"job_{args[0]}.err").read_text(encoding="utf-8", errors="replace")[-600:]
    raise RuntimeError(f"il job '{args[0]}' non e' partito: {errore or 'nessun errore scritto'}")


@mcp.tool()
def job_start(mode: str, file: str) -> str:
    """Processo autonomo che continua anche a Claude chiuso.
    mode 'record': registra in un .jsonl tutto quello che cambia in MadMapper.
    mode 'play':   riproduce una registrazione.
    mode 'auto':   pilota automatico da un .json (media a rotazione, modulazioni, effetti).
    'file' e' relativo alla cartella del server. Fermalo con job_stop."""
    if mode not in ("record", "play", "auto"):
        raise ValueError("mode: record, play o auto")
    if "in corso" in job_status():
        raise RuntimeError(f"c'e' gia' un job attivo, fermalo prima: {job_status()}")
    return _run([mode, str(_dentro(HERE, file))], detach=True)


@mcp.tool()
def job_stop() -> str:
    """Ferma il job in corso in modo pulito (gli effetti rimettono le opacita')."""
    STOP.write_text("stop", encoding="utf-8")
    time.sleep(1.5)
    return job_status()


@mcp.tool()
def job_status() -> str:
    """C'e' un job attivo? Cosa sta facendo?"""
    if not JOB.exists():
        return "nessun job mai avviato"
    try:
        j = json.loads(JOB.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        time.sleep(0.3)
        j = json.loads(JOB.read_text(encoding="utf-8-sig"))
    age = time.time() - j.get("last_tick", 0)
    stato = "in corso" if age < 5 and not STOP.exists() else "fermo"
    return f"{j['mode']}: {stato} — {j.get('note', '')} (ultimo segnale {age:.0f}s fa)"


@mcp.tool()
def scene_save(name: str) -> str:
    """Fotografa lo stato di MadMapper (superfici, media assegnati, master,
    parametri di media e moduli) come scena richiamabile."""
    return _run(["snapshot", str(_dentro(SCENES, name, ".json"))], detach=False)


@mcp.tool()
def scene_recall(name: str) -> str:
    """Rimette MadMapper nella scena salvata."""
    f = _dentro(SCENES, name, ".json")
    if not f.exists():
        raise RuntimeError(f"scena '{name}' inesistente: {scene_list()}")
    return _run(["restore", str(f)], detach=False)


@mcp.tool()
def scene_list() -> list[str]:
    """Scene salvate."""
    return sorted(p.stem for p in SCENES.glob("*.json")) if SCENES.exists() else []


# ---------------------------------------------------------------- effetti a battute

def _fx_save(sezione: str, cfg: dict | None) -> str:
    """Aggiorna fx.json e fa ripartire il pilota con la nuova config."""
    data = json.loads(FX.read_text(encoding="utf-8-sig")) if FX.exists() else {}
    data.pop(sezione, None)
    if cfg:
        data[sezione] = cfg
    return _fx_write(data, sezione)


def _fx_write(data: dict, cosa: str = "") -> str:
    FX.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    if "in corso" in job_status():
        job_stop()
    attivi = [k for k in ("strobe", "chase", "mix", "media", "modulate", "sections") if data.get(k)]
    if not attivi:
        return f"{cosa}: spento. Nessun effetto attivo, pilota fermo."
    return f"{_run(['auto', str(FX)], detach=True)}\nattivi: {', '.join(attivi)}"


def _targets(surfaces: list[str] | None) -> list[str]:
    tutte = _surfaces()
    if not surfaces:
        return tutte
    manca = [s for s in surfaces if s not in tutte]
    if manca:
        raise ValueError(f"superfici inesistenti: {manca}. Ci sono: {tutte}")
    return surfaces


@mcp.tool()
def fx_strobe(beats: float = 0.5, duty: float = 0.4, surfaces: list[str] | None = None,
              on: float = 1.0, off: float = 0.0) -> str:
    """Strobo a tempo sull'opacita' delle superfici, agganciato al BPM di MadMapper.
    beats = durata di un lampo (0.25 sedicesimi, 1 quarto); duty = quanto resta
    acceso (0.1 flash secco, 0.5 onda quadra). Senza surfaces: tutte. beats=0 spegne."""
    if beats <= 0:
        return _fx_save("strobe", None)
    return _fx_save("strobe", {"surfaces": _targets(surfaces), "beats": beats,
                               "duty": duty, "on": on, "off": off})


@mcp.tool()
def fx_chase(beats: float = 1, order: str = "name", surfaces: list[str] | None = None,
             seed: int = 0, on: float = 1.0, off: float = 0.0) -> str:
    """Una superficie accesa alla volta, una ogni 'beats' battute.
    order: 'name' (alfabetico), 'reverse', 'random' (permutazione fissa col seed)
    oppure l'ordine in 'surfaces'='custom'. beats=0 spegne."""
    if beats <= 0:
        return _fx_save("chase", None)
    s = _targets(surfaces)
    if order == "reverse":
        s = s[::-1]
    elif order == "random":
        s = random.Random(seed).sample(s, len(s))
    elif order == "name" and not surfaces:
        s = sorted(s)
    return _fx_save("chase", {"surfaces": s, "beats": beats, "on": on, "off": off})


@mcp.tool()
def fx_mix(beats: float = 2, seed: int = 0, surfaces: list[str] | None = None,
           on: float = 1.0, off: float = 0.0) -> str:
    """Salti caotici ma ripetibili: ogni 'beats' si accende una superficie a caso,
    mai la stessa due volte di fila. Stesso seed = stessa sequenza tutte le sere.
    beats=0 spegne."""
    if beats <= 0:
        return _fx_save("mix", None)
    return _fx_save("mix", {"surfaces": _targets(surfaces), "beats": beats,
                            "seed": seed, "on": on, "off": off})


@mcp.tool()
def fx_media(surface: str, clips: list[str], beats: float = 8) -> str:
    """Cambia media su una superficie ogni 'beats' battute, a caso dalla lista,
    mai lo stesso di fila. clips=[] toglie la rotazione da quella superficie."""
    data = json.loads(FX.read_text(encoding="utf-8-sig")) if FX.exists() else {}
    rot = [m for m in data.get("media", []) if m["surface"] != surface]
    if clips:
        rot.append({"surface": _targets([surface])[0], "beats": beats,
                    "clips": [_display(c) for c in clips]})
    return _fx_save("media", rot or None)


@mcp.tool()
def fx_modulate(address: str, shape: str = "sine", beats: float = 4,
                min: float = 0.0, max: float = 1.0) -> str:
    """Muove un parametro nel tempo: shape 'sine' (oscilla), 'ramp' (sale e
    riparte), 'random' (un valore nuovo ogni ciclo). beats = durata di un ciclo.
    beats=0 toglie la modulazione da quell'address."""
    data = json.loads(FX.read_text(encoding="utf-8-sig")) if FX.exists() else {}
    mods = [m for m in data.get("modulate", []) if m["path"] != address]
    if beats > 0:
        mm.type_of(address)  # solleva subito se MadMapper non risponde
        mods.append({"path": address, "shape": shape, "beats": beats, "min": min, "max": max})
    return _fx_save("modulate", mods or None)


SHOW_ESCLUSI = ("TestCard", "Solid", "Text", "Camera", "Output", "Spectrum", "Waveform")

ENERGIA = {
    # media: ogni quante battute cambia il media su ogni superficie
    # blocchi: la scaletta di effetti che si alterna ogni 'sezione' battute
    "calma": {"media": 32, "sezione": 32, "blocchi": [
        {}, {"chase": {"beats": 4, "on": 1.0, "off": 0.25}}]},
    "media": {"media": 16, "sezione": 16, "blocchi": [
        {}, {"chase": {"beats": 1}}, {}, {"mix": {"beats": 2}}]},
    "alta": {"media": 8, "sezione": 8, "blocchi": [
        {"chase": {"beats": 0.5}}, {"strobe": {"beats": 0.25, "duty": 0.3}},
        {"mix": {"beats": 1}}, {}, {"strobe": {"beats": 0.5, "duty": 0.5}},
        {"chase": {"beats": 0.25}}]},
}


@mcp.tool()
def auto_show(energia: str = "media", surfaces: list[str] | None = None,
              medias: list[str] | None = None, seed: int = 0) -> str:
    """Pilota automatico delle visual sulle superfici mappate, a tempo di BPM.

    Ogni superficie cambia media a rotazione (sfalsate: non cambiano tutte
    insieme), e una scaletta di effetti si alterna a blocchi di battute:
    'calma'  media ogni 32 battute, ogni tanto un chase morbido
    'media'  media ogni 16, alterna pulito / chase / mix
    'alta'   media ogni 8, chase veloci, strobo, mix
    medias: la lista da cui pescare (default: i generatori e i materiali del
    progetto, tranne test, colori pieni, testo, camere e audio).
    Stesso seed = stesso show. Gira staccato anche a Claude chiuso; si ferma con
    fx_stop_all() (le opacita' tornano com'erano). Cambia il BPM con set_bpm/tap."""
    if energia not in ENERGIA:
        raise ValueError(f"energia: {', '.join(ENERGIA)}")
    e = ENERGIA[energia]
    sup = _targets(surfaces)
    if not sup:
        raise RuntimeError("nessuna superficie in MadMapper")
    if medias:
        pool = [_display(m) for m in medias]
    else:
        tutti = mm.children("/medias")
        pool = [n.get("DESCRIPTION") or k for k, n in tutti.items()
                if "assign" in (n.get("CONTENTS") or {})
                and not any(x.lower() in k.lower() for x in SHOW_ESCLUSI)]
    if len(pool) < 2:
        raise RuntimeError(f"servono almeno 2 media da alternare, ho {pool}")
    passo = e["media"] / len(sup)
    data = {
        "seed": seed,
        "media": [{"surface": s, "clips": pool, "beats": e["media"],
                   "offset": round(i * passo, 3)} for i, s in enumerate(sup)],
        "sections": {"beats": e["sezione"],
                     "seq": [{k: {**v, "surfaces": sup, "seed": seed} for k, v in b.items()}
                             for b in e["blocchi"]]},
    }
    esito = _fx_write(data, "show")
    return (f"show '{energia}' su {sup}: {len(pool)} media a rotazione ogni {e['media']} "
            f"battute, {len(e['blocchi'])} blocchi da {e['sezione']}.\n{esito}")


@mcp.tool()
def fx_status() -> str:
    """Effetti configurati e stato del pilota."""
    cfg = FX.read_text(encoding="utf-8-sig") if FX.exists() else "nessun effetto configurato"
    return f"{cfg}\n{job_status()}"


@mcp.tool()
def fx_stop_all() -> str:
    """Spegne tutti gli effetti e il pilota, rimettendo le opacita' di prima."""
    FX.unlink(missing_ok=True)
    return job_stop()


# ---------------------------------------------------------------- dissolvenze, look, media
# Aggiunti confrontando con jaxondoss/map-mapper-agent (fade, preset di look):
# qui pero' si legge il valore di partenza da OSCQuery invece di indovinarlo.

LOOKS = HERE / "looks.json"
FADE_FPS, FADE_MAX = 30, 120.0


def _addr(target: str) -> str:
    """'master' -> livello master, 'Quad-1' -> opacita' di quella superficie,
    '/indirizzo/completo' -> se stesso."""
    if target.startswith("/"):
        return target
    if target.lower() == "master":
        return "/master/master_level"
    if target.lower() in ("video", "master_video"):
        return "/master/master_video_level"
    if target not in _surfaces():
        raise ValueError(f"'{target}' non e' una superficie. Ci sono: {_surfaces()}")
    return f"/surfaces/{target}/opacity"


def _ramp(moves: dict[str, tuple[float, float]], seconds: float, curve: str = "linear") -> None:
    steps = max(1, int(seconds * FADE_FPS))
    t0 = time.monotonic()
    for i in range(1, steps + 1):
        k = i / steps
        if curve == "smooth":
            k = k * k * (3 - 2 * k)
        for a, (da, a_) in moves.items():
            mm.write(a, da + (a_ - da) * k)
        attesa = t0 + i * seconds / steps - time.monotonic()
        if attesa > 0:
            time.sleep(attesa)


@mcp.tool()
def fade(targets: list[str], to: float, seconds: float = 2.0,
         from_value: float | None = None, curve: str = "linear") -> str:
    """Dissolvenza a tempo di uno o piu' bersagli insieme. Blocca fino alla fine.
    targets: nomi di superficie (opacita'), 'master', 'video' (master video) o address
    completi di parametri numerici (es. '/medias/Clouds/Speed').
    Il valore di partenza si legge da MadMapper; from_value lo forza.
    curve: 'linear' | 'smooth'. Massimo 120 s: per fade piu' lunghi usa fx_modulate."""
    if not 0 <= seconds <= FADE_MAX:
        raise ValueError(f"seconds tra 0 e {FADE_MAX}")
    moves = {}
    for t in targets:
        a = _addr(t)
        start = from_value if from_value is not None else _v(a)
        try:
            start = float(start)
        except (TypeError, ValueError):
            raise RuntimeError(f"{a}: valore attuale {start!r} non numerico (passa from_value)")
        moves[a] = (start, float(to))
    _ramp(moves, seconds, curve)
    return f"fade {', '.join(moves)} -> {to} in {seconds}s"


def _numeric_params(root: str) -> dict[str, float]:
    out = {}
    for a, n in mm.walk(mm.tree(root), root):
        if n.get("TYPE") in ("f", "i", "d", "h") and not str(n.get("ACCESS", 3)) == "2":
            v = mm.first(n.get("VALUE"))
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[a] = v
    return out


def _looks() -> dict:
    return json.loads(LOOKS.read_text(encoding="utf-8")) if LOOKS.exists() else {}


@mcp.tool()
def look_save(name: str, roots: list[str], description: str = "") -> str:
    """Salva un 'look': i valori numerici attuali sotto uno o piu' nodi.
    roots: es. ['/medias/Frattali_Chip'] per i parametri di un materiale/shader,
    ['/surfaces/Quad-1'] per una superficie. Piu' mirato di scene_save, e si puo'
    richiamare con una dissolvenza (look_apply)."""
    values = {}
    for r in roots:
        values.update(_numeric_params(r))
    if not values:
        raise RuntimeError(f"nessun parametro numerico sotto {roots}")
    looks = _looks()
    looks[name] = {"description": description, "roots": roots, "values": values}
    LOOKS.write_text(json.dumps(looks, indent=1, ensure_ascii=False), encoding="utf-8")
    return f"look '{name}': {len(values)} parametri salvati"


@mcp.tool()
def look_apply(name: str, seconds: float = 0.0, curve: str = "smooth") -> str:
    """Richiama un look salvato. seconds > 0 = morph dai valori attuali a quelli del look."""
    looks = _looks()
    if name not in looks:
        raise RuntimeError(f"look '{name}' inesistente: {sorted(looks)}")
    target = looks[name]["values"]
    if seconds <= 0:
        for a, v in target.items():
            mm.write(a, v)
        return f"look '{name}' applicato ({len(target)} parametri)"
    moves = {}
    for a, v in target.items():
        cur = _v(a)
        if isinstance(cur, (int, float)) and not isinstance(cur, bool):
            moves[a] = (float(cur), float(v))
    _ramp(moves, min(seconds, FADE_MAX), curve)
    return f"look '{name}' raggiunto in {seconds}s ({len(moves)} parametri)"


@mcp.tool()
def look_list() -> dict:
    """Look salvati, con descrizione e numero di parametri."""
    return {k: {"description": v.get("description", ""), "roots": v.get("roots"),
                "params": len(v.get("values", {}))} for k, v in _looks().items()}


@mcp.tool()
def media_import(path: str) -> str:
    """Importa un file (video, immagine, shader .fs/ISF) nella lista media di MadMapper.
    path: percorso sul PC dove gira MadMapper. Poi assegnalo con assign_media."""
    prima = set(mm.children("/medias"))
    mm.write("/application/medias/add", path)
    time.sleep(0.5)
    nuovi = sorted(set(mm.children("/medias")) - prima)
    return f"importato {path}" + (f" -> {nuovi}" if nuovi else " (non ancora visibile nella lista)")


@mcp.tool()
def media_speed(media: str, speed: float) -> str:
    """Velocita' di riproduzione di un media (il parametro Speed di MadMapper)."""
    a = f"{_media(media)}/Speed"
    return f"{a} = {mm.write(a, float(speed), verify=True)!r}"


@mcp.tool()
def surface_visible(surfaces: list[str], visible: bool = True, solo: bool = False) -> str:
    """Mostra/nasconde superfici. solo=True: mostra SOLO queste e nasconde tutte le altre."""
    tutte, scelte = _surfaces(), _targets(surfaces)
    if solo:
        for s in tutte:
            mm.write(f"/surfaces/{s}/visible", int(s in scelte))
    else:
        for s in scelte:
            mm.write(f"/surfaces/{s}/visible", int(visible))
    return f"{'solo ' if solo else ''}{scelte} visible={visible}"


@mcp.tool()
def show_check() -> dict:
    """Controllo pre-show (sola lettura): master a 0, uscite spente, superfici
    invisibili, a opacita' 0 o senza media, freeze attivo."""
    s = get_state()
    problemi = []
    if (s.get("master_level") or 0) <= 0:
        problemi.append("master_level a 0")
    if s.get("master_video_level") is not None and s["master_video_level"] <= 0:
        problemi.append("master_video_level a 0")
    for o, en in (s.get("outputs") or {}).items():
        if not en:
            problemi.append(f"uscita {o} disattivata")
    for x in s.get("surfaces", []):
        if not x.get("visible"):
            problemi.append(f"superficie {x['name']} invisibile")
        if (x.get("opacity") or 0) <= 0:
            problemi.append(f"superficie {x['name']} a opacita' 0")
        if not x.get("media"):
            problemi.append(f"superficie {x['name']} senza media")
    if _v("/master/freeze_engine"):
        problemi.append("freeze_engine attivo")
    return {"ok": not problemi, "problemi": problemi,
            "superfici": len(s.get("surfaces", [])), "media": len(s.get("medias", [])), "bpm": s.get("bpm")}


if __name__ == "__main__":
    mcp.run()
