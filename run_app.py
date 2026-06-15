"""Top-level entry point for the packaged desktop build (PyInstaller target).

Kept at the repo root so PyInstaller analyses a plain script (no package-relative
__main__ quirks). From source you can run it directly:

    python run_app.py

which is equivalent to `python -m app.launcher`.
"""

from app.launcher import main

if __name__ == "__main__":
    main()
