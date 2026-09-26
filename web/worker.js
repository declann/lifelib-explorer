// Pyodide web worker: owns the ModelSession (same execution model as the
// desktop QThread worker). Messages: {seq, cmd, ...}; replies: {seq, cmd, ok,
// payload | error}. Requests carry a `seq`; the main thread posts the latest
// seq per command in `latest` so stale queued work is skipped.

// This is a *module* worker (`new Worker(url, {type: "module"})`): Pyodide
// ≥ v0.28 / v314 no longer supports classic workers (`importScripts` fails
// with "Classic web workers are not supported", surfaced as a NetworkError).
//
// Runtime location: the vendored copy under ./pyodide/ (built by
// build_models.py from the npm package) is preferred so the site works with
// no CDN access (ad-blockers / corporate proxies commonly block jsDelivr);
// the CDN of the same version is the fallback. Python 3.14 / pandas 3.0 —
// the exact stack the models were validated on.
const LOCAL_URL = new URL("pyodide/", self.location.href).href;
const CDN_URL = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";
let PYODIDE_URL = LOCAL_URL;

async function importPyodide() {
  try {
    return (await import(LOCAL_URL + "pyodide.mjs")).loadPyodide;
  } catch (e) {
    self.postMessage({ cmd: "status", text: "vendored runtime not found — trying CDN …" });
    PYODIDE_URL = CDN_URL;
    return (await import(CDN_URL + "pyodide.mjs")).loadPyodide;
  }
}

let py = null;
let session = null;          // Python ModelSession proxy
let loadedName = null;
let bootError = null;        // boot() failed: every request is answered with this
let crashed = null;          // Pyodide suffered a fatal error: text of the cause
const latest = { compute: 0, inspect: 0 };

// Pyodide "fatal errors" (WASM stack overflow, heap exhaustion, memory
// corruption) poison the whole runtime: every later `pyodide.*` access throws
// "Pyodide already fatally failed and can no longer be used." The only cure is
// a fresh worker. Pyodide prints the *cause* to the console and calls
// API.on_fatal — we hook that so the page can flash it and restart us. The
// heap size at the time tells an OOM apart from a stack overflow.
function onFatal(e) {
  if (crashed) return;
  let heapMB = null;
  try { heapMB = Math.round(py._module.HEAPU8.byteLength / 1048576); } catch { /* module gone */ }
  crashed = String(e?.message || e || "unknown fatal error").split("\n")[0].slice(0, 300);
  self.postMessage({ cmd: "crashed", error: crashed, stack: String(e?.stack || "").slice(0, 4000), heapMB });
}
const isFatal = (e) => !!(e && (e.pyodide_fatal_error || /fatally failed|fatal error/i.test(String(e.message || e))));

/** fetch a same-origin site file; fail with URL + status instead of letting an
 *  HTML 404 page (or an empty body) masquerade as the file */
async function fetchOk(rel, opts = {}) {
  const url = new URL(rel, self.location.href).href;
  const r = await fetch(url, { cache: "no-cache", ...opts });
  if (!r.ok) {
    throw new Error(`${url} → HTTP ${r.status}. Are you serving web/dist/ (the built site)? ` +
                    `Build it with: uv run python web/build_models.py`);
  }
  return r;
}
const fetchText = async (rel) => (await fetchOk(rel)).text();

function post(seq, cmd, ok, payloadOrError) {
  self.postMessage(ok ? { seq, cmd, ok, payload: payloadOrError }
                      : { seq, cmd, ok, error: String(payloadOrError) });
}
function status(text) { self.postMessage({ cmd: "status", text }); }

async function boot() {
  status("loading Python runtime …");
  const loadPyodide = await importPyodide();
  py = await loadPyodide({ indexURL: PYODIDE_URL });
  try { py._api.on_fatal = onFatal; } catch { /* private API; the catch in onmessage still detects it */ }
  status("loading numpy / pandas …");
  await py.loadPackage(["numpy", "pandas", "micropip"]);
  status("installing modelx …");
  const micropip = py.pyimport("micropip");
  // prefer vendored wheels (pyodide/wheels/*.whl, listed in wheels.json);
  // fall back to PyPI when they aren't there
  let installed = false;
  if (PYODIDE_URL === LOCAL_URL) {
    try {
      const list = await (await fetchOk("pyodide/wheels.json")).json();
      if (Array.isArray(list) && list.length) {
        // the list is dependency-complete (modelx, networkx, openpyxl,
        // et_xmlfile), so nothing needs PyPI
        await micropip.install(list.map(w => LOCAL_URL + "wheels/" + w));
        installed = true;
      }
    } catch (e) { /* fall through to PyPI */ }
  }
  if (!installed) await micropip.install(["modelx", "openpyxl"]);
  const core = await fetchText("engine_core.py");
  if (!/class ModelSession\b/.test(core)) {
    throw new Error(
      `engine_core.py served from ${new URL("engine_core.py", self.location.href)} is not the ` +
      `engine (${core.length} bytes, starts "${core.slice(0, 40).replace(/\s+/g, " ")}"). ` +
      `Serve the *built* site — web/dist/ — not web/. Build with: uv run python web/build_models.py`);
  }
  py.FS.writeFile("/engine_core.py", core);
  py.runPython(`
import sys, json, zipfile, io
sys.path.insert(0, "/")
import pandas as pd
from engine_core import (ModelSession, loaded_json, compute_json,
                         inspect_json, frame_json, generate_model_points,
                         extract_model_zip, cell_values_json)
from pathlib import Path
_session = ModelSession()

def _extract(name, data):
    # zip layout: model/… plus sibling input CSVs at the root, so that
    # _model.path.parent (used by Reference Liability Models) holds them
    return str(extract_model_zip(bytes(data), Path(f"/models/{name}")))

def _load(path):
    return json.dumps(loaded_json(_session.load(path)))

def _compute(row_pos, edits_json):
    r = _session.compute(int(row_pos), json.loads(edits_json))
    return json.dumps(compute_json(r)) if r is not None else "null"

def _inspect(space, name, args_json):
    args = json.loads(args_json)
    return json.dumps(inspect_json(_session.inspect(space, name,
                                                    tuple(args) if args is not None else None)))

def _set_points(frame_json_str):
    f = json.loads(frame_json_str)
    df = pd.DataFrame(f["data"], columns=f["columns"])
    df.index = pd.Index(f["index"], name=f.get("index_name"))
    return json.dumps(frame_json(_session.set_points(df)))

def _generate(n):
    t = _session.model_point_table
    new = generate_model_points(t, int(n))
    return json.dumps(frame_json(_session.set_points(pd.concat([t, new]))))

def _set_formula(space, name, source):
    return json.dumps(inspect_json(_session.set_formula(space, name, source)))

def _reset_formula(space, name):
    return json.dumps(inspect_json(_session.reset_formula(space, name)))

def _cell_graph():
    return json.dumps(_session.cell_graph())

def _cell_values():
    return json.dumps(cell_values_json(_session.cell_values()))

def _export_model():
    return _session.export_model()          # bytes -> JsProxy of a Uint8Array view

def _load_bytes(name, data):
    # user-supplied zip: extract under /models/user/<name>, then load
    info = _session.load_bytes(name, bytes(data))
    return json.dumps(loaded_json(info))
`);
  self.postMessage({ cmd: "ready", pyodide: py.version,
                     pandas: py.runPython("pd.__version__"),
                     modelx: py.runPython("import modelx; modelx.__version__") });
}

const ready = boot().catch(e => { bootError = String(e); self.postMessage({ cmd: "fatal", error: bootError }); });

async function ensureModel(name) {
  if (loadedName === name) return;
  status(`fetching ${name} …`);
  const buf = await (await fetchOk("models/" + name + ".zip")).arrayBuffer();
  const extract = py.globals.get("_extract");
  const path = extract(name, new Uint8Array(buf));
  extract.destroy();
  status(`loading ${name} into modelx …`);
  const load = py.globals.get("_load");
  const info = JSON.parse(load(path));
  load.destroy();
  loadedName = name;
  return info;
}

self.onmessage = async (ev) => {
  const m = ev.data;
  if (m.cmd === "latest") { Object.assign(latest, m.latest); return; }
  await ready;
  if (crashed) { post(m.seq, m.cmd, false, `Python runtime crashed (${crashed}) — restarting`); return; }
  if (!py || bootError) { post(m.seq, m.cmd, false, `Python runtime failed to start: ${bootError || "unknown"}`); return; }
  try {
    switch (m.cmd) {
      case "load": {
        latest.compute = m.seq; latest.inspect = m.seq;
        loadedName = null;
        const info = await ensureModel(m.name);
        post(m.seq, "load", true, info);
        break;
      }
      case "compute": {
        if (m.seq < latest.compute) return;        // superseded
        const fn = py.globals.get("_compute");
        const out = fn(m.rowPos, JSON.stringify(m.edits));
        fn.destroy();
        if (m.seq < latest.compute) return;        // superseded while running
        post(m.seq, "compute", true, JSON.parse(out));
        break;
      }
      case "inspect": {
        if (m.seq < latest.inspect) return;
        const fn = py.globals.get("_inspect");
        const out = fn(m.space, m.name, JSON.stringify(m.args ?? null));
        fn.destroy();
        if (m.seq < latest.inspect) return;
        post(m.seq, "inspect", true, JSON.parse(out));
        break;
      }
      case "set_points": {
        const fn = py.globals.get("_set_points");
        const out = fn(JSON.stringify(m.frame));
        fn.destroy();
        post(m.seq, "set_points", true, JSON.parse(out));
        break;
      }
      case "generate": {
        const fn = py.globals.get("_generate");
        const out = fn(m.n);
        fn.destroy();
        post(m.seq, "set_points", true, JSON.parse(out));
        break;
      }
      case "set_formula": {
        latest.inspect = m.seq;                    // the edit supersedes pending inspects
        const fn = py.globals.get("_set_formula");
        const out = fn(m.space, m.name, m.source);
        fn.destroy();
        post(m.seq, "set_formula", true, JSON.parse(out));
        break;
      }
      case "reset_formula": {
        latest.inspect = m.seq;
        const fn = py.globals.get("_reset_formula");
        const out = fn(m.space, m.name);
        fn.destroy();
        post(m.seq, "reset_formula", true, JSON.parse(out));
        break;
      }
      case "cell_graph": {
        const fn = py.globals.get("_cell_graph");
        const out = fn();
        fn.destroy();
        post(m.seq, "cell_graph", true, JSON.parse(out));
        break;
      }
      case "cell_values": {
        const fn = py.globals.get("_cell_values");
        const out = fn();
        fn.destroy();
        post(m.seq, "cell_values", true, JSON.parse(out));
        break;
      }
      case "export_model": {
        const fn = py.globals.get("_export_model");
        const proxy = fn();
        fn.destroy();
        const bytes = proxy.toJs();                // copy out of the WASM heap
        proxy.destroy();
        const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        self.postMessage({ seq: m.seq, cmd: "export_model", ok: true, payload: buf }, [buf]);
        break;
      }
      case "_crash": {
        // test hook (browser_test.mjs): trigger Pyodide's real fatal-error path
        // so the page's crash recovery is exercised end to end
        try { py._api.fatal_error(new Error("simulated fatal error (test hook)")); }
        catch (e) { onFatal(e); }
        if (!crashed) onFatal(new Error("simulated fatal error (test hook)"));
        break;
      }
      case "load_bytes": {
        // a user model: same reset semantics as "load"
        latest.compute = m.seq; latest.inspect = m.seq;
        loadedName = null;
        status(`loading ${m.name} into modelx …`);
        const fn = py.globals.get("_load_bytes");
        const out = fn(m.name, new Uint8Array(m.buf));
        fn.destroy();
        const info = JSON.parse(out);
        loadedName = "user:" + info.name;
        post(m.seq, "load", true, info);
        break;
      }
    }
  } catch (e) {
    if (isFatal(e)) onFatal(e);              // in case on_fatal did not fire
    post(m.seq, m.cmd, false, e?.message ?? e);
  }
};
