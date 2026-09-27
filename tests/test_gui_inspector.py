import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from lifelib_explorer.main import MainWindow, find_lifelib_root

app = QApplication([])
win = MainWindow(find_lifelib_root())
win.show()

def fail(msg):
    print("FAIL:", msg); app.exit(1)

def step_load():
    idx = next(i for i in range(win.model_combo.count()) if getattr(win.model_combo.itemData(i), "name", None) == "BasicTerm_S")
    win.model_combo.setCurrentIndex(idx)
    win._on_load_clicked()

def step_after_load():
    if win._loaded is None:
        QTimer.singleShot(400, step_after_load); return
    insp = win.inspector
    assert win.tabs.currentWidget() is win.formulas, "Formulas is the landing tab"
    win.tabs.setCurrentWidget(insp)               # this test drives the Inspector
    print("index size:", len(insp._index))
    # search
    insp.search.setText("pols_if")
    print("results:", insp.results.count())
    assert insp.results.count() >= 3
    # wait for the initial compute to finish before tracing
    QTimer.singleShot(2500, step_inspect)

def step_inspect():
    insp = win.inspector
    insp.navigate("Projection", "result_cf", ())
    QTimer.singleShot(800, step_check_node)

def step_check_node():
    insp = win.inspector
    print("header:", insp.header.text()[:60])
    cards = [ax.patch for ax in insp.node_graph.figure.axes
             if hasattr(ax.patch, "_node_data")]
    print("graph cards:", len(cards))
    assert len(cards) >= 5
    # navigate into a precedent group (premiums cell) via card click
    target = next(p for p in cards if p._node_data[1] == "premiums")
    class E: artist = target
    insp.node_graph._on_pick(E)
    QTimer.singleShot(800, step_check_cell)

def step_check_cell():
    insp = win.inspector
    print("now at:", insp.header.text()[:60])
    print("cached invocations:", insp.cached_combo.count())
    assert insp.cached_combo.count() == 121
    # pick a cached invocation -> node view
    insp.cached_combo.setCurrentIndex(5)
    insp._on_cached_pick(5)
    QTimer.singleShot(800, step_check_history)

def step_check_history():
    insp = win.inspector
    print("value:", insp.value_view.toPlainText()[:40])
    print("history entries:", [v.label for v in insp._history.entries])
    # auto result_cf visit at load + test's 3 navigations
    assert len(insp._history.entries) == 4
    insp.go_back()
    insp.go_back()
    QTimer.singleShot(800, step_check_back)

def step_check_back():
    insp = win.inspector
    print("after back x2:", insp.header.text()[:60])
    assert "result_cf" in insp.header.text()
    assert insp.fwd_btn.isEnabled()
    QTimer.singleShot(200, step_formula_edit)

# ---------------------------------------------------------------- formulas
def _net_pv():
    return win._last_pv_row

def step_formula_edit():
    insp = win.inspector
    F["base"] = _net_pv()
    insp.navigate("Projection", "premium_pp", None)
    QTimer.singleShot(800, step_formula_begin)

def step_formula_begin():
    insp = win.inspector
    assert insp.edit_btn.isVisible() and not insp.edited_badge.isVisible(), "Edit should be offered"
    insp._begin_edit()
    assert insp._editing and not insp.source_view.isReadOnly() and insp.apply_btn.isVisible()
    # a broken edit is rejected inline, editor stays open
    insp.source_view.setPlainText("def premium_pp(:\n    return 1")
    insp._apply_formula()
    QTimer.singleShot(600, step_formula_bad)

def step_formula_bad():
    insp = win.inspector
    print("formula error:", insp.formula_error.text()[:70])
    assert insp.formula_error.isVisible() and "SyntaxError" in insp.formula_error.text()
    assert insp._editing and insp.apply_btn.isEnabled()
    src = win.inspector._payload["source"]
    insp.source_view.setPlainText(src.replace("return round(", "return 2 * round("))
    insp._apply_formula()
    QTimer.singleShot(2500, step_formula_applied)

def step_formula_applied():
    insp = win.inspector
    print("edited badge:", insp.edited_badge.isVisible(), "| revert:", insp.revert_btn.isVisible(),
          "| export label:", win.export_model_btn.text())
    assert insp.edited_badge.isVisible() and insp.revert_btn.isVisible() and not insp._editing
    assert "2 * round" in insp.source_view.toPlainText()
    assert win.export_model_btn.text().endswith("●") and insp.edited_count() == 1
    insp.search.setText("premium_pp")          # the results list marks the edited cell
    assert any(insp.results.item(i).text().split("\n")[0].endswith("●")
               for i in range(insp.results.count()) if "premium_pp" in insp.results.item(i).text())
    # the recompute after the edit changed the headline PV
    win._request_compute()
    QTimer.singleShot(2000, step_formula_pv)

def step_formula_pv():
    cur = win._last_pv_row
    print("net pv base -> edited:", F["base"], "->", cur)
    assert cur is not None and abs(cur - F["base"]) > 1
    F["edited"] = cur
    # export the edited model, then revert
    import tempfile
    F["zip"] = os.path.join(tempfile.mkdtemp(), "BasicTerm_S-edited.zip")
    win._bridge.export_model.emit(F["zip"])
    QTimer.singleShot(3000, step_formula_exported)

def step_formula_exported():
    size = os.path.getsize(F["zip"])
    print("exported:", F["zip"], size // 1024, "KB")
    assert size > 100_000
    win.inspector._revert_formula()
    QTimer.singleShot(2500, step_formula_reverted)

def step_formula_reverted():
    insp = win.inspector
    assert not insp.edited_badge.isVisible() and insp.edited_count() == 0
    assert "2 * round" not in insp.source_view.toPlainText()
    assert win.export_model_btn.text() == "Export model…"
    win._request_compute()
    QTimer.singleShot(2000, step_formula_restored)

def step_formula_restored():
    print("net pv after revert:", win._last_pv_row)
    assert abs(win._last_pv_row - F["base"]) < 1e-6
    # load the exported zip as a user model
    win._add_user_model("MyTerm", F["zip"])
    win._load_path(F["zip"], "MyTerm")
    QTimer.singleShot(4000, step_user_model)

def step_user_model():
    assert win._loaded is not None and win._loaded.name == "BasicTerm_S-edited", win._loaded.name
    print("user model loaded:", win._loaded.name, "| picker:", win.model_combo.currentText().strip())
    assert win.model_combo.currentText().strip() == "user / MyTerm"
    win._request_compute()
    QTimer.singleShot(2500, step_user_model_pv)

def step_user_model_pv():
    print("net pv of loaded export:", win._last_pv_row, "(edited was", F["edited"], ")")
    assert abs(win._last_pv_row - F["edited"]) < 1e-6
    assert win.inspector.edited_count() == 0      # the edit is now the model's original
    QTimer.singleShot(200, step_formulas_tab)

# ------------------------------------------------------------ formulas tab
def step_formulas_tab():
    fx = win.formulas
    assert win.tabs.widget(0) is fx, "Formulas is the first tab"
    fx.values_box.setChecked(False)               # persisted (QSettings) — make the static checks deterministic
    assert len(fx._cards) == len(win._loaded.cells_index) >= 40, len(fx._cards)
    assert fx._edges and all(a in fx._cards and b in fx._cards for a, b in fx._edges)
    # follows the Inspector: result_cf was the default visit -> selected, its reads violet
    win.inspector.navigate("Projection", "result_cf", ())
    assert fx.selected_key() == "Projection.result_cf"
    roles = {k: c._role for k, c in fx._cards.items()}
    assert roles["Projection.result_cf"] == "sel" and roles["Projection.claims"] == "prec" and roles["Projection.age"] == "dim", roles
    # select pols_if: reads + read by + mutual recursion split
    fx.select("Projection", "pols_if")
    roles = {k: c._role for k, c in fx._cards.items()}
    assert roles["Projection.pols_if"] == "sel" and roles["Projection.policy_term"] == "prec"
    assert roles["Projection.pv_pols_if"] == "dep" and roles["Projection.pols_lapse"] == "both", roles
    assert fx.source.toPlainText().startswith("def pols_if(") and "pols_lapse" in fx.deps.text()
    assert fx.inspect_btn.isEnabled() and fx.inspect_btn.text() == "Open in Inspector ↗"
    # selection history (◀ ▶ / dropdown): [result_cf, pols_if] since the zip load; replaying never pushes
    assert [v.name for v in fx._history.entries] == ["result_cf", "pols_if"], fx._history.entries
    assert fx.back_btn.isEnabled() and not fx.fwd_btn.isEnabled()
    assert fx.history_combo.itemText(0).startswith("▸ ") and fx.history_combo.itemText(0).endswith("Projection.pols_if")
    fx.go_back()
    assert fx.selected_key() == "Projection.result_cf" and fx.fwd_btn.isEnabled() and not fx.back_btn.isEnabled()
    fx._on_history_pick(0)                          # dropdown: newest entry = pols_if
    assert fx.selected_key() == "Projection.pols_if" and len(fx._history.entries) == 2
    # Alt+Left/Right drive the open tab's history: the Formulas selection here, the Inspector's elsewhere
    win.tabs.setCurrentWidget(fx); insp_pos = win.inspector._history.pos
    win._history_back()
    assert fx.selected_key() == "Projection.result_cf" and win.inspector._history.pos == insp_pos
    win._history_forward()
    assert fx.selected_key() == "Projection.pols_if" and win.inspector._history.pos == insp_pos
    # show values: cards get sparklines/values at t; t follows the Inspector's invocation
    win.tabs.setCurrentWidget(fx)
    fx.values_box.setChecked(True)
    QTimer.singleShot(2500, step_formulas_values)

def step_formulas_values():
    fx = win.formulas
    assert fx._values and "Projection.pols_if" in fx._values and fx._values["Projection.pols_if"]["series"], "values not loaded"
    assert fx.t_spin.maximum() == 120 and fx.t_spin.isVisible(), (fx.t_spin.minimum(), fx.t_spin.maximum())
    fx.set_t(12)
    card = fx._cards["Projection.pols_if"]
    assert card.spark.isVisible() and "t=12" in card.cap.text() and "0.8994" in card.cap.text(), card.cap.text()
    assert fx._cards["Projection.policy_term"].big.text() == "10", fx._cards["Projection.policy_term"].big.text()
    assert fx._cards["Projection.mort_table"].cap.text() == "table"
    assert "0.899407" in fx.header.text() and fx.inspect_btn.text() == "Open pols_if(12) in Inspector ↗", fx.inspect_btn.text()
    # inspector navigation moves t; a recompute (refresh) must not
    win.inspector.navigate("Projection", "pols_if", (30,))
    assert fx._t == 30 and fx.selected_key() == "Projection.pols_if"
    fx.set_t(5); win.inspector.refresh_current()
    assert fx._t == 5
    # Open in Inspector traces (t,) — the button carries the invocation
    fx._open_selected()
    QTimer.singleShot(800, step_formulas_open)

def step_formulas_open():
    print("open in inspector ->", win.inspector.header.text()[:40], "| tab:", win.tabs.currentWidget() is win.inspector)
    assert win.tabs.currentWidget() is win.inspector and "pols_if(5)" in win.inspector.header.text()
    # values off -> doc lines back
    win.formulas.values_box.setChecked(False)
    card = win.formulas._cards["Projection.pols_if"]
    assert card.doc.isVisibleTo(win.formulas) and not card.spark.isVisibleTo(win.formulas)
    print("INSPECTOR GUI TEST OK")
    win.close(); app.exit(0)

F = {}

win._worker.error.connect(lambda e: fail(e))
QTimer.singleShot(100, step_load)
QTimer.singleShot(1200, step_after_load)
QTimer.singleShot(90000, lambda: fail("timeout"))
raise SystemExit(app.exec())
