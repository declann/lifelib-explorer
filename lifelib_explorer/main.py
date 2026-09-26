"""Main window and application entry point.

Execution model
---------------
- GUI thread: widgets only. Field edits are debounced (400 ms QTimer).
- Engine thread: a single QThread hosting EngineWorker, which owns the modelx
  model. Requests travel via queued signals; a monotonically increasing
  sequence number lets the worker drop stale/superseded compute requests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .engine import (
    ComputeResult,
    EngineWorker,
    LoadedModel,
    discover_models,
    generate_model_points,
    unsupported_reason,
)
from .engine_core import CATEGORY_ORDER, library_category
from .formulas import FormulasPanel
from .inspector import InspectorPanel
from .widgets import CashflowChart, PointEditor, PolicyChart, PVChart

DEBOUNCE_MIN_MS = 150      # floor of the adaptive throttle interval
DEBOUNCE_MAX_MS = 3000     # ceiling (very slow models)


class Bridge(QObject):
    """Signals emitted from the GUI thread, consumed by the engine thread."""

    load = pyqtSignal(str)
    compute = pyqtSignal(int, int, dict)
    inspect = pyqtSignal(int, str, str, object)
    set_points = pyqtSignal(object)
    export_model = pyqtSignal(str)
    cell_graph = pyqtSignal()
    cell_values = pyqtSignal(int)


class MainWindow(QMainWindow):
    def __init__(self, lifelib_root: Path):
        super().__init__()
        self.setWindowTitle("lifelib Explorer")
        self.resize(1280, 860)

        self._models = discover_models(lifelib_root)
        self._loaded: LoadedModel | None = None
        self._seq = 0
        self._inspect_seq = 0

        # -- engine thread ------------------------------------------------
        self._thread = QThread(self)
        self._worker = EngineWorker()
        self._worker.moveToThread(self._thread)
        self._bridge = Bridge()
        self._bridge.load.connect(self._worker.load_model)
        self._bridge.compute.connect(self._worker.compute)
        self._bridge.inspect.connect(self._worker.inspect)
        self._bridge.set_points.connect(self._worker.set_model_points)
        self._bridge.export_model.connect(self._worker.export_model)
        self._bridge.cell_graph.connect(self._worker.cell_graph)
        self._bridge.cell_values.connect(self._worker.cell_values)
        self._worker.model_exported.connect(self._on_model_exported)
        self._worker.model_loaded.connect(self._on_model_loaded)
        self._worker.compute_done.connect(self._on_compute_done)
        self._worker.points_changed.connect(self._on_points_changed)
        self._worker.error.connect(self._on_error)
        self._worker.status.connect(self.statusBar().showMessage)
        self._thread.start()

        # -- adaptive throttle timer -----------------------------------------
        # Trailing-edge throttle: while a slider is dragged the timer keeps
        # firing at the current interval (it is NOT restarted per edit), so
        # fast models update live during the drag. The interval adapts to the
        # last observed compute time.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MIN_MS)
        self._debounce.timeout.connect(self._request_compute)

        # -- left panel -----------------------------------------------------
        # grouped by lifelib's own taxonomy (Generic / Reference / Past /
        # Miscellaneous); unsupported entries are greyed with the reason
        self.model_combo = QComboBox()
        by_cat: dict[str, list] = {}
        for info in self._models:
            by_cat.setdefault(library_category(info), []).append(info)
        for cat in CATEGORY_ORDER:
            infos = by_cat.get(cat)
            if not infos:
                continue
            self.model_combo.addItem(f"— {cat} —", None)
            hdr = self.model_combo.model().item(self.model_combo.count() - 1)
            hdr.setEnabled(False)
            f = hdr.font(); f.setBold(True); hdr.setFont(f)
            for info in infos:
                reason = unsupported_reason(info)
                if reason:
                    self.model_combo.addItem(f"   {info.label}  — not supported", info)
                    item = self.model_combo.model().item(self.model_combo.count() - 1)
                    item.setEnabled(False)
                    item.setToolTip(reason)
                else:
                    self.model_combo.addItem(f"   {info.label}", info)
                    item = self.model_combo.model().item(self.model_combo.count() - 1)
                    item.setToolTip(f"{cat}\n{info.path}")
        self._user_models: list = []   # ModelInfo-like entries for zips loaded here
        # models auto-load on selection (debounced so keyboard-scrolling
        # through the list doesn't queue a load per step)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(250)
        self._load_timer.timeout.connect(self._on_load_clicked)
        self.model_combo.currentIndexChanged.connect(
            lambda _i: self._load_timer.start())

        self.point_combo = QComboBox()
        self.point_combo.setEditable(True)
        self.point_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.point_combo.currentIndexChanged.connect(self._on_point_changed)

        # model point data management (Parquet store, generator, import)
        self.gen_btn = QPushButton("Generate…")
        self.gen_btn.setToolTip(
            "Append synthetic model points sampled from the current table")
        self.gen_btn.clicked.connect(self._on_generate_clicked)
        self.import_btn = QPushButton("Import…")
        self.import_btn.setToolTip("Load model points from Parquet/CSV/Excel")
        self.import_btn.clicked.connect(self._on_import_clicked)
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip(
            "Save the current model point table (Parquet keeps exact dtypes)")
        self.export_btn.clicked.connect(self._on_export_clicked)
        points_row = QHBoxLayout()
        points_row.addWidget(self.gen_btn)
        points_row.addWidget(self.import_btn)
        points_row.addWidget(self.export_btn)

        # whole-model export/load (formula edits + current model points)
        self.export_model_btn = QPushButton("Export model…")
        self.export_model_btn.clicked.connect(self._on_export_model_clicked)
        self.load_model_btn = QPushButton("Load model…")
        self.load_model_btn.setToolTip(
            "Load a modelx model from a zip exported here, or from a model folder")
        self.load_model_btn.clicked.connect(self._on_load_model_clicked)
        model_row = QHBoxLayout()
        model_row.addWidget(self.export_model_btn)
        model_row.addWidget(self.load_model_btn)
        self._update_export_label()

        self.editor = PointEditor()
        self.editor.edited.connect(self._on_field_edited)

        self.reset_btn = QPushButton("Reset fields")
        self.reset_btn.setToolTip("Restore the selected point's original values")
        self.reset_btn.setEnabled(False)
        self.reset_btn.clicked.connect(self._on_reset_clicked)

        # headline results (visible from every tab) + baseline deltas
        self.summary = QLabel("—")
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            "QLabel { background:#f7f9fc; border:1px solid #d9e2ef; "
            "border-radius:4px; padding:6px; }")
        self.summary.setToolTip(
            "Present values for the selected point. Deltas compare against "
            "the point's original (unedited) values.")
        self._baseline_pv: dict | None = None
        self._last_pv_row: float | None = None

        # modeless error banner (replaces modal dialogs for compute errors)
        self.error_banner = QLabel()
        self.error_banner.setWordWrap(True)
        self.error_banner.setTextFormat(Qt.TextFormat.RichText)
        self.error_banner.setStyleSheet(
            "QLabel { background:#fdecec; border:1px solid #e8a3a3; "
            "border-radius:4px; padding:6px; color:#7a1f1f; }")
        self.error_banner.setOpenExternalLinks(False)
        self.error_banner.linkActivated.connect(self._on_error_link)
        self.error_banner.hide()
        self._last_error_text = ""

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Model"))
        lv.addWidget(self.model_combo)
        lv.addLayout(model_row)
        lv.addSpacing(8)
        lv.addWidget(QLabel("Model point"))
        lv.addWidget(self.point_combo)
        lv.addLayout(points_row)
        lv.addSpacing(8)
        lv.addWidget(QLabel("Results"))
        lv.addWidget(self.summary)
        lv.addWidget(self.error_banner)
        lv.addSpacing(8)
        lv.addWidget(QLabel("Fields  (drag sliders — results update live)"))
        lv.addWidget(self.editor, stretch=1)
        lv.addWidget(self.reset_btn)

        # -- right panel: charts --------------------------------------------
        self.cf_chart = CashflowChart()
        self.pv_chart = PVChart()
        self.pol_chart = PolicyChart()
        self.inspector = InspectorPanel()
        self.inspector.request_inspect.connect(self._on_inspect_requested)
        self._worker.inspect_done.connect(self._on_inspect_done)
        self.inspector.request_set_formula.connect(self._worker.set_formula)
        self.inspector.request_reset_formula.connect(self._worker.reset_formula)
        self._worker.formula_changed.connect(self._on_formula_changed)
        self._worker.formula_error.connect(self.inspector.on_formula_error)
        # Formulas tab: all cards; follows the Inspector's navigation and can
        # send a cell back to it
        self.formulas = FormulasPanel()
        self.formulas.open_in_inspector.connect(self._open_in_inspector)
        self.formulas.values_requested.connect(self._maybe_request_values)
        self.inspector.visited.connect(self._on_inspector_moved)
        self._worker.graph_ready.connect(self.formulas.set_graph)
        self._worker.values_ready.connect(self._on_values_ready)
        self.tabs = QTabWidget()
        # Formulas first: the whole model at a glance (live with "Show values");
        # the Inspector is where you trace one cell
        self.tabs.addTab(self.formulas, "Formulas")
        self.tabs.setTabToolTip(
            0, "All formulas as cards — click one to see what it reads and what "
               "reads it; Show values makes them live")
        self.tabs.addTab(self.inspector, "Inspector")
        self.tabs.setTabToolTip(
            1, "Trace one cell: value, formula, invocations, precedents → dependents")
        self.tabs.addTab(self.cf_chart, "Cashflows")
        self.tabs.addTab(self.pv_chart, "Present values")
        self.tabs.addTab(self.pol_chart, "Policy counts")
        self.tabs.addTab(self._make_about(), "About")
        self.tabs.currentChanged.connect(lambda _i: self._maybe_request_values())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 940])
        self.setCentralWidget(splitter)

        # persistent busy indicator on the right of the status bar
        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet("QLabel { color:#7a5a00; padding:0 8px; }")
        self.statusBar().addPermanentWidget(self.busy_label)
        self._busy_count = 0
        self.statusBar().showMessage(
            f"Found {len(self._models)} models — pick one to load it.")

        # window-level shortcuts (work from every tab)
        QShortcut(QKeySequence("Ctrl+R"), self, self._on_reset_clicked)
        QShortcut(QKeySequence("Ctrl+K"), self, self._focus_inspector_search)
        QShortcut(QKeySequence("Alt+Left"), self, self.inspector.go_back)
        QShortcut(QKeySequence("Alt+Right"), self, self.inspector.go_forward)

        # start on BasicTerm_S with the Formulas tab in front (auto-loads)
        default = next((i for i in range(self.model_combo.count())
                        if getattr(self.model_combo.itemData(i), "name", None)
                        == "BasicTerm_S"), -1)
        if default >= 0:
            if self.model_combo.currentIndex() == default:
                self._load_timer.start()
            else:
                self.model_combo.setCurrentIndex(default)

    def _make_about(self) -> QWidget:
        photo = Path(__file__).resolve().parent / "assets" / "dec.jpg"
        about = QLabel(
            "<div style='margin:24px; max-width:640px;'>"
            "<h2>lifelib Explorer</h2>"
            "<table cellpadding='0' cellspacing='0'><tr>"
            "<td valign='top' style='padding-right:16px'>"
            f"<a href='https://calcwithdec.dev/about'><img src='{photo}' "
            "width='120' height='160'></a></td><td valign='top'>"
            "<p style='color:#666; margin-top:0'>by "
            "<a href='https://calcwithdec.dev/about'>Declan Naughton</a> · "
            "<a href='https://github.com/declann/lifelib-explorer'>source on "
            "GitHub</a></p>"
            "<p>An <b>experimental</b> explorer for "
            "<a href='https://lifelib.io'>lifelib</a>'s Python actuarial models "
            "— interactive and visual. This is the PyQt desktop "
            "application; the browser build runs the same engine.</p>"
            "<p>lifelib is a collection of free and open-source actuarial "
            "models started by <b><a href='https://www.linkedin.com/in/"
            "fumito-hamamura/'>Fumito Hamamura</a></b>, built on his "
            "<a href='https://modelx.io'>modelx</a> framework.</p>"
            "</td></tr></table>"
            "<p>Here you can:</p>"
            "<ul>"
            "<li>Choose from a selection of models, or load your own (many "
            "don't work yet, or aren't well suited)</li>"
            "<li>Interactively explore and manipulate model points, with "
            "cashflows recomputing as you drag</li>"
            "<li>Trace any cell's formula, value and precedents/dependents "
            "through modelx's graph, including the tables it reads</li>"
            "<li>Edit formulas and see the effects immediately, then export the "
            "modified model as a modelx zip (not an optimised workflow yet)</li>"
            "</ul>"
            "<p>Fast feedback in modelling workflows — interpretation and "
            "communication as much as for development — is a keen interest of "
            "mine. <a href='https://actuarialplayground.com'>Actuarial "
            "Playground</a> gets its fast, interactive feedback from being based "
            "on calculang compiled to JavaScript<sup>*</sup>. lifelib Explorer "
            "shows that, thanks to <a href='https://pyodide.org'>Pyodide</a> and "
            "WebAssembly, we can get a similar experience from Python actuarial "
            "models: Python runs in the browser, with no server or backend "
            "complexity. The browser build works on mobile too — try it!</p>"
            "<p><b>How it was made.</b> This was also an experiment in process: "
            "the code was largely written by LLMs, driven from a "
            "terminal session in <code>tmux</code> — once on the web, a good "
            "part of it from my phone. It is <b>largely vibe-coded</b>: I "
            "steered technical and design choices and checked the behaviour, "
            "not much of the code, so <b>expect bugs</b>.</p>"
            "<p>I continue to develop other modelling work and research through "
            "<a href='https://calculang.dev'>calculang</a>. In 2025 I presented "
            "some aspects around this to the Society of Actuaries in Ireland, a "
            "presentation which is available "
            "<a href='https://www.youtube.com/watch?v=3C1mojRzfBA'>on "
            "YouTube</a>.</p>"
            "<p style='color:#666'><sup>*</sup> While calculang relies on "
            "JavaScript now, thanks to WebAssembly future-calculang might not — "
            "and without sacrificing its core aims.</p>"
            "<h3>Source</h3>"
            "<p>This app: <a href='https://github.com/declann/lifelib-explorer'>"
            "github.com/declann/lifelib-explorer</a> — issues and feedback "
            "welcome; the browser build is at "
            "<a href='https://declann.github.io/lifelib-explorer/'>"
            "declann.github.io/lifelib-explorer</a>. The models come from "
            "<a href='https://github.com/lifelib-dev/lifelib'>"
            "github.com/lifelib-dev/lifelib</a> (vendored unmodified) and run "
            "on <a href='https://github.com/fumitoh/modelx'>modelx</a>.</p>"
            "<h3>Keyboard</h3>"
            "<table cellpadding='3'>"
            "<tr><td><code>Ctrl+K</code></td><td>focus the Inspector search</td></tr>"
            "<tr><td><code>↓</code> / <code>Enter</code> in search</td>"
            "<td>move into results / open the selected cell</td></tr>"
            "<tr><td><code>Alt+←</code> / <code>Alt+→</code></td>"
            "<td>Inspector history back / forward</td></tr>"
            "<tr><td><code>Ctrl+R</code></td><td>reset the point's fields</td></tr>"
            "<tr><td><code>Ctrl+Enter</code> / <code>Esc</code> in the formula editor</td>"
            "<td>apply / cancel the edit</td></tr>"
            "</table>"
            "<p style='color:#666'>lifelib Explorer is released under the "
            "<a href='https://opensource.org/license/mit'>MIT License</a>. "
            "lifelib models are MIT-licensed by their authors and are not "
            "modified.</p>"
            "</div>")
        about.setTextFormat(Qt.TextFormat.RichText)
        about.setOpenExternalLinks(True)
        about.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        about.setWordWrap(True)
        return about

    # ------------------------------------------------------------------
    # slots
    # ------------------------------------------------------------------

    def _on_load_clicked(self) -> None:
        info = self.model_combo.currentData()
        if info is None or unsupported_reason(info):
            return
        if self._loaded is not None and self._loaded.path == info.path:
            return  # already loaded
        if not self._confirm_discard_edits():
            # put the combo back on the loaded model
            for i in range(self.model_combo.count()):
                d = self.model_combo.itemData(i)
                if d is not None and self._loaded is not None and d.path == self._loaded.path:
                    self.model_combo.blockSignals(True)
                    self.model_combo.setCurrentIndex(i)
                    self.model_combo.blockSignals(False)
                    break
            return
        self._load_path(info.path, info.name)

    def _load_path(self, path: str, name: str) -> None:
        # invalidate anything in flight for the previous model: stop the
        # throttle and bump both sequence numbers so queued/running compute
        # and inspect requests are dropped instead of hitting the new model
        self._debounce.stop()
        self._seq += 1
        self._worker.latest_seq = self._seq
        self._inspect_seq += 1
        self._set_busy(f"loading {name} …")
        self.editor.setEnabled(False)   # old fields must not be edited now
        self._clear_error()
        self.summary.setText("—")
        self._bridge.load.emit(path)

    def _confirm_discard_edits(self) -> bool:
        n = self.inspector.edited_count()
        if not n:
            return True
        r = QMessageBox.question(
            self, "Discard formula edits?",
            f"{n} formula edit{'s' if n > 1 else ''} will be lost when the model "
            "changes.\nExport the model first if you want to keep them.\n\n"
            "Continue?")
        return r == QMessageBox.StandardButton.Yes

    def _on_model_loaded(self, loaded: LoadedModel) -> None:
        self._set_busy(None)
        self._loaded = loaded
        table = loaded.model_point_table
        self._update_export_label()

        self.editor.set_table(table)
        self.editor.setEnabled(True)
        self.inspector.set_index(loaded.cells_index)
        self.formulas.set_model(loaded.cells_index, loaded.cell_graph)
        # default the inspector to the headline cell: result_cf, else a
        # net-cashflow cell (Past Libraries: NetInsurCF), else the first cell
        first = self._default_cell(loaded)
        if first:
            self.inspector.select_entry(first["space"], first["name"])
            self.inspector.navigate(first["space"], first["name"], None)

        self.point_combo.blockSignals(True)
        self.point_combo.clear()
        for pid in table.index:
            self.point_combo.addItem(str(pid))
        self.point_combo.blockSignals(False)

        self.cf_chart.clear()
        self.pv_chart.clear()
        self.pol_chart.clear()

        if len(table):
            self.point_combo.setCurrentIndex(0)
            self._on_point_changed(0)

    @staticmethod
    def _default_cell(loaded: LoadedModel) -> dict | None:
        import re
        idx = [e for e in loaded.cells_index if e["params"] != "reference"]
        in_space = [e for e in idx if e["space"] == loaded.space_name]
        for pick in (
            lambda e: e["name"] == "result_cf",
            lambda e: re.fullmatch(r"(?i)net_?cf|NetInsurCF|NetCF|net_cashflow", e["name"]),
            lambda e: re.match(r"(?i)pv_?net", e["name"]),
        ):
            hit = next((e for e in in_space if pick(e)), None)
            if hit:
                return hit
        return in_space[0] if in_space else (idx[0] if idx else None)

    def _on_point_changed(self, row_pos: int) -> None:
        if self._loaded is None or row_pos < 0:
            return
        row = self._loaded.model_point_table.iloc[row_pos]
        self.editor.set_row(row)
        self._baseline_pv = None       # new point: baseline re-captured
        self._update_dirty()
        self._clear_error()
        self._debounce.start()

    def _on_field_edited(self) -> None:
        self._update_dirty()
        # trailing-edge throttle: don't restart while running, so continuous
        # slider drags produce periodic recomputes instead of none at all
        if not self._debounce.isActive():
            self._debounce.start()

    def _on_reset_clicked(self) -> None:
        self._on_point_changed(self.point_combo.currentIndex())

    def _focus_inspector_search(self) -> None:
        self.tabs.setCurrentWidget(self.inspector)
        self.inspector.search.setFocus()
        self.inspector.search.selectAll()

    def _open_in_inspector(self, space: str, name: str, args=None) -> None:
        """Formulas tab → Inspector (double-click / “Open in Inspector”);
        in "show values" mode ``args`` is the (t,) shown on the cards."""
        self.inspector.select_entry(space, name)
        self.inspector.navigate(space, name, tuple(args) if args else None)
        self.tabs.setCurrentWidget(self.inspector)

    def _on_inspector_moved(self, space: str, name: str, args) -> None:
        """Inspector navigation: the Formulas tab follows — same cell selected,
        and its t = this invocation's first (numeric) argument."""
        self.formulas.select(space, name)          # scrolls the card into view (harmless while hidden)
        if args and isinstance(args[0], (int, float)) and not isinstance(args[0], bool):
            self.formulas.set_t(float(args[0]))

    # -- Formulas tab "show values": fetch only when it will be seen -------------

    def _maybe_request_values(self) -> None:
        if (self._loaded is not None and self.formulas.show_values
                and self.formulas.values_stale()
                and self.tabs.currentWidget() is self.formulas):
            self._bridge.cell_values.emit(self._seq)

    def _on_values_ready(self, seq: int, values: dict) -> None:
        if seq != self._seq:
            return                       # a newer compute will re-request
        self.formulas.set_values(values)

    def _update_dirty(self) -> None:
        dirty = self.editor.is_dirty()
        self.reset_btn.setEnabled(dirty)
        self.reset_btn.setText("Reset fields  ●" if dirty else "Reset fields")

    # -- status / errors -----------------------------------------------------

    def _set_busy(self, text: str | None) -> None:
        self.busy_label.setText(f"⏳ {text}" if text else "")

    def _show_error(self, text: str) -> None:
        self._last_error_text = text
        # modelx FormulaErrors end with the offending formula's source; pick
        # the real exception line (e.g. "KeyError: ('T10', 'M', ...)")
        import re as _re
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        exc_re = _re.compile(r"^([A-Za-z_][\w.]*(Error|Exception|Warning)): ")
        exc_lines = [ln for ln in lines if exc_re.match(ln)
                     and not ln.startswith("modelx.core.errors.FormulaError")]
        # the last exception line is the most specific (outermost cause)
        last = exc_lines[-1] if exc_lines else (lines[-1] if lines else "error")
        if len(last) > 160:
            last = last[:157] + "…"
        self.error_banner.setText(
            f"<b>Model error</b> — {last}<br>"
            "<a href='details'>details</a> &nbsp;·&nbsp; "
            "<a href='dismiss'>dismiss</a>")
        self.error_banner.show()

    def _clear_error(self) -> None:
        self.error_banner.hide()

    def _on_error_link(self, href: str) -> None:
        if href == "details":
            QMessageBox.critical(self, "lifelib Explorer — error details",
                                 self._last_error_text)
        else:
            self._clear_error()

    # -- model point data management ----------------------------------------

    def _on_generate_clicked(self) -> None:
        if self._loaded is None:
            return
        n, ok = QInputDialog.getInt(
            self, "Generate model points",
            "Number of synthetic points to append\n"
            "(sampled from the current table's ranges/values):",
            100, 1, 1_000_000)
        if not ok:
            return
        table = self._loaded.model_point_table
        new = generate_model_points(table, n)
        self._bridge.set_points.emit(pd.concat([table, new]))

    def _on_import_clicked(self) -> None:
        if self._loaded is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import model points", str(self._points_dir()),
            "Model points (*.parquet *.csv *.xlsx)")
        if not path:
            return
        try:
            if path.endswith(".parquet"):
                df = pd.read_parquet(path)
            elif path.endswith(".csv"):
                df = pd.read_csv(path)
            else:
                df = pd.read_excel(path)
            idx_name = self._loaded.model_point_table.index.name
            if idx_name and idx_name in df.columns:
                df = df.set_index(idx_name)
            self._bridge.set_points.emit(df)
        except Exception as exc:
            self._show_error(f"Import failed: {exc}")

    def _on_export_clicked(self) -> None:
        if self._loaded is None:
            return
        default = self._points_dir() / f"{self._loaded.name}_points.parquet"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export model points", str(default),
            "Parquet (*.parquet);;CSV (*.csv)")
        if not path:
            return
        try:
            df = self._loaded.model_point_table
            if path.endswith(".csv"):
                df.to_csv(path)
            else:
                df.to_parquet(path)
            self.statusBar().showMessage(f"Exported {len(df)} points to {path}")
        except Exception as exc:
            self._show_error(f"Export failed: {exc}")

    def _on_formula_changed(self, payload: dict) -> None:
        """Worker applied/reverted a formula: refresh the inspector, recompute."""
        self.inspector.on_formula_changed(payload)
        self.formulas.refresh_edited()      # edited dots / source (shared index)
        self._bridge.cell_graph.emit()      # the edit may have changed what it reads
        self._update_export_label()
        self._request_compute()

    def _update_export_label(self) -> None:
        n = self.inspector.edited_count() if hasattr(self, "inspector") else 0
        self.export_model_btn.setText("Export model… ●" if n else "Export model…")
        self.export_model_btn.setToolTip(
            (f"{n} formula edit{'s' if n > 1 else ''} in this session. " if n else "")
            + "Write the model as a modelx zip — with your formula edits and "
              "the current model point table. The source model is never changed.")

    def _on_export_model_clicked(self) -> None:
        if self._loaded is None:
            return
        n = self.inspector.edited_count()
        default = self._points_dir().parent / (
            f"{self._loaded.model_name or self._loaded.name}{'-edited' if n else ''}.zip")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export model", str(default), "modelx model zip (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        self._set_busy("exporting model …")
        self._bridge.export_model.emit(path)

    def _on_model_exported(self, path: str, size: int) -> None:
        self._set_busy(None)
        self.statusBar().showMessage(
            f"Exported model to {path} ({size // 1024} KB, "
            f"{self.inspector.edited_count()} formula edit(s))")
        self._request_compute()     # export rebinds the table ref; refresh caches

    def _on_load_model_clicked(self) -> None:
        if not self._confirm_discard_edits():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load model (zip exported here, or a modelx model folder's _system.json)",
            str(self._points_dir().parent), "Model (*.zip _system.json)")
        if not path:
            return
        if path.endswith("_system.json"):
            path = str(Path(path).parent)
        name = Path(path).stem if path.lower().endswith(".zip") else Path(path).name
        self._add_user_model(name, path)
        self._load_path(path, name)

    def _add_user_model(self, name: str, path: str) -> None:
        """List a user-loaded model at the top of the picker (no reload trigger)."""
        from types import SimpleNamespace
        info = SimpleNamespace(name=name, path=path, library="user",
                               label=f"user / {name}")
        self._user_models.append(info)
        self.model_combo.blockSignals(True)
        if not self.model_combo.count() or self.model_combo.itemText(0) != "— Your models —":
            self.model_combo.insertItem(0, "— Your models —", None)
            hdr = self.model_combo.model().item(0)
            hdr.setEnabled(False)
            f = hdr.font(); f.setBold(True); hdr.setFont(f)
        self.model_combo.insertItem(len(self._user_models), f"   {info.label}", info)
        self.model_combo.model().item(len(self._user_models)).setToolTip(path)
        self.model_combo.setCurrentIndex(len(self._user_models))
        self.model_combo.blockSignals(False)

    def _points_dir(self) -> Path:
        d = Path(__file__).resolve().parent.parent / "modelpoints"
        if self._loaded is not None:
            d = d / self._loaded.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _on_points_changed(self, table) -> None:
        """Worker accepted a new model point table."""
        if self._loaded is None:
            return
        current = self.point_combo.currentText()
        self._loaded.model_point_table = table
        self.editor.set_table(table)   # rebuild widgets (slider ranges etc.)
        self.point_combo.blockSignals(True)
        self.point_combo.clear()
        for pid in table.index:
            self.point_combo.addItem(str(pid))
        self.point_combo.blockSignals(False)
        pos = self.point_combo.findText(current)
        self.point_combo.setCurrentIndex(pos if pos >= 0 else 0)
        self._on_point_changed(self.point_combo.currentIndex())

    def _request_compute(self) -> None:
        if self._loaded is None:
            return
        row_pos = self.point_combo.currentIndex()
        if row_pos < 0:
            return
        self._seq += 1
        self._worker.latest_seq = self._seq  # lets the worker skip stale jobs
        self._set_busy("computing …")
        self._bridge.compute.emit(self._seq, row_pos, self.editor.values())

    def _on_compute_done(self, result: ComputeResult) -> None:
        if result.seq != self._seq:
            return  # stale result
        self._set_busy(None)
        self._clear_error()
        # adaptive throttle: fast models feel live under slider drags,
        # slow models don't pile up work
        interval = int(min(DEBOUNCE_MAX_MS,
                           max(DEBOUNCE_MIN_MS, result.elapsed * 1200)))
        self._debounce.setInterval(interval)
        self._update_summary(result)
        title = f"point {result.point_label}  ({result.elapsed:.2f}s)"
        if result.result_cf is not None:
            self.cf_chart.plot(result.result_cf, f"Cashflows — {title}")
        else:
            self.cf_chart.clear()
        if result.result_pv is not None:
            self.pv_chart.plot(result.result_pv, f"Present values — {title}")
        else:
            self.pv_chart.clear()
        if result.policy_series:
            self.pol_chart.plot(result.policy_series, f"Policy counts — {title}")
        else:
            self.pol_chart.clear()
        # values in the inspector may have changed — re-query the current view
        self.inspector.refresh_current()
        self.formulas.mark_values_stale()
        self._maybe_request_values()

    def _on_inspect_requested(self, space: str, name: str, args) -> None:
        self._inspect_seq += 1
        self._bridge.inspect.emit(self._inspect_seq, space, name, args)

    def _on_inspect_done(self, payload: dict) -> None:
        if payload.get("seq") != self._inspect_seq:
            return  # stale
        self.inspector.show_payload(payload)

    def _update_summary(self, result: ComputeResult) -> None:
        """Headline PV strip with deltas vs the point's unedited baseline."""
        pv = result.result_pv
        if pv is None or not len(pv):
            n = len(result.result_cf) if result.result_cf is not None else 0
            self.summary.setText(
                f"<b>point {result.point_label}</b> — {n} projection steps, "
                f"{result.elapsed:.2f}s (no result_pv in this model)")
            return
        row = pv.loc["PV"] if "PV" in pv.index else pv.iloc[0]
        vals = pd.to_numeric(row, errors="coerce").dropna()
        current = {str(k): float(v) for k, v in vals.items()}
        # headline "net" value of the latest compute (tests and status use it)
        self._last_pv_row = next((v for k, v in current.items() if "net" in k.lower()), None)
        if not self.editor.is_dirty():
            self._baseline_pv = current
        base = self._baseline_pv or {}
        rows = []
        for k, v in current.items():
            label = k.replace("PV ", "")
            cell = f"{v:,.0f}"
            if base and k in base and base[k] != v:
                d = v - base[k]
                color = "#1a7f37" if d > 0 else "#b42318"
                cell += (f" <span style='color:{color}'>"
                         f"({'+' if d > 0 else ''}{d:,.0f})</span>")
            bold = "net" in label.lower()
            rows.append(f"<tr><td style='padding-right:10px'>"
                        f"{'<b>' if bold else ''}{label}{'</b>' if bold else ''}"
                        f"</td><td align='right'>{'<b>' if bold else ''}{cell}"
                        f"{'</b>' if bold else ''}</td></tr>")
        edited = " · <i>edited</i>" if self.editor.is_dirty() else ""
        self.summary.setText(
            f"<b>point {result.point_label}</b> · {result.elapsed:.2f}s{edited}"
            f"<table style='margin-top:4px'>{''.join(rows)}</table>")

    def _on_error(self, text: str) -> None:
        self._set_busy(None)
        self.statusBar().showMessage("Error — see banner")
        self.editor.setEnabled(True)   # a failed load must not lock the editor
        self._show_error(text)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._thread.quit()
        self._thread.wait(3000)
        super().closeEvent(event)


def find_lifelib_root() -> Path:
    """Locate the lifelib libraries folder relative to this repo."""
    here = Path(__file__).resolve().parent.parent
    for candidate in (
        here / "lifelib" / "lifelib" / "libraries",
        here / "lifelib" / "libraries",
        here / "lifelib",
    ):
        if candidate.is_dir() and list(candidate.glob("**/_system.json")):
            return candidate
    return here


def main() -> int:
    app = QApplication(sys.argv)
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else find_lifelib_root()
    win = MainWindow(root)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
