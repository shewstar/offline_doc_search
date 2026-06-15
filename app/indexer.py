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

from . import db, extractor


@dataclass
class IndexStats:
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0
    scanned_pdfs: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (f"indexed={self.indexed} skipped={self.skipped} "
                f"deleted={self.deleted} failed={self.failed} "
                f"scanned_detected={len(self.scanned_pdfs)}")


def discover_pdfs(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.pdf") if p.is_file())


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def index_folder(conn, root: Path, *, run_optimize: bool = True) -> IndexStats:
    """Incrementally index every PDF under `root`. Returns counts."""
    root = Path(root).resolve()
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

            _index_one(conn, pdf, stat, digest, stats)
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


def _index_one(conn, pdf: Path, stat, digest: str, stats: IndexStats) -> None:
    pages = extractor.extract_pages(str(pdf))
    if extractor.looks_scanned(pages):
        # Phase 1 still indexes whatever native text exists; Phase 2 will OCR these.
        stats.scanned_pdfs.append(pdf)
        ocr_status = "required"
    else:
        ocr_status = "none"

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
