# Plan: Edit / export / load

> **Status (2026-09-20): Part A shipped** — engine, web, desktop, tests.
> Deviations from the plan below are noted inline as *[done: …]*. Parts B
> and C are still open.

Scope agreed 2026-09-20: three things, phased —

- **A. Model formulas** — edit a cell's formula in the Inspector, export the
  modified model, load a user's own or modified model (web + desktop).
- **B. Model point table** — a real table editor (add / delete / edit rows),
  richer import/export in the web, reachable on mobile.
- **C. Session state** — save/restore model + point + field edits + formula
  edits + inspector history as a file or a URL.

Everything goes through `engine_core.ModelSession` (shared, no Qt) and is
exposed twice: `engine.py` (QThread) and `worker.js` (Pyodide). Keep the two
in lockstep, as today.

Verified before writing this (modelx 0.33.0, BasicTerm_S):

```python
p.premium_pp.set_formula(new_src)   # live edit; dependents invalidate
                                    # pv_net_cf 910.92 -> 1627.67
mx.write_model(m, out_dir)          # writes Projection/, _data/, *.xlsx inputs
mx.read_model(out_dir)              # round-trips the edited formula
```

So no serialization work is needed on our side — modelx already does it.

---

## A. Formula editing, model export, model load — DONE

*[done: as specified, plus]* `_restore_table()` before `write_model` (the
compute override would otherwise be exported — bit us on `CashValue_ME`),
`model.path` restored after `write_model` (Reference libs broke after export
otherwise), `zip_model_dir`/`extract_model_zip` moved into `engine_core` and
`build_models.py` now uses them. `load_bytes` accepts `model/…`, root-level
and single-folder zips. Renamed-`def` is rejected explicitly. Export label is
`Export model… ●` (count in tooltip) rather than `(n edits)` — the long form
wrapped at 330 px. Desktop `Load model…` also takes a folder's `_system.json`.

### A1. Engine (`engine_core.py`)

| Method | Behaviour |
|---|---|
| `set_formula(space_path, name, source) -> dict` | `_resolve_cells` on the **base** space (not `_last_item`), `c.set_formula(source)`. Validate first with `compile(source, "<cell>", "exec")` and check the `def` name matches `name` (modelx renames otherwise). Record `self._edits[(space_path, name)] = {"orig": old_src, "new": source}`. Clear `_last_item` (cache is invalid). Return the same payload as `inspect(space_path, name, None)` plus `"edited": True`. |
| `reset_formula(space_path, name)` | `set_formula` back to `orig`, drop the entry. |
| `formula_edits() -> list[dict]` | `[{space, name, source}]` — for session state and for the "modified" badge. |
| `export_model(fmt="zip") -> bytes` | `mx.write_model(self._model, tmp/<name>)`, then zip with the **same layout as `build_models.zip_model`** (`model/…` + sibling inputs at the root) so the web `_extract` accepts it unchanged. `zip_model` moves to `engine_core` (or a tiny shared module) so both use one implementation. |
| `load_bytes(name, data: bytes) -> dict` | Extract to a temp dir, then `load(path)`. For the desktop, `load(path)` keeps working on directories; the web already extracts in `worker.js:92`. |
| `cells_index` | add `edited: bool` per entry, and `source` is already there — the editor pre-fills from `payload["source"]`. |

Notes:

- Only cells (kind `cell`) are editable in phase 1. Refs (tables) are B's
  business; editing `ReferenceNode`s = editing the table.
- **Edits never touch `lifelib/`**: `set_formula` is in-memory; export writes
  to a temp dir. Add an assertion in `export_model` that the output path is
  not under the lifelib root (cheap insurance; the AGENTS.md rule is
  load-bearing).
- Past Libraries: `write_model` on models with `ExcelRange` iospecs writes
  the workbook back. Test at least one (`PA_UK_S` equivalent in
  `lifelib/libraries/…/nestedlife`) before claiming export works there.
- Reloading a model wipes edits. `load()` must clear `self._edits`; the
  UI should warn ("3 formula edits will be lost — export first?").

### A2. Desktop (`inspector.py`, `main.py`, `engine.py`)

- Inspector: the `source` pane becomes read-only `QPlainTextEdit` + an
  **Edit** button → editable, with **Apply (Ctrl+Enter)** / **Revert**.
  Reuse `PythonHighlighter` (already a `QSyntaxHighlighter` on a document).
  On Apply: `bridge.set_formula.emit(space, name, text)` → worker →
  `inspect_done` with the fresh payload + `_request_compute()`.
- Header shows `● edited` badge; results list marks edited cells.
- Menu / left panel: **Export model…** (`QFileDialog.getSaveFileName`,
  `*.zip`), **Load model…** (directory *or* zip; `find_lifelib_root` picker
  already exists — add "Other…" entry to the model combo).
- `engine.py`: three new slots (`set_formula`, `export_model`, `load_bytes`)
  mirroring the existing `set_model_points` pattern (`engine.py:119`).

### A3. Web (`worker.js`, `app.js`, `index.html`, `styles.css`)

- `worker.js`: Python shims `_set_formula`, `_reset_formula`,
  `_export_model` (returns `bytes` → `Uint8Array` → transferable
  `ArrayBuffer`), `_load_bytes`. New `case`s; `load_bytes` must reset
  `loadedName` and bump `latest.compute/inspect` like `load` does
  (`worker.js:150`).
- `app.js`: the `#source` `<pre>` gains an **Edit** button; editing uses a
  `<textarea>` overlay (monospace, same box) — **no CodeMirror**: it would
  break the "vendored, no CDN" rule and the hand-rolled highlighter
  (`app.js:274` `PY_KW`, `tk-cell` links) already exists for the read view.
  Apply → `send("set_formula", …)` → `showPayload` + `requestCompute(true)`.
- Model picker: add `— Load model (zip)…` option → `<input type=file
  accept=.zip>` → `send("load_bytes", {name, buf})` with the buffer
  transferred. Manifest entries for user models are added client-side only
  (`state.manifest.models.push({name, user: true})`).
- **Export model** button next to Export… (points): receives `ArrayBuffer`,
  `URL.createObjectURL(new Blob([...]))`, download `<model>-edited.zip`.
- Mobile: Edit/Apply live in the Inspector detail so they work at 390 px
  with no new panels. Export/Load model go in the ☰ Model panel with the
  existing buttons.

### A4. Tests

- `tests/test_engine_families.py`: `set_formula` on `BasicTerm_S`,
  `BasicTerm_M`, `CashValue_ME` changes `pv_net_cf`; `export_model` →
  `load_bytes` round-trip gives the same PV; `reset_formula` restores.
- `tests/test_gui_inspector.py`: Edit → Apply → header badge → compute
  updated → Revert.
- `web/validate.mjs`: same round-trip under Pyodide (pandas 3.0 / Python
  3.14) — this is where `write_model` on the WASM FS is most likely to
  surprise (openpyxl writing xlsx, temp dirs on MEMFS).
- `web/browser_test.mjs`: edit a formula via the textarea, assert the
  summary number changes, export → non-empty blob.

Effort: engine ½ day, desktop ½ day, web 1 day, tests ½ day.

---

## B. Model point table editor + import/export

Today: sliders edit **one row in memory** (`edits()` dict, never written to
the table); Generate appends synthetic rows; Import/Export are whole-table
CSV (web) or Parquet/CSV/Excel (desktop). There is no way to add one row,
delete a row, or persist the slider edit into the table.

### B1. Engine

| Method | Behaviour |
|---|---|
| `commit_edits(row_pos, edits) -> DataFrame` | Apply `_coerce`d edits into `_mpt.iloc[row_pos]`, return the table. Lets a slider tweak become permanent ("Save to point"). |
| `add_point(from_row_pos=None, pid=None) -> DataFrame` | Clone a row (or the first) with a new unique id (`max+1` for integer indexes, `f"{pid}_copy"` otherwise). |
| `delete_points(pids) -> DataFrame` | Refuse to delete the last row. |
| `set_points` | already validates schema/dtypes/unique index — keep it as the single entry point; the three above call it. |
| `points_bytes(fmt) / points_from_bytes(name, data)` | `csv` / `parquet` / `xlsx` via pandas so the web gets the same formats as the desktop. `openpyxl` is vendored; **pyarrow is not** available in Pyodide by default → parquet stays desktop-only unless the wheel is vendored (~10 MB; probably not worth it). |

### B2. Desktop

- Below the `PointEditor`: **Save to point**, **Add point**, **Delete point**
  buttons; a table view (`QTableView` on a pandas model) in a new "Points"
  tab of the right-hand tabs, with inline editing that goes through
  `set_points`. Reuse the dtype rules in `widgets.PointEditor._make_widget`
  (bool → checkbox, nullable numeric → N/A, unhashable → read-only) for the
  table delegates.

### B3. Web

- Same three buttons in the Fields sheet (`index.html:47`).
- New **Points** tab (`.tabs`): a scrollable `<table>` (virtualised only if
  a model has > ~2k rows; `BasicTerm_M` has 10k → render a window of 200
  rows with a range input for offset, no library). Cells editable on tap →
  `<input>` per dtype, commit on blur → `send("set_points", …)`.
- Import: accept `.csv, .xlsx` (`points_from_bytes` in Python instead of the
  hand-rolled CSV split at `app.js:494`, which mishandles quoted commas).
  Export: CSV / XLSX dropdown.
- Mobile: the Points tab is a normal tab, so it is reachable without the
  ☰ panel; the sheet buttons are 36 px touch targets already.

### B4. Tests

- Engine: commit/add/delete on the three families; import xlsx → same table.
- `validate.mjs`: xlsx round-trip under Pyodide.
- `browser_test.mjs`: add point → point dropdown grows → select → compute.

Effort: engine ½ day, desktop ½ day, web 1 day (table editor dominates).

---

## C. Session state (save / load / share)

A session = everything the user did that isn't in the model files:

```json
{ "v": 1, "model": "BasicTerm_S", "point": 1,
  "edits": {"age_at_entry": 52},
  "formulas": [{"space": "Projection", "name": "premium_pp", "source": "..."}],
  "points": null | {frame_json},           // only if the table was changed
  "inspector": {"space": "Projection", "name": "result_cf", "args": null,
                "history": [...]},
  "tab": "inspector" }
```

### C1. Engine

- `session_state() -> dict` (model name, formula edits, `points` if
  `_mpt` differs from the loaded original — keep `self._mpt_orig`).
- `apply_session(state)` after `load`: `set_points`, then each
  `set_formula`. Row/field edits and inspector position are UI state and
  are applied by the front-end.
- The user-model case (A3 load from zip) can't be restored from a name;
  the session then embeds nothing and says `"model_source": "user"` — the
  UI prompts for the zip. Don't embed 48 MB models in sessions.

### C2. UI

- **URL hash for the small case**: `#m=BasicTerm_S&p=1&e=age_at_entry:52&c=Projection.result_cf`.
  Cheap, shareable, no formulas/points. Parse on boot after `loadModel`;
  update with `history.replaceState` on point/field/navigate (debounced).
  This alone covers "send someone a link to this model point".
- **File for the full case**: Save session… / Load session… (`.json`) in
  the ☰ panel (web) and File menu (desktop). Formula edits and changed
  points included.
- Desktop: also auto-save the last session to
  `modelpoints/<model>/.session.json` and offer "Restore last session?" on
  start (cheap, high value).

### C3. Tests

- Engine: `session_state` → new `ModelSession` → `apply_session` → same PV.
- `browser_test.mjs`: open with a hash → correct model/point/field applied;
  slider → hash updates.

Effort: engine ¼ day, web ½ day, desktop ¼ day.

---

## Order and dependencies

1. **A1 engine + A4 engine tests** — foundation; everything else calls it.
2. **A3 web Edit/Apply** — the visible win; formula editing on a phone in
   WASM is the demo.
3. **A2 desktop Edit/Apply** (parity).
4. **A export/load** (web + desktop together — same zip layout).
5. **C2 URL hash** — small, independent, can ship any time after 1.
6. **B** — biggest UI surface; do after A so "Save to point" and formula
   edits compose.
7. **C file sessions** — last, once A and B define what a session contains.

Total ≈ 5–6 focused days. Each numbered step is shippable on its own.

## Risks / open questions

- `write_model` under Pyodide (MEMFS, openpyxl writing xlsx) — unverified;
  test in `validate.mjs` first (step 1), before any UI.
- `set_formula` with a **renamed** `def` or syntax error: catch, surface via
  the inline error banner (never a modal — house rule), leave the old
  formula in place.
- Editing a cell in a **parameterised** space (`Projection[1]` ItemSpaces):
  `set_formula` must go to the base space; ItemSpaces are regenerated. Our
  `_resolve_cells` prefers `_last_item` — bypass it for edits.
- Large tables in the web Points tab (10k rows) → windowed rendering, no
  library.
- Parquet in the web: skip (no pyarrow wheel vendored).
- Sessions referencing user-loaded models: prompt for the zip; don't embed.

## Unrelated bug found while surveying — FIXED

*[done: kept the sheet inside `.left`; `.left` now stays rendered on mobile
and its other children are hidden via `.app:not(.panel-open) .left >
:not(.fields-sheet)`. Verified 390×844: sliders and charts visible together,
toggle/✕/☰ consistent.]*

**Mobile "Fields" toggle does nothing.** `#fields-sheet` is a child of
`.left` (`web/index.html:28–55`), and on mobile `.left { display: none }`
(`web/styles.css:124`) unless the ☰ panel is open. A `position: fixed`
element inside a `display: none` ancestor is not rendered, so the toggle
flips `fields-open` and the ▾/▴ label (`app.js:511`) but nothing appears.
Fix: move `#fields-sheet` out of `.left` to be a direct child of `#app`
(desktop CSS then needs `.fields-sheet` placed in the left column via grid
area or `order`), or keep it in `.left` and hide the *other* children on
mobile instead of `.left` itself. Also requested: sliders **and** charts
visible by default on mobile — with the sheet fixed at `max-height: 46dvh`
and `.main { padding-bottom: 46dvh }` (`styles.css:131–132`) that already
holds once the sheet actually renders; verify at 390×844 with
playwright-cli and regenerate `docs/screenshot-mobile.png`.
