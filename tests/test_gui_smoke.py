import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"


from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from lifelib_explorer.main import MainWindow, find_lifelib_root
from lifelib_explorer.widgets import y_mode

y_mode.set("shared")          # persisted setting: make the run deterministic

app = QApplication([])
root = find_lifelib_root()
print("root:", root)
win = MainWindow(root)
win.show()

steps = []

def step_select_and_load():
    idx = next(i for i in range(win.model_combo.count()) if getattr(win.model_combo.itemData(i), "name", None) == "BasicTerm_S")
    assert idx >= 0
    win.model_combo.setCurrentIndex(idx)
    win._on_load_clicked()

def step_check_loaded():
    if win._loaded is None:
        QTimer.singleShot(500, step_check_loaded)
        return
    print("loaded:", win._loaded.name, "points:", win.point_combo.count())
    # switch point and edit a field
    win.point_combo.setCurrentIndex(2)
    w = win.editor._widgets["sum_assured"]
    w.spin.setValue(900000)
    QTimer.singleShot(2000, step_check_result)

results = {"n": 0}
win._worker.compute_done.connect(lambda r: results.__setitem__("n", results["n"] + 1))
win._worker.error.connect(lambda e: (print("GUI ERROR:", e), app.exit(1)))

def step_check_result():
    print("compute_done count:", results["n"])
    # charts should have axes drawn
    print("cf axes:", len(win.cf_chart.figure.axes))
    print("pv axes:", len(win.pv_chart.figure.axes))
    print("pol axes:", len(win.pol_chart.figure.axes))
    assert results["n"] >= 1
    assert len(win.cf_chart.figure.axes) == 1
    # y-axis mode: one setting drives the Cashflows tab and the Inspector value chart
    n_lines = len(win._loaded and win.cf_chart._last[0])
    y_mode.set("multiples"); app.processEvents()
    print("multiples axes:", len(win.cf_chart.figure.axes), "lines:", n_lines)
    assert len(win.cf_chart.figure.axes) == n_lines >= 3
    y_mode.set("independent"); app.processEvents()
    ax = win.cf_chart.figure.axes[0]
    assert len(win.cf_chart.figure.axes) == 1 and "max |y|" in ax.get_ylabel(), ax.get_ylabel()
    assert all(abs(y) <= 1.0 + 1e-9 for ln in ax.get_lines() if ln.get_label() and not ln.get_label().startswith("_") for y in ln.get_ydata()), "independent mode must scale each line to its own max"
    assert win.cf_chart.bar.isVisibleTo(win.cf_chart)
    y_mode.set("shared"); app.processEvents()
    assert len(win.cf_chart.figure.axes) == 1 and win.cf_chart.figure.axes[0].get_ylabel() == "amount"
    print("GUI SMOKE TEST OK")
    win.close()
    app.exit(0)

QTimer.singleShot(100, step_select_and_load)
QTimer.singleShot(1500, step_check_loaded)
raise SystemExit(app.exec())
