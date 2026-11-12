# /// script
# requires-python = ">=3.10"
# dependencies = ["opencv-python>=4.10", "numpy>=1.26"]
# ///
"""Automapping: dove stanno le superfici fisiche, in pixel di proiettore.

  uv run --script automap.py auto [--schermo=1] [--camera=0] [--punti]
  uv run --script automap.py verifica          prova il rilevatore senza hardware

'auto' fa tutto in un colpo, con una finestra a tutto schermo sul proiettore:
  1. scacchiera  -> foto -> omografia camera->proiettore (calibrazione.json)
  2. bianco pieno -> foto, nero pieno -> foto
  3. la differenza bianco-nero accende solo cio' che il proiettore colpisce;
     ogni macchia chiara a 4 lati e' una superficie
L'ultima riga stampata e' un JSON: misura, risoluzione, quadrilateri in pixel
di proiettore (alto-sx, alto-dx, basso-dx, basso-sx), foto salvate.

Why bianco/nero e non una foto sola: la differenza cancella la luce ambiente,
le luci di scena e tutto quello che il proiettore non tocca.
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def superfici(bianco, nero, min_frac: float = 0.002, max_frac: float = 0.6,
              soglia: int = 0) -> list[np.ndarray]:
    """Quadrilateri (pixel camera) delle superfici illuminate dal proiettore.

    soglia=0 -> Otsu. min_frac/max_frac: area minima/massima rispetto alla foto,
    per scartare puntini e il muro di fondo intero.
    ponytail: solo superfici a 4 lati e ben staccate dal fondo; per forme
    complesse o oggetti che si toccano si passa ai punti a mano."""
    g = cv2.cvtColor(cv2.absdiff(bianco, nero), cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (5, 5), 0)
    if soglia:
        m = cv2.threshold(g, soglia, 255, cv2.THRESH_BINARY)[1]
    else:
        m = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if not (g.size * min_frac <= a <= g.size * max_frac):
            continue
        quad = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(quad) != 4 or not cv2.isContourConvex(quad):
            # forma non a 4 lati: si prende il rettangolo minimo che la contiene
            quad = cv2.boxPoints(cv2.minAreaRect(c))
        out.append(np.asarray(quad, np.float32).reshape(4, 2))
    return out


def ordina(q) -> list[list[float]]:
    s, d = q.sum(1), q[:, 0] - q[:, 1]
    return [q[np.argmin(s)].tolist(), q[np.argmax(d)].tolist(),
            q[np.argmax(s)].tolist(), q[np.argmin(d)].tolist()]


def auto(schermo: int = 1, camera: int = 0, punti: bool = False) -> int:
    import ctypes.wintypes  # noqa: F401  (serve a calibra.schermi)
    import calibra
    import webcam

    x, y = calibra.schermi()[schermo][:2]
    w, h = calibra.raster(schermo)
    nome = "automap"
    cv2.namedWindow(nome, cv2.WINDOW_NORMAL)
    cv2.moveWindow(nome, x, y)
    cv2.setWindowProperty(nome, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    try:
        cv2.setWindowProperty(nome, cv2.WND_PROP_TOPMOST, 1)
    except AttributeError:
        pass

    def scatto(img, file: str):
        cv2.imshow(nome, img)
        cv2.waitKey(700)             # il proiettore ha latenza: si aspetta che cambi davvero
        webcam.scatta(HERE / file, camera)
        return cv2.imread(str(HERE / file))

    try:
        pattern, p_proj = (calibra.crea_punti if punti else calibra.crea_pattern)(w, h)
        foto_p = scatto(pattern, "automap_pattern.jpg")
        bianco = scatto(np.full((h, w, 3), 255, np.uint8), "automap_bianco.jpg")
        nero = scatto(np.zeros((h, w, 3), np.uint8), "automap_nero.jpg")
    finally:
        cv2.destroyAllWindows()

    angoli = (calibra.trova_punti if punti else calibra.trova_angoli)(foto_p)
    if angoli is None:
        print(json.dumps({"errore": "pattern non trovato in automap_pattern.jpg: tutto "
                          "nell'inquadratura, a fuoco, senza riflessi?"}))
        return 1
    H, misura = calibra.omografia(angoli, p_proj)
    calibra.salva(H, misura, {"camera": [int(foto_p.shape[1]), int(foto_p.shape[0])],
                              "proiettore": [w, h]})

    quads = superfici(bianco, nero)
    debug = bianco.copy()
    for i, q in enumerate(quads):
        cv2.polylines(debug, [q.astype(np.int32)], True, (0, 0, 255), 3)
        cv2.putText(debug, str(i), tuple(int(v) for v in q.mean(0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
    cv2.imwrite(str(HERE / "automap_debug.jpg"), debug)

    proj = [ordina(calibra.applica(H, q)) for q in quads]
    proj.sort(key=lambda q: sum(p[0] for p in q))      # da sinistra a destra
    print(json.dumps({"misura": misura, "proiettore": [w, h], "quad": proj,
                      "foto": "automap_bianco.jpg", "debug": "automap_debug.jpg"}))
    return 0


def verifica() -> int:
    """Due superfici storte su fondo scuro, luce ambiente diversa: le ritrova?"""
    W, H = 1280, 720
    nero = np.full((H, W, 3), 30, np.uint8)
    bianco = nero.copy()
    cv2.rectangle(bianco, (0, 0), (W, H), (70, 70, 70), -1)   # muro lontano, poco illuminato
    vere = [np.float32([[200, 150], [480, 180], [460, 520], [190, 490]]),
            np.float32([[700, 200], [1050, 170], [1080, 450], [720, 470]])]
    for q in vere:
        cv2.fillConvexPoly(bianco, q.astype(np.int32), (230, 230, 230))
    trovate = sorted(superfici(bianco, nero), key=lambda q: q[:, 0].mean())
    if len(trovate) != 2:
        print(f"FALLITO: trovate {len(trovate)} superfici invece di 2")
        return 1
    scarto = max(float(np.abs(np.float32(ordina(t)) - v).max()) for t, v in zip(trovate, vere))
    print(f"2 superfici, scarto massimo sugli angoli {scarto:.1f}px")
    ok = scarto < 4
    print("OK" if ok else "DA GUARDARE")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    opt = {a.split("=")[0]: (a.split("=")[1] if "=" in a else "1") for a in argv if a.startswith("--")}
    if argv[:1] == ["verifica"]:
        return verifica()
    if argv[:1] == ["auto"]:
        return auto(int(opt.get("--schermo", 1)), int(opt.get("--camera", 0)), "--punti" in opt)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
