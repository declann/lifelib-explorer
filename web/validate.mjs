// Node harness: exercises exactly the Python the worker runs, against dist/.
import { loadPyodide } from "pyodide";
import fs from "node:fs";
const t0 = Date.now();
const py = await loadPyodide();
await py.loadPackage(["numpy", "pandas", "micropip"]);
await py.pyimport("micropip").install(["modelx", "openpyxl"]);
py.FS.writeFile("/engine_core.py", fs.readFileSync("dist/engine_core.py"));
py.FS.mkdirTree("/models");
py.runPython(`
import sys, json, zipfile, io; sys.path.insert(0, "/")
import pandas as pd
from engine_core import ModelSession, loaded_json, compute_json, inspect_json
_s = ModelSession()
def run(name, data):
    zipfile.ZipFile(io.BytesIO(bytes(data))).extractall(f"/models/{name}")
    import time
    t=time.perf_counter(); info = _s.load(f"/models/{name}/model"); tl=time.perf_counter()-t
    li = loaded_json(info)
    t=time.perf_counter(); r = _s.compute(0, {}); tc=time.perf_counter()-t
    cj = compute_json(r)
    tbl = info["model_point_table"]
    col = next((c for c in tbl.columns if "sum" in c.lower() or "assured" in c.lower()), None)
    changed = None
    if col is not None and str(tbl[col].dtype).startswith(("int","float")):
        r2 = _s.compute(0, {col: float(tbl.iloc[0][col]) * 1.5})
        changed = (r2["result_pv"] is not None and not r2["result_pv"].equals(r["result_pv"])) or (not r2["result_cf"].equals(r["result_cf"]))
    cell = "result_cf" if info["has_result_cf"] else ("NetInsurCF" if "NetInsurCF" in [e["name"] for e in info["cells_index"]] else info["cells_index"][0]["name"])
    ij = inspect_json(_s.inspect(info["space_name"], cell, None))
    json.dumps(li); json.dumps(cj); json.dumps(ij)   # transport-safe
    # static formula graph (Formulas tab): every edge end must be a card
    g = _s.cell_graph(); keys = {f"{e['space']}.{e['name']}" for e in info["cells_index"]}
    bad = [x for e in g["edges"] for x in e if x not in keys]
    assert not bad, f"graph edges to unknown keys: {bad[:3]}"
    assert li["cell_graph"] == g
    # number cards: no pred of the headline cell may be an unwrapped-able length-1 Series
    wrapped = [p["name"] for p in ij["preds"] if (p["value_repr"] or "").startswith(("Series len=1", "DataFrame (1, 1)"))]
    assert not wrapped, f"length-1 values not unwrapped: {wrapped}"
    return json.dumps({"load_s": round(tl,2), "compute_s": round(tc,3), "points": len(tbl), "cells": len(info["cells_index"]),
                       "cf": list(cj["result_cf"]["columns"])[:4] if cj["result_cf"] else None, "pv": bool(cj["result_pv"]),
                       "edit_changed": changed, "inspect_preds": len(ij["preds"]), "inspect_cached": len(ij["cached"]),
                       "edges": len(g["edges"]), "pred_sums": sum(1 for p in ij["preds"] if p.get("consumed_sum") is not None)})

def _net(s):
    r = s.compute(0, {}); f = r["result_pv"] if r["result_pv"] is not None else r["result_cf"]
    net = [c for c in f.columns if "net" in str(c).lower()]
    return float(f[net[0]].sum()) if net else float(f.select_dtypes("number").sum().sum())

def formula_export_load(name, data, space, cell, old, new):
    """set_formula -> export_model (write_model on MEMFS) -> load_bytes round-trip.
    Returns (report_json, zip_bytes) — the bytes cross to JS like the worker does."""
    s = ModelSession()
    from engine_core import extract_model_zip
    from pathlib import Path
    s.load(str(extract_model_zip(bytes(data), Path(f"/models/rt_{name}"))))
    base = _net(s)
    src = s.inspect(space, cell, None)["source"]
    p = s.set_formula(space, cell, src.replace(old, new))
    assert p["edited"], "not marked edited"
    json.dumps(inspect_json(p))
    edited = _net(s)
    zb = s.export_model()
    assert abs(_net(s) - edited) < 1e-9, "source session broken by export"
    s2 = ModelSession(); li = s2.load_bytes(name + "-edited.zip", zb); json.dumps(loaded_json(li))
    rt = _net(s2)
    s.reset_formula(space, cell)
    return json.dumps({"base": round(base, 4), "edited": round(edited, 4), "roundtrip": round(rt, 4),
                       "zip_kb": len(zb) // 1024, "reset_ok": abs(_net(s) - base) < 1e-9,
                       "edits_after_reset": s.formula_edits(), "ok": edited != base and abs(rt - edited) < 1e-6 * max(1, abs(edited))}), zb
`);
const run = py.globals.get("run");
const manifest = JSON.parse(fs.readFileSync("dist/manifest.json"));
const targets = ["BasicTerm_S", "BasicTermASL_ME", "CashValue_ME", "Term_US_S", "PA_UK_S", "Cancer_JP_S", "Dep_FR_S", "Term_KR_S", "RLV_DE_S", "simplelife", "nestedlife", "ifrs17sim"];
let fails = 0;
for (const name of targets) {
  const md = manifest.models.find(m => m.name === name); if (!md) { console.log(name, "NOT IN MANIFEST"); fails++; continue; }
  try { const r = JSON.parse(run(name, new Uint8Array(fs.readFileSync("dist/" + md.zip)))); console.log(`${name.padEnd(16)} [${md.category.split(" ")[0]}]`, JSON.stringify(r)); if (r.edit_changed === false) fails++; }
  catch (e) { console.log(name, "FAIL:", String(e).split("\n").filter(l => /Error/.test(l)).slice(-1)[0]); fails++; }
}
run.destroy();

// formula edit -> export -> load round-trip, one model per table layout
const fel = py.globals.get("formula_export_load");
const rtTargets = [["BasicTerm_S", "Projection", "premium_pp", "return round(", "return 2 * round("],
                   ["CashValue_ME", "Projection", "expense_acq", "return ", "return 3 * "],
                   ["Term_US_S", "Projection", "premiums", "return ", "return 2 * "],
                   ["Term_KR_S", "Projection", "expenses", "return ", "return 2 * "],
                   ["simplelife", "Projection", "ExpsAcq", "return ", "return 3 * "]];
let rtN = 0;
for (const [name, space, cell, old, nw] of rtTargets) {
  const md = manifest.models.find(m => m.name === name);
  try {
    const res = fel(name, new Uint8Array(fs.readFileSync("dist/" + md.zip)), space, cell, old, nw);
    const [rep, zb] = [res.get(0), res.get(1)];
    const r = JSON.parse(rep); const bytes = zb.toJs(); zb.destroy(); res.destroy();
    const isU8 = bytes instanceof Uint8Array && bytes[0] === 0x50 && bytes[1] === 0x4b;   // "PK"
    console.log(`${name.padEnd(16)} formula/export/load`, JSON.stringify(r), `bytes->JS Uint8Array: ${isU8}`);
    if (!r.ok || !isU8) fails++;
  } catch (e) { console.log(name, "FORMULA FAIL:", String(e).split("\n").filter(l => /Error/.test(l)).slice(-1)[0]); fails++; }
  rtN++;
}
fel.destroy();
console.log(`\n${targets.length + rtN - fails}/${targets.length + rtN} OK in ${((Date.now()-t0)/1000).toFixed(0)}s`);
process.exit(fails ? 1 : 0);
