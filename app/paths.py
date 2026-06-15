"""Filesystem locations, resolved for both source runs and frozen builds.

A normal source checkout keeps everything under the project root. A PyInstaller
build splits those concerns:

* **Read-only resources** (the `web/` UI + vendored PDF.js) are unpacked into the
  bundle directory, which PyInstaller exposes as ``sys._MEIPASS``. They must be
  read from there, never written to.
* **Writable data** (the SQLite index, OCR cache, logs) must live *beside the
  executable* so it survives across runs, is visible to the operator, and keeps
  the build itself immutable. This is the "portable mode" the plan calls for —
  point the .exe at a folder and all its data stays in one place next to it.

Resolving both through this module keeps the rest of the code path-agnostic.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) frozen bundle."""
    return getattr(sys, "frozen", False)


def resource_root() -> Path:
    """Directory holding bundled read-only resources (the `web/` tree).

    Frozen: the unpack dir PyInstaller advertises via ``sys._MEIPASS`` (onedir
    builds point it at the bundle folder). Source: the project root.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _install_root() -> Path:
    """Directory the writable/bundled-binary folders sit beside.

    Frozen: next to the launcher executable. Source: the project root.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """Writable directory for the index DB, OCR cache, and logs.

    Created on demand. Frozen builds get a `data/` folder beside the .exe;
    source runs reuse the repo's `data/`.
    """
    d = _install_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundled_bin_dir() -> Path:
    """Optional `bin/` folder beside the install for drop-in native tools.

    Operators in an air-gapped environment can drop `tesseract.exe`,
    `gswin64c.exe`, etc. here instead of installing them system-wide; the
    launcher prepends this to PATH so OCR detection finds them. Not created
    automatically — its presence is what signals "use bundled tools".
    """
    return _install_root() / "bin"


def models_dir() -> Path:
    """Optional `models/` folder beside the install for drop-in GGUF models.

    Not created automatically — place a single ``*.gguf`` instruct model here
    to enable natural-language Ask mode. See PACKAGING.md.
    """
    return _install_root() / "models"


def find_gguf_model() -> Path | None:
    """Return the first ``*.gguf`` in ``models/`` (sorted by name), or None."""
    d = models_dir()
    if not d.is_dir():
        return None
    ggufs = sorted(d.glob("*.gguf"))
    return ggufs[0] if ggufs else None


# Read-only resource locations (resolved once at import).
WEB_DIR = resource_root() / "web"
