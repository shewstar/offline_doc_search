"""Desktop launcher — the entry point for the packaged (frozen) build.

It does three things the bare `python -m app.main` dev entry does not:

1. Optionally extends PATH with a `bin/` folder beside the executable, so an
   air-gapped operator can drop the OCR toolchain (`tesseract`, `gswin64c`/
   `gs`, `ocrmypdf`) there instead of installing it system-wide.
2. Picks a free port if the default is taken, so a second launch doesn't error.
3. Opens the default browser at the UI once the server is accepting connections.

The server is still bound to 127.0.0.1 only — no external listener, no network.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser

import uvicorn

from . import paths
from .main import app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _extend_path_with_bundled_bin() -> None:
    """Prepend the optional bundled `bin/` dir to PATH if it exists.

    Lets OCR detection (`shutil.which`) find drop-in binaries without a
    system install. No-op when the folder is absent.
    """
    bin_dir = paths.bundled_bin_dir()
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def _pick_port(host: str, preferred: int) -> int:
    """Return `preferred` if bindable, else an OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            s.bind((host, 0))
            return s.getsockname()[1]


def _open_browser_when_ready(host: str, port: int, timeout: float = 15.0) -> None:
    """Poll the port, then open the browser — avoids a blank-page race."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex((host, port)) == 0:
                break
        time.sleep(0.2)
    webbrowser.open(f"http://{host}:{port}/")


def main() -> None:
    _extend_path_with_bundled_bin()
    port = _pick_port(HOST, DEFAULT_PORT)
    threading.Thread(
        target=_open_browser_when_ready, args=(HOST, port), daemon=True
    ).start()
    print(f"Offline PDF Search — serving http://{HOST}:{port}/  (Ctrl+C to quit)")
    uvicorn.run(app, host=HOST, port=port, log_level="info")


if __name__ == "__main__":
    main()
