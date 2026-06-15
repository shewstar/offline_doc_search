"""Folder discovery, change detection, and indexing orchestration.

Change detection uses content hashing as the source of truth (a re-saved PDF can
keep the same size), with size+mtime as a cheap pre-filter to avoid hashing files
that demonstrably have not changed.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, extractor, ocr

DEFAULT_OCR_CACHE = db.PROJECT_ROOT / "data" / "ocr-cache"


@dataclass
class OcrConfig:
    enabled: bool = False
    cache_dir: Path = DEFAULT_OCR_CACHE
    language: str = "eng"


@dataclass
class IndexStats:
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0
    ocr_done: int = 0
    ocr_failed: int = 0
    ocr_unavailable: int = 0
    scanned_pdfs: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (f"indexed={self.indexed} skipped={self.skipped} "
                f"deleted={self.deleted} failed={self.failed} "
                f"scanned_detected={len(self.scanned_pdfs)} "
                f"ocr_done={self.ocr_done} ocr_failed={self.ocr_failed} "
                f"ocr_unavailable={self.ocr_unavailable}")


def discover_pdfs(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.pdf") if p.is_file())


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def index_folder(conn, root: Path, *, ocr_config: OcrConfig | None = None,
                 run_optimize: bool = True) -> IndexStats:
    """Incrementally index every PDF under `root`. Returns counts."""
    root = Path(root).resolve()
    ocr_config = ocr_config or OcrConfig()
    stats = IndexStats()

    existing = {
        row["path"]: row
        for row in conn.execute(
            "SELECT path, file_hash, size_bytes, modified_at FROM documents"
        )
    }
    seen: set[str] = set()

    for pdf in discover_pdfs(root):
        path_str = str(pdf)
        seen.add(path_str)
        try:
            stat = pdf.stat()
            prior = existing.get(path_str)

            # Pre-filter: unchanged size+mtime => assume unchanged, skip hashing.
            if prior and prior["size_bytes"] == stat.st_size \
                    and abs(prior["modified_at"] - stat.st_mtime) < 1e-6:
                stats.skipped += 1
                continue

            digest = file_hash(pdf)
            if prior and prior["file_hash"] == digest:
                # Content identical (e.g. just touched) — refresh metadata only.
                conn.execute(
                    "UPDATE documents SET modified_at=?, size_bytes=? WHERE path=?",
                    (stat.st_mtime, stat.st_size, path_str),
                )
                stats.skipped += 1
                continue

            _index_one(conn, pdf, stat, digest, stats, ocr_config)
            stats.indexed += 1
            db.log_event(conn, "indexed", path_str)
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            stats.failed += 1
            db.log_event(conn, "failed", path_str, f"{type(exc).__name__}: {exc}")
        conn.commit()

    # Remove documents whose files are gone (pages + FTS cascade/triggers handle rest).
    for path_str in set(existing) - seen:
        conn.execute("DELETE FROM documents WHERE path=?", (path_str,))
        stats.deleted += 1
        db.log_event(conn, "deleted", path_str)
    conn.commit()

    if run_optimize and stats.indexed:
        db.optimize(conn)
    return stats


def _index_one(conn, pdf: Path, stat, digest: str, stats: IndexStats,
               ocr_config: OcrConfig) -> None:
    pages = extractor.extract_pages(str(pdf))
    method = "native"
    ocr_status = "none"

    if extractor.looks_scanned(pages):
        stats.scanned_pdfs.append(pdf)
        ocr_status = "required"
        if ocr_config.enabled:
            ocr_status, pages, method = _try_ocr(conn, pdf, digest, pages,
                                                 stats, ocr_config)

    # Stamp the chosen extraction method onto every page.
    for p in pages:
        p.extraction_method = method

    # Replace any prior rows for this path (UPSERT on documents, cascade on pages).
    conn.execute("DELETE FROM documents WHERE path=?", (str(pdf),))
    cur = conn.execute(
        """INSERT INTO documents
           (path, filename, folder, file_hash, size_bytes, modified_at,
            page_count, ocr_status, last_indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(pdf), pdf.name, str(pdf.parent), digest, stat.st_size,
         stat.st_mtime, len(pages), ocr_status, time.time()),
    )
    doc_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO pages
           (document_id, page_number, text_content, char_count, extraction_method)
           VALUES (?,?,?,?,?)""",
        [(doc_id, p.page_number, p.text, p.char_count, p.extraction_method)
         for p in pages],
    )


def _try_ocr(conn, pdf: Path, digest: str, native_pages, stats: IndexStats,
             ocr_config: OcrConfig):
    """Run/reuse OCR for a scanned file. Returns (ocr_status, pages, method).

    Falls back to the native pages (whatever little text they had) if OCR is
    unavailable or fails, so a bad OCR run never loses an already-indexed file.
    """
    try:
        ocr_pdf = ocr.ensure_ocr(pdf, digest, ocr_config.cache_dir,
                                 language=ocr_config.language)
        pages = extractor.extract_pages(str(ocr_pdf))
        stats.ocr_done += 1
        return "complete", pages, "ocr"
    except ocr.OCRUnavailable:
        stats.ocr_unavailable += 1
        return "required", native_pages, "native"
    except Exception as exc:  # OCRFailed or anything the toolchain throws
        stats.ocr_failed += 1
        db.log_event(conn, "ocr_failed", str(pdf), f"{type(exc).__name__}: {exc}")
        return "failed", native_pages, "native"
