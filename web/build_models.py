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
    ap.add_argument("--libraries",
                    default=str(ROOT / "lifelib" / "lifelib" / "libraries"))
    ap.add_argument("--only", nargs="*", help="model names to include")
    ap.add_argument("--no-runtime", action="store_true",
                    help="skip vendoring Pyodide + wheels (CDN/PyPI only)")
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
        manifest.append({
            "name": key,
            "library": info.library,
            "label": info.label,
            "category": library_category(info),
            "zip": f"models/{key}.zip",
            "bytes": size,
        })
        print(f"  {info.label:45s} {size / 1024:7.0f} KB")

    (out / "manifest.json").write_text(json.dumps(
        {"models": manifest,
         "categories": list(CATEGORY_ORDER),
         "default": "BasicTerm_S",
         "license": "lifelib models: MIT (c) lifelib authors — see "
                    "https://lifelib.io"},
        indent=1))
    # the engine core is the worker's Python payload
    shutil.copy(ROOT / "lifelib_explorer" / "engine_core.py",
                out / "engine_core.py")
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


#: Pyodide wheels the app needs (the npm package also ships matplotlib,
#: pillow, … which we skip to keep dist small).
RUNTIME_KEEP = {"numpy", "pandas", "micropip", "networkx", "packaging",
                "python_dateutil", "pytz", "six", "tzdata", "openpyxl", "et_xmlfile",
                # modelx deps that ship with the Pyodide distribution
                "asttokens", "libcst", "pyyaml"}
RUNTIME_FILES = ("pyodide.js", "pyodide.asm.js", "pyodide.asm.mjs", "pyodide.asm.wasm",
                 "pyodide.mjs", "python_stdlib.zip", "pyodide-lock.json")


def vendor_runtime(out: Path) -> None:
    """Copy the Pyodide runtime from web/node_modules and download the
    pure-Python wheels micropip would otherwise fetch from PyPI, so the site
    works with no CDN access. Falls back gracefully if npm wasn't run."""
    import subprocess

    src = ROOT / "web" / "node_modules" / "pyodide"
    dst = out / "pyodide"
    if not src.exists():
        print("\n(no web/node_modules/pyodide — run `npm install` in web/ to "
              "vendor the runtime; the site will use the CDN)")
        return
    dst.mkdir(exist_ok=True)
    n = 0
    for p in src.iterdir():
        if p.name in RUNTIME_FILES or (
                p.suffix == ".whl" and p.name.split("-")[0].lower() in RUNTIME_KEEP):
            shutil.copy(p, dst / p.name)
            n += 1
    # extra wheels: modelx (+ its deps not in the Pyodide distribution)
    wheels = dst / "wheels"
    wheels.mkdir(exist_ok=True)
    pkgs = ["modelx", "openpyxl", "et_xmlfile", "networkx"]
    dl = ["-m", "pip", "download", "--quiet", "--only-binary=:all:",
          "--no-deps", "-d", str(wheels), *pkgs]
    # uv-managed venvs have no pip; borrow one for the download
    cmds = ([["uv", "run", "--with", "pip", "python", *dl]] if shutil.which("uv") else []) \
        + [[sys.executable, *dl]]
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
    print(f"runtime vendored: {n} files + {len(have)} wheels, "
          f"{size / 1024 / 1024:.0f} MB -> {dst}")


if __name__ == "__main__":
    raise SystemExit(main())
