"""GUI widgets: model point field editor and matplotlib chart panels."""

from __future__ import annotations

from typing import Any

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from PyQt6.QtCore import QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

MAX_COMBO_UNIQUES = 40

#: combo entry representing a missing value (NaN) in nullable columns
NA_LABEL = "«N/A»"


class NumericField(QWidget):
    """Spin box + slider, kept in sync. Slider range spans the observed
    column values, the spin box allows going beyond it. Columns containing
    NaN get an N/A checkbox — writing 0.0 where the model expects "missing"
    (e.g. *_override columns) would silently change semantics."""

    changed = pyqtSignal()
    SLIDER_STEPS = 500

    def __init__(self, series: pd.Series, parent=None):
        super().__init__(parent)
        self._is_int = pd.api.types.is_integer_dtype(series.dtype)
        self._nullable = bool(series.isna().any())
        s = series.dropna()
        lo = float(s.min()) if len(s) else 0.0
        hi = float(s.max()) if len(s) else 1.0
        if hi <= lo:
            hi = lo + (abs(lo) or 1.0)
        self._lo, self._hi = lo, hi

        if self._is_int:
            self.spin = QSpinBox()
            self.spin.setRange(int(min(0, lo)), int(max(hi * 10, hi + 100)))
            span = int(hi) - int(lo)
            self._steps = span if 0 < span <= self.SLIDER_STEPS \
                else self.SLIDER_STEPS
        else:
            self.spin = QDoubleSpinBox()
            self.spin.setDecimals(6)
            self.spin.setRange(-1e15, 1e15)
            self.spin.setSingleStep(max((hi - lo) / 100.0, 0.01))
            self._steps = self.SLIDER_STEPS

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, self._steps)
        self._guard = False
        self.spin.valueChanged.connect(self._spin_changed)
        self.slider.valueChanged.connect(self._slider_changed)

        self.na_check = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.spin)
        lay.addWidget(self.slider, stretch=1)
        if self._nullable:
            self.na_check = QCheckBox("N/A")
            self.na_check.toggled.connect(self._na_toggled)
            lay.addWidget(self.na_check)

    def _na_toggled(self, checked: bool) -> None:
        self.spin.setEnabled(not checked)
        self.slider.setEnabled(not checked)
        if not self._guard:
            self.changed.emit()

    def _to_slider(self, v: float) -> int:
        v = min(max(v, self._lo), self._hi)
        return round((v - self._lo) / (self._hi - self._lo) * self._steps)

    def _from_slider(self, pos: int):
        v = self._lo + pos / self._steps * (self._hi - self._lo)
        return int(round(v)) if self._is_int else v

    def _spin_changed(self, *_):
        if self._guard:
            return
        self._guard = True
        self.slider.setValue(self._to_slider(float(self.spin.value())))
        self._guard = False
        self.changed.emit()

    def _slider_changed(self, pos: int):
        if self._guard:
            return
        self._guard = True
        self.spin.setValue(self._from_slider(pos))
        self._guard = False
        self.changed.emit()

    def set_value(self, v) -> None:
        self._guard = True
        try:
            if pd.isna(v):
                if self.na_check is not None:
                    self.na_check.setChecked(True)
                return
            if self.na_check is not None:
                self.na_check.setChecked(False)
            self.spin.setValue(int(v) if self._is_int else float(v))
            self.slider.setValue(self._to_slider(float(v)))
        finally:
            self._guard = False

    def value(self):
        if self.na_check is not None and self.na_check.isChecked():
            return float("nan")
        return self.spin.value()


# --------------------------------------------------------------------------
# Model point editor
# --------------------------------------------------------------------------

class PointEditor(QScrollArea):
    """Auto-generated form for the columns of a model point table row.

    Emits ``edited`` whenever the user changes any field.
    """

    edited = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self.setWidget(self._form_host)
        self._widgets: dict[str, QWidget] = {}
        self._table: pd.DataFrame | None = None
        self._loading = False
        self._baseline: dict | None = None

    # -- construction --------------------------------------------------

    def set_table(self, table: pd.DataFrame) -> None:
        """Rebuild the form for a new model's table schema."""
        self._table = table
        # clear existing rows
        while self._form.rowCount():
            self._form.removeRow(0)
        self._widgets.clear()

        for col in table.columns:
            series = table[col]
            widget = self._make_widget(series)
            self._widgets[col] = widget
            self._form.addRow(col, widget)

    def _make_widget(self, series: pd.Series) -> QWidget:
        dtype = series.dtype
        if pd.api.types.is_bool_dtype(dtype):
            w = QCheckBox()
            w.toggled.connect(self._on_change)
            return w
        if pd.api.types.is_integer_dtype(dtype) or pd.api.types.is_float_dtype(dtype):
            w = NumericField(series)
            w.changed.connect(self._on_change)
            return w
        # categorical / object columns
        try:
            uniques = pd.unique(series.dropna())
        except TypeError:
            # unhashable values (lists etc.): display-only
            w = QLineEdit()
            w.setReadOnly(True)
            w.setToolTip("column type not editable")
            return w
        if 0 < len(uniques) <= MAX_COMBO_UNIQUES:
            w = QComboBox()
            for u in sorted(map(str, uniques)):
                w.addItem(u)
            if series.isna().any():
                w.addItem(NA_LABEL)
            w.currentTextChanged.connect(self._on_change)
            return w
        w = QLineEdit()
        w.textEdited.connect(self._on_change)
        return w

    # -- values ---------------------------------------------------------

    def is_dirty(self) -> bool:
        """True if any editable field differs from the row last set."""
        if self._baseline is None:
            return False
        for col, val in self.values().items():
            orig = self._baseline.get(col)
            if isinstance(val, float) and isinstance(orig, (int, float)):
                a, b = float(val), float(orig)
                if (a != a) and (b != b):      # both NaN
                    continue
                if abs(a - b) > 1e-9 * max(1.0, abs(b)):
                    return True
                continue
            if str(val) != str(orig):
                return True
        return False

    def set_row(self, row: pd.Series) -> None:
        """Populate widgets from a table row without emitting ``edited``."""
        self._loading = True
        self._baseline = {col: row[col] for col in self._widgets}
        try:
            for col, widget in self._widgets.items():
                val = row[col]
                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(val))
                elif isinstance(widget, NumericField):
                    widget.set_value(val)
                elif isinstance(widget, QComboBox):
                    text = NA_LABEL if pd.isna(val) else str(val)
                    idx = widget.findText(text)
                    if idx < 0:
                        widget.addItem(text)
                        idx = widget.count() - 1
                    widget.setCurrentIndex(idx)
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(val))
        finally:
            self._loading = False

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for col, widget in self._widgets.items():
            if isinstance(widget, QCheckBox):
                out[col] = widget.isChecked()
            elif isinstance(widget, NumericField):
                out[col] = widget.value()
            elif isinstance(widget, QComboBox):
                text = widget.currentText()
                out[col] = np.nan if text == NA_LABEL else text
            elif isinstance(widget, QLineEdit):
                if not widget.isReadOnly():   # display-only exotic columns
                    out[col] = widget.text()
        return out

    def _on_change(self, *_args) -> None:
        if not self._loading:
            self.edited.emit()


# --------------------------------------------------------------------------
# Chart panels
# --------------------------------------------------------------------------

class ChartPanel(FigureCanvas):
    def __init__(self, parent=None):
        self.figure = Figure(figsize=(6, 4), tight_layout=True)
        super().__init__(self.figure)
        self.setParent(parent)

    def clear(self) -> None:
        self.figure.clear()
        self.draw_idle()


# -- y-axis mode for multi-line charts -----------------------------------------
# Cashflow columns can differ by orders of magnitude (a single premium at t=0
# vs. monthly claims), so one shared axis hides the shape of the small ones;
# independent axes show every shape but hide magnitude; small multiples show
# both at the cost of height. One setting drives every multi-line chart
# (Inspector value chart + Cashflows tab), persisted across runs.

Y_MODES = (
    ("shared", "shared", "one y-axis for all lines — compare magnitudes"),
    ("independent", "indep.",
     "each line scaled to its own max |y| on one plot — compare shapes; "
     "the legend shows each max"),
    ("multiples", "multiples",
     "small multiples — one panel per line, each with its own y-axis"),
)


class YMode(QObject):
    changed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings("lifelib-playground", "lifelib-playground")
        m = str(self._settings.value("charts/yMode", "shared"))
        self._mode = m if m in {k for k, _, _ in Y_MODES} else "shared"

    @property
    def mode(self) -> str:
        return self._mode

    def set(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._settings.setValue("charts/yMode", mode)
        self.changed.emit(mode)


y_mode = YMode()


class YModeBar(QWidget):
    """Tiny segmented control: y · [shared] [indep.] [multiples]."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        lay.addStretch(1)
        lbl = QLabel("y")
        lbl.setStyleSheet("color: #6b7785; font-size: 10px;")
        lay.addWidget(lbl)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QToolButton] = {}
        for key, label, tip in Y_MODES:
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setChecked(key == y_mode.mode)
            b.setStyleSheet(
                "QToolButton { font-size: 10px; padding: 0 5px; border: 1px solid #d9e2ef; "
                "border-radius: 3px; color: #6b7785; background: #fff; }"
                "QToolButton:checked { background: #eef4fc; border-color: #b8cbe8; "
                "color: #1a466b; font-weight: 600; }")
            b.clicked.connect(lambda _=False, k=key: y_mode.set(k))
            self._group.addButton(b)
            self._buttons[key] = b
            lay.addWidget(b)
        y_mode.changed.connect(self._sync)

    def _sync(self, mode: str) -> None:
        for k, b in self._buttons.items():
            b.setChecked(k == mode)


class LinesChart(QWidget):
    """A figure with a y-mode bar above it.

    ``plot_lines(lines, …)`` renders ``[{label, x, y, color?, width?}]`` in
    the current y mode and re-renders when the mode changes; the bar is only
    shown for multi-line data (the modes coincide for a single line).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.figure = Figure(figsize=(6, 4), tight_layout=True)
        self.canvas = FigureCanvas(self.figure)
        self.bar = YModeBar()
        self.bar.hide()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.bar)
        lay.addWidget(self.canvas, stretch=1)
        self._last: tuple | None = None
        y_mode.changed.connect(self._replot)

    def draw_idle(self) -> None:
        self.canvas.draw_idle()

    def clear(self) -> None:
        self._last = None
        self.bar.hide()
        self.figure.clear()
        self.draw_idle()

    def _replot(self, _mode: str) -> None:
        if self._last is not None:
            self.plot_lines(*self._last)

    def plot_lines(self, lines: list[dict], title: str, xlabel: str | None = None,
                   mark: dict | None = None, ylabel: str | None = None) -> None:
        """``mark`` = {"x", "y", "line": idx, "text"} highlights one point."""
        self._last = (lines, title, xlabel, mark, ylabel)
        multi = len(lines) > 1
        self.bar.setVisible(multi)
        mode = y_mode.mode if multi else "shared"
        self.figure.clear()
        if mode == "multiples":
            self._plot_multiples(lines, title, xlabel, mark)
        else:
            self._plot_overlay(lines, title, xlabel, mark, ylabel, mode == "independent")
        self.draw_idle()

    def _plot_overlay(self, lines, title, xlabel, mark, ylabel, indep) -> None:
        ax = self.figure.add_subplot(111)
        multi = len(lines) > 1
        scales = []
        for line in lines:
            ys = np.asarray(line["y"], float)
            m = float(np.nanmax(np.abs(ys))) if np.isfinite(ys).any() else 0.0
            scales.append((m or 1.0) if indep else 1.0)
        for i, line in enumerate(lines):
            label = str(line.get("label") or "")
            if indep and label:
                label += f"  ·  max {scales[i]:,.4g}"
            ax.plot(line["x"], np.asarray(line["y"], float) / scales[i],
                    label=label or None, drawstyle="steps-post",
                    linewidth=line.get("width", 1.4),
                    color=line.get("color") if multi else (line.get("color") or "tab:blue"))
        if mark is not None:
            sc = scales[mark.get("line", 0)]
            ax.plot([mark["x"]], [mark["y"] / sc], "o", color="tab:red",
                    markersize=8, zorder=5)
            ax.annotate(mark.get("text") or f"{mark['y']:,.6g}",
                        (mark["x"], mark["y"] / sc), textcoords="offset points",
                        xytext=(8, 8), fontsize=9, color="tab:red")
        ax.axhline(0, color="grey", linewidth=0.6)
        if indep:
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v * 100:.0f}%"))
            ax.set_ylabel("% of each line's max |y|", fontsize=8)
        elif ylabel:
            ax.set_ylabel(ylabel)
        if xlabel:
            ax.set_xlabel(xlabel)
        ax.set_title(title, fontsize=10)
        if multi and any(line.get("label") for line in lines):
            leg = ax.legend(fontsize=8)
            leg.get_frame().set_alpha(0.8)

    def _plot_multiples(self, lines, title, xlabel, mark) -> None:
        n = len(lines)
        axes = self.figure.subplots(n, 1, sharex=True, squeeze=False)[:, 0]
        self.figure.suptitle(title, fontsize=10)
        cycle = matplotlib.rcParams["axes.prop_cycle"].by_key()["color"]
        for i, (ax, line) in enumerate(zip(axes, lines)):
            col = line.get("color") or cycle[i % len(cycle)]
            ys = np.asarray(line["y"], float)
            ax.plot(line["x"], ys, drawstyle="steps-post", color=col,
                    linewidth=line.get("width", 1.4))
            finite = ys[np.isfinite(ys)]
            if finite.size:
                y0, y1 = min(0.0, float(finite.min())), max(0.0, float(finite.max()))
                if y1 == y0:
                    y1 = y0 + 1.0
                ax.set_ylim(y0 - 0.04 * (y1 - y0), y1 + 0.04 * (y1 - y0))
                ax.set_yticks([y0, y1] if y0 != y1 else [y0])
                if y0 < 0 < y1:
                    ax.axhline(0, color="grey", linewidth=0.6)
            ax.tick_params(labelsize=7)
            ax.set_facecolor("#fafbfc")
            if line.get("label"):
                ax.text(0.99, 0.9, str(line["label"]), transform=ax.transAxes,
                        ha="right", va="top", fontsize=8, fontweight="bold",
                        color=col, bbox=dict(facecolor="white", alpha=0.75,
                                             edgecolor="none", pad=1.5))
            if mark is not None and mark.get("line", 0) == i:
                ax.plot([mark["x"]], [mark["y"]], "o", color="tab:red",
                        markersize=6, zorder=5)
        if xlabel:
            axes[-1].set_xlabel(xlabel)
        self.figure.subplots_adjust(hspace=0.12)


def frame_lines(frame: pd.DataFrame, max_cols: int = 8) -> list[dict]:
    """Numeric columns of a frame as chart lines; a ``net`` column is
    emphasised (black, thicker) like on the web."""
    numeric = frame.select_dtypes(include=[np.number]).iloc[:, :max_cols]
    x = np.arange(len(numeric))
    out = []
    for col in numeric.columns:
        is_net = "net" in str(col).lower()
        out.append({"label": str(col), "x": x, "y": numeric[col].to_numpy(float),
                    "width": 2.5 if is_net else 1.2,
                    "color": "black" if is_net else None})
    return out


class CashflowChart(LinesChart):
    def plot(self, cf: pd.DataFrame, title: str) -> None:
        self.plot_lines(frame_lines(cf), title, xlabel="projection step (t)",
                        ylabel="amount")


class PVChart(ChartPanel):
    def plot(self, pv: pd.DataFrame, title: str) -> None:
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        row = pv.iloc[0] if len(pv) else pd.Series(dtype=float)
        # For *_S models result_pv has rows ("PV", "% Premium"); take "PV" row.
        if "PV" in pv.index:
            row = pv.loc["PV"]
        vals = pd.to_numeric(row, errors="coerce").dropna()
        colors = ["tab:red" if v < 0 else "tab:blue" for v in vals]
        ax.barh([str(i) for i in vals.index], vals.values, color=colors)
        for i, v in enumerate(vals.values):
            ax.text(v, i, f" {v:,.0f}", va="center", fontsize=8)
        ax.axvline(0, color="grey", linewidth=0.6)
        ax.set_title(title)
        self.draw_idle()


class PolicyChart(ChartPanel):
    def plot(self, series: dict[str, list[float]], title: str) -> None:
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        for name, ys in series.items():
            ax.plot(np.arange(len(ys)), ys, label=name, linewidth=1.4,
                    drawstyle="steps-post")
        ax.set_xlabel("projection step (t)")
        ax.set_ylabel("policy count")
        ax.set_title(title)
        ax.legend(fontsize=8)
        self.draw_idle()
