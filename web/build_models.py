"""Package the supported lifelib models for the web app.

For each supported model directory this writes ``web/dist/models/<name>.zip``
(the serialized modelx model: ``_system.json``, space modules, ``.xlsx``/
``.csv`` inputs, ``_data/``) and a ``manifest.json`` the picker reads.

Also copies the pure-Python engine (``engine_core.py``) next to the worker so
the site is fully static.

Usage:  uv run python web/build_models.py [--out web/dist]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lifelib_explorer.engine_core import (  # noqa: E402
    CATEGORY_ORDER,
    discover_models,
    library_category,
    unsupported_reason,
    zip_model_dir,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "web" / "dist"))
    ap.add_argument("--libraries", default=str(ROOT / "lifelib" / "lifelib" / "libraries"))
    ap.add_argument("--only", nargs="*", help="model names to include")
    ap.add_argument(
        "--no-runtime", action="store_true", help="skip vendoring Pyodide + wheels (CDN/PyPI only)"
    )
    args = ap.parse_args()

    out = Path(args.out)
    models_out = out / "models"
    models_out.mkdir(parents=True, exist_ok=True)

    manifest = []
    for info in discover_models(Path(args.libraries)):
        if unsupported_reason(info):
            continue
        if args.only and info.name not in args.only:
            continue
        # Past Libraries are all named "model" — key zips by library then
        key = info.name if info.name != "model" else info.library
        zpath = models_out / f"{key}.zip"
        zip_model_dir(Path(info.path), zpath)
        size = zpath.stat().st_size
        manifest.append(
            {
                "name": key,
                "library": info.library,
                "label": info.label,
                "category": library_category(info),
                "zip": f"models/{key}.zip",
                "bytes": size,
            }
        )
        print(f"  {info.label:45s} {size / 1024:7.0f} KB")

    (out / "manifest.json").write_text(
        json.dumps(
            {
                "models": manifest,
                "categories": list(CATEGORY_ORDER),
                "default": "BasicTerm_S",
                "license": "lifelib models: MIT (c) lifelib authors — see https://lifelib.io",
            },
            indent=1,
        )
    )
    # the engine core is the worker's Python payload
    shutil.copy(ROOT / "lifelib_explorer" / "engine_core.py", out / "engine_core.py")
    # static frontend files (+ the About photo, shared with the desktop app)
    for name in ("index.html", "styles.css", "app.js", "worker.js"):
        src = ROOT / "web" / name
        if src.exists():
            shutil.copy(src, out / name)
    shutil.copy(ROOT / "lifelib_explorer" / "assets" / "dec.jpg", out / "dec.jpg")
    total = sum(m["bytes"] for m in manifest)
    print(f"\n{len(manifest)} models, {total / 1024 / 1024:.1f} MB total -> {out}")
    if not args.no_runtime:
        vendor_runtime(out)
    return 0


#: Pyodide packages the app needs at run time, by *lock* name: the worker
#: loadPackage()s numpy/pandas/micropip and micropip then resolves modelx's
#: binary deps (libcst → pyyaml, asttokens) against pyodide-lock.json. The
#: distribution also has matplotlib, pillow, … which we skip to keep dist
#: small (networkx/openpyxl/et_xmlfile come from PyPI into pyodide/wheels/).
RUNTIME_KEEP = {
    "numpy",
    "pandas",
    "micropip",
    "networkx",
    "packaging",
    "python-dateutil",
    "pytz",
    "six",
    "tzdata",
    # modelx deps that ship with the Pyodide distribution
    "asttokens",
    "libcst",
    "pyyaml",
}
RUNTIME_FILES = (
    "pyodide.js",
    "pyodide.asm.js",
    "pyodide.asm.mjs",
    "pyodide.asm.wasm",
    "pyodide.mjs",
    "python_stdlib.zip",
    "pyodide-lock.json",
)
#: same CDN (and therefore the same build) the worker falls back to
CDN = "https://cdn.jsdelivr.net/pyodide/v{v}/full/"


def vendor_runtime(out: Path) -> None:
    """Copy the Pyodide runtime from web/node_modules, fetch the wheels it
    needs, and download the pure-Python wheels micropip would otherwise pull
    from PyPI, so the site works with no CDN access at *run* time. Falls back
    gracefully if npm wasn't run."""
    import subprocess

    src = ROOT / "web" / "node_modules" / "pyodide"
    dst = out / "pyodide"
    if not src.exists():
        print(
            "\n(no web/node_modules/pyodide — run `npm install` in web/ to "
            "vendor the runtime; the site will use the CDN)"
        )
        return
    dst.mkdir(exist_ok=True)
    n = 0
    for p in src.iterdir():
        if p.name in RUNTIME_FILES:
            shutil.copy(p, dst / p.name)
            n += 1
    n += vendor_wheels(src, dst)
    # extra wheels: modelx (+ its deps not in the Pyodide distribution)
    wheels = dst / "wheels"
    wheels.mkdir(exist_ok=True)
    pkgs = ["modelx", "openpyxl", "et_xmlfile", "networkx"]
    dl = [
        "-m",
        "pip",
        "download",
        "--quiet",
        "--only-binary=:all:",
        "--no-deps",
        "-d",
        str(wheels),
        *pkgs,
    ]
    # uv-managed venvs have no pip; borrow one for the download
    cmds = ([["uv", "run", "--with", "pip", "python", *dl]] if shutil.which("uv") else []) + [
        [sys.executable, *dl]
    ]
    err: Exception | None = None
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            break
        except Exception as exc:
            err = exc
    else:  # offline / no pip: micropip falls back to PyPI at run time
        print(f"(could not download wheels: {err}; micropip will use PyPI)")
    have = sorted(p.name for p in wheels.glob("*.whl"))
    (dst / "wheels.json").write_text(json.dumps(have))
    size = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file())
    print(f"runtime vendored: {n} files + {len(have)} wheels, {size / 1024 / 1024:.0f} MB -> {dst}")


def vendor_wheels(src: Path, dst: Path) -> int:
    """Put the ``RUNTIME_KEEP`` wheels next to the runtime in *dst*.

    The npm ``pyodide`` package ships the *core* runtime only (14 files, no
    wheels), so every wheel named in the vendored ``pyodide-lock.json`` is
    taken from the first source that has it: ``node_modules/pyodide`` (older
    full distributions did carry them), ``web/.wheel-cache/<version>/``, or
    the CDN of the exact same version — checked against the lock's sha256.
    Raises if a wheel can't be obtained: a runtime with no ``micropip``/
    ``numpy``/``pandas`` wheel boots to "No module named 'micropip'", and a
    build that looks fine is worse than one that fails.
    """
    import hashlib
    import urllib.request

    lock = json.loads((dst / "pyodide-lock.json").read_text())["packages"]
    version = json.loads((src / "package.json").read_text())["version"]
    cache = ROOT / "web" / ".wheel-cache" / version
    cache.mkdir(parents=True, exist_ok=True)
    missing = sorted(RUNTIME_KEEP - set(lock))
    if missing:  # a Pyodide bump renamed/dropped a package
        raise RuntimeError(f"not in pyodide-lock.json ({version}): {missing}")

    n, fetched = 0, 0
    for name in sorted(RUNTIME_KEEP):
        pkg = lock[name]
        fname, want = pkg["file_name"], pkg["sha256"]
        target = dst / fname
        for cand in (src / fname, cache / fname):
            if cand.is_file() and hashlib.sha256(cand.read_bytes()).hexdigest() == want:
                shutil.copy(cand, target)
                break
        else:
            url = CDN.format(v=version) + fname
            with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (https, fixed host)
                blob = r.read()
            got = hashlib.sha256(blob).hexdigest()
            if got != want:
                raise RuntimeError(f"{url}: sha256 {got} != {want} in pyodide-lock.json")
            (cache / fname).write_bytes(blob)
            target.write_bytes(blob)
            fetched += 1
        n += 1
    print(f"  wheels: {n} vendored ({fetched} downloaded from the CDN, rest cached)")
    return n


if __name__ == "__main__":
    raise SystemExit(main())
