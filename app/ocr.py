"""OCR fallback via the OCRmyPDF CLI (which drives Tesseract + Ghostscript).

We shell out to the `ocrmypdf` binary rather than import the library so the
heavy native toolchain stays an external, separately-auditable dependency — and
so a machine without it degrades gracefully instead of failing to import.

OCR output is cached by the source file's content hash, so a file that is later
moved or renamed reuses its existing OCR result instead of paying for it twice.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class OCRUnavailable(RuntimeError):
    """Raised when the OCRmyPDF/Tesseract toolchain is not on PATH."""


class OCRFailed(RuntimeError):
    """Raised when OCRmyPDF runs but exits non-zero."""


def ocr_available() -> bool:
    return shutil.which("ocrmypdf") is not None and shutil.which("tesseract") is not None


def cache_path(cache_dir: Path | str, file_hash: str) -> Path:
    return Path(cache_dir) / f"{file_hash}.pdf"


# Module-level indirection so tests can substitute a fake runner.
def run_ocr(src_pdf: Path, out_pdf: Path, *, language: str = "eng",
            timeout: int = 1800) -> None:
    """Add a searchable text layer to `src_pdf`, writing to `out_pdf`.

    `--skip-text` leaves pages that already have text untouched (safe for mixed
    native/scanned documents); only image pages get OCR'd.
    """
    cmd = [
        "ocrmypdf",
        "--skip-text",
        "--language", language,
        "--quiet",
        "--output-type", "pdf",
        str(src_pdf),
        str(out_pdf),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=timeout,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise OCRFailed(exc.stderr.strip() or f"ocrmypdf exit {exc.returncode}") from exc


def ensure_ocr(src_pdf: Path, file_hash: str, cache_dir: Path | str,
               *, language: str = "eng") -> Path:
    """Return a path to an OCR'd copy of `src_pdf`, using the cache if present.

    Raises OCRUnavailable if the toolchain is missing and no cached result exists.
    """
    out = cache_path(cache_dir, file_hash)
    if out.exists():
        return out  # content-hash keyed: survives moves/renames
    if not ocr_available():
        raise OCRUnavailable("ocrmypdf/tesseract not found on PATH")
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp name and commit only on success, so a crashed/aborted OCR
    # never leaves a half-written file that a later run would treat as cached.
    tmp = out.with_suffix(".tmp.pdf")
    try:
        run_ocr(src_pdf, tmp, language=language)
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return out
