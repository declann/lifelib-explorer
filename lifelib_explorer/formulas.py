"""Formulas tab: every cell/table of the model as a card, grouped by space.

Click a card to select it: the card turns yellow, what it reads (precedents)
violet, what reads it (dependents) orange, everything else dims; the source
shows on the right with clickable cell names. Edges come from
``ModelSession.cell_graph()`` — static, read off the formula sources — so this
works for cells the last computation never touched. Mirrors the web's
``#tab-formulas`` (``app.js`` renderFormulas/fxSelect); keep them in lockstep.
"""

from __future__ import annotations

import math

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QRect, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .inspector import History, PythonHighlighter, Visit

SPARK_PALETTE = ("#3a76b0", "#4da06a", "#b0763a", "#9a5aa8",
                 "#a83a3a", "#3aa8a0", "#77772e", "#666666")


def fmt_num(v, digits: int = 5) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f):
        return "—"
    if abs(f) >= 1e6 or (0 < abs(f) < 1e-4):
        return f"{f:.{digits - 1}e}"
    return f"{f:,.{digits}g}"


def series_at(series: dict | None, t: float) -> tuple[float, float] | None:
    """(x, y) of the first line at t — exact, else the last x ≤ t (step
    semantics) — or None when t is before the line starts."""
    if not series or not series.get("lines"):
        return None
    line = series["lines"][0]
    xs, ys = line["x"], line["y"]
    best = -1
    for j, x in enumerate(xs):
        if x == t:
            return x, ys[j]
        if x <= t:
            best = j
    return (xs[best], ys[best]) if best >= 0 else None

# palette shared with web/styles.css (--cell, --ref, --cur, --prec, --dep)
ROLE_STYLE = {
    "": ("#eef4fc", "#b8cbe8", "#1a466b"),
    "ref": ("#eaf6ec", "#a8d3ae", "#2b6a33"),
    "sel": ("#fff3d6", "#e0b74c", "#1a466b"),
    "prec": ("#ede7f8", "#a98bd6", "#1a466b"),
    "dep": ("#fdebd9", "#eaa15e", "#1a466b"),
    "dim": ("#f6f7f9", "#e3e7ee", "#8a94a0"),
}
BOTH_BG = ("qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ede7f8, "
           "stop:0.5 #ede7f8, stop:0.501 #fdebd9, stop:1 #fdebd9)")


def key_of(space: str, name: str) -> str:
    return f"{space}.{name}" if space else name


class FlowLayout(QLayout):
    """Left-to-right, wrapping layout (Qt's flow-layout example, trimmed)."""

    def __init__(self, parent=None, spacing: int = 5):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 (Qt naming)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x, y, row_h = rect.x(), rect.y(), 0
        for item in self._items:
            w, h = item.sizeHint().width(), item.sizeHint().height()
            if x + w > rect.right() + 1 and row_h > 0:
                x = rect.x()
                y += row_h + self._spacing
                row_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x += w + self._spacing
            row_h = max(row_h, h)
        return y + row_h - rect.y()


class SparkWidget(QWidget):
    """QPainter sparkline — same anatomy as the graph cards (step lines,
    zero-baselined when one-signed, red dot at the marked x — no vertical
    rule, unlike the Inspector's cards). Cheap enough for hundreds of cards;
    matplotlib would not be."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._lines: list[tuple[np.ndarray, np.ndarray]] = []
        self._mark: float | None = None

    def set_data(self, series: dict | None, frame=None, mark: float | None = None) -> None:
        self._lines = []
        if series and series.get("lines"):
            for line in series["lines"]:
                self._lines.append((np.asarray(line["x"], float), np.asarray(line["y"], float)))
        elif frame is not None:
            vals = frame.to_numpy(dtype=float)
            x = np.arange(vals.shape[0], dtype=float)
            for j in range(min(vals.shape[1], 8)):
                self._lines.append((x, vals[:, j]))
        self._mark = mark
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        if not self._lines:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        allx = np.concatenate([l[0] for l in self._lines])
        ally = np.concatenate([l[1] for l in self._lines])
        ally = ally[np.isfinite(ally)]
        if not ally.size:
            return
        x0, x1 = float(allx.min()), float(allx.max())
        y0, y1 = float(ally.min()), float(ally.max())
        if y0 > 0:
            y0 = 0.0
        elif y1 < 0:
            y1 = 0.0

        def sx(v: float) -> float:
            return 2 + (w - 4) * ((v - x0) / (x1 - x0) if x1 > x0 else 0.5)

        def sy(v: float) -> float:
            return 2 + (h - 4) * (1 - ((v - y0) / (y1 - y0) if y1 > y0 else 0.5))

        if y0 <= 0 <= y1:
            p.setPen(QPen(QColor("#bbbbbb"), 0.6))
            p.drawLine(QPointF(0, sy(0.0)), QPointF(w, sy(0.0)))
        multi = len(self._lines) > 1
        for i, (xs, ys) in enumerate(self._lines):
            path = QPainterPath()
            started = False
            for x, y in zip(xs, ys):
                if not np.isfinite(y):
                    started = False
                    continue
                pt = QPointF(sx(x), sy(y))
                if not started:
                    path.moveTo(pt)
                    started = True
                else:
                    path.lineTo(QPointF(pt.x(), path.currentPosition().y()))
                    path.lineTo(pt)
            p.setPen(QPen(QColor(SPARK_PALETTE[i % len(SPARK_PALETTE)] if multi else "#3a76b0"), 1.2))
            p.drawPath(path)
        if self._mark is not None and x0 <= self._mark <= x1:
            # dot only — no vertical rule (the caption names the t; the rule
            # was visual noise across hundreds of cards)
            xs, ys = self._lines[0]
            i = int(np.searchsorted(xs, self._mark, side="right")) - 1
            i = max(0, min(i, len(xs) - 1))
            mx = sx(float(xs[i]))
            if np.isfinite(ys[i]):
                p.setBrush(QColor("#d62728"))
                p.setPen(QPen(QColor("white"), 1.2))
                p.drawEllipse(QPointF(mx, sy(float(ys[i]))), 3.2, 3.2)
        p.end()


class FormulaCard(QFrame):
    clicked = pyqtSignal(str, str)
    double_clicked = pyqtSignal(str, str)
    WIDTH = 178          # three columns fit next to the source panel at the 1280 px default

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.is_ref = entry.get("params") == "reference"
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(7, 3, 7, 4)
        lay.setSpacing(0)
        self.title = QLabel()
        f = QFont("monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSizeF(8.5)
        f.setItalic(self.is_ref)
        self.title.setFont(f)
        # "show values" body: sparkline + caption, or a big value
        self.spark = SparkWidget()
        self.spark.hide()
        self.big = QLabel()
        self.big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.big.setStyleSheet("font: 700 10pt monospace; color: #333; background: transparent; border: none;")
        self.big.hide()
        self.cap = QLabel()
        cf = QFont("monospace")
        cf.setStyleHint(QFont.StyleHint.Monospace)
        cf.setPointSizeF(7.5)
        self.cap.setFont(cf)
        self.cap.hide()
        self.doc = QLabel()
        self.doc.setStyleSheet("color: #6b7785; font-size: 8pt; background: transparent; border: none;")
        lay.addWidget(self.title)
        lay.addWidget(self.spark)
        lay.addWidget(self.big)
        lay.addWidget(self.cap)
        lay.addWidget(self.doc)
        self._role = "init"
        self.refresh()
        self.set_role("")

    def refresh(self) -> None:
        e = self.entry
        title = e["name"] if self.is_ref else f"{e['name']}({e.get('params', '')})"
        if e.get("edited"):
            title += "  <span style='color:#e0b74c'>●</span>"
        self.title.setText(title)
        doc = (e.get("doc") or "").split("\n")[0]
        fm = self.doc.fontMetrics()
        self.doc.setText(fm.elidedText(doc, Qt.TextElideMode.ElideRight, self.WIDTH - 18) or " ")

    def set_value(self, show: bool, v: dict | None, t: float) -> None:
        """Values mode: sparkline + 't=12: 0.899' (red), a big scalar, or a
        muted 'not computed' / 'table'; off: back to the doc line."""
        self.doc.setVisible(not show)
        if not show:
            self.spark.hide()
            self.big.hide()
            self.cap.hide()
            return
        cap_style = "color: #7a1f1f; background: transparent; border: none;"
        muted = "color: #8a94a0; font-style: italic; background: transparent; border: none;"
        self.spark.hide()
        self.big.hide()
        self.cap.show()
        if self.is_ref or not v:
            self.cap.setStyleSheet(muted)
            self.cap.setText("table" if self.is_ref else "not computed")
            return
        if v.get("series"):
            at = series_at(v["series"], t)
            param = v["series"].get("param") or "t"
            self.spark.set_data(v["series"], mark=at[0] if at else None)
            self.spark.show()
            self.cap.setStyleSheet(cap_style)
            if at is None:
                self.cap.setText(f"no value at {param}={t:g}")
            else:
                # "t=12:" at 50 % so the value is what reads first
                self.cap.setText(f"<span style='color:rgba(122,31,31,0.5)'>{param}={t:g}:</span> <b>{fmt_num(at[1])}</b>"
                                 + ("" if at[0] == t else f" <span style='color:#8a94a0'>({param}={at[0]:g})</span>"))
            return
        if v.get("frame") is not None:
            self.spark.set_data(None, frame=v["frame"])
            self.spark.show()
            self.cap.setStyleSheet(cap_style)
            self.cap.setText(str(v.get("value_repr") or ""))
            return
        self.cap.hide()
        self.big.show()
        args = v.get("args")
        prefix = (f"<span style='color:#8a94a0;font-weight:400'>({', '.join(map(repr, args))}) = </span>"
                  if args else "")
        self.big.setText(prefix + str(v.get("value_repr") or "")[:40])

    def set_role(self, role: str) -> None:
        """'' | sel | prec | dep | both | dim (refs keep their green when neutral)."""
        if role == self._role:
            return
        self._role = role
        base = "ref" if (role == "" and self.is_ref) else role
        if role == "both":
            bg, border, fg = BOTH_BG, ROLE_STYLE["dep"][1], ROLE_STYLE["dep"][2]
        else:
            bg, border, fg = ROLE_STYLE.get(base, ROLE_STYLE[""])
        width = 2 if role == "sel" else 1
        self.setStyleSheet(
            f"FormulaCard {{ background: {bg}; border: {width}px solid {border}; "
            f"border-radius: 5px; }} QLabel {{ background: transparent; border: none; }}")
        self.title.setStyleSheet(
            f"color: {fg}; background: transparent; border: none; "
            f"font-weight: {'700' if role == 'sel' else '400'};")
        self.doc.setStyleSheet(
            f"color: {'#a0a8b3' if role == 'dim' else '#6b7785'}; font-size: 8pt; "
            "background: transparent; border: none;")

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.entry["space"], self.entry["name"])
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self.double_clicked.emit(self.entry["space"], self.entry["name"])


class SourceView(QPlainTextEdit):
    """Read-only source; clicking an identifier that names a cell emits it."""

    cell_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        f = QFont("monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.setFont(f)
        self.cell_names: set[str] = set()
        self.highlighter = PythonHighlighter(self.document())

    def set_names(self, names: set[str]) -> None:
        self.cell_names = names
        self.highlighter.cell_names = names
        self.highlighter.rehighlight()

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        super().mousePressEvent(ev)
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        cur = self.cursorForPosition(ev.pos())
        cur.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cur.selectedText()
        if word in self.cell_names:
            self.cell_clicked.emit(word)


class FormulasPanel(QWidget):
    """All formulas as cards + selected formula's source and neighbours."""

    #: (space, name, args | None) -> MainWindow: show it in the Inspector
    open_in_inspector = pyqtSignal(str, str, object)
    #: "show values" is on and the cards are stale -> MainWindow asks the engine
    values_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._index: list[dict] = []
        self._edges: list[list[str]] = []
        self._precs: dict[str, set[str]] = {}
        self._deps: dict[str, set[str]] = {}
        self._by_key: dict[str, dict] = {}
        self._cards: dict[str, FormulaCard] = {}
        self._selected: str | None = None
        self._history = History()      # selection history (same rules as the Inspector's)
        self._values: dict[str, dict] | None = None
        self._values_stale = True
        self._t: float | None = None
        self._has_range = False        # t spin/slider range set from real series

        # -- top: history + filter + show values + t + legend ------------------
        self.back_btn = QPushButton("◀")
        self.back_btn.setFixedWidth(32)
        self.back_btn.setToolTip("Back (Alt+Left)")
        self.back_btn.clicked.connect(self.go_back)
        self.fwd_btn = QPushButton("▶")
        self.fwd_btn.setFixedWidth(32)
        self.fwd_btn.setToolTip("Forward (Alt+Right)")
        self.fwd_btn.clicked.connect(self.go_forward)
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(200)
        self.history_combo.setToolTip(
            "Selection history (newest first) — pick an entry to jump to it")
        self.history_combo.activated.connect(self._on_history_pick)
        self._sync_history_combo()
        self._update_nav_buttons()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter formulas — name, space, docstring")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMinimumWidth(150)
        self.filter.setMaximumWidth(280)
        self.filter.textChanged.connect(self._apply_filter)
        self.values_box = QCheckBox("Show values")
        self.values_box.setToolTip(
            "Show each cell's values from the last computation: a sparkline over t\n"
            "with the value at the selected t (like the Inspector's graph cards).\n"
            "Updates live as you change fields.")
        # persisted like the web's localStorage.fxValues; on by default — the
        # Formulas tab is the landing view and should show the model alive
        self._settings = QSettings("lifelib-playground", "lifelib-playground")
        self.values_box.setChecked(
            str(self._settings.value("formulas/showValues", "1")) in ("1", "true", "True"))
        self.values_box.toggled.connect(self._on_values_toggled)
        self.t_label = QLabel("at <b>t</b> =")
        self.t_spin = QSpinBox()
        self.t_spin.setRange(0, 0)
        self.t_spin.setFixedWidth(64)
        self.t_slider = QSlider(Qt.Orientation.Horizontal)
        self.t_slider.setRange(0, 0)
        self.t_slider.setFixedWidth(120)
        tip = ("The projection step whose value is shown on every card (red marker).\n"
               "Open in Inspector traces this invocation.")
        for w in (self.t_label, self.t_spin, self.t_slider):
            w.setToolTip(tip)
            w.setVisible(self.values_box.isChecked())
        self.t_spin.valueChanged.connect(self._on_t_spin)
        self.t_slider.valueChanged.connect(self._on_t_slider)
        legend = QLabel(
            "<span style='background:#fff3d6;border:1px solid #e0b74c'>&nbsp;&nbsp;&nbsp;</span> selected &nbsp; "
            "<span style='background:#ede7f8;border:1px solid #a98bd6'>&nbsp;&nbsp;&nbsp;</span> reads &nbsp; "
            "<span style='background:#fdebd9;border:1px solid #eaa15e'>&nbsp;&nbsp;&nbsp;</span> read by &nbsp; "
            "<span style='background:#eaf6ec;border:1px solid #a8d3ae'>&nbsp;&nbsp;&nbsp;</span> table")
        legend.setToolTip("selected card · what it reads (precedents) · what reads it (dependents) · table reference")
        legend.setStyleSheet("color: #6b7785;")
        self.mark_legend = QLabel("<span style='color:#d62728'>●</span> value at t")
        self.mark_legend.setStyleSheet("color: #6b7785;")
        self.mark_legend.setVisible(self.values_box.isChecked())
        self.count = QLabel("")
        self.count.setStyleSheet("color: #6b7785;")
        top = QHBoxLayout()
        top.addWidget(self.back_btn)
        top.addWidget(self.fwd_btn)
        top.addWidget(self.history_combo)
        top.addSpacing(6)
        top.addWidget(self.filter)
        top.addWidget(self.values_box)
        top.addWidget(self.t_label)
        top.addWidget(self.t_spin)
        top.addWidget(self.t_slider)
        top.addSpacing(8)
        top.addWidget(legend)
        top.addWidget(self.mark_legend)
        top.addStretch(1)
        top.addWidget(self.count)

        # -- left: scrollable card groups ------------------------------------
        self.cards_host = QWidget()
        self.cards_lay = QVBoxLayout(self.cards_host)
        self.cards_lay.setContentsMargins(6, 4, 6, 12)
        self.cards_lay.setSpacing(4)
        self.cards_lay.addStretch(1)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.cards_host)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        # -- right: selected formula ---------------------------------------
        self.header = QLabel("Click a formula card")
        self.header.setStyleSheet("font-size: 11pt; color: #6b7785;")
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.doc = QLabel("")
        self.doc.setStyleSheet("color: #6b7785;")
        self.doc.setWordWrap(True)
        self.source = SourceView()
        self.source.cell_clicked.connect(self._on_source_cell_clicked)
        self.precs = QLabel("")
        self.deps = QLabel("")
        for lbl in (self.precs, self.deps):
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
            lbl.setOpenExternalLinks(False)
            lbl.linkActivated.connect(self._on_link)
            lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lists = QHBoxLayout()
        lists.addWidget(self.precs, 1)
        lists.addWidget(self.deps, 1)
        self.inspect_btn = QPushButton("Open in Inspector ↗")
        self.inspect_btn.setEnabled(False)
        self.inspect_btn.setToolTip(
            "Trace this cell's values and cached invocations in the Inspector")
        self.inspect_btn.clicked.connect(self._open_selected)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(8, 4, 6, 6)
        sv.addWidget(self.header)
        sv.addWidget(self.doc)
        sv.addWidget(self.source, stretch=3)
        sv.addLayout(lists, stretch=1)
        row = QHBoxLayout()
        row.addWidget(self.inspect_btn)
        row.addStretch(1)
        sv.addLayout(row)

        side.setMinimumWidth(300)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.scroll_area)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([600, 360])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 4)
        lay.addLayout(top)
        lay.addWidget(split, stretch=1)

    # -- data ------------------------------------------------------------------

    def set_model(self, index: list[dict], graph: dict | None) -> None:
        """New model: rebuild the cards (the index list is shared with the
        Inspector, which patches ``edited``/``source`` in place)."""
        self._index = index
        self._by_key = {key_of(e["space"], e["name"]): e for e in index}
        self._selected = None
        self._history = History()
        self._sync_history_combo()
        self._update_nav_buttons()
        self._values = None
        self._values_stale = True
        self._t = None
        self._has_range = False
        self.source.set_names({e["name"] for e in index})
        self._rebuild_cards()
        self.set_graph(graph or {"edges": []})
        self.header.setText("Click a formula card")
        self.header.setStyleSheet("font-size: 11pt; color: #6b7785;")
        self.doc.setText("")
        self.source.setPlainText("")
        self.precs.setText("")
        self.deps.setText("")
        self.inspect_btn.setEnabled(False)
        self.inspect_btn.setText("Open in Inspector ↗")
        self._paint_values()

    # -- "show values" -------------------------------------------------------------

    @property
    def show_values(self) -> bool:
        return self.values_box.isChecked()

    def values_stale(self) -> bool:
        """True when a compute happened since the cards last got values."""
        return self._values_stale

    def mark_values_stale(self) -> None:
        self._values_stale = True

    def set_values(self, values: dict) -> None:
        """Engine's cell_values(): rebind the cards' sparklines/values."""
        self._values = values
        self._values_stale = False
        lo, hi = math.inf, -math.inf
        for v in values.values():
            s = v.get("series")
            if s:
                for line in s["lines"]:
                    lo = min(lo, min(line["x"]))
                    hi = max(hi, max(line["x"]))
        if not math.isfinite(lo):
            lo, hi = 0, 0
        lo_i, hi_i = int(math.floor(lo)), int(math.ceil(hi))
        for w in (self.t_spin, self.t_slider):
            w.blockSignals(True)
            w.setRange(lo_i, hi_i)
            w.blockSignals(False)
        self._has_range = True
        if self._t is None:
            self._t = lo_i
        self.set_t(self._t)
        QTimer.singleShot(0, self._scroll_to_selected)

    def set_t(self, t: float) -> None:
        """Pick the projection step the cards show (clamped to the cached range)."""
        t = int(round(t))
        if self._has_range:
            t = max(self.t_spin.minimum(), min(self.t_spin.maximum(), t))
        self._t = t
        for w in (self.t_spin, self.t_slider):
            w.blockSignals(True)
            w.setValue(t)
            w.blockSignals(False)
        self._paint_values()

    def _on_values_toggled(self, on: bool) -> None:
        self._settings.setValue("formulas/showValues", "1" if on else "0")
        for w in (self.t_label, self.t_spin, self.t_slider, self.mark_legend):
            w.setVisible(on)
        self._paint_values()
        if on:
            self.values_requested.emit()

    def _on_t_spin(self, v: int) -> None:
        self.set_t(v)

    def _on_t_slider(self, v: int) -> None:
        self.set_t(v)

    def _paint_values(self) -> None:
        show = self.show_values
        t = self._t if self._t is not None else 0
        for k, card in self._cards.items():
            card.set_value(show, (self._values or {}).get(k), t)
        if self._selected and self._selected in self._by_key:
            self._show_header(self._by_key[self._selected])

    def _invocation(self, e: dict) -> tuple | None:
        """The (t,) invocation 'Open in Inspector' traces in values mode, for
        plain one-parameter series cells; else None (the cell itself)."""
        if not self.show_values or self._t is None or not self._values:
            return None
        v = self._values.get(key_of(e["space"], e["name"]))
        params = [p.strip() for p in (e.get("params") or "").split(",") if p.strip()]
        if not v or not v.get("series") or len(params) != 1:
            return None
        at = series_at(v["series"], self._t)
        if not at:
            return None
        x = at[0]
        return (int(x) if float(x).is_integer() else x,)

    def set_graph(self, graph: dict) -> None:
        """(Re)load static edges; after a formula edit they may have changed."""
        self._edges = list(graph.get("edges", []))
        self._precs, self._deps = {}, {}
        for reader, read in self._edges:
            self._precs.setdefault(reader, set()).add(read)
            self._deps.setdefault(read, set()).add(reader)
        self._update_count()
        self._paint()
        if self._selected and self._selected in self._by_key:
            e = self._by_key[self._selected]
            self._show_side(e)

    def refresh_edited(self) -> None:
        """Index entries changed in place (edited flags / sources)."""
        for card in self._cards.values():
            card.refresh()
        if self._selected and self._selected in self._by_key:
            self._show_side(self._by_key[self._selected])

    def selected_key(self) -> str | None:
        return self._selected

    # -- selection ---------------------------------------------------------------

    def select(self, space: str, name: str, scroll: bool = True,
               from_history: bool = False) -> None:
        key = key_of(space, name)
        e = self._by_key.get(key)
        if e is None:
            return
        changed = key != self._selected
        self._selected = key
        # selection history: revisiting the current entry is a no-op, a new
        # selection truncates the forward branch; ◀ ▶ replay without pushing
        if not from_history:
            self._history.visit(Visit(space, name, None))
        self._sync_history_combo()
        self._update_nav_buttons()
        if changed:
            self._paint()
        self._show_side(e)
        if scroll:
            # deferred: at load time the cards are not laid out yet
            QTimer.singleShot(0, self._scroll_to_selected)

    # -- selection history (◀ ▶ + dropdown; the Inspector has the same trio) ------

    def go_back(self) -> None:
        v = self._history.back()
        if v:
            self.select(v.space, v.name, from_history=True)

    def go_forward(self) -> None:
        v = self._history.forward()
        if v:
            self.select(v.space, v.name, from_history=True)

    def _on_history_pick(self, i: int) -> None:
        pos = self.history_combo.itemData(i)
        if isinstance(pos, int) and 0 <= pos < len(self._history.entries):
            # jump within history (no new entry, forward branch preserved)
            self._history.pos = pos
            v = self._history.entries[pos]
            self.select(v.space, v.name, from_history=True)

    def _sync_history_combo(self) -> None:
        """Newest first; current entry marked and selected; never blank."""
        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        entries = self._history.entries
        if not entries:
            self.history_combo.addItem("History — nothing selected yet", None)
            self.history_combo.setCurrentIndex(0)
        else:
            for pos in range(len(entries) - 1, -1, -1):
                mark = "▸ " if pos == self._history.pos else "   "
                self.history_combo.addItem(mark + entries[pos].label, pos)
            self.history_combo.setCurrentIndex(
                len(entries) - 1 - self._history.pos)
        self.history_combo.blockSignals(False)

    def _update_nav_buttons(self) -> None:
        self.back_btn.setEnabled(self._history.pos > 0)
        self.fwd_btn.setEnabled(
            self._history.pos < len(self._history.entries) - 1)

    def _scroll_to_selected(self) -> None:
        card = self._cards.get(self._selected or "")
        if card is not None:
            self.scroll_area.ensureWidgetVisible(card, 20, 40)

    def _paint(self) -> None:
        key = self._selected
        P = self._precs.get(key, set()) if key else set()
        D = self._deps.get(key, set()) if key else set()
        for k, card in self._cards.items():
            if k == key:
                role = "sel"
            elif k in P and k in D:
                role = "both"
            elif k in P:
                role = "prec"
            elif k in D:
                role = "dep"
            else:
                role = "dim" if key else ""
            card.set_role(role)
            np_, nd = len(self._precs.get(k, ())), len(self._deps.get(k, ()))
            rel = {"sel": "selected", "both": f"read by {key} and reads it (mutual recursion, e.g. via t−1)",
                   "prec": f"read by {key} (precedent)", "dep": f"reads {key} (dependent)"}.get(role, "")
            card.setToolTip("\n".join(filter(None, [
                k, (card.entry.get("doc") or "").split("\n")[0], rel,
                f"{np_} reads · {nd} read by",
                "click to select · double-click to open in the Inspector"])))

    def _show_header(self, e: dict) -> None:
        """'Projection.pols_if (t)  = 0.899 at t=12' + the Open button's label."""
        key = key_of(e["space"], e["name"])
        is_ref = e.get("params") == "reference"
        val = ""
        v = (self._values or {}).get(key) if self.show_values else None
        if v and v.get("series"):
            at = series_at(v["series"], self._t if self._t is not None else 0)
            if at:
                val = (f" <span style='color:#6b7785;font-size:9pt'>= "
                       f"<b style='color:#d62728;font-family:monospace'>{fmt_num(at[1], 6)}</b>"
                       f" at {v['series'].get('param') or 't'}={at[0]:g}</span>")
        elif v and v.get("value_repr") is not None and v.get("frame") is None:
            val = (f" <span style='color:#6b7785;font-size:9pt'>= <b style='color:#d62728;"
                   f"font-family:monospace'>{str(v['value_repr'])[:40]}</b></span>")
        self.header.setStyleSheet("font-size: 11pt;")
        self.header.setText(
            f"<b style='font-family:monospace'>{key}</b> "
            + ("<i style='color:#6b7785'>table reference</i>" if is_ref
               else f"<i style='color:#6b7785'>({e.get('params', '')})</i>") + val)
        inv = self._invocation(e)
        self.inspect_btn.setText(
            f"Open {e['name']}({', '.join(map(repr, inv))}) in Inspector ↗" if inv
            else "Open in Inspector ↗")

    def _show_side(self, e: dict) -> None:
        key = key_of(e["space"], e["name"])
        is_ref = e.get("params") == "reference"
        self._show_header(e)
        self.doc.setText((e.get("doc") or "").split("\n")[0])
        src = e.get("source") or ""
        self.source.setPlainText(
            src if src else ("# table reference — formulas read it directly; no source"
                             if is_ref else "# no source"))

        def links(keys: set[str], title: str, color: str, bg: str) -> str:
            head = (f"<span style='background:{bg};color:{color};font-weight:600;"
                    f"padding:1px 4px'>{title}</span><br>")
            if not keys:
                return head + "<i style='color:#6b7785'>none</i>"
            sp = e["space"] + "."
            return head + " ".join(
                f"<a href='{k}' style='color:#1f2933;text-decoration:none;"
                f"font-family:monospace'>[{k[len(sp):] if k.startswith(sp) else k}]</a>"
                for k in sorted(keys))
        self.precs.setText(links(self._precs.get(key, set()), "reads", "#4b2d83", "#ede7f8"))
        self.deps.setText(links(self._deps.get(key, set()), "read by", "#8a4a12", "#fdebd9"))
        self.inspect_btn.setEnabled(True)

    # -- internals ---------------------------------------------------------------

    def _rebuild_cards(self) -> None:
        while self.cards_lay.count() > 1:
            item = self.cards_lay.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        self._cards = {}
        self._groups: list[tuple[QLabel, QWidget, list[FormulaCard]]] = []
        by_space: dict[str, list[dict]] = {}
        for e in self._index:
            by_space.setdefault(e["space"], []).append(e)
        for space in sorted(by_space):
            head = QLabel(space or "(model)")
            head.setStyleSheet("color: #6b7785; font-weight: 600; margin-top: 6px;")
            grid = QWidget()
            flow = FlowLayout(grid)
            cards = []
            for e in sorted(by_space[space], key=lambda e: e["name"]):
                card = FormulaCard(e)
                card.clicked.connect(lambda s, n: self.select(s, n, scroll=False))
                card.double_clicked.connect(self._open_card)
                flow.addWidget(card)
                self._cards[key_of(e["space"], e["name"])] = card
                cards.append(card)
            self.cards_lay.insertWidget(self.cards_lay.count() - 1, head)
            self.cards_lay.insertWidget(self.cards_lay.count() - 1, grid)
            grid.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            self._groups.append((head, grid, cards))
        self._apply_filter(self.filter.text())

    def _apply_filter(self, text: str) -> None:
        q = text.strip().lower()
        shown = 0
        for head, grid, cards in self._groups:
            any_visible = False
            for card in cards:
                e = card.entry
                ok = (not q) or (q in f"{e['space']}.{e['name']} {e.get('doc', '')}".lower())
                card.setVisible(ok)
                any_visible = any_visible or ok
                shown += ok
            head.setVisible(any_visible)
            grid.setVisible(any_visible)
            grid.updateGeometry()
        self._filtered = shown
        self._update_count()

    def _update_count(self) -> None:
        n = len(self._index)
        shown = getattr(self, "_filtered", n)
        self.count.setText(f"{shown} of {n}" if shown != n
                           else f"{n} cells & tables · {len(self._edges)} edges")

    def _on_link(self, href: str) -> None:
        e = self._by_key.get(href)
        if e:
            self.select(e["space"], e["name"])

    def _on_source_cell_clicked(self, word: str) -> None:
        if not self._selected:
            return
        space = self._by_key[self._selected]["space"]
        e = self._by_key.get(key_of(space, word)) or next(
            (x for x in self._index if x["name"] == word), None)
        if e:
            self.select(e["space"], e["name"])

    def _open_selected(self) -> None:
        if self._selected and self._selected in self._by_key:
            e = self._by_key[self._selected]
            self.open_in_inspector.emit(e["space"], e["name"], self._invocation(e))

    def _open_card(self, space: str, name: str) -> None:
        e = self._by_key.get(key_of(space, name))
        if e:
            self.open_in_inspector.emit(space, name, self._invocation(e))
