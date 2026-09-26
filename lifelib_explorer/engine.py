"""Qt worker wrapping the pure-Python engine core.

All modelx work happens in :class:`EngineWorker`, which is moved onto a
dedicated ``QThread`` and delegates to :class:`~.engine_core.ModelSession`.
The GUI thread never calls modelx directly; it sends requests through queued
signal/slot connections and receives plain pandas/numpy result objects back.
modelx models are not thread-safe, so a single worker thread owning the model
is the simplest correct execution model.

The web app uses the same core inside a Pyodide web worker — see ``web/``.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

# Re-exported for the GUI modules (main.py imports from here).
from .engine_core import (  # noqa: F401
    MP_CELL_NAMES,
    MP_REF_NAMES,
    POLICY_CELLS,
    UNSUPPORTED,
    ModelInfo,
    ModelSession,
    discover_models,
    generate_model_points,
    unsupported_reason,
)

# --------------------------------------------------------------------------
# Payloads exchanged with the GUI thread
# --------------------------------------------------------------------------

@dataclass
class LoadedModel:
    """Static description of a loaded model, consumed by the GUI."""
    name: str
    path: str
    space_name: str              # space whose result cells are computed
    ref_name: str                # name of the table ref/cell and its space
    parameterized: bool          # True for *_S style models (Projection[point_id])
    model_point_table: pd.DataFrame  # a copy; GUI reads rows, never mutates model
    load_seconds: float
    has_result_cf: bool
    has_result_pv: bool
    cells_index: list = field(default_factory=list)  # searchable cell metadata
    cell_graph: dict = field(default_factory=dict)   # {"edges": [[reader, read], …]}
    model_name: str = ""         # key used for exports (Past Libraries: library dir)
    formula_edits: list = field(default_factory=list)


@dataclass
class ComputeResult:
    seq: int
    point_label: str
    result_cf: pd.DataFrame | None
    result_pv: pd.DataFrame | None
    policy_series: dict[str, list[float]] = field(default_factory=dict)
    elapsed: float = 0.0


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

class EngineWorker(QObject):
    """Owns a ModelSession. Lives on a background QThread."""

    model_loaded = pyqtSignal(object)   # LoadedModel
    compute_done = pyqtSignal(object)   # ComputeResult
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    inspect_done = pyqtSignal(object)   # dict payload for the inspector panel
    points_changed = pyqtSignal(object)  # new model point table (DataFrame)
    formula_changed = pyqtSignal(object)  # inspector payload after set/reset_formula
    formula_error = pyqtSignal(str)       # validation error (shown inline in the editor)
    model_exported = pyqtSignal(str, int)  # (path, bytes written)
    graph_ready = pyqtSignal(object)      # cell_graph dict (after formula edits)
    values_ready = pyqtSignal(int, object)  # (seq, cell_values dict) for the Formulas tab

    def __init__(self) -> None:
        super().__init__()
        self._session = ModelSession()
        # Written by the GUI thread before each compute request; reads/writes
        # of a Python int are atomic under the GIL. Drops stale requests.
        self.latest_seq: int = 0

    @pyqtSlot(str)
    def load_model(self, path: str) -> None:
        """Load a model directory, or a zip in the packaged/exported layout."""
        try:
            self.status.emit(f"Loading {Path(path).name} …")
            if path.lower().endswith(".zip"):
                info = self._session.load_bytes(Path(path).name, Path(path).read_bytes())
            else:
                info = self._session.load(path)
            loaded = LoadedModel(**info)
            self.status.emit(
                f"Loaded {loaded.name} in {loaded.load_seconds:.1f}s — table "
                f"'{loaded.ref_name}', results from '{loaded.space_name}', "
                f"{len(loaded.model_point_table)} model points")
            self.model_loaded.emit(loaded)
        except Exception:
            self.error.emit(traceback.format_exc())

    @pyqtSlot(int, int, dict)
    def compute(self, seq: int, row_pos: int, edits: dict) -> None:
        if seq < self.latest_seq:
            return  # a newer request is already queued; skip stale work
        try:
            self.status.emit("Computing …")
            result = self._session.compute(row_pos, edits)
            if result is None:
                return  # stale request against a previous model/table
            if seq < self.latest_seq:
                return  # superseded while computing
            res = ComputeResult(seq=seq, **result)
            self.status.emit(
                f"Point {res.point_label} computed in {res.elapsed:.2f}s")
            self.compute_done.emit(res)
        except Exception:
            self.error.emit(traceback.format_exc())

    @pyqtSlot(object)
    def set_model_points(self, df: pd.DataFrame) -> None:
        try:
            new = self._session.set_points(df)
            self.status.emit(f"Model point table replaced: {len(new)} points")
            self.points_changed.emit(new)
        except Exception:
            self.error.emit(traceback.format_exc())

    @pyqtSlot(int, str, str, object)
    def inspect(self, seq: int, space_path: str, name: str, args) -> None:
        try:
            payload = self._session.inspect(space_path, name, args)
        except Exception:
            payload = {"space": space_path, "name": name, "args": args,
                       "params": "", "doc": "", "source": "",
                       "value_repr": None, "value_data": None, "series": None,
                       "cached": [], "preds": [], "succs": [],
                       "error": traceback.format_exc()}
        payload["seq"] = seq
        self.inspect_done.emit(payload)

    @pyqtSlot(str, str, str)
    def set_formula(self, space_path: str, name: str, source: str) -> None:
        try:
            payload = self._session.set_formula(space_path, name, source)
        except Exception as exc:
            self.formula_error.emit(str(exc))
            return
        self.status.emit(f"Formula of {space_path}.{name} replaced — "
                         f"{len(self._session.formula_edits())} edit(s) in this session")
        self.formula_changed.emit(payload)

    @pyqtSlot(str, str)
    def reset_formula(self, space_path: str, name: str) -> None:
        try:
            payload = self._session.reset_formula(space_path, name)
        except Exception:
            self.error.emit(traceback.format_exc())
            return
        self.status.emit(f"Formula of {space_path}.{name} restored")
        self.formula_changed.emit(payload)

    @pyqtSlot()
    def cell_graph(self) -> None:
        """Re-derive the static formula graph (edges change with formula edits)."""
        try:
            self.graph_ready.emit(self._session.cell_graph())
        except Exception:
            self.error.emit(traceback.format_exc())

    @pyqtSlot(int)
    def cell_values(self, seq: int) -> None:
        """Per-cell series/values for the Formulas tab ("show values")."""
        if seq < self.latest_seq:
            return                      # a newer compute is queued; it will re-request
        try:
            self.values_ready.emit(seq, self._session.cell_values())
        except Exception:
            self.error.emit(traceback.format_exc())

    @pyqtSlot(str)
    def export_model(self, path: str) -> None:
        try:
            self.status.emit("Exporting model …")
            data = self._session.export_model()
            Path(path).write_bytes(data)
            self.model_exported.emit(path, len(data))
        except Exception:
            self.error.emit(traceback.format_exc())

    def formula_edit_count(self) -> int:
        """Read from the GUI thread; the list is tiny and only the worker writes it."""
        return len(self._session.formula_edits())
