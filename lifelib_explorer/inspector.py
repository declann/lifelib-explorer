"""Inspector panel: search cells, view formulas/values, navigate the
dependency graph (precedents/dependents) with browser-style history."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .widgets import ChartPanel, LinesChart, frame_lines

MAX_RESULTS = 300
MAX_HISTORY = 200


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def rank_cells(index: list[dict], query: str) -> list[tuple[int, dict, str]]:
    """Rank cells against a query.

    Tokens are AND-ed; each token scores by where it matches:
    exact name > name prefix > name substring > space > doc > source.
    Returns (score, entry, context) sorted best-first.
    """
    tokens = [t for t in query.lower().split() if t]
    results = []
    for e in index:
        name = e["name"].lower()
        space = e["space"].lower()
        doc = e["doc"].lower()
        source = e["source"].lower()
        total, context = 0, ""
        ok = True
        for tok in tokens:
            if tok == name:
                total += 100
            elif name.startswith(tok):
                total += 80
            elif tok in name:
                total += 60
            elif tok in space:
                total += 40
            elif tok in doc:
                total += 25
                if not context:
                    context = _match_line(e["doc"], tok)
            elif tok in source:
                total += 15
                if not context:
                    context = _match_line(e["source"], tok)
            else:
                ok = False
                break
        if ok:
            results.append((total, e, context))
    results.sort(key=lambda r: (-r[0], r[1]["space"], r[1]["name"]))
    return results[:MAX_RESULTS]


def rank_cells_counted(index: list[dict], query: str
                       ) -> tuple[int, list[tuple[int, dict, str]]]:
    """Like :func:`rank_cells` but also returns the total number of matches
    (before truncation) so the UI can say how many were hidden."""
    ranked = rank_cells(index, query)
    if len(ranked) < MAX_RESULTS:
        return len(ranked), ranked
    tokens = [t for t in query.lower().split() if t]
    total = 0
    for e in index:
        hay = " ".join((e["name"], e["space"], e["doc"], e["source"])).lower()
        if all(t in hay for t in tokens):
            total += 1
    return total, ranked


def _match_line(text: str, tok: str) -> str:
    for line in text.splitlines():
        if tok in line.lower():
            return line.strip()[:80]
    return ""


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

@dataclass
class Visit:
    space: str
    name: str
    args: tuple | None

    @property
    def label(self) -> str:
        loc = f"{self.space}.{self.name}" if self.space else self.name
        if self.args is None:
            return loc
        return loc + "(" + ", ".join(map(repr, self.args)) + ")"


@dataclass
class History:
    entries: list[Visit] = field(default_factory=list)
    pos: int = -1

    def visit(self, v: Visit) -> None:
        # de-dup: revisiting the current entry is a no-op
        if 0 <= self.pos < len(self.entries) and self.entries[self.pos] == v:
            return
        del self.entries[self.pos + 1:]        # truncate forward branch
        self.entries.append(v)
        if len(self.entries) > MAX_HISTORY:
            del self.entries[0]
        self.pos = len(self.entries) - 1

    def back(self) -> Visit | None:
        if self.pos > 0:
            self.pos -= 1
            return self.entries[self.pos]
        return None

    def forward(self) -> Visit | None:
        if self.pos < len(self.entries) - 1:
            self.pos += 1
            return self.entries[self.pos]
        return None


# --------------------------------------------------------------------------
# Python syntax highlighting for the formula source view
# --------------------------------------------------------------------------

import keyword
import re

from PyQt6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat


class PythonHighlighter(QSyntaxHighlighter):
    """Small regex-based Python highlighter (keywords, builtins, strings incl.
    triple-quoted, comments, numbers, decorators, def/class names).

    ``cell_names`` (optional) are highlighted distinctly so references to
    other cells stand out in a formula."""

    def __init__(self, doc, cell_names: set[str] | None = None):
        super().__init__(doc)
        self.cell_names: set[str] = cell_names or set()

        def fmt(color: str, bold=False, italic=False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(700)
            if italic:
                f.setFontItalic(True)
            return f

        self.f_kw = fmt("#0f5fa8", bold=True)
        self.f_builtin = fmt("#6f42c1")
        self.f_str = fmt("#a31515")
        self.f_comment = fmt("#6a737d", italic=True)
        self.f_num = fmt("#0e7c7b")
        self.f_def = fmt("#1a466b", bold=True)
        self.f_deco = fmt("#b3660b")
        self.f_cell = fmt("#2b6a33")
        kws = "|".join(keyword.kwlist)
        builtins = ("abs|all|any|bool|dict|enumerate|float|int|isinstance|len|"
                    "list|map|max|min|print|range|round|set|str|sum|tuple|zip|"
                    "np|pd|math")
        self.rules = [
            (re.compile(rf"\b({kws})\b"), self.f_kw),
            (re.compile(rf"\b({builtins})\b"), self.f_builtin),
            (re.compile(r"\b\d+(\.\d*)?([eE][-+]?\d+)?\b"), self.f_num),
            (re.compile(r"^\s*@\w[\w.]*"), self.f_deco),
        ]
        self.re_def = re.compile(r"\b(def|class)\s+(\w+)")
        self.re_ident = re.compile(r"\b[A-Za-z_]\w*\b")
        self.re_str = re.compile(r"(\"\"\"|'''|\"|')")
        self.re_comment = re.compile(r"#.*$")

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt naming)
        for rx, f in self.rules:
            for m in rx.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), f)
        for m in self.re_def.finditer(text):
            self.setFormat(m.start(2), m.end(2) - m.start(2), self.f_def)
        if self.cell_names:
            for m in self.re_ident.finditer(text):
                if m.group(0) in self.cell_names:
                    self.setFormat(m.start(), m.end() - m.start(), self.f_cell)
        # strings (with multi-line triple-quote state) and comments
        # state: 0 none, 1 in """ , 2 in '''
        state = self.previousBlockState()
        i = 0
        n = len(text)
        while i < n:
            if state in (1, 2):
                q = '"""' if state == 1 else "'''"
                j = text.find(q, i)
                if j < 0:
                    self.setFormat(i, n - i, self.f_str)
                    self.setCurrentBlockState(state)
                    return
                self.setFormat(i, j + 3 - i, self.f_str)
                i = j + 3
                state = 0
                continue
            cm = self.re_comment.search(text, i)
            sm = self.re_str.search(text, i)
            if sm is None and cm is None:
                break
            if cm is not None and (sm is None or cm.start() < sm.start()):
                self.setFormat(cm.start(), n - cm.start(), self.f_comment)
                break
            assert sm is not None
            q = sm.group(1)
            if len(q) == 3:
                j = text.find(q, sm.end())
                if j < 0:
                    self.setFormat(sm.start(), n - sm.start(), self.f_str)
                    self.setCurrentBlockState(1 if q == '"""' else 2)
                    return
                self.setFormat(sm.start(), j + 3 - sm.start(), self.f_str)
                i = j + 3
            else:
                j = sm.end()
                while j < n and text[j] != q:
                    j += 2 if text[j] == "\\" else 1
                self.setFormat(sm.start(), min(j + 1, n) - sm.start(), self.f_str)
                i = j + 1
        self.setCurrentBlockState(0)


# --------------------------------------------------------------------------
# Visuals
# --------------------------------------------------------------------------

class ValueChart(LinesChart):
    """Chart of a cell across its cached invocations, or of a value.

    Multi-line data (a ``result_cf`` frame, a ``(t, life)`` series) goes
    through :meth:`LinesChart.plot_lines`, so the shared / independent /
    small-multiples y-mode applies here as on the Cashflows tab.
    """

    def _text_only(self):
        """Fresh single axes for text/bar displays (no y-mode bar)."""
        self._last = None
        self.bar.hide()
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.axis("off")
        return ax

    def show_message(self, msg: str) -> None:
        ax = self._text_only()
        ax.text(0.5, 0.5, msg, ha="center", va="center",
                fontsize=10, color="grey", wrap=True)
        self.draw_idle()

    def plot_series(self, series: dict, title: str,
                    current_args: tuple | None = None) -> None:
        """One line per combination of the non-x parameters (e.g. life=0/1),
        with the currently traced invocation marked on its own line."""
        lines = [{"label": line.get("label"), "x": line["x"], "y": line["y"],
                  "width": 1.6} for line in series.get("lines", [])]
        mark = None
        if current_args and len(current_args) >= 1 \
                and isinstance(current_args[0], (int, float)) \
                and not isinstance(current_args[0], bool):
            key = tuple(current_args[1:])
            li = next((i for i, l in enumerate(series.get("lines", []))
                       if tuple(l["key"]) == key), None)
            if li is not None:
                line = series["lines"][li]
                try:
                    i = line["x"].index(float(current_args[0]))
                    mark = {"x": line["x"][i], "y": line["y"][i], "line": li}
                except ValueError:
                    pass
        self.plot_lines(lines, title, xlabel=series.get("param", "x"), mark=mark)

    def show_scalar(self, value_text: str, subtitle: str = "") -> None:
        """Large value display for invocations that don't depend on t."""
        ax = self._text_only()
        size = 26 if len(value_text) <= 24 else 14
        ax.text(0.5, 0.55, value_text, ha="center", va="center",
                fontsize=size, fontweight="bold", family="monospace",
                wrap=True)
        if subtitle:
            ax.text(0.5, 0.2, subtitle, ha="center", va="center",
                    fontsize=9, color="grey")
        self.draw_idle()

    def plot_value(self, data, title: str) -> None:
        """Plot a DataFrame/Series value of a single invocation."""
        if isinstance(data, pd.Series):
            data = data.to_frame()
        numeric = data.select_dtypes(include=[np.number])
        if numeric.empty:
            self.show_message("value has no numeric columns to chart")
            return
        if len(numeric) == 1:
            ax = self._text_only()
            ax.axis("on")
            row = numeric.iloc[0]
            colors = ["tab:red" if v < 0 else "tab:blue" for v in row]
            ax.barh([str(i) for i in row.index], row.values, color=colors)
            ax.axvline(0, color="grey", linewidth=0.5)
            ax.set_title(title, fontsize=10)
            self.draw_idle()
            return
        self.plot_lines(frame_lines(numeric), title)


class NodeGraph(ChartPanel):
    """Clickable ego-graph of sparkline cards:
    precedents -> current node -> dependents.

    Each card shows the cell name, its value (or group size), a step
    sparkline over the cell's cached invocations, and red dots marking the
    invocation(s) actually consumed by / consuming the current node.
    """

    node_clicked = pyqtSignal(str, str, object)   # space, name, args | None
    MAX_SIDE = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        # cards are placed with explicit figure coordinates
        self.figure.set_layout_engine(None)
        self.mpl_connect("pick_event", self._on_pick)
        self.mpl_connect("motion_notify_event", self._on_motion)
        self.setMouseTracking(True)
        self._hover_ax = None

    def _on_motion(self, event) -> None:
        """Hover: pointer cursor + tooltip with the card's full label."""
        ax = event.inaxes
        if ax is self._hover_ax:
            return
        self._hover_ax = ax
        tip = getattr(ax, "_card_tip", None) if ax is not None else None
        navigable = ax is not None and hasattr(ax.patch, "_node_data")
        self.setCursor(Qt.CursorShape.PointingHandCursor if navigable
                       else Qt.CursorShape.ArrowCursor)
        self.setToolTip(tip or "")

    def show_message(self, msg: str) -> None:
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.axis("off")
        ax.text(0.5, 0.5, msg, ha="center", va="center",
                fontsize=10, color="grey", wrap=True)
        self.draw_idle()

    # -- cards ---------------------------------------------------------------

    def _card(self, rect, title, value, series, marks,
              navigable, node_data=None, current=False, frame=None,
              is_ref=False, side="pred", param="t", consumed_sum=None):
        ax = self.figure.add_axes(rect)
        ax.set_xticks([])
        ax.set_yticks([])
        if current:
            fc, ec = "#fff3d6", "#e0b74c"
        elif is_ref:
            fc, ec = "#eaf6ec", "#a8d3ae"   # data/table reference
        elif navigable:
            fc, ec = "#eef4fc", "#b8cbe8"
        else:
            fc, ec = "#f2f2f2", "#d9d9d9"
        ax.set_facecolor(fc)
        for s in ax.spines.values():
            s.set_color(ec)
            s.set_linewidth(1.4 if current else 1.0)
        full_title = title
        if len(title) > 30:
            title = title[:29] + "…"
        title_color = "#2b6a33" if is_ref else (
            "#1a466b" if navigable or current else "dimgrey")
        ax.text(0.04, 0.94, title, transform=ax.transAxes, va="top",
                fontsize=8.5 if not current else 9.5,
                fontweight="bold" if current else "normal",
                fontstyle="italic" if is_ref else "normal",
                color=title_color)
        if series is not None and not series.get("lines"):
            series = None
        if series is not None:
            frame = None
        # caption: what the red marker means, in words ("t=11: 0.9074",
        # "Σ 5,814.7 · t 0…120"); replaces the bare corner value
        caption = ""
        mk = [m for m in (marks or ())]
        if mk and series is not None:
            if len(mk) > 3:
                xs_ = [m[0] for m in mk]
                caption = (f"Σ {consumed_sum:,.5g}" if consumed_sum is not None
                           else f"×{len(mk)}") + f" · {param} {min(xs_):g}…{max(xs_):g}"
            else:
                parts = []
                for m in mk:
                    key = tuple(m[1:])
                    line = next((l for l in series["lines"]
                                 if tuple(l["key"]) == key), series["lines"][0])
                    xs_ = np.asarray(line["x"], float)
                    ys_ = np.asarray(line["y"], float)
                    i = min(int(np.searchsorted(xs_, m[0])), len(xs_) - 1)
                    v = ys_[i]
                    parts.append(f"{param}={m[0]:g}"
                                 + (f": {v:,.5g}" if np.isfinite(v) else ""))
                caption = "  ".join(parts)
        elif value and (series is not None or frame is not None):
            caption = str(value)[:18]
        if caption:
            ax.text(0.04, 0.03, caption, transform=ax.transAxes,
                    va="bottom", ha="left", fontsize=7.5, family="monospace",
                    color="#333333" if current else "#7a1f1f",
                    fontweight="bold" if current else "normal")
        if series is not None:
            # sparkline(s) normalized jointly into the lower part of the card.
            # Zero-baselined when the data is one-signed, so magnitude shows
            # (a min…max fit makes any declining cashflow look like ~0).
            lines = series["lines"]
            allx = np.concatenate([np.asarray(l["x"], float) for l in lines])
            ally = np.concatenate([np.asarray(l["y"], float) for l in lines])
            x0, x1 = allx.min(), allx.max()
            y0, y1 = ally.min(), ally.max()
            if y0 > 0:
                y0 = 0.0
            elif y1 < 0:
                y1 = 0.0

            def nx(v):
                frac = (np.asarray(v, float) - x0) / (x1 - x0) if x1 > x0 \
                    else np.full_like(np.asarray(v, float), 0.5)
                return 0.04 + 0.92 * frac

            def ny(v):
                frac = (np.asarray(v, float) - y0) / (y1 - y0) if y1 > y0 \
                    else np.full_like(np.asarray(v, float), 0.5)
                return 0.22 + 0.46 * frac

            marks = [m for m in (marks or ()) if x1 >= m[0] >= x0]
            if len(marks) > 3:
                # many invocations consumed: a light band behind the span
                firsts = [m[0] for m in marks]
                ax.axvspan(float(nx(min(firsts))), float(nx(max(firsts))),
                           ymin=0.18, ymax=0.72, transform=ax.transAxes,
                           color="tab:red", alpha=0.10, zorder=1, lw=0)
            if y0 <= 0 <= y1:
                ax.axhline(float(ny(0.0)), xmin=0.04, xmax=0.96,
                           color="#999999", linewidth=0.5, zorder=1)
            multi = len(lines) > 1
            palette = ("#3a76b0", "#4da06a", "#b0763a", "#9a5aa8",
                       "#a83a3a", "#3aa8a0", "#77772e", "#666666")
            for j, line in enumerate(lines):
                xs = np.asarray(line["x"], float)
                ys = np.asarray(line["y"], float)
                ax.plot(nx(xs), ny(ys), transform=ax.transAxes,
                        drawstyle="steps-post", linewidth=1.0,
                        color=palette[j % len(palette)] if multi else "#3a76b0",
                        zorder=2)
            if len(marks) <= 3:
                for m in marks:
                    key = tuple(m[1:])
                    line = next((l for l in lines if tuple(l["key"]) == key),
                                lines[0])
                    xs = np.asarray(line["x"], float)
                    ys = np.asarray(line["y"], float)
                    i = min(int(np.searchsorted(xs, m[0])), len(xs) - 1)
                    ax.axvline(float(nx(xs[i])), ymin=0.18, ymax=0.72,
                               color="tab:red", linewidth=0.8, alpha=0.7,
                               zorder=2)
                    ax.plot([nx(xs[i])], [ny(ys[i])], "o",
                            transform=ax.transAxes, markersize=4.5,
                            color="tab:red", markeredgecolor="white",
                            markeredgewidth=0.8, zorder=3)
        elif frame is not None:
            # mini multi-line chart of a tabular value (e.g. result_cf)
            vals = frame.to_numpy(dtype=float)
            v0, v1 = np.nanmin(vals), np.nanmax(vals)
            span = (v1 - v0) or 1.0
            X = 0.04 + 0.92 * np.linspace(0, 1, len(frame))
            for j in range(vals.shape[1]):
                Y = 0.22 + 0.46 * (vals[:, j] - v0) / span
                ax.plot(X, Y, transform=ax.transAxes,
                        drawstyle="steps-post", linewidth=0.9, alpha=0.85)
        elif value:
            # not t-dependent: show the value itself, front and center
            text = str(value)
            size = 13 if len(text) <= 12 else (10 if len(text) <= 22 else 8)
            ax.text(0.5, 0.34, text[:40], transform=ax.transAxes,
                    ha="center", va="center", fontsize=size,
                    fontweight="bold", family="monospace", color="#333333")
        if navigable:
            ax.patch.set_picker(True)
            ax.patch._node_data = node_data
        # full-detail tooltip (titles/values are truncated on the card)
        tip_lines = [full_title or title]
        if value:
            tip_lines.append(f"value: {value}")
        if series is not None and len(series.get("lines", [])) > 1:
            tip_lines.append(
                f"{len(series['lines'])} lines: "
                + ", ".join(str(l.get("label")) for l in series["lines"]))
        if marks:
            where = ("the invocation you are viewing" if current else
                     "the invocation(s) of this cell the current node read"
                     if side == "pred" else
                     "the invocation(s) of this cell that read the current node")
            tip_lines.append(f"{where}: {caption}")
            if len(marks) > 3:
                tip_lines.append(
                    "shaded span = the range of invocations consumed; Σ = the "
                    "sum of those values (the amount that flowed in, when the "
                    "values are additive)")
        if is_ref:
            tip_lines.append("data reference (table)")
        if navigable and not current:
            tip_lines.append("click to navigate")
        ax._card_tip = "\n".join(tip_lines)
        return ax

    def plot(self, current_label: str, preds: list[dict], succs: list[dict],
             current_series: dict | None = None,
             current_marks: list | None = None,
             current_frame=None, current_value: str = "") -> None:
        self.figure.clear()
        overlay = self.figure.add_axes([0, 0, 1, 1])
        overlay.axis("off")
        overlay.set_xlim(0, 1)
        overlay.set_ylim(0, 1)
        overlay.set_zorder(0)
        overlay.text(0.5, 0.985, "precedents  →  node  →  dependents"
                     "   (click a card to navigate · hover for details)",
                     ha="center", va="top", fontsize=8.5, color="grey")
        # legend (bottom centre, away from the "+N more" notes at the sides)
        overlay.text(0.5, 0.012,
                     "red marker = the invocation involved — left: what this node "
                     "read (e.g. t−1)\nright: what read this node · centre: the "
                     "one you're viewing\nshaded span = many invocations, Σ = "
                     "their sum · green = table",
                     ha="center", va="bottom", fontsize=7.5, color="#888888",
                     linespacing=1.3)

        def column(groups, x0, side):
            shown = groups[:self.MAX_SIDE]
            n = len(shown)
            ys = []
            if not n:
                return ys
            # cards occupy [0.11, 0.93] (legend below); keep a real gap so
            # sparklines never spill into the next card
            avail = 0.79
            h = min(0.17, avail / n * 0.84)
            gap = (avail - n * h) / (n + 1)
            for i, g in enumerate(shown):
                top = 0.93 - gap * (i + 1) - h * i
                rect = [x0, top - h, 0.27, h]
                args = (tuple(g["args_list"][0])
                        if len(g["args_list"]) == 1 else None)
                n_inv = len(g["args_list"])
                if n_inv <= 1:
                    value = g["value_repr"]
                elif g.get("consumed_sum") is not None:
                    value = f"Σ {g['consumed_sum']:,.6g}"
                else:
                    value = f"×{n_inv}"
                title = f"{g['space']}.{g['name']}" if g["space"] else g["name"]
                ser = g.get("series") or {}
                self._card(rect, title, value, g.get("series"),
                           g.get("marks"), g["navigable"],
                           (g["space"], g["name"], args),
                           frame=g.get("frame"),
                           is_ref=g.get("kind") == "ref", side=side,
                           param=ser.get("param") or "t",
                           consumed_sum=g.get("consumed_sum"))
                ys.append(top - h / 2)
            if len(groups) > self.MAX_SIDE:
                overlay.text(x0 + 0.135, 0.115,
                             f"… +{len(groups) - self.MAX_SIDE} more "
                             "(search to reach them)",
                             ha="center", va="bottom", fontsize=7.5,
                             color="grey")
            return ys

        left_ys = column(preds, 0.01, "pred")
        right_ys = column(succs, 0.72, "succ")

        # current node card in the middle
        cur_marks = list(current_marks or [])
        self._card([0.345, 0.36, 0.31, 0.26], current_label, current_value,
                   current_series, cur_marks, navigable=False, current=True,
                   frame=current_frame,
                   param=(current_series or {}).get("param") or "t")

        for y in left_ys:
            overlay.annotate("", xy=(0.34, 0.49), xytext=(0.285, y),
                             arrowprops=dict(arrowstyle="->", color="#b8b8b8",
                                             lw=0.8, shrinkA=2, shrinkB=2))
        for y in right_ys:
            overlay.annotate("", xy=(0.715, y), xytext=(0.66, 0.49),
                             arrowprops=dict(arrowstyle="->", color="#b8b8b8",
                                             lw=0.8, shrinkA=2, shrinkB=2))
        self.draw_idle()

    def _on_pick(self, event) -> None:
        data = getattr(event.artist, "_node_data", None)
        if data:
            self.node_clicked.emit(*data)


# --------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------

class InspectorPanel(QWidget):
    """Search + detail + graph navigation + history."""

    #: (space_path, cell_name, args tuple or None) -> engine thread
    request_inspect = pyqtSignal(str, str, object)
    #: same, but only for deliberate navigation (not refresh_current) — the
    #: Formulas tab follows these (selection + t)
    visited = pyqtSignal(str, str, object)
    #: (space_path, cell_name, new source) -> engine thread
    request_set_formula = pyqtSignal(str, str, str)
    #: (space_path, cell_name) -> engine thread
    request_reset_formula = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._index: list[dict] = []
        self._history = History()
        self._navigating = False       # True while replaying history
        self._current: Visit | None = None
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)

        # -- top bar: history + search --------------------------------------
        self.back_btn = QPushButton("◀")
        self.back_btn.setFixedWidth(32)
        self.back_btn.setToolTip("Back (Alt+Left)")
        self.back_btn.clicked.connect(self.go_back)
        self.fwd_btn = QPushButton("▶")
        self.fwd_btn.setFixedWidth(32)
        self.fwd_btn.setToolTip("Forward (Alt+Right)")
        self.fwd_btn.clicked.connect(self.go_forward)

        # History: newest at the top, the current visit marked with ▸, and a
        # placeholder row so the combo never renders blank.
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(220)
        self.history_combo.setToolTip(
            "Navigation history (newest first) — pick an entry to jump to it")
        self.history_combo.activated.connect(self._on_history_pick)
        self._sync_history_combo()

        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(
            "Search cells & tables — name, docstring, formula  (Ctrl+K · ↓ to results)")
        self.search.textChanged.connect(self._refresh_results)
        self.search.returnPressed.connect(self._open_selected_or_top)
        self.search.installEventFilter(self)

        top = QHBoxLayout()
        top.addWidget(self.back_btn)
        top.addWidget(self.fwd_btn)
        top.addWidget(self.history_combo, stretch=1)
        top.addWidget(self.search, stretch=2)

        # -- left: search results -------------------------------------------
        self.results = QListWidget()
        self.results.setToolTip("Enter / double-click to open")
        self.results.itemActivated.connect(self._on_result_activated)
        self.results.itemClicked.connect(self._on_result_activated)

        # -- right: detail ----------------------------------------------------
        self.header = QLabel("Loading a model …")
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.header.setWordWrap(True)

        # Invocation selector: one combo per parameter (t, life, kind, …),
        # each listing the cached values of that parameter. Combos are built
        # per payload in _build_param_combos(); the full-tuple combo is kept
        # for single-parameter cells and for "not cached" display.
        self.cached_combo = QComboBox()
        self.cached_combo.setToolTip(
            "Invocations of this cell cached by the last computation")
        self.cached_combo.activated.connect(self._on_cached_pick)
        self.param_host = QWidget()
        self.param_row = QHBoxLayout(self.param_host)
        self.param_row.setContentsMargins(0, 0, 0, 0)
        self._param_combos: list[QComboBox] = []
        self.args_edit = QLineEdit()
        self.args_edit.setClearButtonEnabled(True)
        self.args_edit.setPlaceholderText("or type args, e.g. 12 — Enter")
        self.args_edit.setToolTip(
            "Trace a specific invocation by typing its arguments "
            "(Python literals, comma-separated)")
        self.args_edit.returnPressed.connect(self._on_trace_args)
        cache_row = QHBoxLayout()
        cache_row.addWidget(QLabel("Invocation:"))
        cache_row.addWidget(self.cached_combo, stretch=2)
        cache_row.addWidget(self.param_host, stretch=2)
        cache_row.addWidget(self.args_edit, stretch=1)

        self.value_view = QPlainTextEdit()
        self.value_view.setReadOnly(True)
        self.value_view.setFont(mono)
        self.value_view.setMaximumHeight(84)
        self.value_view.setPlaceholderText("value")

        self.source_view = QPlainTextEdit()
        self.source_view.setReadOnly(True)
        self.source_view.setFont(mono)
        self.source_view.setPlaceholderText("formula source")
        self._highlighter = PythonHighlighter(self.source_view.document())
        self.source_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.source_view.setTabStopDistance(
            4 * self.source_view.fontMetrics().horizontalAdvance(" "))

        # Formula editing: the same QPlainTextEdit flips to writable. Apply
        # sends the new source to the engine (live model only — nothing on
        # disk changes); Revert restores the original.
        self._payload: dict | None = None
        self._editing = False
        self.edited_badge = QLabel("● edited")
        self.edited_badge.setStyleSheet("QLabel { color:#b3660b; font-weight:600; }")
        self.edited_badge.setToolTip("This cell's formula was edited in this session")
        self.edited_badge.hide()
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setToolTip("Edit this cell's formula (applies to the live "
                                 "model; the model on disk is never changed)")
        self.edit_btn.clicked.connect(self._begin_edit)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setToolTip("Restore the original formula")
        self.revert_btn.clicked.connect(self._revert_formula)
        self.revert_btn.hide()
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setToolTip("Apply the edited formula (Ctrl+Enter)")
        self.apply_btn.setStyleSheet("QPushButton { font-weight:600; }")
        self.apply_btn.clicked.connect(self._apply_formula)
        self.apply_btn.hide()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Discard the edit (Esc)")
        self.cancel_btn.clicked.connect(self._end_edit)
        self.cancel_btn.hide()
        for b in (self.edit_btn, self.revert_btn, self.apply_btn, self.cancel_btn):
            b.setFixedHeight(22)
        self.formula_error = QLabel()
        self.formula_error.setWordWrap(True)
        self.formula_error.setStyleSheet(
            "QLabel { background:#fdecec; border:1px solid #e8a3a3; "
            "border-radius:3px; padding:3px 6px; color:#7a1f1f; }")
        self.formula_error.hide()
        src_bar = QHBoxLayout()
        src_bar.setContentsMargins(0, 0, 0, 0)
        src_bar.addWidget(self.edited_badge)
        src_bar.addStretch(1)
        for b in (self.edit_btn, self.revert_btn, self.apply_btn, self.cancel_btn):
            src_bar.addWidget(b)
        src_box = QWidget()
        sv = QVBoxLayout(src_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(3)
        sv.addLayout(src_bar)
        sv.addWidget(self.source_view, stretch=1)
        sv.addWidget(self.formula_error)
        QShortcut(QKeySequence("Ctrl+Return"), self.source_view, self._apply_formula)
        QShortcut(QKeySequence("Ctrl+Enter"), self.source_view, self._apply_formula)
        QShortcut(QKeySequence("Escape"), self.source_view, self._end_edit)

        # One combined view: formula source beside the visual (t-series
        # chart, table chart, or plain value), dependency graph below.
        self.value_chart = ValueChart()
        self.node_graph = NodeGraph()
        self.node_graph.node_clicked.connect(self.navigate)

        top_split = QSplitter(Qt.Orientation.Horizontal)
        top_split.addWidget(src_box)
        top_split.addWidget(self.value_chart)
        top_split.setSizes([420, 420])

        body_split = QSplitter(Qt.Orientation.Vertical)
        body_split.addWidget(top_split)
        body_split.addWidget(self.node_graph)
        body_split.setSizes([300, 440])   # cards need ~45 px each to carry a caption

        detail = QWidget()
        dv = QVBoxLayout(detail)
        dv.addWidget(self.header)
        dv.addLayout(cache_row)
        dv.addWidget(self.value_view)
        dv.addWidget(body_split, stretch=1)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.results)
        split.addWidget(detail)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([280, 720])

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(split, stretch=1)
        # shortcuts are registered window-wide by MainWindow so they work
        # from every tab
        self._update_nav_buttons()

    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        # ↓ from the search box moves into the results list
        if obj is self.search and event.type() == QEvent.Type.KeyPress \
                and event.key() == Qt.Key.Key_Down and self.results.count():
            self.results.setFocus()
            if self.results.currentRow() < 0:
                self.results.setCurrentRow(0)
            return True
        return super().eventFilter(obj, event)

    # -- public API -------------------------------------------------------

    def set_index(self, index: list[dict]) -> None:
        """New model loaded: reset search index and history."""
        self._index = index
        self._history = History()
        self._current = None
        self._highlighter.cell_names = {e["name"] for e in index}
        self._sync_history_combo()
        self.search.clear()
        self.args_edit.clear()
        self._clear_detail()
        n_cells = sum(1 for e in index if e["params"] != "reference")
        n_tables = len(index) - n_cells
        self.header.setText(
            f"<b>{n_cells}</b> cells and <b>{n_tables}</b> tables indexed — "
            "search above or pick from the list.")
        self._refresh_results()
        self._update_nav_buttons()

    def show_payload(self, p: dict) -> None:
        """Inspection result arriving from the engine thread."""
        if self._editing and not (self._payload
                                  and self._payload["space"] == p["space"]
                                  and self._payload["name"] == p["name"]):
            self._end_edit()           # navigated away: drop the draft
        self._payload = p
        loc = f"{p['space']}.{p['name']}" if p["space"] else p["name"]
        sig = f"{loc}({p['params']})"
        if p["args"] is not None:
            call = loc + "(" + ", ".join(map(repr, p["args"])) + ")"
            self.header.setText(f"<b>{call}</b> &nbsp; <i>{sig}</i>")
        else:
            self.header.setText(f"<b>{sig}</b>")
        self.args_edit.clear()
        self.select_entry(p["space"], p["name"])   # keep the list in sync

        doc = p["doc"]
        doc_head = doc.splitlines()[0].strip() if doc else ""
        src = (f'"""{doc}"""\n\n' if doc_head and doc_head not in (p["source"] or "") else "")
        if not self._editing:
            self.source_view.setPlainText(src + (p["source"] or ""))
        editable = p["params"] != "reference" and bool(p["source"])
        self.edit_btn.setVisible(editable and not self._editing)
        self.edited_badge.setVisible(bool(p.get("edited")))
        self.revert_btn.setVisible(bool(p.get("edited")) and not self._editing)

        if p.get("error"):
            self.value_view.setPlainText(p["error"])
        elif p["value_repr"] is not None:
            self.value_view.setPlainText(p["value_repr"])
        else:
            self.value_view.setPlainText(
                "(pick an invocation above to see its value)"
                if p["cached"] else
                "(no cached value yet — this cell was not needed by the "
                "last computation, or the computation is still running)")

        # invocation labels carry the parameter names when there are several
        # parameters, e.g. "(t=0, life=1)" in a two-life pension model
        pnames = [s.strip() for s in p["params"].split(",") if s.strip()] \
            if p["params"] and p["params"] != "reference" else []

        def fmt(a) -> str:
            if len(pnames) > 1 and len(pnames) == len(a):
                return "(" + ", ".join(
                    f"{n}={v!r}" for n, v in zip(pnames, a)) + ")"
            return "(" + ", ".join(map(repr, a)) + ")"

        self.cached_combo.blockSignals(True)
        self.cached_combo.clear()
        for a in p["cached"]:
            self.cached_combo.addItem(fmt(a), tuple(a))
        # Preselect the invocation being viewed. QComboBox.findData cannot
        # match Python tuples, so compare item data manually.
        if p["args"] is not None:
            args = tuple(p["args"])
            idx = next((i for i in range(self.cached_combo.count())
                        if self.cached_combo.itemData(i) == args), -1)
            if idx < 0 and self.cached_combo.count():
                # traced-but-uncached invocation: list it explicitly rather
                # than silently showing the first cached one
                self.cached_combo.insertItem(0, fmt(args) + "  — not cached",
                                             args)
                idx = 0
            self.cached_combo.setCurrentIndex(idx)
        else:
            self.cached_combo.setCurrentIndex(-1)
        self.cached_combo.blockSignals(False)

        # multi-parameter cells: discrete per-parameter selectors instead of
        # one long tuple list (a two-life model: [t ▾] [life ▾])
        self._build_param_combos(p, pnames)
        self._update_visuals(p, loc)

    def _build_param_combos(self, p: dict, pnames: list[str]) -> None:
        self._param_combos = []
        while self.param_row.count():
            item = self.param_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        multi = len(pnames) > 1 and p["cached"] \
            and all(len(a) == len(pnames) for a in p["cached"])
        self.param_host.setVisible(bool(multi))
        self.cached_combo.setVisible(not multi)
        if not multi:
            return
        cached = [tuple(a) for a in p["cached"]]
        cur = tuple(p["args"]) if p["args"] is not None else cached[0]
        self._cached_set = set(cached)
        for i, pname in enumerate(pnames):
            lbl = QLabel(f"{pname}=")
            combo = QComboBox()
            combo.setToolTip(f"cached values of parameter '{pname}'")
            values = sorted({a[i] for a in cached}, key=repr)
            for v in values:
                combo.addItem(repr(v), v)
            j = next((k for k in range(combo.count())
                      if combo.itemData(k) == cur[i]), 0)
            combo.setCurrentIndex(j)
            combo.activated.connect(self._on_param_pick)
            self.param_row.addWidget(lbl)
            self.param_row.addWidget(combo, stretch=1)
            self._param_combos.append(combo)

    def _on_param_pick(self, _i: int) -> None:
        if not self._current:
            return
        args = tuple(c.itemData(c.currentIndex()) for c in self._param_combos)
        if args not in getattr(self, "_cached_set", set()):
            # the chosen combination was not computed: snap to the nearest
            # cached tuple sharing the just-changed values where possible
            best = next((a for a in sorted(self._cached_set, key=repr)
                         if all(a[k] == args[k] for k in range(1, len(args)))),
                        None) or next(iter(sorted(self._cached_set, key=repr)))
            args = best
        self.navigate(self._current.space, self._current.name, args)

    def _update_visuals(self, p: dict, loc: str) -> None:
        call = loc if p["args"] is None else \
            loc + "(" + ", ".join(map(repr, p["args"])) + ")"
        # -- visual: t-series chart, table chart, or plain value -------------
        series = p.get("series")
        value_data = p.get("value_data")
        if value_data is not None:
            self.value_chart.plot_value(value_data, call)
        elif series is not None:
            args = tuple(p["args"]) if p["args"] is not None else None
            self.value_chart.plot_series(
                series, f"{loc} over cached {series.get('param', 'args')}",
                args)
        elif p.get("value_repr") is not None:
            # not t-dependent: show the value itself
            self.value_chart.show_scalar(p["value_repr"], call)
        else:
            self.value_chart.show_message(
                "no value — run a computation, or trace an invocation")
        # -- dependency graph (the navigation surface) ------------------------
        if p["preds"] or p["succs"]:
            marks = []
            if p["args"] is not None and len(p["args"]) >= 1 \
                    and isinstance(p["args"][0], (int, float)) \
                    and not isinstance(p["args"][0], bool):
                marks = [(float(p["args"][0]),) + tuple(p["args"][1:])]
            cur_frame = None
            if value_data is not None:
                df = value_data.to_frame() if isinstance(value_data, pd.Series) \
                    else value_data
                num = df.select_dtypes(include=[np.number])
                if not num.empty and len(num) >= 2:
                    cur_frame = num.iloc[:500, :8]
            cur_value = ""
            if series is None and cur_frame is None and p["value_repr"]:
                cur_value = p["value_repr"].splitlines()[0]
            self.node_graph.plot(call, p["preds"], p["succs"],
                                 current_series=series, current_marks=marks,
                                 current_frame=cur_frame,
                                 current_value=cur_value)
        else:
            self.node_graph.show_message(
                "no traced dependencies — pick a cached invocation "
                "(only nodes touched by the last computation are traceable)")

    # -- navigation --------------------------------------------------------

    # -- formula editing --------------------------------------------------

    def _begin_edit(self) -> None:
        p = self._payload
        if not p or not p.get("source"):
            return
        self._editing = True
        self.source_view.setPlainText(p["source"])
        self.source_view.setReadOnly(False)
        self.source_view.setStyleSheet("QPlainTextEdit { border: 2px solid #1a466b; }")
        self.edit_btn.hide()
        self.revert_btn.hide()
        self.apply_btn.show()
        self.cancel_btn.show()
        self.formula_error.hide()
        self.source_view.setFocus()
        cur = self.source_view.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.source_view.setTextCursor(cur)

    def _end_edit(self) -> None:
        if not self._editing:
            return
        self._editing = False
        self.source_view.setReadOnly(True)
        self.source_view.setStyleSheet("")
        self.apply_btn.hide()
        self.cancel_btn.hide()
        self.formula_error.hide()
        if self._payload:
            self.show_payload(self._payload)      # restores the read view

    def _apply_formula(self) -> None:
        if not self._editing or not self._payload:
            return
        self.apply_btn.setEnabled(False)
        self.request_set_formula.emit(
            self._payload["space"], self._payload["name"],
            self.source_view.toPlainText())

    def _revert_formula(self) -> None:
        if self._payload and self._payload.get("edited"):
            self.request_reset_formula.emit(self._payload["space"], self._payload["name"])

    def on_formula_changed(self, p: dict) -> None:
        """Engine accepted set/reset_formula: leave edit mode, mark the index."""
        self.apply_btn.setEnabled(True)
        self._editing = False
        self.source_view.setReadOnly(True)
        self.source_view.setStyleSheet("")
        self.apply_btn.hide()
        self.cancel_btn.hide()
        self.formula_error.hide()
        for e in self._index:
            if e["space"] == p["space"] and e["name"] == p["name"]:
                e["edited"] = bool(p.get("edited"))
                if p.get("source"):
                    e["source"] = p["source"]
        self._refresh_results()
        self.show_payload(p)

    def on_formula_error(self, text: str) -> None:
        """Engine rejected the edit: keep the editor open, show why inline."""
        self.apply_btn.setEnabled(True)
        last = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
        self.formula_error.setText((last[-1] if last else str(text))[:300])
        self.formula_error.show()

    def edited_count(self) -> int:
        return sum(1 for e in self._index if e.get("edited"))

    def navigate(self, space: str, name: str, args: tuple | None,
                 from_history: bool = False) -> None:
        v = Visit(space, name, args)
        self._current = v
        if not from_history:
            self._history.visit(v)
            self._sync_history_combo()
        self._update_nav_buttons()
        self.request_inspect.emit(space, name, args)
        self.visited.emit(space, name, args)

    def go_back(self) -> None:
        v = self._history.back()
        if v:
            self._current = v
            self._sync_history_combo()
            self._update_nav_buttons()
            self.request_inspect.emit(v.space, v.name, v.args)
            self.visited.emit(v.space, v.name, v.args)

    def go_forward(self) -> None:
        v = self._history.forward()
        if v:
            self._current = v
            self._sync_history_combo()
            self._update_nav_buttons()
            self.request_inspect.emit(v.space, v.name, v.args)
            self.visited.emit(v.space, v.name, v.args)

    def select_entry(self, space: str, name: str) -> None:
        """Highlight a cell in the (unfiltered) results list."""
        for i in range(self.results.count()):
            e = self.results.item(i).data(Qt.ItemDataRole.UserRole)
            if e and e["name"] == name and e["space"] == space:
                self.results.setCurrentRow(i)
                self.results.scrollToItem(self.results.item(i))
                break

    def refresh_current(self) -> None:
        """Re-query the current visit (e.g. after a recompute)."""
        if self._current:
            v = self._current
            self.request_inspect.emit(v.space, v.name, v.args)

    # -- internals ---------------------------------------------------------

    def _on_result_activated(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            e = data
            self.navigate(e["space"], e["name"], None)

    def _open_selected_or_top(self) -> None:
        """Enter in the search box opens the highlighted result, else the top."""
        item = self.results.currentItem()
        if item is None and self.results.count():
            item = self.results.item(0)
        if item is not None:
            self._on_result_activated(item)

    def _on_cached_pick(self, i: int) -> None:
        args = self.cached_combo.itemData(i)
        if args is not None and self._current:
            self.navigate(self._current.space, self._current.name, tuple(args))

    def _on_trace_args(self) -> None:
        if not self._current:
            return
        text = self.args_edit.text().strip()
        try:
            import ast
            args = () if not text else ast.literal_eval(f"({text},)")
        except Exception:
            # keep the displayed value; flag the problem on the field itself
            self.args_edit.setToolTip(
                f"Could not parse {text!r} — use Python literals, e.g. 12  or  12, 1")
            self.args_edit.setStyleSheet("QLineEdit { border: 1px solid #d33; }")
            return
        self.args_edit.setStyleSheet("")
        self.navigate(self._current.space, self._current.name, tuple(args))

    def _on_history_pick(self, i: int) -> None:
        pos = self.history_combo.itemData(i)
        if isinstance(pos, int) and 0 <= pos < len(self._history.entries):
            # jump within history (no new entry, forward branch preserved)
            self._history.pos = pos
            v = self._history.entries[pos]
            self._current = v
            self._sync_history_combo()
            self._update_nav_buttons()
            self.request_inspect.emit(v.space, v.name, v.args)
            self.visited.emit(v.space, v.name, v.args)

    def _sync_history_combo(self) -> None:
        """Newest first; current entry marked and selected; never blank."""
        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        entries = self._history.entries
        if not entries:
            self.history_combo.addItem("History — nothing visited yet", None)
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

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _clear_detail(self) -> None:
        self._editing = False
        self._payload = None
        self.source_view.setReadOnly(True)
        self.source_view.setStyleSheet("")
        for w in (self.apply_btn, self.cancel_btn, self.revert_btn,
                  self.edited_badge, self.formula_error):
            w.hide()
        self.edit_btn.hide()
        self.value_view.clear()
        self.source_view.clear()
        self.cached_combo.clear()
        self.value_chart.clear()
        self.node_graph.clear()

    def _refresh_results(self) -> None:
        query = self.search.text()
        self.results.clear()
        if not self._index:
            return
        if not query.strip():
            entries = sorted(self._index, key=lambda e: (e["space"], e["name"]))
            total = len(entries)
            ranked = [(0, e, "") for e in entries[:MAX_RESULTS]]
        else:
            total, ranked = rank_cells_counted(self._index, query)
        for _score, e, context in ranked:
            text = f"{e['space']}.{e['name']}({e['params']})"
            if e.get("edited"):
                text += "  ●"
            extra = context or (e["doc"].splitlines()[0] if e["doc"] else "")
            if extra:
                text += f"\n    {extra[:80]}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, e)
            self.results.addItem(item)
        if total > len(ranked):
            more = QListWidgetItem(
                f"… {total - len(ranked)} more — refine the search to see them")
            more.setFlags(more.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.results.addItem(more)
