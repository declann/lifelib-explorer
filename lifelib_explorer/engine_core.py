"""Pure-Python engine: model discovery, loading, computation, inspection.

No Qt (or any UI) imports here. The desktop app wraps :class:`ModelSession`
in a QThread worker (``engine.py``); the web app runs it inside Pyodide in a
web worker (``web/``). Payloads are plain dicts, with pandas DataFrames where
the caller may want full fidelity — the ``*_json`` helpers convert those to
JSON-able structures for the web transport.
"""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

#: files inside a model dir that a packaged/exported model doesn't need
ZIP_SKIP_SUFFIXES = {".ipynb", ".md", ".rst", ".png"}
ZIP_SKIP_DIRS = {"__pycache__", ".ipynb_checkpoints", "tests"}

#: sibling input files a model may read from ``_model.path.parent`` at run
#: time (Reference Liability Models keep their CSV inputs *next to* the
#: model folder, so the model folder itself is pure formulas).
SIBLING_INPUT_SUFFIXES = {".csv", ".xlsx", ".xls", ".json"}


def zip_model_dir(mdir: Path, out, siblings_from: Path | None = None) -> int:
    """Zip a modelx model directory as ``model/…`` plus sibling input files
    at the zip root, so ``<extract>/model`` is the model and ``<extract>``
    (= ``_model.path.parent``) holds the inputs.

    ``out`` is a path or a binary file object. Returns the number of files
    written. This single layout is used by the web packager (build_models)
    and by :meth:`ModelSession.export_model`, so an exported model loads
    back through the same path as a bundled one.
    """
    siblings = mdir.parent if siblings_from is None else siblings_from
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(mdir.rglob("*")):
            if p.is_dir():
                continue
            rel = p.relative_to(mdir)
            if any(part in ZIP_SKIP_DIRS for part in rel.parts):
                continue
            if p.suffix in ZIP_SKIP_SUFFIXES:
                continue
            zf.write(p, ("model" / rel).as_posix())
            n += 1
        if siblings.is_dir():
            for p in sorted(siblings.iterdir()):
                if p.is_file() and p.suffix in SIBLING_INPUT_SUFFIXES:
                    zf.write(p, p.name)
                    n += 1
    return n


def extract_model_zip(data: bytes, dest: Path) -> Path:
    """Extract a zip in the :func:`zip_model_dir` layout; return the model dir.

    Accepts the ``model/…`` layout, or a zip whose root *is* the model
    (contains ``_system.json``), for hand-made zips.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        for n in names:  # zip-slip guard
            if n.startswith("/") or ".." in Path(n).parts:
                raise RuntimeError(f"Refusing to extract unsafe path: {n}")
        zf.extractall(dest)
    if (dest / "model" / "_system.json").exists():
        return dest / "model"
    if (dest / "_system.json").exists():
        return dest
    # single top-level folder holding the model
    subs = [p for p in dest.iterdir() if p.is_dir()]
    for s in subs:
        if (s / "_system.json").exists():
            return s
    raise RuntimeError(
        "Zip does not contain a modelx model (no _system.json found; expected "
        "layout: model/_system.json, model/<Space>/… plus input files at root)"
    )


#: Reference names (in priority order) that may hold the model point table.
MP_REF_NAMES = ("model_point_table", "model_point_data", "PolicyData", "policy_data")

#: No-argument cell names that may return the model point table (frlib/jplib/
#: uklib/uslib models keep the table in a ``Data.model_point_table()`` cell).
MP_CELL_NAMES = ("model_point_table", "model_point_data", "policy_data")

#: Optional per-period cells worth charting if the model defines them.
POLICY_CELLS = ("pols_if", "pols_death", "pols_lapse", "pols_maturity", "pols_new_biz")

#: lifelib's own taxonomy (doc/source/libraries/index.rst), keyed by library
#: dir name. "Past Libraries" were introduced before lifelib v0.1.1 and were
#: originally referred to as "projects"; they use an older cashflow model.
LIBRARY_CATEGORY = {
    "basiclife": "Generic Liability Models",
    "savings": "Generic Liability Models",
    "annuallife": "Generic Liability Models",
    "appliedlife": "Generic Liability Models",
    "uslib": "Reference Liability Models",
    "uklib": "Reference Liability Models",
    "jplib": "Reference Liability Models",
    "frlib": "Reference Liability Models",
    "delib": "Reference Liability Models",
    "krlib": "Reference Liability Models",
    "assets": "Miscellaneous Models",
    "economic": "Miscellaneous Models",
    "economic_curves": "Miscellaneous Models",
    "ifrs17a": "Miscellaneous Models",
    "cluster": "Miscellaneous Models",
    "fastlife": "Past Libraries",
    "simplelife": "Past Libraries",
    "nestedlife": "Past Libraries",
    "ifrs17sim": "Past Libraries",
    "solvency2": "Past Libraries",
    "smithwilson": "Past Libraries",
}
CATEGORY_ORDER = (
    "Generic Liability Models",
    "Reference Liability Models",
    "Past Libraries",
    "Miscellaneous Models",
    "Other",
)


def library_category(info: ModelInfo) -> str:
    """Category per lifelib's docs; reference-library products nest one level
    deeper (``uslib/products/term_life/Term_US_S``), so walk up the path."""
    parts = Path(info.path).parts
    for p in reversed(parts):
        if p in LIBRARY_CATEGORY:
            return LIBRARY_CATEGORY[p]
    return "Other"


#: Models that cannot be driven by this explorer, keyed by library dir name
#: or model dir name -> short reason shown in the picker.
UNSUPPORTED = {
    "smithwilson": "no model points (yield-curve extrapolation)",
    "BasicHullWhite": "no model points (economic scenario generator)",
    "BasicBonds": "no model points (bond portfolio; needs QuantLib)",
    "IntegratedLife": "model points bound per Run[run_id] × product × segment "
    "— needs a run/segment picker (not yet)",
    "TradLife_A": "policy attributes flow through internal array bindings "
    "(PolicyAttrs.pol), not a table override (not yet)",
    "TradLife_A_EX1": "as TradLife_A (not yet)",
    "TradLife_A_mx30": "as TradLife_A (not yet)",
    "BasicTerm_ME_for_Cluster": "projection parameterized by sensitivity "
    "multipliers, not a point id",
    "fastlife": "vectorized over all 300 policies at once (cells return a "
    "Series per t) — no per-point view (not yet)",
    "solvency2": "no projection space; SCR_life(t0, PolicyID, ScenID) puts "
    "the point id second (not yet)",
}

#: Per-period cells used to synthesize a cashflow table when a model has no
#: ``result_cf`` (Past Libraries expose per-t cells only). Ordered groups:
#: (column label, candidate cell names).
CF_CANDIDATES = (
    ("Premiums", ("PremIncome", "premiums", "prem_income")),
    ("Claims", ("BenefitTotal", "claims", "benefits")),
    ("Expenses", ("ExpsTotal", "expenses", "exps_total")),
    ("Commissions", ("ExpsCommTotal", "commissions")),
    ("Investment Income", ("IntAccumCF", "InvstIncome", "inv_income")),
    ("Net Cashflow", ("NetInsurCF", "net_cf", "NetCF")),
)
PV_PREFIXES = ("PV_", "pv_")
PROJ_LEN_CELLS = ("last_t", "proj_len", "max_proj_len")

MAX_SERIES_LINES = 8

#: Integer columns whose max exceeds this are treated as monetary amounts
#: (safe to jitter); smaller ints (ages, terms, counts, durations) are
#: structural and copied verbatim from the source row.
_MONETARY_THRESHOLD = 1000
_JITTER = 0.15  # ±15 %


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelInfo:
    library: str
    name: str
    path: str

    @property
    def label(self) -> str:
        return f"{self.library} / {self.name}"


def discover_models(root: Path) -> list[ModelInfo]:
    """Find serialized modelx models (directories containing ``_system.json``)."""
    infos = []
    for sysfile in sorted(Path(root).glob("**/_system.json")):
        mdir = sysfile.parent
        if not (mdir / "__init__.py").exists():
            continue
        infos.append(ModelInfo(library=mdir.parent.name, name=mdir.name, path=str(mdir)))
    return infos


def unsupported_reason(info: ModelInfo) -> str | None:
    """Reason a model is known to be unsupported, or None if it should work."""
    return UNSUPPORTED.get(info.name) or UNSUPPORTED.get(info.library)


def generate_model_points(table: pd.DataFrame, n: int, seed: int | None = None) -> pd.DataFrame:
    """Synthesize n new model points by bootstrapping existing rows.

    Whole rows are sampled with replacement — this preserves cross-field
    constraints (e.g. ``duration_mth <= policy_term * 12``, rate-table
    coverage) that independent per-column sampling would violate. Only
    monetary-scale columns (floats, and ints ≥ 1000) are jittered ±15 %.
    Index numbering continues from the existing table.
    """
    rng = np.random.default_rng(seed)
    rows = table.iloc[rng.integers(0, len(table), n)].copy()
    for col in rows.columns:
        dtype = table[col].dtype
        jitter = rng.uniform(1 - _JITTER, 1 + _JITTER, n)
        if pd.api.types.is_float_dtype(dtype):
            rows[col] = np.round(rows[col].to_numpy() * jitter, 6)
        elif pd.api.types.is_integer_dtype(dtype) and abs(table[col]).max() >= _MONETARY_THRESHOLD:
            rows[col] = np.round(rows[col].to_numpy() * jitter).astype(dtype)
    if pd.api.types.is_integer_dtype(table.index.dtype):
        start = int(table.index.max()) + 1
        rows.index = pd.Index(range(start, start + n), name=table.index.name)
    else:
        rows.index = pd.Index([f"GEN{i + 1}" for i in range(n)], name=table.index.name)
    return rows.astype(table.dtypes.to_dict())


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class ModelSession:
    """Owns one loaded modelx model. Not thread-safe: single owner only."""

    def __init__(self) -> None:
        self._model = None
        self._src_space = None  # space where the table is overridden
        self._src_name: str | None = None  # ref or cell name of the table
        self._src_kind = "ref"  # "ref" | "cell" | "mapping" (ExcelRange)
        self._target = None  # space with result cells
        self._extra_args: dict = {}  # extra projection params -> defaults
        self._last_item = None  # last computed space/itemspace (traceable)
        self._mpt: pd.DataFrame | None = None
        self._mpt_orig: pd.DataFrame | None = None
        self._src_orig: Any = None
        self._parameterized = False
        self._ref_prov: dict[int, str] = {}  # id(value) -> source file info
        self._path: str | None = None
        self._name: str | None = None
        # (space_path, name) -> {"orig": source, "new": source}
        self._edits: dict[tuple[str, str], dict[str, str]] = {}
        self._tmpdirs: list[str] = []  # extraction dirs for load_bytes

    @property
    def model_point_table(self) -> pd.DataFrame | None:
        return self._mpt

    @property
    def model_name(self) -> str | None:
        return self._name

    # -- loading -----------------------------------------------------------

    def load(self, path: str) -> dict:
        import modelx as mx

        if self._model is not None:
            try:
                self._model.close()
            except Exception:
                pass
            self._model = None
            self._src_space = None
            self._target = None
            self._last_item = None
        self._edits = {}  # formula edits belong to the previous model

        t0 = time.perf_counter()
        self._model = mx.read_model(path)
        self._path = path
        self._name = Path(path).name if Path(path).name != "model" else Path(path).parent.name
        src_space, src_name, mpt, kind = self._find_model_point_table(self._model)
        if src_space is None or src_name is None or mpt is None:
            raise RuntimeError(
                "Could not find a model point table in this model — looked "
                f"for DataFrame references named {MP_REF_NAMES}, no-argument "
                f"cells named {MP_CELL_NAMES}, and Excel-range mappings "
                "(Past Libraries) in every space.\n\n"
                "Models with no model points (asset / ESG / yield-curve "
                "models) cannot be driven by this explorer."
            )
        target = self._find_result_space(self._model, src_space)
        # extra projection parameters (e.g. ScenID=1, scen_id=1) are fixed at
        # their defaults; the first parameter is the point id
        self._extra_args = self._param_defaults(target)
        params = list(target.parameters or ())
        if len(params) > 1 and len(self._extra_args) < len(params) - 1:
            raise RuntimeError(
                f"Result space '{target.name}' takes parameters {params}; "
                "parameters after the point id must have defaults."
            )
        if kind == "mapping":
            # Past Libraries bind the Excel range on a sub-space of the
            # projection (Projection.Policy.PolicyData); override it there
            bound = self._find_binding_space(target, src_name) or src_space
            src_space = bound
        self._src_space = src_space
        self._src_name = src_name
        self._src_kind = kind
        self._target = target
        self._mpt = mpt.copy()
        self._mpt_orig = mpt.copy()
        # the original bound object (ExcelRange / DataFrame with iospec) so an
        # export of an unchanged table keeps its file reference
        self._src_orig = getattr(src_space, src_name) if kind != "cell" else None
        self._parameterized = bool(target.parameters)
        # provenance of data references (which file a table came from)
        self._ref_prov = {}
        try:
            for spec in self._model.iospecs:
                try:
                    self._ref_prov[id(spec.value)] = str(spec.path)
                except Exception:
                    pass
        except Exception:
            pass
        return {
            "name": Path(path).name,
            "path": path,
            "space_name": target.name,
            "ref_name": f"{src_space.name}.{src_name}",
            "parameterized": self._parameterized,
            "model_point_table": mpt.copy(),
            "load_seconds": time.perf_counter() - t0,
            "has_result_cf": "result_cf" in target.cells,
            "has_result_pv": "result_pv" in target.cells,
            "cells_index": self._build_cells_index(self._model),
            "cell_graph": self.cell_graph(),
            "model_name": self._name,
            "formula_edits": [],
        }

    def load_bytes(self, name: str, data: bytes) -> dict:
        """Load a model from zip bytes (user-supplied or exported).

        Extracts into a fresh temp dir (kept alive while the session lives —
        models read sibling inputs at run time) and delegates to :meth:`load`.
        """
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).removesuffix(".zip") or "model"
        tmp = tempfile.mkdtemp(prefix=f"lifelib-ui-{safe}-")
        self._tmpdirs.append(tmp)
        mdir = extract_model_zip(data, Path(tmp) / safe)
        info = self.load(str(mdir))
        info["name"] = safe
        info["model_name"] = safe
        self._name = safe
        return info

    # -- formula editing ---------------------------------------------------

    def _base_cells(self, space_path: str, name: str):
        """The Cells on the *base* space (never an ItemSpace) — edits go here."""
        if self._model is None:
            raise RuntimeError("No model loaded")
        space: Any = self._model
        for seg in space_path.split("."):
            space = space.spaces[seg]
        if name not in space.cells:
            raise RuntimeError(
                f"{space_path} has no cell named {name!r} "
                "(only cells' formulas can be edited, not tables)"
            )
        return space.cells[name]

    def set_formula(self, space_path: str, name: str, source: str) -> dict:
        """Replace a cell's formula with ``source`` (a ``def`` with the cell's
        name). Validates first; on success dependents are invalidated by
        modelx and the inspector payload for the cell is returned."""
        c = self._base_cells(space_path, name)
        source = source.replace("\r\n", "\n").strip("\n") + "\n"
        try:
            compile(source, f"<{space_path}.{name}>", "exec")
        except SyntaxError as exc:
            raise RuntimeError(f"SyntaxError in {name}: {exc.msg} (line {exc.lineno})") from exc
        m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", source)
        if m is None:
            if not re.match(r"\s*lambda\b", source):
                raise RuntimeError(f"Formula must be a 'def {name}(...):' function (or a lambda)")
        elif m.group(1) != name:
            raise RuntimeError(
                f"The function must keep the cell's name: 'def {name}(...)', "
                f"not 'def {m.group(1)}(...)'"
            )
        key = (space_path, name)
        orig = self._edits[key]["orig"] if key in self._edits else self._formula_source(c)
        c.set_formula(source)
        if source.strip() == orig.strip():
            self._edits.pop(key, None)
        else:
            self._edits[key] = {"orig": orig, "new": source}
        # cached nodes on the last itemspace are stale now
        self._last_item = None
        payload = self.inspect(space_path, name, None)
        payload["edited"] = key in self._edits
        return payload

    def reset_formula(self, space_path: str, name: str) -> dict:
        key = (space_path, name)
        if key in self._edits:
            self._base_cells(space_path, name).set_formula(self._edits[key]["orig"])
            del self._edits[key]
            self._last_item = None
        payload = self.inspect(space_path, name, None)
        payload["edited"] = False
        return payload

    def formula_edits(self) -> list[dict]:
        """``[{space, name, source, orig}]`` for every edited cell."""
        return [
            {"space": s, "name": n, "source": e["new"], "orig": e["orig"]}
            for (s, n), e in self._edits.items()
        ]

    @staticmethod
    def _formula_source(c) -> str:
        try:
            return str(c.formula.source) if c.formula is not None else ""
        except Exception:
            try:
                return str(c.formula)
            except Exception:
                return ""

    # -- whole-model formula graph -------------------------------------------

    def cell_graph(self) -> dict:
        """Static precedent edges for *every* cell, read off the formula
        sources (``ast``), independent of what the last computation cached.

        ``{"edges": [[reader, read], …]}`` where both ends are
        ``"<space path>.<name>"`` keys matching ``cells_index`` (cells and
        table references). ``reader`` calls/reads ``read``; self-recursion
        (``pols_if(t-1)``) is omitted. Names are resolved the way modelx
        does — a bare name is a cell or ref of the same space; ``X.y`` (also
        ``X[k].y``, ``X().y``) is resolved through the ref ``X`` when it is a
        space, so cross-space reads (``Assumptions.mort_table``) are edges too.
        Cheap (~ms), so it runs at load and after every formula edit.
        """
        import ast

        if self._model is None:
            raise RuntimeError("No model loaded")
        edges: set[tuple[str, str]] = set()

        def is_space(o) -> bool:
            try:
                return hasattr(o, "cells") and hasattr(o, "spaces") and hasattr(o, "refs")
            except Exception:  # refs can point at deleted modelx objects
                return False

        def is_cells(o) -> bool:
            # spaces also have .formula/.parameters (their item formula)
            try:
                return hasattr(o, "formula") and hasattr(o, "parameters") and not is_space(o)
            except Exception:
                return False

        def is_table(o) -> bool:
            return isinstance(o, (pd.DataFrame, pd.Series))

        def member(o, attr):
            """(kind, obj) for ``o.attr`` on a space-like ``o``."""
            try:
                if attr in o.cells:
                    return "cell", o.cells[attr]
                if attr in o.spaces:
                    return "space", o.spaces[attr]
                if attr in o.refs:
                    return "ref", getattr(o, attr)
            except Exception:
                pass
            return None, None

        def key_of(kind, obj, owner, attr) -> str | None:
            try:
                if kind == "cell":
                    return f"{self._space_path(obj.parent)}.{obj.name}"
                if kind == "ref" and is_table(obj):
                    return f"{self._space_path(owner)}.{attr}"
                if kind == "ref" and is_cells(obj):  # ref bound to a cell
                    return f"{self._space_path(obj.parent)}.{obj.name}"
            except Exception:
                pass
            return None

        def base_of(expr):
            # Projection[1].x / Projection().x -> Projection
            while isinstance(expr, (ast.Subscript, ast.Call)):
                expr = expr.value if isinstance(expr, ast.Subscript) else expr.func
            return expr

        def resolve(expr, space):
            """Object an expression denotes, if it is a space/cell/table."""
            expr = base_of(expr)
            if isinstance(expr, ast.Name):
                kind, obj = member(space, expr.id)
                return kind, obj, space, expr.id
            if isinstance(expr, ast.Attribute):
                kind, obj, _, _ = resolve(expr.value, space)
                if kind == "space" or (kind == "ref" and is_space(obj)):
                    k2, o2 = member(obj, expr.attr)
                    return k2, o2, obj, expr.attr
            return None, None, None, None

        for space in self._walk_spaces(self._model):
            sp = self._space_path(space)
            try:
                cells = space.cells
            except Exception:
                continue
            for name in cells:
                src = self._formula_source(cells[name])
                if not src.strip():
                    continue
                me = f"{sp}.{name}"
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    # unparsable (should not happen: modelx compiled it) —
                    # fall back to a name scan within the same space
                    for tok in set(re.findall(r"\b([A-Za-z_]\w*)\b", src)):
                        kind, obj = member(space, tok)
                        k = key_of(kind, obj, space, tok) if kind else None
                        if k and k != me:
                            edges.add((me, k))
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name):
                        if not isinstance(node.ctx, ast.Load):
                            continue
                        kind, obj, owner, attr = resolve(node, space)
                    elif isinstance(node, ast.Attribute):
                        kind, obj, owner, attr = resolve(node, space)
                    else:
                        continue
                    k = key_of(kind, obj, owner, attr) if kind else None
                    if k and k != me:
                        edges.add((me, k))
        return {"edges": [list(e) for e in sorted(edges)]}

    def cell_values(self) -> dict:
        """What every *computed* cell looks like right now, for the Formulas
        tab's "show values" mode — the same ingredients as a graph card:

        ``{key: {"series", "value_repr", "frame", "args", "n_cached"}}``
        keyed like :meth:`cell_graph`. ``series`` is the cell over its cached
        first parameter (``t``); ``value_repr``/``frame`` describe the single
        cached value of a no-parameter cell (or of the only cached invocation,
        ``args`` says which). Cells the last computation never touched are
        absent — the UI says "not computed". Table refs are not included
        (their cards stay static). Cheap: everything is already cached.
        """
        if self._model is None:
            raise RuntimeError("No model loaded")
        out: dict[str, dict] = {}
        for space in self._walk_spaces(self._model):
            sp = self._space_path(space)
            try:
                names = list(space.cells)
            except Exception:
                continue
            for name in names:
                try:
                    c = self._resolve_cells(sp, name)
                    cached = self._cached_args(c)
                    if not cached:
                        continue
                    entry: dict[str, Any] = {
                        "series": self._cached_series(c, cached),
                        "value_repr": None,
                        "frame": None,
                        "args": None,
                        "n_cached": len(cached),
                    }
                    if entry["series"] is None:
                        node = c.node(*cached[0])
                        if node.has_value():
                            v = self._unwrap(node.value)
                            entry["value_repr"] = self._value_repr(v, short=True)
                            if isinstance(v, (pd.DataFrame, pd.Series)):
                                entry["frame"] = self._mini_frame(v)
                            entry["args"] = tuple(cached[0])
                    out[f"{sp}.{name}"] = entry
                except Exception:
                    continue
        return out

    # -- export ------------------------------------------------------------

    def _restore_table(self) -> None:
        """Undo compute()'s per-point override of the model point table.

        Unchanged table -> rebind the original object (keeps the xlsx/csv
        iospec, so the export references the file instead of pickling);
        imported/generated table -> bind the working DataFrame.
        Invalidates cached nodes: callers must recompute before inspecting.
        """
        if self._src_space is None or self._src_name is None or self._mpt is None:
            return
        unchanged = (
            self._mpt_orig is not None
            and self._mpt.shape == self._mpt_orig.shape
            and self._mpt.equals(self._mpt_orig)
        )
        if self._src_kind == "cell":
            cells = self._src_space.cells[self._src_name]
            if unchanged:
                cells.clear_at()  # drop the input value -> formula again
            else:
                setattr(self._src_space, self._src_name, self._mpt.copy())
        elif unchanged and self._src_orig is not None:
            setattr(self._src_space, self._src_name, self._src_orig)
        elif self._src_kind == "mapping":
            setattr(self._src_space, self._src_name, self._frame_to_mapping(self._mpt))
        else:
            setattr(self._src_space, self._src_name, self._mpt.copy())
        self._last_item = None

    def export_model(self) -> bytes:
        """Serialize the current (possibly edited) model to zip bytes in the
        :func:`zip_model_dir` layout. Writes only to a temp dir — the vendored
        lifelib tree is never touched."""
        import modelx as mx

        if self._model is None or self._path is None:
            raise RuntimeError("No model loaded")
        name = self._name or "model"
        tmp = Path(tempfile.mkdtemp(prefix="lifelib-ui-export-"))
        try:
            out_dir = tmp / name
            assert not str(out_dir.resolve()).startswith(str(Path(self._path).resolve())), (
                "export must not write into the source model"
            )
            # compute() leaves a per-point override on the table ref (a
            # one-row table for vectorized models) — put the real table back
            # so the export carries the full working model point table
            self._restore_table()
            # write_model re-serializes formulas (incl. edits) and copies
            # the model's own inputs (xlsx/csv under the model dir, _data/)
            orig_model_path = self._model.path
            mx.write_model(self._model, str(out_dir))
            # write_model re-points model.path at the output dir; Reference
            # Liability Models read `_model.path.parent / *.csv` at run time,
            # so put it back before the temp dir goes away
            self._model.path = str(orig_model_path)
            buf = io.BytesIO()
            zip_model_dir(out_dir, buf, siblings_from=Path(self._path).parent)
            return buf.getvalue()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _param_defaults(space) -> dict:
        """Defaults for a parameterized space's parameters after the first.

        ``lambda PolicyID, ScenID=1: None`` -> ``{"ScenID": 1}``.
        """
        params = list(space.parameters or ())
        if len(params) <= 1:
            return {}
        src = str(space.formula)
        m = re.search(r"lambda\s+([^:]*):", src)
        if not m:
            return {}
        out = {}
        for part in m.group(1).split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    out[k.strip()] = eval(v.strip(), {"__builtins__": {}})  # noqa: S307
                except Exception:
                    pass
        return {p: out[p] for p in params[1:] if p in out}

    def _find_binding_space(self, target, name: str):
        """Sub-space of ``target`` where reference ``name`` is bound
        (Past Libraries: Projection.Policy.PolicyData)."""

        def walk(space):
            yield space
            for child in space.spaces.values():
                yield from walk(child)

        for s in walk(target):
            try:
                if name in s.refs:
                    return s
            except Exception:
                continue
        return None

    @staticmethod
    def _walk_spaces(model):
        def walk(space):
            yield space
            for child in space.spaces.values():
                yield from walk(child)

        for top in model.spaces.values():
            yield from walk(top)

    def _find_model_point_table(self, model):
        """Locate the model point table.

        Two layouts are supported:

        - a DataFrame *reference* named e.g. ``model_point_table``
          (basiclife, savings — the table comes from an Excel DataClient);
        - a no-argument *cell* named e.g. ``model_point_table`` returning a
          DataFrame (frlib/jplib/uklib/uslib keep it in a ``Data`` space).

        Both can be overridden the same way — ``setattr(space, name, df)``
        reassigns the ref, or sets an input value on the no-arg cell; either
        way modelx invalidates all dependent caches.
        """
        candidates: list[tuple[int, Any, str, pd.DataFrame, str]] = []
        for space in self._walk_spaces(model):
            try:
                refs = set(space.refs)
                cells = space.cells
            except Exception:
                continue
            for name in MP_REF_NAMES:
                if name in refs:
                    value = getattr(space, name)
                    if isinstance(value, pd.DataFrame) and len(value):
                        score = 10 + (2 if "result_cf" in cells else 0)
                        candidates.append((score, space, name, value, "ref"))
                    elif self._is_mapping(value):
                        # Past Libraries: ExcelRange keyed by (PolicyID, attr)
                        table = self._mapping_to_frame(value)
                        if table is not None and not space.parameters:
                            candidates.append((5, space, name, table, "mapping"))
            if space.parameters:
                continue  # don't evaluate cells of parameterized base spaces
            for name in MP_CELL_NAMES:
                if name in cells and not cells[name].parameters:
                    try:
                        value = cells[name]()
                    except Exception:
                        continue
                    if isinstance(value, pd.DataFrame) and len(value):
                        candidates.append((1, space, name, value, "cell"))
        if not candidates:
            return None, None, None, None
        candidates.sort(key=lambda c: c[0], reverse=True)
        _, space, name, table, kind = candidates[0]
        return space, name, table, kind

    @staticmethod
    def _is_mapping(value) -> bool:
        return (
            hasattr(value, "keys")
            and hasattr(value, "__getitem__")
            and not isinstance(value, (pd.DataFrame, pd.Series, dict, str))
        )

    @staticmethod
    def _mapping_to_frame(value) -> pd.DataFrame | None:
        """Pivot an ``{(policy_id, attr): value}`` mapping to a DataFrame."""
        try:
            items = dict(value)
            if not items:
                return None
            k0 = next(iter(items))
            if not (isinstance(k0, tuple) and len(k0) == 2):
                return None
            df = pd.Series(items).unstack()
            df.index.name = "PolicyID"
            # infer proper dtypes (mapping values are object)
            for col in df.columns:
                try:
                    df[col] = pd.to_numeric(df[col])
                except (ValueError, TypeError):
                    pass
            return df.infer_objects()
        except Exception:
            return None

    @staticmethod
    def _frame_to_mapping(df: pd.DataFrame) -> dict:
        out = {}
        for pid, row in df.iterrows():
            for col, val in row.items():
                if hasattr(val, "item"):
                    val = val.item()
                if isinstance(val, float) and val != val:
                    val = None  # empty Excel cell round-trips as None
                out[(pid, col)] = val
        return out

    def _find_result_space(self, model, src_space):
        """Pick the space whose results we compute.

        Prefers a space with ``result_cf``/``result_pv``; abstract base spaces
        (whose ``result_cf`` raises NotImplementedError, e.g.
        BasicTermASL_ME.Base) are skipped. Past Libraries have no result
        cells: their top-level ``Projection``/``OuterProj`` space is used and
        a cashflow table is synthesized from per-period cells.
        """
        best, best_score = None, -1
        for space in self._walk_spaces(model):
            try:
                cells = space.cells
            except Exception:
                continue
            score = 0
            if "result_cf" in cells:
                score += 4
            if "result_pv" in cells:
                score += 2
            if score and space is src_space:
                score += 1
            if score and not space.parameters and not self._concrete(space):
                continue  # abstract base (NotImplementedError)
            if score and space.name in ("Projection", "Pricing"):
                score += 1  # prefer the user-facing space over bases
            if score > best_score and score > 0:
                best, best_score = space, score
        if best is not None:
            return best
        # Past Libraries: no result cells anywhere — use the projection space
        for name in ("Projection", "OuterProj"):
            if name in model.spaces:
                return model.spaces[name]
        return src_space

    @staticmethod
    def _concrete(space) -> bool:
        """False if the space is an abstract base: some cell's formula raises
        NotImplementedError (BasicTermASL_ME.Base.premium_pp). Static check —
        never evaluates the (possibly expensive) space."""
        try:
            for name in space.cells:
                src = str(space.cells[name].formula or "")
                if "raise NotImplementedError" in src:
                    return False
        except Exception:
            pass
        return True

    # -- computing ---------------------------------------------------------

    def compute(self, row_pos: int, edits: dict) -> dict | None:
        """Recompute results for one model point with edited field values.

        Returns None for stale/invalid requests (wrong table shape/schema —
        can only come from a superseded request against a previous model).
        """
        if (
            self._src_space is None
            or self._mpt is None
            or self._src_name is None
            or self._target is None
        ):
            return None
        t0 = time.perf_counter()
        if row_pos >= len(self._mpt):
            return None
        if any(col not in self._mpt.columns for col in edits):
            return None
        pid = self._mpt.index[row_pos]
        df = self._mpt.copy()
        for col, val in edits.items():
            val = self._coerce(df[col].dtype, val)
            df.loc[pid, col] = val

        if self._src_kind == "mapping":
            # Past Libraries: the Excel range is a {(pid, attr): value}
            # mapping bound on Projection.Policy — override with a dict
            setattr(self._src_space, self._src_name, self._frame_to_mapping(df))
            target = self._target(pid, **self._extra_args)
        elif self._parameterized:
            # *_S models: overriding the table (ref reassignment, or input
            # value on the no-arg cell) invalidates every dependent
            # cell/itemspace; then build the itemspace for this point.
            setattr(self._src_space, self._src_name, df)
            target = self._target(pid, **self._extra_args)
        else:
            # *_M / *_ME models are vectorized over the whole table:
            # feed a one-row table so results relate to this point only.
            setattr(self._src_space, self._src_name, df.loc[[pid]])
            target = self._target
        self._last_item = target  # kept alive so the inspector can trace

        result: dict = {
            "point_label": str(pid),
            "result_cf": None,
            "result_pv": None,
            "policy_series": {},
        }
        cells = target.cells
        if "result_cf" in cells:
            result["result_cf"] = self._to_frame(target.result_cf())
        if "result_pv" in cells:
            result["result_pv"] = self._to_frame(target.result_pv())
        if result["result_cf"] is None:
            result["result_cf"] = self._synth_result_cf(target)
        if result["result_pv"] is None:
            result["result_pv"] = self._synth_result_pv(target)

        n = len(result["result_cf"]) if result["result_cf"] is not None else 0
        for name in POLICY_CELLS:
            if name in cells and n:
                try:
                    result["policy_series"][name] = [
                        float(np.asarray(cells[name](t)).sum()) for t in range(n)
                    ]
                except Exception:
                    pass

        # Note: the itemspace is intentionally NOT cleared here — the
        # inspector traces precedents/dependents through its cached nodes.
        # Memory stays bounded: the next table override invalidates caches.

        if result["result_cf"] is None and result["result_pv"] is None:
            available = ", ".join(sorted(cells)[:40])
            raise RuntimeError(
                "Model has no 'result_cf'/'result_pv' cells to visualize.\n"
                f"Available cells: {available} …"
            )
        result["elapsed"] = time.perf_counter() - t0
        return result

    # -- result synthesis (models without result_cf/result_pv) ---------------

    @staticmethod
    def _proj_len(target) -> int | None:
        cells = target.cells
        for name in PROJ_LEN_CELLS:
            if name in cells and not cells[name].parameters:
                try:
                    n = int(cells[name]())
                    return n + 1 if name == "last_t" else n
                except Exception:
                    continue
        return None

    def _synth_result_cf(self, target) -> pd.DataFrame | None:
        """Cashflow table from per-period cells when ``result_cf`` is absent
        (Past Libraries: PremIncome, BenefitTotal, ExpsTotal, NetInsurCF…)."""
        n = self._proj_len(target)
        if not n:
            return None
        cells = target.cells
        data = {}
        for label, names in CF_CANDIDATES:
            name = next(
                (c for c in names if c in cells and len(cells[c].parameters or ()) == 1), None
            )
            if name is None:
                continue
            try:
                data[label] = [float(np.asarray(cells[name](t)).sum()) for t in range(n)]
            except Exception:
                continue
        if not data:
            return None
        return pd.DataFrame(data, index=pd.Index(range(n), name="t"))

    def _synth_result_pv(self, target) -> pd.DataFrame | None:
        """One-row PV table from ``PV_*``/``pv_*`` cells (period 0)."""
        cells = target.cells
        row = {}
        for name in cells:
            if not name.startswith(PV_PREFIXES):
                continue
            c = cells[name]
            try:
                v = c(0) if len(c.parameters or ()) == 1 else (c() if not c.parameters else None)
            except Exception:
                continue
            if isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool):
                row[name[3:]] = float(v)
        if not row:
            return None
        return pd.DataFrame([row], index=pd.Index(["PV"]))

    # -- model point management ---------------------------------------------

    def set_points(self, df: pd.DataFrame) -> pd.DataFrame:
        """Replace the working model point table (import/generate/append).

        Columns are reordered and coerced to the schema of the originally
        loaded table, so a CSV round-trip can't corrupt dtypes.
        """
        if self._mpt is None:
            raise RuntimeError("No model loaded")
        missing = [c for c in self._mpt.columns if c not in df.columns]
        if missing:
            raise RuntimeError(
                f"Imported table is missing columns: {missing}\n"
                f"Expected schema: {list(self._mpt.columns)}"
            )
        df = df[list(self._mpt.columns)]
        try:
            df = df.astype(self._mpt.dtypes.to_dict())
        except Exception as exc:
            raise RuntimeError(f"Could not coerce column dtypes: {exc}") from exc
        if df.index.duplicated().any():
            raise RuntimeError("Model point ids (index) must be unique")
        self._mpt = df.copy()
        return df.copy()

    # -- inspection --------------------------------------------------------

    def _build_cells_index(self, model) -> list[dict]:
        """Static, searchable metadata for every cell in the model."""
        index = []
        for space in self._walk_spaces(model):
            space_path = self._space_path(space)
            try:
                cells = space.cells
            except Exception:
                continue
            for name in cells:
                c = cells[name]
                try:
                    source = str(c.formula) if c.formula is not None else ""
                except Exception:
                    source = ""
                index.append(
                    {
                        "space": space_path,
                        "name": name,
                        "params": ", ".join(c.parameters or ()),
                        "doc": (c.doc or "").strip(),
                        "source": source,
                        "edited": (space_path, name) in self._edits,
                    }
                )
            # data references (tables) are searchable and inspectable too
            try:
                refs = list(space.refs)
            except Exception:
                refs = []
            for name in refs:
                if name.startswith("_"):
                    continue
                try:
                    val = getattr(space, name)
                except Exception:
                    continue
                if not isinstance(val, (pd.DataFrame, pd.Series)):
                    continue
                prov = self._ref_prov.get(id(val))
                index.append(
                    {
                        "space": space_path,
                        "name": name,
                        "params": "reference",
                        "doc": (
                            f"table reference — {prov}"
                            if prov
                            else f"table reference ({type(val).__name__} "
                            f"{getattr(val, 'shape', '')})"
                        ),
                        "source": "",
                    }
                )
        return index

    def _space_path(self, space) -> str:
        """Space path without the model name and without itemspace segments.

        ``BasicTerm_S.Projection.__Space1`` -> ``Projection``.
        """
        parts = space.fullname.split(".")[1:]
        return ".".join(p for p in parts if not p.startswith("__Space"))

    def _resolve_cells(self, space_path: str, name: str):
        """Find the live Cells object for a (space_path, name) pair.

        Prefer the last computed space/itemspace so traced nodes carry values;
        fall back to the base space reached by walking the model.
        """
        if self._last_item is not None:
            try:
                if (
                    self._space_path(self._last_item) == space_path
                    and name in self._last_item.cells
                ):
                    return self._last_item.cells[name]
            except Exception:
                pass
        if self._model is None:
            raise RuntimeError("No model loaded")
        space: Any = self._model
        for seg in space_path.split("."):
            space = space.spaces[seg]
        return space.cells[name]

    def inspect(self, space_path: str, name: str, args) -> dict:
        """Inspect a cell (args=None) or a node (args=tuple).

        Returns static info (doc, source), the cached invocations, and — for a
        node — its value plus precedents/dependents grouped by cell.
        """
        payload = {
            "space": space_path,
            "name": name,
            "args": args,
            "params": "",
            "doc": "",
            "source": "",
            "value_repr": None,
            "value_data": None,
            "series": None,
            "cached": [],
            "preds": [],
            "succs": [],
            "error": None,
            "edited": (space_path, name) in self._edits,
        }
        try:
            c = self._resolve_cells(space_path, name)
        except Exception:
            if self._inspect_ref(payload, space_path, name):
                return payload
            raise
        payload["params"] = ", ".join(c.parameters or ())
        payload["doc"] = (c.doc or "").strip()
        try:
            payload["source"] = str(c.formula) if c.formula is not None else ""
        except Exception:
            pass
        payload["cached"] = self._cached_args(c)
        payload["series"] = self._cached_series(c, payload["cached"])

        if args is None and payload["cached"]:
            # default to the first cached invocation so the value and
            # dependency graph appear immediately
            args = payload["cached"][0]
            payload["args"] = args

        if args is not None:
            args = tuple(args)
            node = c.node(*args)
            if node.has_value():
                value = self._unwrap(node.value)
                payload["value_repr"] = self._value_repr(value)
                if isinstance(value, (pd.DataFrame, pd.Series)):
                    payload["value_data"] = value.copy()
                elif isinstance(value, np.ndarray):
                    payload["value_data"] = pd.Series(np.asarray(value).ravel())
            else:
                payload["error"] = (
                    "This invocation has no cached value (it was not "
                    "needed by the last computation). Pick a cached "
                    "invocation, or re-run a computation."
                )
            try:
                # node.precedents (unlike preds) includes ReferenceNodes,
                # i.e. table/data reads such as mort_table
                payload["preds"] = self._group_nodes(node.precedents)
                payload["succs"] = self._group_nodes(c.succs(*args))
            except Exception:
                pass
        return payload

    def _inspect_ref(self, payload: dict, space_path: str, name: str) -> bool:
        """Fill an inspect payload for a data reference (table read).

        Returns False if (space_path, name) is not a reference either.
        """
        try:
            space: Any = self._model
            for seg in space_path.split("."):
                space = space.spaces[seg]
            if name not in space.refs:
                return False
            val = getattr(space, name)
        except Exception:
            return False
        prov = self._ref_prov.get(id(val))
        payload["args"] = None
        payload["params"] = "reference"
        payload["doc"] = f"Data reference — loaded from {prov}" if prov else "Data reference"
        lines = [f"# reference: {space_path}.{name}", f"# type: {type(val).__name__}"]
        if isinstance(val, pd.DataFrame):
            lines.append(
                f"# shape: {val.shape}, index: {val.index.name}, columns: {list(val.columns)[:12]}"
            )
        if prov:
            lines.append(f"# source file: {prov}")
        lines.append("#")
        lines.append("# Formulas read this reference directly; modelx tracks")
        lines.append("# the dependency at whole-object granularity.")
        payload["source"] = "\n".join(lines)
        payload["value_repr"] = self._value_repr(val)
        if isinstance(val, (pd.DataFrame, pd.Series)):
            payload["value_data"] = val.copy()
        # precedent card: where the data came from
        if prov:
            payload["preds"] = [
                {
                    "space": "",
                    "name": prov,
                    "navigable": False,
                    "kind": "ref",
                    "args_list": [],
                    "value_repr": None,
                    "frame": None,
                    "series": None,
                    "marks": [],
                    "label": prov,
                }
            ]
        # dependents: cells whose formulas reference this table. modelx has
        # no reverse index for refs (ReferenceNode.succs is empty), so scan
        # formula sources for the name.
        payload["succs"] = self._ref_readers(name)
        return True

    def _ref_readers(self, ref_name: str) -> list[dict]:
        readers = []
        pattern = re.compile(rf"\b{re.escape(ref_name)}\b")
        for space in self._walk_spaces(self._model):
            try:
                cells = space.cells
            except Exception:
                continue
            sp = self._space_path(space)
            for cname in cells:
                try:
                    src = str(cells[cname].formula or "")
                except Exception:
                    continue
                if cname == ref_name or not pattern.search(src):
                    continue
                series = None
                try:
                    c = self._resolve_cells(sp, cname)
                    series = self._cached_series(c, self._cached_args(c))
                except Exception:
                    pass
                readers.append(
                    {
                        "space": sp,
                        "name": cname,
                        "navigable": True,
                        "kind": "cell",
                        "args_list": [],
                        "value_repr": None,
                        "frame": None,
                        "series": series,
                        "marks": [],
                        "label": f"{sp}.{cname}" if sp else cname,
                    }
                )
        readers.sort(key=lambda g: (g["space"], g["name"]))
        return readers

    def _cached_args(self, c, limit: int = 1000) -> list:
        """List of args tuples with cached values, e.g. [(0,), (1,), …]."""
        try:
            if not c.parameters:
                return [()] if c.node().has_value() else []
            frame = c.to_frame()
            out = []
            for key in frame.index[:limit]:
                key = key if isinstance(key, tuple) else (key,)
                # plain Python scalars so tuple equality works in the GUI
                out.append(tuple(x.item() if hasattr(x, "item") else x for x in key))
            return out
        except Exception:
            return []

    def _cached_series(self, c, cached: list, limit: int = 2000) -> dict | None:
        """Numeric series of a cell over its cached args.

        The first parameter (usually ``t``) is the x-axis. Cells with more
        parameters — e.g. pension models with ``(t, life)`` — produce one
        line per combination of the remaining parameters, labelled with the
        parameter names (``life=0``, ``life=1``). Values are already cached,
        so this is nearly free.

        Returns ``{"param": name, "lines": [{"key", "label", "x", "y"}]}``.
        """
        try:
            params = list(c.parameters or ())
            if not params or len(cached) < 2:
                return None
            lines_map: dict[tuple, list] = {}
            for a in cached[:limit]:
                x = a[0]
                if isinstance(x, bool) or not isinstance(x, (int, float, np.integer, np.floating)):
                    return None  # non-numeric first parameter: no chart
                node = c.node(*a)
                if not node.has_value():
                    continue
                v = self._unwrap(node.value)
                if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)):
                    continue  # skip non-scalar values
                lines_map.setdefault(tuple(a[1:]), []).append((float(x), float(v)))
            lines = []
            for key in sorted(lines_map, key=repr)[:MAX_SERIES_LINES]:
                pts = sorted(lines_map[key])
                if len(pts) < 2:
                    continue
                label = ", ".join(f"{p}={v!r}" for p, v in zip(params[1:], key)) or None
                lines.append(
                    {
                        "key": key,
                        "label": label,
                        "x": [pt[0] for pt in pts],
                        "y": [pt[1] for pt in pts],
                    }
                )
            if not lines:
                return None
            return {"param": params[0], "lines": lines}
        except Exception:
            return None

    def _group_nodes(self, nodes) -> list[dict]:
        """Group node-level precedents/dependents by cell for display.

        ``result_cf()`` has hundreds of node preds (premiums(0…120), …);
        one card per cell keeps navigation usable. Each group also carries a
        sparkline series of the cell over its cached invocations, plus the
        args actually consumed ("marks"), so the graph cards can show *which*
        part of a series feeds the current node.
        """
        groups: dict[tuple, dict] = {}
        for n in nodes:
            obj = n.obj
            is_cell = hasattr(obj, "formula")  # Cells
            try:
                objname = obj.name
                if is_cell:
                    space_path = self._space_path(obj.parent)
                else:  # ReferenceNode (table read)
                    # only *data* references are interesting; skip modules
                    # (pd, np), builtins and other plumbing
                    try:
                        val = n.value if n.has_value() else None
                    except Exception:
                        val = None
                    if not isinstance(
                        val,
                        (
                            pd.DataFrame,
                            pd.Series,
                            np.ndarray,
                            int,
                            float,
                            str,
                            bool,
                            dict,
                            list,
                            tuple,
                        ),
                    ):
                        continue
                    parts = obj.fullname.split(".")[1:-1]
                    space_path = ".".join(p for p in parts if not p.startswith("__Space"))
            except Exception:
                continue
            key = (space_path, objname)
            g = groups.setdefault(
                key,
                {
                    "space": space_path,
                    "name": objname,
                    "navigable": True,
                    "kind": "cell" if is_cell else "ref",
                    "args_list": [],
                    "value_repr": None,
                    "frame": None,
                    "series": None,
                    "marks": [],
                    "_cells": obj if is_cell else None,
                    "consumed_sum": None,
                    "_sum_ok": True,
                },
            )
            a = getattr(n, "args", None)
            if a is not None:
                g["args_list"].append(tuple(a))
            try:
                has = n.has_value()
            except Exception:
                has = False
            if has:
                v = self._unwrap(n.value)
                if g["value_repr"] is None:
                    g["value_repr"] = self._value_repr(v, short=True)
                    if isinstance(v, (pd.DataFrame, pd.Series)):
                        g["frame"] = self._mini_frame(v)
                # sum of the consumed scalar values (what e.g. result_cf
                # actually takes from a 121-invocation group)
                if (
                    g["_sum_ok"]
                    and isinstance(v, (int, float, np.integer, np.floating))
                    and not isinstance(v, bool)
                ):
                    g["consumed_sum"] = (g["consumed_sum"] or 0.0) + float(v)
                else:
                    g["_sum_ok"] = False
                    g["consumed_sum"] = None
        for g in groups.values():
            c = g.pop("_cells")
            g.pop("_sum_ok", None)
            if c is None:
                continue
            try:
                g["series"] = self._cached_series(c, self._cached_args(c))
            except Exception:
                pass
            # invocations consumed by the current node (full args tuples,
            # numeric first element so they can be placed on the series)
            g["marks"] = [
                (float(a[0]),) + tuple(a[1:])
                for a in g["args_list"]
                if len(a) >= 1 and isinstance(a[0], (int, float)) and not isinstance(a[0], bool)
            ]
        out = []
        for g in groups.values():
            n_args = len(g["args_list"])
            if n_args == 0:
                label_args = ""
            elif n_args == 1:
                label_args = "(" + ", ".join(map(repr, g["args_list"][0])) + ")"
            else:
                flat = [a[0] for a in g["args_list"] if len(a) == 1]
                if len(flat) == n_args:
                    label_args = f"({min(flat)!r}…{max(flat)!r}) ×{n_args}"
                else:
                    label_args = f"(…) ×{n_args}"
            g["label"] = (f"{g['space']}.{g['name']}" if g["space"] else g["name"]) + label_args
            if n_args == 1 and g["value_repr"] is not None:
                g["label"] += f" = {g['value_repr']}"
            out.append(g)
        out.sort(key=lambda g: (g["space"], g["name"]))
        return out

    @staticmethod
    def _mini_frame(value) -> pd.DataFrame | None:
        """Small numeric frame for card mini-charts (graph view)."""
        try:
            df = value.to_frame() if isinstance(value, pd.Series) else value
            num = df.select_dtypes(include=[np.number])
            if num.empty or len(num) < 2:
                return None
            return num.iloc[:500, :8].copy()
        except Exception:
            return None

    @staticmethod
    def _unwrap(value):
        """Collapse single-element containers to the scalar they hold.

        ``_M``/``_ME`` models vectorise over model points, so with the one-row
        table this explorer runs them on, every per-``t`` cell returns a
        length-1 Series / ndarray. Treating those as tables hides the number
        everywhere (cards say ``Series len=1``, no sparklines, no Σ) — so
        unwrap them at every display/aggregation site.
        """
        try:
            if isinstance(value, (pd.DataFrame, pd.Series)):
                if value.size == 1:
                    v = value.to_numpy().ravel()[0]
                    return v.item() if hasattr(v, "item") else v
                return value
            if isinstance(value, np.ndarray) and value.size == 1:
                return value.item()
            if isinstance(value, np.generic):
                return value.item()
        except Exception:
            pass
        return value

    @staticmethod
    def _value_repr(value, short: bool = False) -> str:
        try:
            value = ModelSession._unwrap(value)
            if isinstance(value, pd.DataFrame):
                if short:
                    return f"DataFrame {value.shape}"
                return value.to_string(max_rows=30, max_cols=12)
            if isinstance(value, pd.Series):
                if short:
                    return f"Series len={len(value)}"
                return value.to_string(max_rows=40)
            if isinstance(value, (np.floating, float)):
                return f"{float(value):,.6g}"
            if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
                return f"{int(value):,}"
            if isinstance(value, np.ndarray):
                return np.array2string(value, threshold=50, precision=6)
            r = repr(value)
            return r if len(r) <= 2000 else r[:2000] + " …"
        except Exception:
            return "<unprintable>"

    @staticmethod
    def _coerce(dtype, val):
        if pd.api.types.is_bool_dtype(dtype):
            if isinstance(val, str):
                return val.strip().lower() in ("true", "1", "yes")
            return bool(val)
        if val is None:
            # JSON transport encodes NaN as null (web N/A fields)
            if pd.api.types.is_integer_dtype(dtype):
                raise ValueError("integer column cannot be N/A")
            return np.nan if pd.api.types.is_float_dtype(dtype) else None
        if pd.api.types.is_integer_dtype(dtype):
            return int(val)
        if pd.api.types.is_float_dtype(dtype):
            return float(val)  # NaN passes through (N/A fields)
        return val

    @staticmethod
    def _to_frame(obj) -> pd.DataFrame:
        if isinstance(obj, pd.DataFrame):
            return obj.copy()
        if isinstance(obj, pd.Series):
            return obj.to_frame()
        return pd.DataFrame(obj)


# --------------------------------------------------------------------------
# JSON conversion (web transport)
# --------------------------------------------------------------------------


def _scalar(v):
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):  # before int: bool subclasses int
        return bool(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if f != f else f  # NaN -> null
    if isinstance(v, (np.integer, int)):
        return int(v)
    return str(v)


def frame_json(obj: pd.DataFrame | pd.Series | None) -> dict | None:
    if obj is None:
        return None
    df: pd.DataFrame = obj.to_frame() if isinstance(obj, pd.Series) else obj
    return {
        "columns": [str(c) for c in df.columns],
        "index": [_scalar(i) for i in df.index],
        "index_name": df.index.name,
        "data": [[_scalar(v) for v in row] for row in df.itertuples(index=False)],
        "dtypes": [str(t) for t in df.dtypes],
    }


def loaded_json(info: dict) -> dict:
    out = dict(info)
    out["model_point_table"] = frame_json(info["model_point_table"])
    return out


def compute_json(result: dict) -> dict:
    out = dict(result)
    out["result_cf"] = frame_json(result["result_cf"])
    out["result_pv"] = frame_json(result["result_pv"])
    return out


def _group_json(g: dict) -> dict:
    out = dict(g)
    out["frame"] = frame_json(g.get("frame"))
    out["args_list"] = [list(a) for a in g.get("args_list", [])]
    out["marks"] = [list(m) for m in g.get("marks", [])]
    return out


def _series_json(series: dict | None) -> dict | None:
    if not series:
        return None
    s = dict(series)
    s["lines"] = [dict(l, key=list(l["key"])) for l in s["lines"]]
    return s


def inspect_json(payload: dict) -> dict:
    out = dict(payload)
    out["args"] = list(payload["args"]) if payload["args"] is not None else None
    out["cached"] = [list(a) for a in payload["cached"]]
    out["value_data"] = frame_json(payload["value_data"])
    if payload.get("series"):
        out["series"] = _series_json(payload["series"])
    out["preds"] = [_group_json(g) for g in payload["preds"]]
    out["succs"] = [_group_json(g) for g in payload["succs"]]
    return out


def cell_values_json(values: dict) -> dict:
    return {
        k: dict(
            v,
            series=_series_json(v.get("series")),
            frame=frame_json(v.get("frame")),
            args=list(v["args"]) if v.get("args") is not None else None,
        )
        for k, v in values.items()
    }
