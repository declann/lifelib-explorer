import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication

app = QCoreApplication([])

from pathlib import Path

import numpy as np
import pandas as pd

from lifelib_explorer.engine import EngineWorker, discover_models
from lifelib_explorer.main import find_lifelib_root

# Must resolve to *this* repo's lifelib submodule — an absolute path here once
# pointed at a stale sibling checkout, so new libraries went untested.
root = find_lifelib_root()
assert root.is_dir(), f"lifelib submodule missing: {root} (git submodule update --init)"
models = discover_models(root)
print(f"discovered {len(models)} models")
for m in models[:6]:
    print("  ", m.label)

w = EngineWorker()
state = {}
w.model_loaded.connect(lambda info: state.__setitem__("loaded", info))
w.compute_done.connect(lambda r: state.__setitem__("result", r))
w.error.connect(lambda e: (print("ERROR:\n", e), state.__setitem__("err", e)))
w.status.connect(lambda s: print("  [status]", s))


def run(name, edits):
    info = next(m for m in models if m.name == name)
    state.clear()
    w.load_model(info.path)
    if "err" in state:
        return
    loaded = state["loaded"]
    print(
        f"{name}: space={loaded.space_name} ref={loaded.ref_name} "
        f"param={loaded.parameterized} points={len(loaded.model_point_table)} "
        f"cf={loaded.has_result_cf} pv={loaded.has_result_pv}"
    )
    # baseline compute for first point
    row = loaded.model_point_table.iloc[0].to_dict()
    w.latest_seq = 1
    w.compute(1, 0, row)
    if "result" in state:
        r = state["result"]
        print(
            f"  baseline: cf shape={None if r.result_cf is None else r.result_cf.shape}, "
            f"pv shape={None if r.result_pv is None else r.result_pv.shape}, "
            f"policy series={list(r.policy_series)}, {r.elapsed:.2f}s"
        )
        base_pv = None if r.result_pv is None else r.result_pv.iloc[0].to_dict()
    # edited compute
    row.update(edits)
    state.pop("result", None)
    w.latest_seq = 2
    w.compute(2, 0, row)
    if "result" in state:
        r = state["result"]
        pv = None if r.result_pv is None else r.result_pv.iloc[0].to_dict()
        print(f"  edited {edits}: pv changed = {pv != base_pv}")
        print(f"    base pv row: {base_pv}")
        print(f"    new  pv row: {pv}")


run("BasicTerm_S", {"sum_assured": 1_000_000, "age_at_entry": 60})
run("BasicTerm_M", {"policy_term": 20})
run("CashValue_ME", {"premium_pp": 600000.0})

# ---------------------------------------------------------------- formulas
# set_formula -> PV changes; export -> load_bytes round-trips the edit;
# reset_formula restores. Pure engine, no Qt.
from lifelib_explorer.engine_core import ModelSession  # noqa: E402


def net_pv(s, row_pos=0):
    r = s.compute(row_pos, {})
    pv = r["result_pv"].iloc[0]
    k = next(c for c in pv.index if "net" in str(c).lower())
    return float(pv[k])


def formula_round_trip(name, space, cell, replace):
    info = next(m for m in models if m.name == name)
    s = ModelSession()
    li = s.load(info.path)
    assert li["formula_edits"] == [] and s.model_name == name, (li["formula_edits"], s.model_name)
    base = net_pv(s)
    src = s.inspect(space, cell, None)["source"]
    assert src and not s.inspect(space, cell, None)["edited"]
    # rejected edits leave the model untouched
    for bad in ("def nope():\n    return 1", f"def {cell}(:\n    return 1", "x = 1"):
        try:
            s.set_formula(space, cell, bad)
            raise AssertionError(f"accepted bad formula {bad!r}")
        except RuntimeError as exc:
            assert "SyntaxError" in str(exc) or "name" in str(exc) or "def" in str(exc), exc
    assert net_pv(s) == base
    new_src = src.replace(*replace)
    assert new_src != src, "replacement did not apply"
    p = s.set_formula(space, cell, new_src)
    assert p["edited"] and p["source"].strip() == new_src.strip()
    edited = net_pv(s)
    assert edited != base, (edited, base)
    assert [e["name"] for e in s.formula_edits()] == [cell]
    assert any(e["edited"] for e in s._build_cells_index(s._model) if e["name"] == cell)
    # inspector still traces through the edited cell after a compute
    ins = s.inspect(space, cell, None)
    assert ins["edited"] and ins["cached"], "edited cell should have cached nodes after compute"
    # export the edited model and load it back into a fresh session
    data = s.export_model()
    assert data[:2] == b"PK" and len(data) > 1000
    s2 = ModelSession()
    li2 = s2.load_bytes(f"{name}-edited.zip", data)
    assert li2["name"] == f"{name}-edited" and li2["formula_edits"] == []
    assert li2["model_point_table"].shape == li["model_point_table"].shape
    assert s2.inspect(space, cell, None)["source"].strip() == new_src.strip()
    rt = net_pv(s2)
    assert abs(rt - edited) < 1e-6 * max(1.0, abs(edited)), (rt, edited)
    # reset in the original session restores the baseline
    p = s.reset_formula(space, cell)
    assert not p["edited"] and s.formula_edits() == []
    assert net_pv(s) == base
    # setting the original source back is not an edit
    s.set_formula(space, cell, new_src)
    s.set_formula(space, cell, src)
    assert s.formula_edits() == []
    # loading another model clears edits
    s.set_formula(space, cell, new_src)
    s.load(info.path)
    assert s.formula_edits() == []
    print(
        f"{name}: formula edit {base:.2f} -> {edited:.2f}, export {len(data) // 1024} KB, "
        f"round-trip OK, reset OK"
    )


formula_round_trip(
    "BasicTerm_S", "Projection", "premium_pp", ("return round(", "return 2 * round(")
)
formula_round_trip("CashValue_ME", "Projection", "expense_acq", ("return ", "return 3 * "))

# Reference lib (table in a no-arg cell, CSV inputs *beside* the model dir):
# export must restore model.path or the source session breaks after export.
_dia = next(m for m in models if m.name == "DIA_US_S")
_s = ModelSession()
_s.load(_dia.path)
_base = float(_s.compute(0, {})["result_cf"].select_dtypes("number").sum().sum())
_zip = _s.export_model()
assert str(_s._model.path) == _dia.path, "export must not re-point model.path"
assert abs(float(_s.compute(0, {})["result_cf"].select_dtypes("number").sum().sum()) - _base) < 1e-9
_s2 = ModelSession()
_s2.load_bytes("DIA_US_S.zip", _zip)
assert (
    abs(float(_s2.compute(0, {})["result_cf"].select_dtypes("number").sum().sum()) - _base) < 1e-6
)
print(f"DIA_US_S: export {len(_zip) // 1024} KB round-trip OK (cell-kind table, sibling CSVs)")

# ---------------------------------------------------- length-1 values unwrap
# _ME models vectorise over points; with the one-row table every per-t cell is
# a length-1 Series. Cards/values/sparklines must see the number, not "Series len=1".
_cv = next(m for m in models if m.name == "CashValue_ME")
_s = ModelSession()
_s.load(_cv.path)
_s.compute(0, {})
_p = _s.inspect("Projection", "pols_if", (12,))
assert _p["value_data"] is None and _p["series"] is not None, (
    "length-1 Series should be a scalar with a t-series"
)
assert len(_p["series"]["lines"][0]["x"]) == 121, _p["series"]["lines"][0]["x"][:3]
assert float(_p["value_repr"].replace(",", "")) > 0, _p["value_repr"]
_g = next(g for g in _p["preds"] if g["name"] == "pols_if_at")
assert (
    _g["series"] is not None and _g["consumed_sum"] is not None and "Series" not in _g["value_repr"]
), _g
_p = _s.inspect("Projection", "result_cf", ())
_claims = next(g for g in _p["preds"] if g["name"] == "claims")
assert (
    _claims["consumed_sum"] is not None
    and _claims["series"] is not None
    and len(_claims["marks"]) == 121
), _claims
assert ModelSession._unwrap(pd.Series([3.5])) == 3.5 and ModelSession._unwrap(np.array([[2]])) == 2
assert ModelSession._unwrap(pd.Series([1.0, 2.0])).shape == (2,)
print("CashValue_ME: length-1 Series unwrapped (value, series, Σ on cards) OK")

# ------------------------------------------------------ static formula graph
_bt = next(m for m in models if m.name == "BasicTerm_S")
_s = ModelSession()
_li = _s.load(_bt.path)
_g = _s.cell_graph()
assert _li["cell_graph"] == _g and _g["edges"], "load() must carry the graph"
_keys = {f"{e['space']}.{e['name']}" for e in _li["cells_index"]}
assert all(a in _keys and b in _keys for a, b in _g["edges"]), "edges must reference cards"
_reads = {b for a, b in _g["edges"] if a == "Projection.result_cf"}
assert {
    "Projection.claims",
    "Projection.premiums",
    "Projection.net_cf",
    "Projection.proj_len",
} <= _reads, _reads
assert ["Projection.pols_if", "Projection.pols_if"] not in _g["edges"], (
    "self-recursion is not an edge"
)
assert ["Projection.mort_rate", "Projection.mort_table"] in _g["edges"], "table refs are edges"
# a formula edit that reads a new cell adds an edge; reset removes it
_src = _s.inspect("Projection", "premium_pp", None)["source"]
_s.set_formula(
    "Projection", "premium_pp", _src.replace("return round(", "return loading_prem() * 0 + round(")
)
assert ["Projection.premium_pp", "Projection.loading_prem"] in _s.cell_graph()["edges"]
_s.reset_formula("Projection", "premium_pp")
assert _s.cell_graph() == _g
# cross-space edges (Reference lib: Projection reads Data.* through the Data ref)
_s = ModelSession()
_s.load(_dia.path)
_edges = _s.cell_graph()["edges"]
assert ["Projection.model_point", "Data.model_point_table"] in _edges, [
    e for e in _edges if e[0] == "Projection.model_point"
]
assert not any(a.startswith(".") or b.startswith(".") for a, b in _edges)
print(
    f"cell_graph: BasicTerm_S {len(_g['edges'])} edges, edit adds/removes edge, DIA_US_S cross-space OK"
)

# --------------------------------------------- per-cell values (Formulas tab)
import json  # noqa: E402

from lifelib_explorer.engine_core import cell_values_json  # noqa: E402

_s = ModelSession()
_s.load(_bt.path)
assert _s.cell_values() == {}, "nothing computed yet -> no values"
_s.compute(0, {})
_v = _s.cell_values()
assert set(_v) <= _keys and "Projection.pols_if" in _v and "Projection.mort_table" not in _v
_pi = _v["Projection.pols_if"]
assert (
    _pi["series"]
    and _pi["series"]["param"] == "t"
    and len(_pi["series"]["lines"][0]["x"]) == 121
    and _pi["n_cached"] == 121
)
_pt = _v["Projection.policy_term"]
assert _pt["series"] is None and _pt["value_repr"] == "10" and _pt["args"] == ()
assert _v["Projection.result_cf"]["frame"] is not None and _v["Projection.result_cf"][
    "value_repr"
].startswith("DataFrame")
json.dumps(cell_values_json(_v))  # transport-safe (tuples, frames, NaN)
print(f"cell_values: {len(_v)} computed cells, series/scalar/frame kinds, JSON OK")

# ------------------------------------------- new reference libraries (delib/krlib)
# lifelib v0.17.1+ added krlib (Korean) and delib (German) products. They use the
# reference-lib layout (table in Data.model_point_table(), CSV inputs beside the
# model dir) but define NO PV cells at all -> result_pv is legitimately None and
# must not raise. Every one of them must load, compute and switch point.
from lifelib_explorer.engine_core import library_category, unsupported_reason  # noqa: E402

_new = [m for m in models if any(p in ("krlib", "delib") for p in Path(m.path).parts)]
assert len(_new) == 20, [m.name for m in _new]
for _m in _new:
    assert library_category(_m) == "Reference Liability Models", (_m.name, library_category(_m))
    assert unsupported_reason(_m) is None, _m.name
_no_pv = []
for _m in _new:
    _s = ModelSession()
    _li = _s.load(_m.path)
    assert _li["space_name"] == "Projection", (_m.name, _li["space_name"])
    _n = len(_li["model_point_table"])
    assert _n > 1, (_m.name, _n)
    _r = _s.compute(0, {})
    assert _r["result_cf"] is not None and len(_r["result_cf"]) > 1, _m.name
    assert _r["policy_series"], _m.name
    if _r["result_pv"] is None:
        _no_pv.append(_m.name)
    # a second point must give a different projection (table override works)
    _r2 = _s.compute(1, {})
    assert _r2["point_label"] != _r["point_label"], _m.name
print(
    f"delib/krlib: {len(_new)} models load+compute+switch point OK ({len(_no_pv)} without PV cells)"
)

# Formula edit / export / load round-trip on one model of each new library.
for _name in ("Term_KR_S", "RLV_DE_S"):
    _m = next(m for m in models if m.name == _name)
    _s = ModelSession()
    _s.load(_m.path)
    _sum = lambda r: float(r["result_cf"]["expenses"].sum())  # noqa: E731
    _b = _sum(_s.compute(0, {}))
    _src = _s.inspect("Projection", "expenses", None)["source"]
    _s.set_formula("Projection", "expenses", _src.replace("return ", "return 2 * ", 1))
    _e = _sum(_s.compute(0, {}))
    assert _e != _b, (_name, _b, _e)
    _zip = _s.export_model()
    assert str(_s._model.path) == _m.path, f"{_name}: export must not re-point model.path"
    _s2 = ModelSession()
    _s2.load_bytes(f"{_name}.zip", _zip)
    _rt = _sum(_s2.compute(0, {}))
    assert abs(_rt - _e) < 1e-6 * max(1.0, abs(_e)), (_name, _rt, _e)
    _s.reset_formula("Projection", "expenses")
    assert _sum(_s.compute(0, {})) == _b, _name
    print(
        f"{_name}: formula edit {_b:.2f} -> {_e:.2f}, export {len(_zip) // 1024} KB, "
        f"round-trip OK, reset OK"
    )

print("done")
