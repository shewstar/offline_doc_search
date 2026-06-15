"""Multi-format text extraction.

PDFs go through PyMuPDF (see :mod:`app.extractor`). Text-bearing formats —
``.txt``/``.md``/``.html``/``.docx``/``.epub`` — are read with the standard
library only (no new dependency, keeping the bundle reviewable and offline).

Those formats have no inherent pagination, so their text is split into synthetic
``PAGE_TARGET``-sized "pages" on paragraph boundaries. That keeps the page-level
FTS / snippet / citation model identical across every file type — search,
ranking, and the viewer all treat a page the same regardless of source format.
"""

from __future__ import annotations

import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from . import extractor
from .extractor import PageText, normalize

# Text-derived formats (everything except PDF).
TEXT_EXTS = {".txt", ".text", ".md", ".markdown", ".html", ".htm", ".docx", ".epub"}
SUPPORTED_EXTS = {".pdf"} | TEXT_EXTS

# Approximate characters per synthetic page for non-paginated formats.
PAGE_TARGET = 3000


def is_pdf(path: Path | str) -> bool:
    return Path(path).suffix.lower() == ".pdf"


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def discover_documents(root: Path) -> list[Path]:
    """Every indexable file under `root`, sorted for stable ordering."""
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )


def page_count(path: Path | str) -> int:
    """Cheap page estimate for progress/ETA sizing (not the final count).

    PDFs report their real page count; text formats are estimated from size,
    since extracting just to count would defeat the purpose of a fast scan pass.
    """
    if is_pdf(path):
        return extractor.page_count(str(path))
    try:
        return max(1, Path(path).stat().st_size // PAGE_TARGET)
    except OSError:
        return 1


def extract(path: Path | str) -> list[PageText]:
    """Return normalized per-page text for any supported format.

    PDFs may raise :class:`extractor.EncryptedPDF`; text formats do not.
    """
    path = Path(path)
    if is_pdf(path):
        return extractor.extract_pages(str(path))
    raw = _read_text_document(path)
    pages = _paginate(normalize(raw))
    return [
        PageText(page_number=i, text=t, char_count=len(t), extraction_method="native")
        for i, t in enumerate(pages, start=1)
    ]


# --- per-format readers --------------------------------------------------------

def _read_text_document(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".epub":
        return _extract_epub(path)
    if ext in (".html", ".htm"):
        return html_to_text(_read_unicode(path))
    return _read_unicode(path)  # .txt / .md / .markdown / .text


def _read_unicode(path: Path) -> str:
    # errors="replace" keeps a stray bad byte from failing a whole file.
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _localname(tag: str) -> str:
    """Strip the XML namespace, e.g. '{...}p' -> 'p'."""
    return tag.rsplit("}", 1)[-1]


def _extract_docx(path: Path) -> str:
    """Paragraph text from a .docx (a zip of WordprocessingML)."""
    with zipfile.ZipFile(path) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError:
            return ""
    root = ET.fromstring(xml)
    paras: list[str] = []
    for p in root.iter():
        if _localname(p.tag) != "p":
            continue
        text = "".join(
            t.text for t in p.iter()
            if _localname(t.tag) == "t" and t.text
        )
        if text.strip():
            paras.append(text)
    return "\n\n".join(paras)


def _extract_epub(path: Path) -> str:
    """Concatenated text of an EPUB's content documents, in spine order."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        opf_path = _epub_opf_path(z, names)
        hrefs, base = _epub_spine(z, opf_path) if opf_path else ([], "")
        if not hrefs:  # no/!parseable spine — fall back to any (x)html, sorted
            hrefs = sorted(
                n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))
            )
            base = ""
        out: list[str] = []
        for href in hrefs:
            data = _epub_read(z, names, base, href)
            if data is not None:
                out.append(html_to_text(data.decode("utf-8", "replace")))
    return "\n\n".join(t for t in out if t.strip())


def _epub_opf_path(z: zipfile.ZipFile, names: list[str]) -> str | None:
    try:
        container = z.read("META-INF/container.xml")
        for el in ET.fromstring(container).iter():
            if _localname(el.tag) == "rootfile" and el.get("full-path"):
                return el.get("full-path")
    except (KeyError, ET.ParseError):
        pass
    opfs = [n for n in names if n.lower().endswith(".opf")]
    return opfs[0] if opfs else None


def _epub_spine(z: zipfile.ZipFile, opf_path: str) -> tuple[list[str], str]:
    """Return (content hrefs in spine order, base dir of the opf)."""
    try:
        opf = ET.fromstring(z.read(opf_path))
    except (KeyError, ET.ParseError):
        return [], ""
    manifest: dict[str, str] = {}
    spine: list[str] = []
    for el in opf.iter():
        name = _localname(el.tag)
        if name == "item" and el.get("id") and el.get("href"):
            manifest[el.get("id")] = el.get("href")
        elif name == "itemref" and el.get("idref"):
            spine.append(el.get("idref"))
    base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
    hrefs = [manifest[i] for i in spine if i in manifest]
    return hrefs, base


def _epub_read(z: zipfile.ZipFile, names: list[str], base: str, href: str) -> bytes | None:
    from urllib.parse import unquote

    href = unquote(href.split("#", 1)[0])
    candidates = []
    if base:
        candidates.append(f"{base}/{href}".replace("//", "/"))
    candidates.append(href)
    candidates.append(href.lstrip("/"))
    for cand in candidates:
        if cand in names:
            return z.read(cand)
    return None


# --- HTML stripping ------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "head", "title", "noscript"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "br", "li", "ul", "ol",
    "tr", "table", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
}


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._buf.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self._buf.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self._buf.append(data)

    def get_text(self) -> str:
        return "".join(self._buf)


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(html)
    except Exception:
        return ""  # malformed markup — best effort, never crash an index run
    return parser.get_text()


# --- synthetic pagination ------------------------------------------------------

_PARA_SPLIT = re.compile(r"\n\s*\n")


def _paginate(text: str, target: int = PAGE_TARGET) -> list[str]:
    """Split text into ~`target`-char pages on paragraph boundaries."""
    text = text.strip()
    if not text:
        return [""]

    # First, break into paragraphs; hard-split any paragraph longer than target.
    chunks: list[str] = []
    for para in _PARA_SPLIT.split(text):
        para = para.strip()
        while len(para) > target:
            chunks.append(para[:target])
            para = para[target:]
        if para:
            chunks.append(para)

    # Then pack paragraphs into pages without exceeding target where avoidable.
    pages: list[str] = []
    buf: list[str] = []
    size = 0
    for chunk in chunks:
        if size and size + len(chunk) > target:
            pages.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(chunk)
        size += len(chunk) + 2
    if buf:
        pages.append("\n\n".join(buf))
    return pages or [""]
