# PyInstaller spec — offline, onedir build of the Offline PDF Search app.
#
# Build:  pyinstaller --noconfirm packaging/offline-doc-search.spec
# Output: dist/offline-doc-search/  (a self-contained folder; run the .exe)
#
# Deliberately a *onedir* build (not onefile): it starts faster, is trivial to
# inspect/audit (every bundled file is visible), and lets the writable `data/`
# and optional `bin/` folders sit plainly beside the executable — matching the
# plan's portable, reviewable-by-cautious-stakeholders posture.

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# SPECPATH is injected by PyInstaller = the directory containing this spec.
ROOT = Path(SPECPATH).resolve().parent  # repo root (spec lives in packaging/)

# uvicorn and the web stack pull protocol/loop implementations via dynamic
# imports that static analysis misses; collect them explicitly. (PyInstaller
# ships hooks for pymupdf/fastapi, but uvicorn's optionals need a nudge.)
hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("anyio")
    + [
        "app",
        "app.launcher",
        "app.main",
        "app.db",
        "app.indexer",
        "app.extractor",
        "app.ocr",
        "app.search",
        "app.ask",
        "app.paths",
    ]
)

# Optional Ask mode: when llama-cpp-python is installed in the build venv,
# bundle it (and its native llama.dll) into the frozen app. Without it, ask.py
# degrades gracefully and keyword search is unaffected.
binaries: list = []
runtime_hooks: list = []
try:
    import llama_cpp  # noqa: F401

    hidden += collect_submodules("llama_cpp")
    binaries += collect_dynamic_libs("llama_cpp")
    hidden.append("numpy")
    runtime_hooks.append(str(ROOT / "packaging" / "pyi_rth_llama_cpp.py"))
except ImportError:
    pass

# The static web UI (HTML/CSS/JS) and the vendored PDF.js viewer are read-only
# resources, unpacked under sys._MEIPASS and found via app.paths.WEB_DIR.
datas = [
    (str(ROOT / "web"), "web"),
]

a = Analysis(
    [str(ROOT / "run_app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=runtime_hooks,
    excludes=[
        # Trim heavy libs that are never imported, keeping the bundle lean.
        # numpy is required by llama-cpp-python when Ask mode is bundled.
        "tkinter",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="offline-doc-search",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # no UPX: keeps binaries byte-for-byte auditable
    console=True,        # console window doubles as the local log view
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="offline-doc-search",
)
