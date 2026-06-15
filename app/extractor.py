"""Native PDF text extraction via PyMuPDF.

Phase 1 handles the fast path: PDFs with a usable text layer. Per-page text is
returned along with a coarse quality signal so Phase 2 can decide where OCR is
needed. No OCR happens here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


class EncryptedPDF(Exception):
    """A PDF that needs a password we don't have — a stable, expected failure."""


@dataclass
class PageText:
    page_number: int          # 1-based
    text: str
    char_count: int
    extraction_method: str = "native"


_WS_RUN = re.compile(r"[ \t\f\v]+")
_NL_RUN = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    """Light normalization: collapse runs of spaces and excess blank lines.

    Deliberately conservative — aggressive cleaning destroys exact-match search.
    Page boundaries are preserved by the caller (one row per page).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RUN.sub(" ", text)
    text = _NL_RUN.sub("\n\n", text)
    return text.strip()


def page_count(pdf_path: str) -> int:
    """Cheap page count without extracting text (used to size up indexing work)."""
    try:
        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception:
        return 0


def extract_pages(pdf_path: str) -> list[PageText]:
    """Extract normalized text for every page.

    Raises EncryptedPDF if the file is password-protected (after trying the empty
    password, which unlocks many "encrypted" PDFs that only restrict printing).
    Raises other exceptions on corrupt/unreadable PDFs — callers treat those as
    transient failures and retry on the next index.
    """
    pages: list[PageText] = []
    with fitz.open(pdf_path) as doc:
        if doc.needs_pass:
            # Empty password is the common case for "owner-locked" PDFs.
            if not doc.authenticate(""):
                raise EncryptedPDF(Path(pdf_path).name)
        for i, page in enumerate(doc, start=1):
            raw = page.get_text("text") or ""
            text = normalize(raw)
            pages.append(PageText(page_number=i, text=text, char_count=len(text)))
    return pages


def looks_scanned(pages: list[PageText], min_chars_per_page: int = 20,
                  min_text_page_ratio: float = 0.2) -> bool:
    """Heuristic OCR trigger for Phase 2.

    True when almost no page has meaningful text (image-only / scanned).
    """
    if not pages:
        return True
    text_pages = sum(1 for p in pages if p.char_count >= min_chars_per_page)
    return (text_pages / len(pages)) < min_text_page_ratio
