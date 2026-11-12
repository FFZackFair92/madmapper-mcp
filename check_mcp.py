"""Verifica dal vivo con MadMapper aperto (OSC Query attivo):
  python check_mcp.py
Salva l'albero completo in albero_madmapper.json (serve a tarare i nomi degli
indirizzi) e prova una scrittura innocua con rilettura.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mm  # noqa: E402

root = mm.tree()
(HERE / "albero_madmapper.json").write_text(json.dumps(root, indent=1), encoding="utf-8")
params = list(mm.walk(root))
print(f"OSCQuery {mm.HOST}:{mm.QPORT} ok, OSC su {mm.osc_port()}, {len(params)} parametri "
      f"-> albero_madmapper.json")
print("radici:", sorted((root.get("CONTENTS") or {}).keys()))

surf = sorted(mm.children("/surfaces"))
print("superfici:", surf)
for s in surf:
    print(f"  {s}: {len(mm.handles(s))} handle di uscita")

if surf:
    a = f"/surfaces/{surf[0]}/opacity"
    v = mm.value(a)
    mm.write(a, v, verify=True)    # stesso valore: nessun cambio visibile
    print(f"scrittura+rilettura {a} = {v}: ok")
for a in ("/master/master_level", "/master/Global_BPM/BPM", "/outputs/Video-Output-1/enabled",
          f"/surfaces/{surf[0]}/visual/name" if surf else "/surfaces"):
    try:
        print(f"  {a} = {mm.value(a)!r}")
    except RuntimeError as e:
        print(f"  {a}: MANCA ({e})")
