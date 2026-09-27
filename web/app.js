/* lifelib Explorer — web UI. Mirrors the desktop app's MainWindow +
   InspectorPanel; the Python engine runs in worker.js (Pyodide). */
(() => {
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtNum = (v, d = 6) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
  : Math.abs(v) >= 1e6 || (Math.abs(v) < 1e-4 && v !== 0) ? Number(v).toPrecision(d) : Number(v).toLocaleString(undefined, { maximumSignificantDigits: d });

// ---------------------------------------------------------------- worker
// One module worker owns Pyodide + the ModelSession. It is replaced wholesale
// when Pyodide suffers a fatal error (see recoverFromCrash below).
let worker = null;
let seq = 0;
const latest = { compute: 0, inspect: 0 };
const pending = new Map();      // seq -> {resolve, reject}
let restartReady = null;        // {resolve, reject} while a replacement worker boots
function startWorker() {
  worker = new Worker("worker.js", { type: "module" });
  worker.onmessage = onWorkerMessage;
}
function send(cmd, body = {}) {
  const s = ++seq;
  if (cmd === "compute" || cmd === "inspect") { latest[cmd] = s; worker.postMessage({ cmd: "latest", latest }); }
  if (cmd === "load" || cmd === "load_bytes") { latest.compute = s; latest.inspect = s; }
  return new Promise((resolve, reject) => {
    pending.set(s, { resolve, reject, cmd });
    worker.postMessage({ seq: s, cmd, ...body });
  });
}
function onWorkerMessage(ev) {
  const m = ev.data;
  if (m.cmd === "status") { setStatus(m.text); bootStatus(m.text); if (state.restarting) flash(`Python runtime crashed — ${state.lastCrash.why}. Restarting: ${m.text}`, 0, "err"); return; }
  if (m.cmd === "ready") { if (restartReady) restartReady.resolve(m); else onReady(m); return; }
  if (m.cmd === "fatal") { if (restartReady) restartReady.reject(new Error(m.error)); else bootStatus("Failed to start: " + m.error); return; }
  if (m.cmd === "crashed") { recoverFromCrash(m); return; }
  const p = pending.get(m.seq); if (!p) return; pending.delete(m.seq);
  // stale replies (superseded compute/inspect) are dropped silently
  if ((m.cmd === "compute" || m.cmd === "inspect") && m.seq < latest[m.cmd]) return;
  m.ok ? p.resolve(m.payload) : p.reject(new Error(m.error));
  // belt and braces: a reply may carry the fatal text before the crashed message arrives
  if (!m.ok && /fatally failed/i.test(m.error)) recoverFromCrash({ error: m.error });
}
startWorker();

// ------------------------------------------------------------------ state
const state = {
  manifest: null, loaded: null, table: null,   // table = {columns,index,data,dtypes}
  rowPos: 0, baselinePV: null, lastCompute: null,
  computeInterval: 150, throttleTimer: null, computing: false,
  index: [], current: null, history: [], histPos: -1,
  formulaEdits: new Map(),      // "space.name" -> edited source (mirror of the engine's list)
  userModels: [],               // [{name, buf}] models loaded from zip in this tab
  editing: false,
  yMode: "shared",              // multi-line charts: shared | independent | multiples
  graph: null,                  // {edges:[[reader, read]]} static formula graph (Formulas tab)
  fxSel: null,                  // "space.name" selected in the Formulas tab
  fxHistory: [], fxHistPos: -1, // Formulas selection history [{space,name}] (◀ ▶, Alt+←/→ while the tab is open)
  fxShowValues: false,          // Formulas tab: cards carry sparkline + value at t
  fxValues: null,               // {key: {series, value_repr, frame, args, n_cached}} from engine.cell_values()
  fxValuesStale: true,          // a compute happened since the last fetch
  fxT: null,                    // the t whose value the cards show (null = not chosen yet)
  restarting: false,            // Pyodide crashed; a replacement worker is booting
  crashes: 0,                   // consecutive crashes without a successful compute (retry cap)
  lastCrash: null,              // {why, stack, heapMB, at} — shown via the error banner's "details"
};
try { const m = localStorage.getItem("yMode"); if (["shared", "independent", "multiples"].includes(m)) state.yMode = m; } catch { /* storage unavailable */ }
state.fxShowValues = true;
try { const v = localStorage.getItem("fxValues"); if (v === "0" || v === "1") state.fxShowValues = v === "1"; } catch { /* storage unavailable */ }
let bootSteps = 0;
function bootStatus(t) { const el = $("boot-status"); if (el) el.textContent = t; $("boot-progress").style.width = Math.min(90, 10 + (bootSteps++ * 12)) + "%"; }
function setStatus(t) { $("status").textContent = t; }
function setBusy(t) { $("busy").textContent = t ? "⏳ " + t : ""; $("mobile-busy").classList.toggle("on", !!t); }
function showError(text, headline = null, title = "Model error", sticky = false) {
  if (state.restarting) return;                 // requests failing because the runtime died are not model errors
  const last = headline || (String(text).split("\n").map(s => s.trim()).filter(Boolean).filter(l => /^[A-Za-z_][\w.]*(Error|Exception): /.test(l) && !l.startsWith("modelx.core.errors.FormulaError")).pop())
             || String(text).trim().split("\n").pop();
  const el = $("error"); el.classList.remove("hidden"); el.dataset.sticky = sticky ? "1" : "";
  el.innerHTML = `<b>${esc(title)}</b> — ${esc(last.slice(0, 200))}<br><a href="#" data-a="details">details</a><a href="#" data-a="dismiss">dismiss</a>`;
  el.onclick = (e) => { const a = e.target.dataset.a; if (!a) return; e.preventDefault(); if (a === "details") alert(text); else { el.dataset.sticky = ""; el.classList.add("hidden"); } };
}
function clearError(force = false) { const el = $("error"); if (force || el.dataset.sticky !== "1") { el.dataset.sticky = ""; el.classList.add("hidden"); } }
// transient message at the top of the viewport — visible on mobile too, where
// the #error banner sits in the (closed) side panel. ms = 0 keeps it until replaced/clicked.
let _toastTimer = null;
function flash(text, ms = 5000, kind = "") {
  const el = $("toast"); el.textContent = text; el.className = "toast" + (kind ? " " + kind : "");
  clearTimeout(_toastTimer); if (ms) _toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
}
$("toast").onclick = () => $("toast").classList.add("hidden");

// ------------------------------------------------------ crash recovery
// Pyodide fatal errors poison the runtime; every later call fails with
// "Pyodide already fatally failed and can no longer be used." Recovery =
// snapshot what the user had, throw the worker away, boot a new one, reload
// the model, re-apply formula edits, restore point/fields/inspector, recompute.
function snapshotSession() {
  return {
    loaded: state.loaded ? state.loaded.name : null, rowPos: state.rowPos,
    fieldValues: state.fields && state.fields.length ? edits() : null,
    formulaEdits: new Map(state.formulaEdits), current: state.current,
    history: state.history.slice(), histPos: state.histPos, fxT: state.fxT,
    fxHistory: state.fxHistory.slice(), fxHistPos: state.fxHistPos,
  };
}
const MAX_CRASH_RESTARTS = 3;
async function recoverFromCrash(m) {
  if (state.restarting) return;
  state.restarting = true; state.crashes++;
  const why = String(m.error || "unknown").split("\n")[0].slice(0, 160) + (m.heapMB ? ` · heap ${m.heapMB} MB` : "");
  state.lastCrash = { why, stack: m.stack || "", heapMB: m.heapMB ?? null, at: new Date().toISOString() };
  console.error("Python runtime crashed:", why, m.stack || "");
  clearTimeout(state.throttleTimer); state.throttleTimer = null;
  for (const [, p] of pending) p.reject(new Error("Python runtime restarting")); pending.clear();
  try { worker.terminate(); } catch { /* already gone */ }
  await new Promise(r => setTimeout(r, 0));      // let the rejected callers' finally/catch run first
  if (state.crashes > MAX_CRASH_RESTARTS) {
    state.restarting = false; setBusy(null);
    flash(`Python crashed again (${why}). Not restarting any more — reload the page.`, 0, "err");
    showError(`${why}\n\n${state.lastCrash.stack}`, why, "Python runtime crashed", true);
    return;
  }
  flash(`Python runtime crashed — ${why}. Restarting …`, 0, "err");
  setBusy("restarting Python …");
  const snap = snapshotSession();
  const booted = new Promise((resolve, reject) => { restartReady = { resolve, reject }; });
  startWorker();
  try {
    const rm = await booted;
    $("versions").textContent = `Pyodide ${rm.pyodide} · pandas ${rm.pandas} · modelx ${rm.modelx}`;
    state.restarting = false;
    if (snap.loaded) {
      const user = state.userModels.find(u => u.name === snap.loaded);
      await loadModelWith(snap.loaded, () => user ? send("load_bytes", { name: user.name, buf: user.buf }) : send("load", { name: snap.loaded }), snap);
    }
    const n = state.formulaEdits.size, lost = snap.formulaEdits.size - n;
    flash(`Python restarted — ${snap.loaded ? `${snap.loaded} reloaded` : "ready"}${n ? `, ${n} formula edit${n > 1 ? "s" : ""} re-applied` : ""}${lost > 0 ? `, ${lost} could not be re-applied` : ""}. Cause: ${why}`, 10000, "ok");
    if (!snap.loaded && state.manifest) loadModel($("model").value);      // it died before the first model was in
    showError(`${why}\n\n${state.lastCrash.stack}`, why, "Python runtime crashed and was restarted", true);
  } catch (e) {
    state.restarting = false; setBusy(null);
    flash(`Python restart failed: ${e.message}. Reload the page.`, 0, "err");
  } finally { restartReady = null; }
}

// ------------------------------------------------------------------ boot
async function onReady(m) {
  $("versions").textContent = `Pyodide ${m.pyodide} · pandas ${m.pandas} · modelx ${m.modelx}`;
  bootStatus("fetching model list …");
  const mr = await fetch("manifest.json", { cache: "no-cache" });
  if (!mr.ok) { bootStatus(`manifest.json → HTTP ${mr.status}. Serve web/dist/ (the built site), not web/. Build: uv run python web/build_models.py`); return; }
  state.manifest = await mr.json();
  const sel = $("model"); sel.innerHTML = "";
  // grouped by lifelib's own taxonomy (Generic / Reference / Past / Misc.)
  const cats = state.manifest.categories || [...new Set(state.manifest.models.map(m => m.category))];
  for (const cat of cats) {
    const ms = state.manifest.models.filter(m => (m.category || "Other") === cat); if (!ms.length) continue;
    const g = document.createElement("optgroup"); g.label = cat;
    for (const md of ms) { const o = document.createElement("option"); o.value = md.name; o.textContent = `${md.label}  (${Math.round(md.bytes / 1024)} KB)`; o.title = `${cat} · ${md.zip}`; g.appendChild(o); }
    sel.appendChild(g);
  }
  sel.value = state.manifest.default;
  sel.onchange = () => {
    if (!confirmDiscardEdits()) { sel.value = state.loaded ? state.loaded.name : state.manifest.default; return; }
    const user = state.userModels.find(u => u.name === sel.value);
    user ? loadModelBytes(user.name, user.buf) : loadModel(sel.value);
  };
  $("boot").classList.add("hidden"); $("app").classList.remove("hidden");
  await loadModel(sel.value);
}

// formula edits live only in the loaded model; switching models drops them
function confirmDiscardEdits() {
  const n = state.formulaEdits.size;
  return !n || confirm(`${n} formula edit${n > 1 ? "s" : ""} will be lost when the model changes.\nExport the model first if you want to keep them.\n\nContinue?`);
}
async function loadModel(name) { return loadModelWith(name, () => send("load", { name })); }
async function loadModelBytes(name, buf) {
  // keep the bytes so the picker can reload a user model later (the worker
  // copies what it receives, so no transfer list here)
  if (!state.userModels.some(u => u.name === name)) state.userModels.push({ name, buf });
  return loadModelWith(name, () => send("load_bytes", { name, buf }));
}
/** keep = a snapshotSession() to restore after a runtime restart (else a fresh start) */
async function loadModelWith(name, doLoad, keep = null) {
  setBusy(`loading ${name} …`); clearError(true); $("summary").textContent = "—";   // a deliberate load also dismisses a crash notice
  $("fields").innerHTML = ""; $("reset").disabled = true;
  state.formulaEdits = new Map(); setEditing(false);
  try {
    const info = await doLoad();
    info.name = info.model_name || info.name;      // web zips extract to <key>/model — key is the name
    state.loaded = info; state.table = info.model_point_table;
    syncModelPicker(info.name);
    state.index = info.cells_index; state.history = []; state.histPos = -1; state.current = null;
    state.graph = info.cell_graph || { edges: [] }; state.fxSel = null; state.fxHistory = []; state.fxHistPos = -1; state.fxValues = null; state.fxValuesStale = true; state.fxT = null;
    if (keep && keep.formulaEdits.size) await reapplyFormulaEdits(keep.formulaEdits);
    fillPoints(); buildFields(); renderResults(); syncHistory(); renderFormulas(); fxSyncHistory(); updateEditCount();
    setStatus(`Loaded ${info.name} in ${info.load_seconds.toFixed(1)}s — ${state.table.index.length} model points, ${info.cells_index.length} cells/tables`);
    setBusy(null);
    if (keep) {
      // same point, same slider edits, same inspector history, same Formulas t
      const pos = keep.rowPos < state.table.index.length ? keep.rowPos : 0;
      state.rowPos = pos; $("point").value = pos; state.baselinePV = null;
      const row = state.table.data[pos]; state.fields.forEach((f, i) => f.set(row[i]));
      if (keep.fieldValues) state.fields.forEach(f => { if (f.col in keep.fieldValues) f.set(keep.fieldValues[f.col]); });
      updateDirty();
      state.fxT = keep.fxT;
      const known = v => !!v && state.index.some(e => e.space === v.space && e.name === v.name);
      if (keep.fxHistory && keep.fxHistory.length && keep.fxHistory.every(known)) { state.fxHistory = keep.fxHistory; state.fxHistPos = keep.fxHistPos; }
      if (known(keep.current)) {
        state.history = keep.history; state.histPos = keep.histPos; state.current = keep.current;
        syncHistory(); highlightResult();
        // the Formulas selection may differ from the Inspector's visit — restore what was selected there
        const fsel = state.fxHistory[state.fxHistPos] || keep.current;
        fxSelect(fsel.space, fsel.name, { scroll: false, fromHistory: true });
      } else { const first = defaultCell(info); if (first) navigate(first.space, first.name, null); }
      fxSyncHistory();
      requestCompute(true);                        // the compute's callback re-inspects the current visit
      return;
    }
    const first = defaultCell(info);
    if (first) navigate(first.space, first.name, null); else { state.current = null; $("header").textContent = "no cells"; }
    selectPoint(0);
    if (isMobile()) { setPanel(false); if (!_fieldsAutoOpened) { _fieldsAutoOpened = true; setFields(true); } }
  } catch (e) { setBusy(null); showError(e.message); }
}
// after a restart: put the session's formula edits back into the fresh model
async function reapplyFormulaEdits(editsMap) {
  let graphStale = false;
  for (const [key, source] of editsMap) {
    const i = key.lastIndexOf("."), space = key.slice(0, i), name = key.slice(i + 1);
    try {
      const np = await send("set_formula", { space, name, source });
      if (np.edited) state.formulaEdits.set(key, source);
      const e = state.index.find(x => x.space === np.space && x.name === np.name);
      if (e) { e.edited = !!np.edited; e.source = np.source || e.source; }
      graphStale = true;
    } catch (e) { flash(`Could not re-apply the edit to ${key}: ${e.message}`, 8000, "err"); }
  }
  if (graphStale) { try { state.graph = await send("cell_graph"); } catch { /* keep the load-time graph */ } }
}
// user-loaded models appear in their own optgroup at the top of the picker
function syncModelPicker(current) {
  const sel = $("model");
  let g = sel.querySelector('optgroup[label="Your models"]');
  if (state.userModels.length) {
    if (!g) { g = document.createElement("optgroup"); g.label = "Your models"; sel.insertBefore(g, sel.firstChild); }
    g.innerHTML = "";
    for (const u of state.userModels) { const o = document.createElement("option"); o.value = u.name; o.textContent = `${u.name}  (${Math.round(u.buf.byteLength / 1024)} KB, loaded from zip)`; g.appendChild(o); }
  }
  if ([...sel.options].some(o => o.value === current)) sel.value = current;
  updateEditCount();
}

function defaultCell(info) {
  const idx = info.cells_index.filter(e => e.params !== "reference");
  const inSpace = idx.filter(e => e.space === info.space_name);
  return inSpace.find(e => e.name === "result_cf")
      || inSpace.find(e => /^(net_?cf|NetInsurCF|NetCF|net_cashflow)$/i.test(e.name))
      || inSpace.find(e => /^pv_?net/i.test(e.name) || /^PV_Net/.test(e.name))
      || inSpace[0] || idx[0] || null;
}

// ------------------------------------------------------------ model points
function fillPoints() {
  const sel = $("point"); sel.innerHTML = "";
  state.table.index.forEach((pid, i) => { const o = document.createElement("option"); o.value = i; o.textContent = pid; sel.appendChild(o); });
  sel.onchange = () => selectPoint(+sel.value);
}
function selectPoint(pos) {
  state.rowPos = pos; $("point").value = pos; state.baselinePV = null; clearError();
  const row = state.table.data[pos];
  state.fields.forEach((f, i) => f.set(row[i]));
  updateDirty(); requestCompute(true);
}
function isNumericDtype(dt) { return /^(int|uint|float)/.test(dt); }
function isBoolDtype(dt) { return dt === "bool"; }
function buildFields() {
  const host = $("fields"); host.innerHTML = ""; state.fields = [];
  const { columns, dtypes, data } = state.table;
  columns.forEach((col, ci) => {
    const name = document.createElement("span"); name.className = "fname"; name.textContent = col; host.appendChild(name);
    const box = document.createElement("div"); box.className = "field"; host.appendChild(box);
    const colvals = data.map(r => r[ci]);
    const dt = dtypes[ci];
    let f;
    if (isBoolDtype(dt)) {
      const cb = document.createElement("input"); cb.type = "checkbox"; box.appendChild(cb);
      cb.onchange = onEdit;
      f = { col, set: v => cb.checked = !!v, get: () => cb.checked, orig: null };
    } else if (isNumericDtype(dt)) {
      const nums = colvals.filter(v => v !== null && v !== undefined);
      let lo = nums.length ? Math.min(...nums) : 0, hi = nums.length ? Math.max(...nums) : 1;   // all-null column: any range
      if (!(hi > lo)) hi = lo + (Math.abs(lo) || 1);
      const isInt = /^u?int/.test(dt), nullable = nums.length < colvals.length;
      const num = document.createElement("input"); num.type = "number"; num.step = isInt ? 1 : (hi - lo) / 100;
      const rng = document.createElement("input"); rng.type = "range"; rng.min = 0; rng.max = 500; rng.title = `${fmtNum(lo)} … ${fmtNum(hi)} (observed range; type to go beyond)`;
      box.appendChild(num); box.appendChild(rng);
      let na = null;
      if (nullable) { const l = document.createElement("label"); l.className = "na"; na = document.createElement("input"); na.type = "checkbox"; l.appendChild(na); l.append(" N/A"); box.appendChild(l);
        na.onchange = () => { num.disabled = rng.disabled = na.checked; onEdit(); }; }
      const toSlider = v => Math.round((Math.min(Math.max(v, lo), hi) - lo) / (hi - lo) * 500);
      const fromSlider = p => { const v = lo + p / 500 * (hi - lo); return isInt ? Math.round(v) : v; };
      let guard = false;
      num.oninput = () => { if (guard) return; guard = true; rng.value = toSlider(+num.value); guard = false; onEdit(); };
      rng.oninput = () => { if (guard) return; guard = true; num.value = fromSlider(+rng.value); guard = false; onEdit(); };
      f = { col, set: v => { if (v === null || v === undefined) { if (na) { na.checked = true; num.disabled = rng.disabled = true; } return; }
                            if (na) { na.checked = false; num.disabled = rng.disabled = false; }
                            num.value = v; rng.value = toSlider(+v); },
            get: () => (na && na.checked) ? null : (isInt ? Math.round(+num.value) : +num.value) };
    } else {
      const uniq = [...new Set(colvals.filter(v => v !== null && v !== undefined).map(String))].sort();
      if (uniq.length > 0 && uniq.length <= 40) {
        const sel = document.createElement("select"); box.appendChild(sel);
        for (const u of uniq) { const o = document.createElement("option"); o.value = u; o.textContent = u; sel.appendChild(o); }
        if (uniq.length < colvals.length) { const o = document.createElement("option"); o.value = "\u0000NA"; o.textContent = "«N/A»"; sel.appendChild(o); }
        sel.onchange = onEdit;
        f = { col, set: v => sel.value = (v === null || v === undefined) ? "\u0000NA" : String(v), get: () => sel.value === "\u0000NA" ? null : sel.value };
      } else {
        const inp = document.createElement("input"); box.appendChild(inp); inp.oninput = onEdit;
        f = { col, set: v => inp.value = v ?? "", get: () => inp.value };
      }
    }
    f.nameEl = name; state.fields.push(f);
  });
}
function edits() { const o = {}; state.fields.forEach(f => o[f.col] = f.get()); return o; }
function isDirty() {
  const row = state.table.data[state.rowPos]; let any = false;
  state.fields.forEach((f, i) => {
    const a = f.get(), b = row[i];
    const same = (a === null || a === undefined) ? (b === null || b === undefined)
      : typeof a === "number" && typeof b === "number" ? Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(b)) : String(a) === String(b);
    f.nameEl.classList.toggle("dirty", !same); if (!same) any = true;
  });
  return any;
}
function updateDirty() { const d = isDirty(); $("reset").disabled = !d; $("reset").textContent = d ? "Reset fields  ●" : "Reset fields"; }
function onEdit() { updateDirty(); requestCompute(false); }
$("reset").onclick = () => selectPoint(state.rowPos);

// trailing-edge throttle with adaptive interval (see README)
function requestCompute(immediate) {
  if (state.throttleTimer && !immediate) return;
  if (immediate) { clearTimeout(state.throttleTimer); state.throttleTimer = null; }
  state.throttleTimer = setTimeout(async () => {
    state.throttleTimer = null;
    setBusy("computing …");
    try {
      const res = await send("compute", { rowPos: state.rowPos, edits: edits() });
      if (!res) return;
      state.computeInterval = Math.min(3000, Math.max(150, res.elapsed * 1200));
      state.lastCompute = res; setBusy(null); clearError(); state.crashes = 0;
      renderSummary(res); renderResultCharts(res);
      setStatus(`Point ${res.point_label} computed in ${res.elapsed.toFixed(2)}s`);
      if (state.current) inspect(state.current);          // refresh inspector values
      state.fxValuesStale = true; fxMaybeFetchValues();    // Formulas tab "show values" follows the compute
    } catch (e) { setBusy(null); showError(e.message); }
  }, immediate ? 0 : state.computeInterval);
}

// --------------------------------------------------------------- summary
function pvRow(pv) {
  if (!pv || !pv.data.length) return null;
  const i = pv.index.indexOf("PV"); const row = pv.data[i >= 0 ? i : 0];
  const out = {}; pv.columns.forEach((c, j) => { if (typeof row[j] === "number") out[c] = row[j]; }); return out;
}
let renderSummary = function (res) {
  const cur = pvRow(res.result_pv); const el = $("summary");
  if (!cur) { el.innerHTML = `<b>point ${esc(res.point_label)}</b> — ${res.result_cf ? res.result_cf.data.length : 0} projection steps, ${res.elapsed.toFixed(2)}s <span class="muted">(no result_pv in this model)</span>`; return; }
  const dirty = isDirty(); if (!dirty) state.baselinePV = cur;
  const base = state.baselinePV || {};
  let rows = "";
  for (const [k, v] of Object.entries(cur)) {
    const label = k.replace(/^PV /, ""); const bold = /net/i.test(label);
    let cell = fmtNum(v, 7);
    if (k in base && base[k] !== v) { const d = v - base[k]; cell += ` <span class="${d > 0 ? "delta-up" : "delta-down"}">(${d > 0 ? "+" : ""}${fmtNum(d, 5)})</span>`; }
    rows += `<tr><td>${bold ? "<b>" : ""}${esc(label)}${bold ? "</b>" : ""}</td><td>${bold ? "<b>" : ""}${cell}${bold ? "</b>" : ""}</td></tr>`;
  }
  el.innerHTML = `<b>point ${esc(res.point_label)}</b> · ${res.elapsed.toFixed(2)}s${dirty ? " · <i>edited</i>" : ""}<table>${rows}</table>`;
};

// ------------------------------------------------------------------ SVG
const PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"];
function stepPath(xs, ys, sx, sy) {
  let d = ""; for (let i = 0; i < xs.length; i++) { const x = sx(xs[i]), y = sy(ys[i]);
    if (i === 0) d += `M${x},${y}`; else d += `H${x}V${y}`; }
  return d;
}
// "nice" tick values (1/2/5 × 10ⁿ) covering [a, b] with about n steps
const ticks = (a, b, n) => { const step = Math.pow(10, Math.floor(Math.log10((b - a) / n))); const m = [1, 2, 5, 10].find(k => (b - a) / (step * k) <= n) * step; const out = []; for (let v = Math.ceil(a / m) * m; v <= b + 1e-9; v += m) out.push(+v.toPrecision(10)); return out; };
// y-axis mode for multi-line charts. Cashflow columns can differ by orders of
// magnitude (a single premium at t=0 vs. monthly claims), so one shared axis
// hides the shape of the small ones; independent axes show every shape but
// hide magnitude; small multiples show both at the cost of height.
const Y_MODES = [["shared", "shared", "one y-axis for all lines — compare magnitudes"],
                 ["independent", "indep.", "each line scaled to its own max |y| on one plot — compare shapes; the legend shows each max"],
                 ["multiples", "multiples", "small multiples — one panel per line, each with its own y-axis"]];
function yModeBar(mode) {
  return `<div class="ymode" title="y-axis mode for multi-line charts"><span class="muted">y</span>${Y_MODES.map(([m, lab, tip]) => `<button data-mode="${m}" class="${m === mode ? "on" : ""}" title="${esc(tip)}">${lab}</button>`).join("")}</div>`;
}
function setYMode(mode) {
  state.yMode = mode; try { localStorage.setItem("yMode", mode); } catch { /* private mode */ }
  if (state.lastCompute) renderResultCharts(state.lastCompute);
  if (state.lastPayload) { const p = state.lastPayload; renderValueChart(p, p.space ? `${p.space}.${p.name}` : p.name); }
}
const lineColor = (l, i) => l.color || PALETTE[i % PALETTE.length];
/** lines: [{x:[],y:[],label,color,width}], mark: {x,y,lineIdx} */
// content-box size of a chart host (the .bigchart padding used to clip the last x label)
function hostSize(host) {
  // a host in a hidden tab measures 0×0 (display:none): fall back to a nominal size;
  // the tab handler re-renders once it becomes visible
  const cs = getComputedStyle(host), r = host.getBoundingClientRect();
  const W = Math.floor(r.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight));
  const H = Math.floor(r.height - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom));
  return { W: W > 40 ? W : 400, H: H > 40 ? H : 220 };
}
function lineChart(host, lines, opts = {}) {
  const { W, H: HH } = hostSize(host);
  const allx = lines.flatMap(l => l.x), ally = lines.flatMap(l => l.y).filter(Number.isFinite);
  if (!allx.length || !ally.length) { host.innerHTML = `<div class="msg">${esc(opts.empty || "no data")}</div>`; return; }
  const multi = lines.length > 1;
  const mode = multi ? (opts.yMode || state.yMode) : "shared";
  const barH = multi ? 22 : 0, H = HH - barH;
  const bar = multi ? yModeBar(mode) : "";
  let x0 = Math.min(...allx), x1 = Math.max(...allx); if (x1 === x0) x1 = x0 + 1;
  let s;
  if (mode === "multiples") s = multiplesSVG(W, H, lines, x0, x1, opts);
  else s = overlaySVG(W, H, lines, x0, x1, opts, mode === "independent");
  host.innerHTML = bar + s;
  host.classList.toggle("scroll", mode === "multiples");
  for (const b of host.querySelectorAll(".ymode button")) b.onclick = () => setYMode(b.dataset.mode);
}
function overlaySVG(W, H, lines, x0, x1, opts, indep) {
  const pad = { l: 56, r: 10, t: opts.title ? 26 : 10, b: 26 };
  // independent: y ÷ own max |y| per line, so all lines share the zero line
  const scales = lines.map(l => { const ys = l.y.filter(Number.isFinite); const m = Math.max(...ys.map(Math.abs)); return indep ? (m || 1) : 1; });
  const ally = lines.flatMap((l, i) => l.y.filter(Number.isFinite).map(v => v / scales[i]));
  let y0 = Math.min(0, ...ally), y1 = Math.max(0, ...ally); if (y1 === y0) y1 = y0 + 1;
  const sx = x => pad.l + (x - x0) / (x1 - x0) * (W - pad.l - pad.r), sy = y => pad.t + (1 - (y - y0) / (y1 - y0)) * (H - pad.t - pad.b);
  let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`;
  for (const t of ticks(y0, y1, 5)) s += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${sy(t)}" y2="${sy(t)}" stroke="#eee"/><text x="${pad.l - 4}" y="${sy(t) + 3}" font-size="10" text-anchor="end" fill="#666">${indep ? (t === 0 ? "0" : `${Math.round(t * 100)}%`) : fmtNum(t, 4)}</text>`;
  for (const t of ticks(x0, x1, 8)) s += `<text x="${sx(t)}" y="${H - 8}" font-size="10" text-anchor="middle" fill="#666">${fmtNum(t, 4)}</text>`;
  s += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${sy(0)}" y2="${sy(0)}" stroke="#999" stroke-width="0.7"/>`;
  lines.forEach((l, i) => s += `<path d="${stepPath(l.x, l.y.map(v => v / scales[i]), sx, sy)}" fill="none" stroke="${lineColor(l, i)}" stroke-width="${l.width || 1.5}"/>`);
  if (opts.mark) { const m = opts.mark, my = sy(m.y / scales[m.lineIdx ?? 0]); s += `<circle cx="${sx(m.x)}" cy="${my}" r="5" fill="${"#d62728"}"/><text x="${sx(m.x) + 8}" y="${my - 6}" font-size="11" fill="#d62728">${fmtNum(m.y)}</text>`; }
  if (opts.title) s += `<text x="${W / 2}" y="16" font-size="12" text-anchor="middle" fill="#333">${esc(opts.title)}</text>`;
  if (indep) s += `<text x="${pad.l - 4}" y="${H - 8}" font-size="9" text-anchor="end" fill="#999">% of max|y|</text>`;
  if (opts.xlabel) s += `<text x="${(pad.l + W - pad.r) / 2}" y="${H - 0}" font-size="10" text-anchor="middle" fill="#666" dy="-0">${esc(opts.xlabel)}</text>`;
  if (lines.length > 1 && lines.some(l => l.label)) {
    // legend: right-aligned on a translucent panel; in independent mode each entry states its own max |y|
    const items = lines.map((l, i) => ({ l, i, text: (l.label || "") + (indep ? `  ·  max ${fmtNum(scales[i], 3)}` : "") })).filter(it => it.l.label);
    const wch = Math.max(...items.map(it => it.text.length)) * 5.6 + 18, lx = Math.max(pad.l + 4, W - pad.r - wch);
    s += `<rect x="${lx - 4}" y="${pad.t}" width="${W - pad.r - lx + 4}" height="${items.length * 13 + 4}" fill="#fff" opacity=".78"/>`;
    let ly = pad.t + 4; for (const it of items) { s += `<rect x="${lx}" y="${ly}" width="10" height="3" fill="${lineColor(it.l, it.i)}"/><text x="${lx + 14}" y="${ly + 5}" font-size="10" fill="#444">${esc(it.text)}</text>`; ly += 13; }
  }
  return s + "</svg>";
}
function multiplesSVG(W, H0, lines, x0, x1, opts) {
  const MIN_PANEL = 44, pad = { l: 56, r: 10, t: opts.title ? 22 : 6, b: 24 }, gap = 6;
  const n = lines.length, H = Math.max(H0, pad.t + pad.b + n * MIN_PANEL + (n - 1) * gap);
  const ph = (H - pad.t - pad.b - (n - 1) * gap) / n;
  const sx = x => pad.l + (x - x0) / (x1 - x0) * (W - pad.l - pad.r);
  let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`;
  if (opts.title) s += `<text x="${W / 2}" y="14" font-size="12" text-anchor="middle" fill="#333">${esc(opts.title)}</text>`;
  lines.forEach((l, i) => {
    const top = pad.t + i * (ph + gap), ys = l.y.filter(Number.isFinite);
    let y0 = Math.min(0, ...ys), y1 = Math.max(0, ...ys); if (y1 === y0) y1 = y0 + 1;
    const sy = y => top + (1 - (y - y0) / (y1 - y0)) * ph, col = lineColor(l, i);
    s += `<rect x="${pad.l}" y="${top}" width="${W - pad.l - pad.r}" height="${ph}" fill="#fafbfc" stroke="#eee"/>`;
    // two labels per panel: the extremes (0 when one-signed), so magnitude stays readable
    for (const t of [y0, y1].filter((v, j, a) => a.indexOf(v) === j)) s += `<text x="${pad.l - 4}" y="${sy(t) + (t === y1 ? 8 : -1)}" font-size="9" text-anchor="end" fill="#666">${fmtNum(t, 3)}</text>`;
    if (y0 < 0 && y1 > 0) s += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${sy(0)}" y2="${sy(0)}" stroke="#999" stroke-width="0.7"/>`;
    s += `<path d="${stepPath(l.x, l.y, sx, sy)}" fill="none" stroke="${col}" stroke-width="${l.width || 1.5}"/>`;
    if (l.label) s += `<text x="${W - pad.r - 4}" y="${top + 10}" font-size="10" font-weight="600" text-anchor="end" fill="${col}">${esc(l.label)}</text>`;
    if (opts.mark && (opts.mark.lineIdx ?? 0) === i) { const m = opts.mark; s += `<circle cx="${sx(m.x)}" cy="${sy(m.y)}" r="4" fill="#d62728"/>`; }
  });
  for (const t of ticks(x0, x1, 8)) s += `<text x="${sx(t)}" y="${H - 8}" font-size="10" text-anchor="middle" fill="#666">${fmtNum(t, 4)}</text>`;
  if (opts.xlabel) s += `<text x="${(pad.l + W - pad.r) / 2}" y="${H}" font-size="10" text-anchor="middle" fill="#666">${esc(opts.xlabel)}</text>`;
  return s + "</svg>";
}
function barChart(host, labels, values, title) {
  const { W, H } = hostSize(host), pad = { l: 120, r: 60, t: 26, b: 10 };
  const mx = Math.max(1e-9, ...values.map(Math.abs)); const zero = pad.l + (W - pad.l - pad.r) / 2; const scale = (W - pad.l - pad.r) / 2 / mx;
  const bh = (H - pad.t - pad.b) / labels.length;
  let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><text x="${W / 2}" y="16" font-size="12" text-anchor="middle" fill="#333">${esc(title)}</text>`;
  labels.forEach((lab, i) => { const v = values[i], y = pad.t + i * bh + 2, w = Math.abs(v) * scale;
    s += `<rect x="${v < 0 ? zero - w : zero}" y="${y}" width="${w}" height="${bh - 4}" fill="${v < 0 ? "#d62728" : "#1f77b4"}"/>`;
    s += `<text x="${pad.l - 6}" y="${y + bh / 2}" font-size="11" text-anchor="end" fill="#333">${esc(lab)}</text>`;
    s += `<text x="${(v < 0 ? zero - w : zero + w) + (v < 0 ? -4 : 4)}" y="${y + bh / 2}" font-size="10" text-anchor="${v < 0 ? "end" : "start"}" fill="#444">${fmtNum(v, 6)}</text>`; });
  s += `<line x1="${zero}" x2="${zero}" y1="${pad.t}" y2="${H - pad.b}" stroke="#999"/></svg>`; host.innerHTML = s;
}
function frameLines(frame, maxCols = 8) {
  const cols = frame.columns.map((c, j) => ({ c, j })).filter(({ j }) => frame.data.some(r => typeof r[j] === "number")).slice(0, maxCols);
  const x = frame.data.map((_, i) => i);
  return cols.map(({ c, j }) => ({ label: c, x, y: frame.data.map(r => (typeof r[j] === "number" ? r[j] : NaN)), width: /net/i.test(c) ? 2.5 : 1.3, color: /net/i.test(c) ? "#000" : undefined }));
}
function renderResultCharts(res) {
  if (res.result_cf) lineChart($("cf-chart"), frameLines(res.result_cf), { title: `Cashflows — point ${res.point_label}`, xlabel: "t" }); else $("cf-chart").innerHTML = "";
  const pv = pvRow(res.result_pv); if (pv) barChart($("pv-chart"), Object.keys(pv), Object.values(pv), `Present values — point ${res.point_label}`); else $("pv-chart").innerHTML = "";
  const ps = Object.entries(res.policy_series || {});
  if (ps.length) lineChart($("pol-chart"), ps.map(([k, ys]) => ({ label: k, x: ys.map((_, i) => i), y: ys })), { title: `Policy counts — point ${res.point_label}`, xlabel: "t" }); else $("pol-chart").innerHTML = "";
}

// ------------------------------------------------------------- inspector
// -- Python syntax highlighting (hand-rolled, offline). Cell names become
//    clickable links into the inspector.
const PY_KW = new Set("False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield".split(" "));
const PY_BUILTIN = new Set("abs all any bool dict enumerate float int isinstance len list map max min print range round set str sum tuple zip np pd math".split(" "));
function highlightPython(src, cellNames) {
  let out = "", i = 0; const n = src.length;
  const push = (cls, text) => { out += cls ? `<span class="${cls}">${esc(text)}</span>` : esc(text); };
  while (i < n) {
    const c = src[i];
    if (c === "#") { const j = src.indexOf("\n", i); const e = j < 0 ? n : j; push("tk-c", src.slice(i, e)); i = e; continue; }
    if (c === '"' || c === "'") {
      const triple = src.startsWith(c.repeat(3), i); const q = triple ? c.repeat(3) : c;
      let j = i + q.length;
      while (j < n) { if (src[j] === "\\") { j += 2; continue; } if (src.startsWith(q, j)) { j += q.length; break; } if (!triple && src[j] === "\n") break; j++; }
      push("tk-s", src.slice(i, Math.min(j, n))); i = Math.min(j, n); continue;
    }
    if (/[0-9]/.test(c) && !/\w/.test(src[i - 1] || "")) { let j = i; while (j < n && /[\d.eE+-]/.test(src[j]) && !(/[+-]/.test(src[j]) && !/[eE]/.test(src[j - 1]))) j++; push("tk-n", src.slice(i, j)); i = j; continue; }
    if (c === "@" && /^\s*$/.test(src.slice(src.lastIndexOf("\n", i) + 1, i))) { let j = i; while (j < n && src[j] !== "\n") j++; push("tk-d", src.slice(i, j)); i = j; continue; }
    if (/[A-Za-z_]/.test(c)) {
      let j = i; while (j < n && /\w/.test(src[j])) j++;
      const w = src.slice(i, j);
      const prev = src.slice(0, i).match(/(def|class)\s+$/);
      if (prev) push("tk-def", w);
      else if (PY_KW.has(w)) push("tk-k", w);
      else if (cellNames && cellNames.has(w) && src[j] === "(") out += `<a class="tk-cell" href="#" data-cell="${esc(w)}">${esc(w)}</a>`;
      else if (PY_BUILTIN.has(w)) push("tk-b", w);
      else push("", w);
      i = j; continue;
    }
    push("", c); i++;
  }
  return out;
}

function rankCells(index, query) {
  const toks = query.toLowerCase().split(/\s+/).filter(Boolean); const out = [];
  for (const e of index) {
    const name = e.name.toLowerCase(), space = e.space.toLowerCase(), doc = e.doc.toLowerCase(), src = e.source.toLowerCase();
    let score = 0, ctx = "", ok = true;
    for (const t of toks) {
      if (t === name) score += 100; else if (name.startsWith(t)) score += 80; else if (name.includes(t)) score += 60;
      else if (space.includes(t)) score += 40;
      else if (doc.includes(t)) { score += 25; ctx ||= e.doc.split("\n").find(l => l.toLowerCase().includes(t)) || ""; }
      else if (src.includes(t)) { score += 15; ctx ||= e.source.split("\n").find(l => l.toLowerCase().includes(t)) || ""; }
      else { ok = false; break; }
    }
    if (ok) out.push({ score, e, ctx: ctx.trim() });
  }
  out.sort((a, b) => b.score - a.score || a.e.space.localeCompare(b.e.space) || a.e.name.localeCompare(b.e.name));
  return out;
}
const MAX_RESULTS = 300;
function renderResults() {
  const q = $("search").value.trim(); const ul = $("results"); ul.innerHTML = "";
  let ranked = q ? rankCells(state.index, q) : [...state.index].sort((a, b) => a.space.localeCompare(b.space) || a.name.localeCompare(b.name)).map(e => ({ e, ctx: "" }));
  const total = ranked.length; ranked = ranked.slice(0, MAX_RESULTS);
  for (const { e, ctx } of ranked) {
    const li = document.createElement("li"); li.dataset.space = e.space; li.dataset.name = e.name;
    const extra = ctx || (e.doc.split("\n")[0] || "");
    li.innerHTML = `${esc(e.space)}.${esc(e.name)}(${esc(e.params)})${e.edited ? '<span class="edited-dot" title="formula edited">●</span>' : ""}${extra ? `<span class="doc">${esc(extra.slice(0, 90))}</span>` : ""}`;
    li.onclick = () => { navigate(e.space, e.name, null); setResultsOpen(false); $("search").blur(); }; ul.appendChild(li);
  }
  if (total > ranked.length) { const li = document.createElement("li"); li.className = "more"; li.textContent = `… ${total - ranked.length} more — refine the search`; ul.appendChild(li); }
  highlightResult();
}
function highlightResult() {
  for (const li of $("results").children) li.classList.toggle("sel", !!state.current && li.dataset.space === state.current.space && li.dataset.name === state.current.name);
}
$("search").oninput = renderResults;
$("search").onkeydown = (e) => {
  if (e.key === "ArrowDown") { const sel = $("results").querySelector("li.sel") || $("results").firstElementChild; if (sel) { sel.focus?.(); sel.scrollIntoView({ block: "nearest" }); } e.preventDefault(); }
  if (e.key === "Enter") { const sel = $("results").querySelector("li.sel") || $("results").firstElementChild; if (sel && !sel.classList.contains("more")) navigate(sel.dataset.space, sel.dataset.name, null); }
  if (e.key === "Escape") { $("search").value = ""; renderResults(); }
};

const visitLabel = v => (v.space ? `${v.space}.${v.name}` : v.name) + (v.args ? `(${v.args.map(a => JSON.stringify(a)).join(", ")})` : "");
function navigate(space, name, args, fromHistory = false) {
  const v = { space, name, args };
  if (!fromHistory) {
    const cur = state.history[state.histPos];
    if (!(cur && cur.space === space && cur.name === name && JSON.stringify(cur.args) === JSON.stringify(args))) {
      state.history.splice(state.histPos + 1); state.history.push(v); if (state.history.length > 200) state.history.shift(); state.histPos = state.history.length - 1;
    }
  }
  state.current = v; syncHistory(); highlightResult(); inspect(v);
  // the Formulas tab follows: same cell selected, and its t = this invocation's
  if (args && typeof args[0] === "number" && args[0] !== state.fxT) { state.fxT = args[0]; if (state.fxValues) fxSetT(args[0], false); }
  if (state.graph && state.fxSel !== fxKey(space, name)) fxSelect(space, name); else if (state.graph && state.fxShowValues) fxPaintValues();
}
function syncHistory() {
  const sel = $("history"); sel.innerHTML = "";
  if (!state.history.length) { const o = document.createElement("option"); o.textContent = "History — nothing visited yet"; sel.appendChild(o); }
  for (let pos = state.history.length - 1; pos >= 0; pos--) { const o = document.createElement("option"); o.value = pos; o.textContent = (pos === state.histPos ? "▸ " : "   ") + visitLabel(state.history[pos]); sel.appendChild(o); }
  sel.value = String(state.histPos);
  $("back").disabled = state.histPos <= 0; $("fwd").disabled = state.histPos >= state.history.length - 1;
}
$("history").onchange = () => { const pos = +$("history").value; if (Number.isInteger(pos) && state.history[pos]) { state.histPos = pos; navigate(state.history[pos].space, state.history[pos].name, state.history[pos].args, true); } };
function goBack() { if (state.histPos > 0) { state.histPos--; const v = state.history[state.histPos]; navigate(v.space, v.name, v.args, true); } }
function goFwd() { if (state.histPos < state.history.length - 1) { state.histPos++; const v = state.history[state.histPos]; navigate(v.space, v.name, v.args, true); } }
$("back").onclick = goBack; $("fwd").onclick = goFwd;

async function inspect(v) {
  try { const p = await send("inspect", { space: v.space, name: v.name, args: v.args }); showPayload(p); }
  catch (e) { if (!/superseded/.test(e.message)) $("value").textContent = e.message; }
}
function argsText(args, pnames) {
  if (!args) return ""; if (pnames.length > 1 && pnames.length === args.length) return "(" + args.map((a, i) => `${pnames[i]}=${JSON.stringify(a)}`).join(", ") + ")";
  return "(" + args.map(a => JSON.stringify(a)).join(", ") + ")";
}
function showPayload(p) {
  state.lastPayload = p;
  if (state.editing && !(state.editTarget && state.editTarget.space === p.space && state.editTarget.name === p.name)) setEditing(false);
  const loc = p.space ? `${p.space}.${p.name}` : p.name;
  const editable = p.params !== "reference" && !!p.source;
  $("src-edit").classList.toggle("hidden", !editable || state.editing);
  $("edited-badge").classList.toggle("hidden", !p.edited);
  $("src-revert").classList.toggle("hidden", !p.edited || state.editing);
  const pnames = p.params && p.params !== "reference" ? p.params.split(",").map(s => s.trim()).filter(Boolean) : [];
  $("header").innerHTML = p.args ? `<b>${esc(loc)}(${esc(p.args.map(a => JSON.stringify(a)).join(", "))})</b> &nbsp; <i>${esc(loc)}(${esc(p.params)})</i>` : `<b>${esc(loc)}(${esc(p.params)})</b>`;
  $("args").value = "";
  const docHead = (p.doc || "").split("\n")[0].trim();
  const srcText = (docHead && !(p.source || "").includes(docHead) ? `"""${p.doc}"""\n\n` : "") + (p.source || "");
  const names = new Set(state.index.map(e => e.name));
  $("source").innerHTML = highlightPython(srcText, names);
  // clickable cell references inside the formula
  $("source").onclick = (e) => { const a = e.target.closest("a.tk-cell"); if (!a) return; e.preventDefault();
    const hit = state.index.find(x => x.name === a.dataset.cell && x.space === p.space) || state.index.find(x => x.name === a.dataset.cell);
    if (hit) navigate(hit.space, hit.name, null); };
  $("value").textContent = p.error ? p.error : (p.value_repr ?? (p.cached.length ? "(pick an invocation above to see its value)" : "(no cached value yet — not needed by the last computation, or it is still running)"));
  const inv = $("invocations"); inv.innerHTML = "";
  p.cached.forEach(a => { const o = document.createElement("option"); o.value = JSON.stringify(a); o.textContent = argsText(a, pnames); inv.appendChild(o); });
  if (p.args) { const key = JSON.stringify(p.args); if (![...inv.options].some(o => o.value === key)) { const o = document.createElement("option"); o.value = key; o.textContent = argsText(p.args, pnames) + "  — not cached"; inv.insertBefore(o, inv.firstChild); } inv.value = key; }
  else inv.selectedIndex = -1;
  inv.onchange = () => navigate(p.space, p.name, JSON.parse(inv.value));
  // multi-parameter cells: one discrete selector per parameter ([k ▾] [life ▾])
  const params = $("params"); params.innerHTML = "";
  const multi = pnames.length > 1 && p.cached.length && p.cached.every(a => a.length === pnames.length);
  inv.classList.toggle("hidden", !!multi); params.classList.toggle("hidden", !multi);
  if (multi) {
    const cachedSet = new Set(p.cached.map(a => JSON.stringify(a)));
    const cur = p.args || p.cached[0];
    const selects = pnames.map((pn, i) => {
      const lab = document.createElement("label"); const sp = document.createElement("span"); sp.textContent = pn + "="; const s = document.createElement("select");
      const vals = [...new Set(p.cached.map(a => JSON.stringify(a[i])))].sort((x, y) => { const a = JSON.parse(x), b = JSON.parse(y); return typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b)); });
      for (const v of vals) { const o = document.createElement("option"); o.value = v; o.textContent = JSON.parse(v); s.appendChild(o); }
      s.value = JSON.stringify(cur[i]); s.title = `cached values of parameter '${pn}'`;
      lab.appendChild(sp); lab.appendChild(s); params.appendChild(lab); return s;
    });
    const pick = () => {
      let args = selects.map(s => JSON.parse(s.value));
      if (!cachedSet.has(JSON.stringify(args))) {
        // combination not computed: snap to a cached tuple matching the non-first params
        const rest = JSON.stringify(args.slice(1));
        const alt = p.cached.find(a => JSON.stringify(a.slice(1)) === rest) || p.cached[0]; args = alt;
      }
      navigate(p.space, p.name, args);
    };
    selects.forEach(s => s.onchange = pick);
  }
  $("args").onkeydown = (e) => { if (e.key !== "Enter") return; const t = $("args").value.trim(); try { const a = t ? JSON.parse(`[${t}]`) : []; $("args").style.borderColor = ""; navigate(p.space, p.name, a); } catch { $("args").style.borderColor = "#d33"; $("args").title = "Use JSON literals, e.g. 12  or  12, 1"; } };
  renderValueChart(p, loc); renderGraph(p, loc);
}
function renderValueChart(p, loc) {
  const host = $("chart"); const call = p.args ? `${loc}(${p.args.map(a => JSON.stringify(a)).join(", ")})` : loc;
  if (p.value_data) {
    const f = p.value_data; const lines = frameLines(f);
    if (f.data.length === 1) { const row = f.data[0]; const labs = [], vals = []; f.columns.forEach((c, j) => { if (typeof row[j] === "number") { labs.push(c); vals.push(row[j]); } }); barChart(host, labs, vals, call); }
    else lineChart(host, lines, { title: call });
  } else if (p.series) {
    const lines = p.series.lines.map(l => ({ label: l.label, x: l.x, y: l.y }));
    let mark = null;
    if (p.args && typeof p.args[0] === "number") { const key = JSON.stringify(p.args.slice(1)); const li = p.series.lines.findIndex(l => JSON.stringify(l.key) === key); if (li >= 0) { const i = p.series.lines[li].x.indexOf(p.args[0]); if (i >= 0) mark = { x: p.args[0], y: p.series.lines[li].y[i], lineIdx: li }; } }
    lineChart(host, lines, { title: `${loc} over cached ${p.series.param}`, xlabel: p.series.param, mark });
  } else if (p.value_repr != null) {
    host.innerHTML = `<div class="scalar"><div><b>${esc(String(p.value_repr).split("\n")[0].slice(0, 40))}</b><div class="muted">${esc(call)}</div></div></div>`;
  } else host.innerHTML = `<div class="msg">no value — pick a cached invocation</div>`;
}
function sparkSVG(series, marks, frame, color = "#3a76b0", { rule = true } = {}) {
  // returns {svg, dots:[{x,y}] in %, span:bool} — the dot itself is a CSS overlay so it stays
  // round (the SVG is stretched with preserveAspectRatio=none). `rule:false` drops the
  // vertical red line at each mark (Formulas cards: the dot alone is enough)
  let lines = [];
  if (series && series.lines.length) lines = series.lines.map(l => ({ x: l.x, y: l.y, key: l.key }));
  else if (frame) lines = frameLines(frame).map(l => ({ x: l.x, y: l.y, key: null }));
  if (!lines.length) return null;
  const allx = lines.flatMap(l => l.x), ally = lines.flatMap(l => l.y).filter(Number.isFinite);
  const x0 = Math.min(...allx), x1 = Math.max(...allx) || x0 + 1;
  let y0 = Math.min(...ally), y1 = Math.max(...ally);
  if (y0 > 0) y0 = 0; else if (y1 < 0) y1 = 0;        // zero-baseline one-signed data
  const nx = x => 3 + 94 * ((x1 > x0) ? (x - x0) / (x1 - x0) : 0.5), ny = y => 3 + 22 * (1 - ((y1 > y0) ? (y - y0) / (y1 - y0) : 0.5));
  let s = `<svg viewBox="0 0 100 28" preserveAspectRatio="none" width="100%" height="100%">`;
  const ms = (marks || []).filter(m => m[0] >= x0 && m[0] <= x1);
  const span = ms.length > 3;
  if (span) { const f = ms.map(m => m[0]); s += `<rect x="${nx(Math.min(...f))}" y="0" width="${Math.max(1, nx(Math.max(...f)) - nx(Math.min(...f)))}" height="28" fill="#d62728" opacity=".12"/>`; }
  if (y0 <= 0 && 0 <= y1) s += `<line x1="0" x2="100" y1="${ny(0)}" y2="${ny(0)}" stroke="#bbb" stroke-width="0.6" vector-effect="non-scaling-stroke"/>`;
  lines.forEach((l, i) => s += `<path d="${stepPath(l.x, l.y, nx, ny)}" fill="none" stroke="${lines.length > 1 ? PALETTE[i % PALETTE.length] : color}" stroke-width="1.2" vector-effect="non-scaling-stroke"/>`);
  const dots = [];
  if (!span) for (const m of ms) {
    const key = JSON.stringify(m.slice(1)); const l = lines.find(l => JSON.stringify(l.key) === key) || lines[0];
    let i = l.x.findIndex(x => x >= m[0]); if (i < 0) i = l.x.length - 1;
    const X = nx(l.x[i]);
    if (rule) s += `<line x1="${X}" x2="${X}" y1="0" y2="28" stroke="#d62728" stroke-width="1" vector-effect="non-scaling-stroke" opacity=".7"/>`;
    dots.push({ x: X, y: ny(l.y[i]) / 28 * 100, value: l.y[i] });
  }
  return { svg: s + "</svg>", dots, span };
}
// what the card's marker means, in words: "t=11: 0.8986" / "t=0…120 · Σ 17,248"
function markCaption(g, sp, opts) {
  const param = g.series && g.series.param ? g.series.param : "t";
  const ms = g.marks || [];
  if (!ms.length) return opts.current ? "" : (g.value_repr ?? "");
  if (sp && sp.span) { const xs = ms.map(m => m[0]); return (g.consumed_sum != null ? `Σ ${fmtNum(g.consumed_sum, 5)}` : `×${ms.length}`) + ` · ${param} ${fmtNum(Math.min(...xs), 4)}…${fmtNum(Math.max(...xs), 4)}`; }
  return ms.map((m, i) => `${param}=${fmtNum(m[0], 4)}` + (sp && sp.dots[i] && Number.isFinite(sp.dots[i].value) ? `: ${fmtNum(sp.dots[i].value, 5)}` : "")).join("  ");
}
function card(g, opts = {}) {
  const el = document.createElement("div"); el.className = "card" + (g.kind === "ref" ? " ref" : "") + (opts.current ? " cur" : "") + (g.navigable === false ? " inert" : "");
  const title = g.space ? `${g.space}.${g.name}` : g.name;
  // short title: drop the space prefix when it's the one being viewed
  const shown = (g.space && state.current && g.space === state.current.space) ? g.name : title;
  const nInv = g.args_list ? g.args_list.length : 0;
  const sp = sparkSVG(g.series, g.marks, g.frame);
  const caption = markCaption(g, sp, opts);
  // no sparkline/frame -> the value itself, front and centre (also for the current card:
  // a no-arg cell like proj_len() or a scalar table ref would otherwise be title-only)
  const fallback = nInv > 1 ? (g.consumed_sum != null ? `Σ ${fmtNum(g.consumed_sum, 6)}` : `×${nInv}`) : (g.value_repr ?? "");
  let html = `<div class="t">${esc(opts.label || shown)}</div>`;
  if (sp) {
    html += `<div class="sp">${sp.svg}${sp.dots.map(d => `<span class="mk" style="left:${d.x}%;top:${d.y}%"></span>`).join("")}</div>`;
    if (caption) html += `<div class="cap">${esc(caption)}</div>`;
  } else if (fallback) html += `<span class="big">${esc(String(fallback).slice(0, 40))}</span>`;
  el.innerHTML = html;
  const where = opts.current ? "the invocation you are viewing" : (opts.side === "pred" ? "the invocation(s) of this cell that the current node read" : "the invocation(s) of this cell that read the current node");
  const tip = [title, caption ? `${where}: ${caption}` : (fallback ? `value: ${fallback}` : ""), g.series && g.series.lines.length > 1 ? `${g.series.lines.length} lines: ${g.series.lines.map(l => l.label).join(", ")}` : "", sp && sp.span ? "shaded span = the range of invocations consumed; Σ = the sum of those values (the amount that flowed in, when the values are additive)" : "", g.kind === "ref" ? "data reference (table)" : "", (!opts.current && g.navigable !== false) ? "click to navigate" : ""].filter(Boolean).join("\n");
  el.title = tip;
  if (!opts.current && g.navigable !== false) el.onclick = () => navigate(g.space, g.name, g.args_list && g.args_list.length === 1 ? g.args_list[0] : null);
  return el;
}
const MAX_SIDE = 6;
function renderGraph(p, loc) {
  const host = $("graph"); host.innerHTML = "";
  if (!p.preds.length && !p.succs.length) { host.classList.remove("dense"); host.innerHTML = `<div class="msg">no traced dependencies — pick a cached invocation (only nodes touched by the last computation are traceable)</div>`; return; }
  // many cards on a side: compact sparklines so all MAX_SIDE fit the column (Qt shrinks
  // its cards the same way); the columns still scroll (thin bar) if the window is short
  host.classList.toggle("dense", Math.max(p.preds.length, p.succs.length) >= 5);
  const col = (groups, side) => { const c = document.createElement("div"); c.className = "col"; groups.slice(0, MAX_SIDE).forEach(g => c.appendChild(card(g, { side })));
    if (groups.length > MAX_SIDE) { const m = document.createElement("div"); m.className = "muted small"; m.style.textAlign = "center"; m.textContent = `… +${groups.length - MAX_SIDE} more (search to reach them)`; c.appendChild(m); }
    // short window: the column scrolls — say so (overlay scrollbars are invisible until touched)
    const hint = document.createElement("div"); hint.className = "scrollhint"; hint.textContent = "▾"; hint.title = "more below — scroll the column"; c.appendChild(hint);
    const upd = () => c.classList.toggle("overflow", c.scrollHeight - c.clientHeight - c.scrollTop > 2);
    c.onscroll = upd; new ResizeObserver(upd).observe(c);                     // also fires when the tab becomes visible
    return c; };
  const arrows = (n) => { const a = document.createElement("div"); a.className = "arrows"; a.textContent = n ? "→" : ""; return a; };
  const call = p.args ? `${loc}(${p.args.map(a => JSON.stringify(a)).join(", ")})` : loc;
  const curFrame = p.value_data && p.value_data.data.length > 1 ? p.value_data : null;
  const curMarks = p.args && typeof p.args[0] === "number" ? [p.args] : [];
  const cur = card({ space: p.space, name: p.name, series: p.series, marks: curMarks, frame: curFrame, value_repr: (!p.series && !curFrame) ? (p.value_repr ?? "").split("\n")[0] : "", args_list: [] }, { current: true, label: call });
  const center = document.createElement("div"); center.className = "col"; center.appendChild(cur);
  host.appendChild(col(p.preds, "pred")); host.appendChild(arrows(p.preds.length)); host.appendChild(center); host.appendChild(arrows(p.succs.length)); host.appendChild(col(p.succs, "succ"));
  host.insertAdjacentHTML("beforeend", `<div class="title">precedents → node → dependents (click a card · hover for details)</div><div class="legend">red marker = the invocation involved — left: what this node read (e.g. t−1) · right: what read this node · centre: the one you're viewing<br>shaded span = many invocations, Σ = their sum · green = table</div>`);
}

// ---------------------------------------------------------- formulas view
// Every cell/table as a card, grouped by space. Click = select: the card
// turns yellow, what it reads (precedents) violet, what reads it (dependents)
// orange, everything else dims; the source shows on the right. Edges come
// from engine.cell_graph() — static, read off the formula sources, so this
// works for cells the last computation never touched. Selection follows the
// Inspector (navigate → fxSelect) and double-click/“Open in Inspector” goes back.
// "Show values" adds each computed cell's sparkline over t + its value at the
// chosen t (engine.cell_values(), refetched after every compute while the tab
// is open); the Inspector's invocation seeds t, and "Open in Inspector" traces
// exactly that invocation.
const fxKey = (space, name) => (space ? `${space}.${name}` : name);
function fxAdjacency() {
  const precs = new Map(), deps = new Map(), byKey = new Map();
  for (const e of state.index) byKey.set(fxKey(e.space, e.name), e);
  for (const [r, d] of (state.graph ? state.graph.edges : [])) {
    (precs.get(r) || precs.set(r, new Set()).get(r)).add(d);
    (deps.get(d) || deps.set(d, new Set()).get(d)).add(r);
  }
  state.fxAdj = { precs, deps, byKey }; return state.fxAdj;
}
function renderFormulas() {
  const host = $("fx-cards"); host.innerHTML = "";
  const q = $("fx-filter").value.trim().toLowerCase();
  fxAdjacency();
  const bySpace = new Map(); let n = 0;
  for (const e of state.index) {
    if (q && !`${e.space}.${e.name} ${e.doc}`.toLowerCase().includes(q)) continue;
    (bySpace.get(e.space) || bySpace.set(e.space, []).get(e.space)).push(e); n++;
  }
  for (const [space, entries] of [...bySpace].sort((a, b) => a[0].localeCompare(b[0]))) {
    const h = document.createElement("h4"); h.textContent = space || "(model)"; host.appendChild(h);
    const grid = document.createElement("div"); grid.className = "fx-grid";
    for (const e of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      const isRef = e.params === "reference";
      const el = document.createElement("div"); el.className = "card fx" + (isRef ? " ref" : ""); el.dataset.key = fxKey(e.space, e.name);
      el.innerHTML = `<div class="t">${esc(e.name)}${isRef ? "" : `(${esc(e.params)})`}${e.edited ? '<span class="edited-dot" title="formula edited">●</span>' : ""}</div><div class="v"></div><div class="d">${esc((e.doc || "").split("\n")[0])}</div>`;
      el.onclick = () => fxSelect(e.space, e.name);
      el.ondblclick = () => fxOpenInspector(e.space, e.name);
      grid.appendChild(el);
    }
    host.appendChild(grid);
  }
  $("fx-count").textContent = q ? `${n} of ${state.index.length}` : `${state.index.length} cells & tables · ${state.graph ? state.graph.edges.length : 0} edges`;
  fxPaint(); fxPaintValues();
}
function fxPaint() {
  const host = $("fx-cards"), key = state.fxSel; host.classList.toggle("has-sel", !!key);
  const adj = state.fxAdj || fxAdjacency();
  const P = (key && adj.precs.get(key)) || new Set(), D = (key && adj.deps.get(key)) || new Set();
  for (const el of host.querySelectorAll(".card.fx")) {
    const k = el.dataset.key, isP = P.has(k), isD = D.has(k), e = adj.byKey.get(k);
    el.classList.toggle("sel", k === key); el.classList.toggle("prec", isP); el.classList.toggle("dep", isD);
    el.title = k + (e && e.doc ? `\n${e.doc.split("\n")[0]}` : "") + (k === key ? "\nselected" : isP && isD ? `\nread by ${key} and reads it (mutual recursion, e.g. via t−1)` : isP ? `\nread by ${key} (precedent)` : isD ? `\nreads ${key} (dependent)` : "")
      + `\n${(adj.precs.get(k) || new Set()).size} reads · ${(adj.deps.get(k) || new Set()).size} read by\nclick to select · double-click to open in the Inspector`;
  }
}
// -- values at t ---------------------------------------------------------------
// value of a series at t: exact x match, else the last x ≤ t (step semantics), else null
function seriesAt(series, t) {
  if (!series || !series.lines.length) return null;
  const l = series.lines[0]; let i = l.x.indexOf(t);
  if (i < 0) { i = -1; for (let j = 0; j < l.x.length; j++) if (l.x[j] <= t) i = j; }
  return i >= 0 ? { x: l.x[i], y: l.y[i], exact: l.x[i] === t } : null;
}
function fxValueHTML(e, v, t) {
  // what the card shows under its title in "show values" mode
  if (!state.fxShowValues) return "";
  if (e.params === "reference") return `<div class="cap none">table</div>`;
  if (!v) return `<div class="cap none">not computed</div>`;
  if (v.series) {
    const at = seriesAt(v.series, t), param = v.series.param || "t";
    const sp = sparkSVG(v.series, at ? [[at.x]] : [], null, undefined, { rule: false });
    const cap = at ? `<span class="at">${param}=${fmtNum(t, 4)}:</span> <b>${fmtNum(at.y, 5)}</b>${at.exact ? "" : ` <span class="muted">(${param}=${fmtNum(at.x, 4)})</span>`}` : `<span class="muted">no value at ${param}=${fmtNum(t, 4)}</span>`;
    return (sp ? `<div class="sp">${sp.svg}${sp.dots.map(d => `<span class="mk" style="left:${d.x}%;top:${d.y}%"></span>`).join("")}</div>` : "") + `<div class="cap">${cap}</div>`;
  }
  if (v.frame) { const sp = sparkSVG(null, [], v.frame); return (sp ? `<div class="sp">${sp.svg}</div>` : "") + `<div class="cap">${esc(v.value_repr || "")}</div>`; }
  const args = v.args && v.args.length ? `<span class="muted">(${esc(v.args.map(a => JSON.stringify(a)).join(", "))}) = </span>` : "";
  return `<span class="big">${args}${esc(String(v.value_repr ?? "").slice(0, 40))}</span>`;
}
function fxPaintValues() {
  const on = state.fxShowValues, sec = $("tab-formulas");
  sec.classList.toggle("values", on); $("fx-values").checked = on; $("fx-tctl").classList.toggle("hidden", !on);
  const adj = state.fxAdj || fxAdjacency(), t = state.fxT ?? 0;
  for (const el of $("fx-cards").querySelectorAll(".card.fx")) {
    const e = adj.byKey.get(el.dataset.key);
    el.querySelector(".v").innerHTML = e ? fxValueHTML(e, state.fxValues && state.fxValues[el.dataset.key], t) : "";
  }
  if (state.fxSel && adj.byKey.get(state.fxSel)) fxSideValue(adj.byKey.get(state.fxSel));
}
// t range = union of the cached first-parameter values; seeded from the Inspector's invocation
function fxSetupT() {
  let lo = Infinity, hi = -Infinity;
  for (const v of Object.values(state.fxValues || {})) if (v.series) for (const l of v.series.lines) { lo = Math.min(lo, ...l.x); hi = Math.max(hi, ...l.x); }
  if (!Number.isFinite(lo)) { lo = 0; hi = 0; }
  const r = $("fx-trange"), n = $("fx-t"); r.min = n.min = lo; r.max = n.max = hi;
  if (state.fxT == null) { const a = state.current && state.current.args; state.fxT = a && typeof a[0] === "number" ? a[0] : lo; }
  state.fxT = Math.min(hi, Math.max(lo, state.fxT)); r.value = n.value = state.fxT;
}
function fxSetT(t, paint = true) {
  if (!Number.isFinite(t)) return;
  const r = $("fx-trange"); const lo = +r.min, hi = +r.max;
  state.fxT = Math.min(hi, Math.max(lo, Math.round(t))); r.value = $("fx-t").value = state.fxT;
  if (paint) fxPaintValues();
}
let _fxValSeq = 0;
async function fxFetchValues() {
  const seq = ++_fxValSeq;
  try {
    const v = await send("cell_values");
    if (seq !== _fxValSeq) return;                         // superseded by a later fetch
    state.fxValues = v; state.fxValuesStale = false; fxSetupT(); fxPaintValues();
  } catch (e) { setStatus(`cell values: ${e.message}`); }
}
// fetch only when it will be seen: values on + Formulas tab visible + stale
function fxMaybeFetchValues() {
  if (state.fxShowValues && state.fxValuesStale && $("tab-formulas").classList.contains("active") && state.loaded) fxFetchValues();
}
function fxToggleValues(on) {
  state.fxShowValues = on; try { localStorage.setItem("fxValues", on ? "1" : "0"); } catch { /* storage unavailable */ }
  fxPaintValues(); fxMaybeFetchValues();
}
function fxSideValue(e) {
  // header: "Projection.pols_if (t)  = 0.899 at t=12" when values are on
  const key = fxKey(e.space, e.name), isRef = e.params === "reference", v = state.fxShowValues && state.fxValues ? state.fxValues[key] : null;
  let val = "";
  if (v && v.series) { const at = seriesAt(v.series, state.fxT ?? 0); if (at) val = ` <span class="fx-val">= <b>${fmtNum(at.y, 6)}</b> at ${esc(v.series.param || "t")}=${fmtNum(at.x, 4)}</span>`; }
  else if (v && v.value_repr != null && !v.frame) val = ` <span class="fx-val">= <b>${esc(String(v.value_repr).slice(0, 40))}</b></span>`;
  $("fx-header").innerHTML = `<b>${esc(key)}</b> ${isRef ? "<i>table reference</i>" : `<i>(${esc(e.params || "")})</i>`}${val}`;
  const inv = fxInvocation(e, v);
  $("fx-inspect").textContent = inv ? `Open ${esc(e.name)}(${inv.map(a => JSON.stringify(a)).join(", ")}) in Inspector ↗` : "Open in Inspector ↗";
}
// the invocation "Open in Inspector" traces: (t,) when values are on and the cell is a plain t-series
function fxInvocation(e, v) {
  if (!state.fxShowValues || !v || !v.series || state.fxT == null) return null;
  const pn = (e.params || "").split(",").map(s => s.trim()).filter(Boolean);
  if (pn.length !== 1) return null;
  const at = seriesAt(v.series, state.fxT); return at ? [at.x] : null;
}
function fxSelect(space, name, opts = {}) {
  const key = fxKey(space, name); state.fxSel = key; fxPaint();
  const adj = state.fxAdj || fxAdjacency(), e = adj.byKey.get(key);
  if (!e) return;
  // selection history — same rules as the Inspector's: revisiting the current entry is a
  // no-op, a new selection truncates the forward branch; ◀ ▶ / Alt+←→ replay without pushing
  if (!opts.fromHistory) {
    const cur = state.fxHistory[state.fxHistPos];
    if (!(cur && cur.space === space && cur.name === name)) {
      state.fxHistory.splice(state.fxHistPos + 1); state.fxHistory.push({ space, name }); if (state.fxHistory.length > 200) state.fxHistory.shift(); state.fxHistPos = state.fxHistory.length - 1;
    }
  }
  fxSyncHistory();
  const card = $("fx-cards").querySelector(`.card.fx[data-key="${CSS.escape(key)}"]`);
  if (card && opts.scroll !== false) card.scrollIntoView({ block: "nearest" });
  const isRef = e.params === "reference";
  $("fx-header").className = "header";
  fxSideValue(e);
  $("fx-doc").textContent = (e.doc || "").split("\n")[0];
  const names = new Set(state.index.map(x => x.name));
  $("fx-source").innerHTML = e.source ? highlightPython(e.source, names)
    : `<span class="muted">${isRef ? "table reference — formulas read it directly; no source" : "no source"}</span>`;
  $("fx-source").onclick = (ev) => { const a = ev.target.closest("a.tk-cell"); if (!a) return; ev.preventDefault();
    const hit = state.index.find(x => x.name === a.dataset.cell && x.space === space) || state.index.find(x => x.name === a.dataset.cell);
    if (hit) fxSelect(hit.space, hit.name); };
  const chips = (host, keys) => { host.innerHTML = ""; if (!keys.size) { host.innerHTML = '<span class="none">none</span>'; return; }
    for (const k of [...keys].sort()) { const s = document.createElement("span"); s.textContent = k.startsWith(space + ".") ? k.slice(space.length + 1) : k; s.title = k;
      s.onclick = () => { const t = adj.byKey.get(k); if (t) fxSelect(t.space, t.name); }; host.appendChild(s); } };
  chips($("fx-precs"), adj.precs.get(key) || new Set()); chips($("fx-deps"), adj.deps.get(key) || new Set());
  $("fx-inspect").disabled = false; $("fx-inspect").onclick = () => fxOpenInspector(space, name);
}
function fxOpenInspector(space, name) {
  const adj = state.fxAdj || fxAdjacency(), e = adj.byKey.get(fxKey(space, name));
  const inv = e ? fxInvocation(e, state.fxValues && state.fxValues[fxKey(space, name)]) : null;
  navigate(space, name, inv); document.querySelector('.tabs button[data-tab="inspector"]').click();
}
// -- selection history (◀ ▶ + dropdown, newest first; the Inspector has the same trio) ------
function fxSyncHistory() {
  const sel = $("fx-history"); sel.innerHTML = "";
  if (!state.fxHistory.length) { const o = document.createElement("option"); o.textContent = "History — nothing selected yet"; sel.appendChild(o); }
  for (let pos = state.fxHistory.length - 1; pos >= 0; pos--) { const o = document.createElement("option"); o.value = pos; o.textContent = (pos === state.fxHistPos ? "▸ " : "   ") + visitLabel(state.fxHistory[pos]); sel.appendChild(o); }
  sel.value = String(state.fxHistPos);
  $("fx-back").disabled = state.fxHistPos <= 0; $("fx-fwd").disabled = state.fxHistPos >= state.fxHistory.length - 1;
}
function fxGoto(pos) { const v = state.fxHistory[pos]; if (!v) return; state.fxHistPos = pos; fxSelect(v.space, v.name, { fromHistory: true }); }
function fxBack() { if (state.fxHistPos > 0) fxGoto(state.fxHistPos - 1); }
function fxFwd() { if (state.fxHistPos < state.fxHistory.length - 1) fxGoto(state.fxHistPos + 1); }
$("fx-back").onclick = fxBack; $("fx-fwd").onclick = fxFwd;
$("fx-history").onchange = () => { const pos = +$("fx-history").value; if (Number.isInteger(pos)) fxGoto(pos); };
async function refreshGraph() {
  try { state.graph = await send("cell_graph"); renderFormulas(); if (state.fxSel) { const e = state.fxAdj.byKey.get(state.fxSel); if (e) fxSelect(e.space, e.name, { scroll: false }); } }
  catch (e) { setStatus(`formula graph: ${e.message}`); }
}
$("fx-filter").oninput = renderFormulas;
$("fx-values").onchange = () => fxToggleValues($("fx-values").checked);
$("fx-trange").oninput = () => fxSetT(+$("fx-trange").value);
$("fx-t").onchange = () => fxSetT(+$("fx-t").value);
$("fx-t").onkeydown = (e) => { if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); fxSetT(state.fxT + (e.key === "ArrowUp" ? 1 : -1)); } };

// -------------------------------------------------------------- formulas
// Edit → textarea over the source view; Apply → engine.set_formula (live
// model only — nothing on disk changes) → recompute; Revert → original.
function setEditing(on, source) {
  state.editing = on;
  state.editTarget = on && state.current ? { space: state.current.space, name: state.current.name } : null;
  $("source").classList.toggle("hidden", on);
  $("source-edit").classList.toggle("hidden", !on);
  $("src-apply").classList.toggle("hidden", !on);
  $("src-cancel").classList.toggle("hidden", !on);
  $("src-edit").classList.toggle("hidden", on || !(state.lastPayload && state.lastPayload.source));
  $("src-revert").classList.toggle("hidden", on || !(state.lastPayload && state.lastPayload.edited));
  $("src-err").classList.add("hidden");
  if (on) { const ta = $("source-edit"); ta.value = source ?? ""; ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
}
function showFormulaError(text) {
  const last = (String(text).split("\n").map(t => t.trim()).filter(Boolean).pop() || String(text)).replace(/^(RuntimeError|Error): /, "");
  const el = $("src-err"); el.textContent = last; el.classList.remove("hidden");
}
async function applyFormula() {
  const p = state.lastPayload; if (!p || !state.editing) return;
  const source = $("source-edit").value;
  $("src-apply").disabled = true; setBusy("applying formula …");
  try {
    const np = await send("set_formula", { space: p.space, name: p.name, source });
    afterFormulaChange(np);
  } catch (e) { showFormulaError(e.message); }
  finally { $("src-apply").disabled = false; setBusy(null); }
}
async function revertFormula() {
  const p = state.lastPayload; if (!p) return;
  setBusy("restoring formula …");
  try { afterFormulaChange(await send("reset_formula", { space: p.space, name: p.name })); }
  catch (e) { showError(e.message); } finally { setBusy(null); }
}
function afterFormulaChange(np) {
  const key = `${np.space}.${np.name}`;
  if (np.edited) state.formulaEdits.set(key, np.source); else state.formulaEdits.delete(key);
  const e = state.index.find(x => x.space === np.space && x.name === np.name);
  if (e) { e.edited = !!np.edited; e.source = np.source || e.source; }
  setEditing(false); showPayload(np); renderResults(); updateEditCount();
  refreshGraph();                // the edit may have changed what this cell reads
  requestCompute(true);          // dependents are invalid now; the compute refreshes the inspector
}
function updateEditCount() {
  const n = state.formulaEdits.size;
  $("export-model").textContent = n ? "Export model… ●" : "Export model…";
  $("export-model").title = (n ? `${n} formula edit${n > 1 ? "s" : ""} in this session. ` : "")
    + "Download the model as a modelx zip — with your formula edits and the current model point table";
  $("export-model").classList.toggle("active", n > 0);
}
$("src-edit").onclick = () => { const p = state.lastPayload; if (p && p.source) setEditing(true, p.source); };
$("src-cancel").onclick = () => setEditing(false);
$("src-apply").onclick = applyFormula;
$("src-revert").onclick = revertFormula;
$("source-edit").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); applyFormula(); }
  else if (e.key === "Escape") { e.preventDefault(); setEditing(false); }
  else if (e.key === "Tab") { e.preventDefault(); const ta = e.target, a = ta.selectionStart, b = ta.selectionEnd; ta.value = ta.value.slice(0, a) + "    " + ta.value.slice(b); ta.setSelectionRange(a + 4, a + 4); }
});
window.addEventListener("beforeunload", (e) => { if (state.formulaEdits.size) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------- model export/load
$("export-model").onclick = async () => {
  if (!state.loaded) return;
  setBusy("exporting model …");
  try {
    const buf = await send("export_model");
    const n = state.formulaEdits.size;
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([buf], { type: "application/zip" }));
    a.download = `${state.loaded.name}${n ? "-edited" : ""}.zip`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    setStatus(`Exported ${state.loaded.name} (${Math.round(buf.byteLength / 1024)} KB, ${n} formula edit${n === 1 ? "" : "s"})`);
    requestCompute(true);        // export rebinds the table ref; cached nodes need a recompute
  } catch (e) { showError(e.message); } finally { setBusy(null); }
};
$("load-model").onclick = () => { if (confirmDiscardEdits()) $("model-file").click(); };
$("model-file").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return;
  const buf = await f.arrayBuffer();
  let name = f.name.replace(/\.zip$/i, "").replace(/[^A-Za-z0-9_.-]+/g, "_") || "model";
  if (state.userModels.some(u => u.name === name)) { let i = 2; while (state.userModels.some(u => u.name === `${name}_${i}`)) i++; name = `${name}_${i}`; }
  await loadModelBytes(name, buf);
};

// -------------------------------------------------------------- data mgmt
$("gen").onclick = async () => { const n = parseInt(prompt("Number of synthetic points to append (bootstrapped from the current table):", "100") || "", 10); if (!n) return;
  try { const t = await send("generate", { n }); onPoints(t); } catch (e) { showError(e.message); } };
$("export").onclick = () => { if (!state.table) return; const t = state.table; const csv = [[t.index_name || "id", ...t.columns].join(","), ...t.data.map((r, i) => [t.index[i], ...r].map(v => v === null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v).join(","))].join("\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = `${state.loaded.name}_points.csv`; a.click(); };
$("import").onclick = () => $("import-file").click();
$("import-file").onchange = async (e) => { const f = e.target.files[0]; if (!f) return; const text = await f.text(); const rows = text.trim().split(/\r?\n/).map(l => l.split(","));
  const hdr = rows.shift(); const idxName = state.table.index_name || "id"; const ii = hdr.indexOf(idxName);
  const cols = hdr.filter((_, j) => j !== ii); const coerce = v => v === "" ? null : (isNaN(+v) ? v : +v);
  const frame = { columns: cols, index: rows.map(r => ii >= 0 ? coerce(r[ii]) : null), index_name: idxName, data: rows.map(r => r.filter((_, j) => j !== ii).map(coerce)) };
  if (ii < 0) frame.index = rows.map((_, i) => i + 1);
  try { const t = await send("set_points", { frame }); onPoints(t); } catch (err) { showError(err.message); } e.target.value = ""; };
function onPoints(t) { const cur = $("point").options[$("point").selectedIndex]?.textContent; state.table = t; fillPoints(); buildFields(); const pos = t.index.findIndex(i => String(i) === cur); selectPoint(pos >= 0 ? pos : 0); setStatus(`Model point table: ${t.index.length} points`); }

// ---------------------------------------------------------------- mobile
const isMobile = () => matchMedia("(max-width: 900px), (pointer: coarse) and (max-width: 1100px)").matches;
function setPanel(open) {
  $("app").classList.toggle("panel-open", open);
  $("panel-toggle").setAttribute("aria-expanded", String(open));
  $("panel-toggle").textContent = open ? "✕ Close" : "☰ Model";
  if (open) setFields(false);
  if (!open && state.lastCompute) renderResultCharts(state.lastCompute);
}
function setFields(open) {
  $("app").classList.toggle("fields-open", open);
  $("fields-toggle").setAttribute("aria-expanded", String(open));
  $("fields-toggle").textContent = open ? "Fields ▾" : "Fields ▴";
  $("fields-toggle").classList.toggle("active", open);
}
$("panel-toggle").onclick = () => setPanel(!$("app").classList.contains("panel-open"));
$("fields-toggle").onclick = () => { if ($("app").classList.contains("panel-open")) setPanel(false); setFields(!$("app").classList.contains("fields-open")); };
$("sheet-close").onclick = () => setFields(false);
// first load on a phone: show the sliders straight away
let _fieldsAutoOpened = false;
function setResultsOpen(open) { document.querySelector(".insp-body").classList.toggle("results-open", open && isMobile()); }
$("search").addEventListener("focus", () => setResultsOpen(true));
$("search").addEventListener("input", () => setResultsOpen(true));
document.addEventListener("click", (e) => { if (!e.target.closest("#results, #search")) setResultsOpen(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setResultsOpen(false); });
// mobile summary: one-line headline next to the panel toggle
const _renderSummary = renderSummary;
renderSummary = function (res) {
  _renderSummary(res);
  const pv = pvRow(res.result_pv); const net = pv && Object.entries(pv).find(([k]) => /net/i.test(k));
  $("mobile-summary").textContent = `pt ${res.point_label}` + (net ? ` · ${net[0].replace(/^PV /, "").replace("Cashflow", "CF")} ${fmtNum(net[1], 6)}` : "") + (isDirty() ? " ●" : "");
};

// --------------------------------------------------------------- tabs/keys
for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => { for (const x of document.querySelectorAll(".tabs button")) x.classList.toggle("active", x === b); for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.id === "tab-" + b.dataset.tab);
  $("app").classList.toggle("tab-about", b.dataset.tab === "about");   // About: the fields sheet is irrelevant (mobile)
  if (state.lastCompute) renderResultCharts(state.lastCompute);
  if (b.dataset.tab === "inspector" && state.lastPayload) { const p = state.lastPayload; renderValueChart(p, p.space ? `${p.space}.${p.name}` : p.name); }   // was sized while hidden
  if (b.dataset.tab === "formulas") { fxMaybeFetchValues(); const c = $("fx-cards").querySelector(".card.fx.sel"); if (c) c.scrollIntoView({ block: "center" }); } };
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); document.querySelector('.tabs button[data-tab="inspector"]').click(); $("search").focus(); $("search").select(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r") { e.preventDefault(); if (!$("reset").disabled) $("reset").click(); }
  // history keys drive the open tab: the Formulas selection there, the Inspector visit elsewhere
  const fxOpen = () => $("tab-formulas").classList.contains("active");
  if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); if (fxOpen()) fxBack(); else goBack(); }
  if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); if (fxOpen()) fxFwd(); else goFwd(); }
});
// debug / test hook (browser_test.mjs, playwright-cli): peek at state, simulate a Pyodide fatal error
window.__playground = { state, simulateCrash: () => worker.postMessage({ cmd: "_crash" }) };
let _rt = null;
window.addEventListener("resize", () => { clearTimeout(_rt); _rt = setTimeout(() => {
  if (state.lastCompute) renderResultCharts(state.lastCompute);
  if (state.lastPayload) { const p = state.lastPayload; renderValueChart(p, p.space ? `${p.space}.${p.name}` : p.name); }
}, 120); });
})();
