"""Native PDF text extraction via PyMuPDF.

Phase 1 handles the fast path: PDFs with a usable text layer. Per-page text is
returned along with a coarse quality signal so Phase 2 can decide where OCR is
needed. No OCR happens here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import fitz  # PyMuPDF


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


def extract_pages(pdf_path: str) -> list[PageText]:
    """Extract normalized text for every page. Raises on unreadable PDFs."""
    pages: list[PageText] = []
    with fitz.open(pdf_path) as doc:
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
