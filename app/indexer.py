"""Folder discovery, change detection, and indexing orchestration.

Change detection uses content hashing as the source of truth (a re-saved file can
keep the same size), with size+mtime as a cheap pre-filter to avoid hashing files
that demonstrably have not changed.

Extraction (hashing + text parsing) is the CPU/IO-bound part and runs in a
``ProcessPoolExecutor``; the SQLite writes, OCR, and progress accounting all stay
on the calling thread, so the database is only ever touched from one place. The
serial path (``max_workers=1``) shares the exact same apply logic, so behaviour is
identical whether or not parallelism is used.
"""

from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import db, extractor, formats, ocr, paths

DEFAULT_OCR_CACHE = paths.data_root() / "ocr-cache"

# Don't spin up worker processes for trivially small jobs — process spawn plus
# re-importing PyMuPDF costs more than it saves below this many files.
_PARALLEL_MIN_FILES = 4
# Cap workers so a large corpus doesn't oversubscribe the box with PyMuPDF procs.
_MAX_WORKERS_CAP = 8


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
    encrypted: int = 0
    ocr_done: int = 0
    ocr_failed: int = 0
    ocr_unavailable: int = 0
    scanned_pdfs: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (f"indexed={self.indexed} skipped={self.skipped} "
                f"deleted={self.deleted} failed={self.failed} "
                f"encrypted={self.encrypted} "
                f"scanned_detected={len(self.scanned_pdfs)} "
                f"ocr_done={self.ocr_done} ocr_failed={self.ocr_failed} "
                f"ocr_unavailable={self.ocr_unavailable}")


@dataclass
class Progress:
    """Live snapshot passed to the progress callback after each unit of work.

    `total_pages`/`pages_done` drive a page-weighted ETA: OCR cost scales with
    pages, so pages are a better work unit than files.
    """
    phase: str = "starting"          # starting / scanning / indexing / done
    total_files: int = 0             # files that need work (skips excluded)
    total_pages: int = 0             # pages across those files
    files_done: int = 0
    pages_done: int = 0
    current_file: str = ""
    stats: "IndexStats" = field(default_factory=lambda: IndexStats())


ProgressCB = Callable[[Progress], None]


def discover_documents(root: Path) -> list[Path]:
    return formats.discover_documents(root)


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


# --- Worker (runs in a separate process) --------------------------------------
# Must be a module-level function with picklable args/return so spawn-based pools
# (Windows / frozen builds) can use it. It NEVER touches the database.

@dataclass
class _WorkerResult:
    path_str: str
    digest: str | None
    size: int
    mtime: float
    kind: str                       # "ok" | "unchanged" | "encrypted" | "failed"
    is_pdf: bool = False
    scanned: bool = False           # PDF looked image-only (OCR candidate)
    error: str = ""
    pages: list = field(default_factory=list)   # list[extractor.PageText]


def _extract_worker(path_str: str, prior_hash: str | None) -> _WorkerResult:
    """Hash + extract one file. Returns a fully self-contained result record."""
    path = Path(path_str)
    try:
        st = path.stat()
        digest = file_hash(path)
    except Exception as exc:  # noqa: BLE001 - unreadable file; mark as failed
        return _WorkerResult(path_str, None, 0, 0.0, "failed",
                             error=f"{type(exc).__name__}: {exc}")

    if prior_hash is not None and prior_hash == digest:
        return _WorkerResult(path_str, digest, st.st_size, st.st_mtime,
                             "unchanged", is_pdf=formats.is_pdf(path))

    try:
        if formats.is_pdf(path):
            pages = extractor.extract_pages(path_str)
            return _WorkerResult(path_str, digest, st.st_size, st.st_mtime,
                                 "ok", is_pdf=True,
                                 scanned=extractor.looks_scanned(pages),
                                 pages=pages)
        pages = formats.extract(path)
        return _WorkerResult(path_str, digest, st.st_size, st.st_mtime,
                             "ok", is_pdf=False, pages=pages)
    except extractor.EncryptedPDF:
        return _WorkerResult(path_str, digest, st.st_size, st.st_mtime,
                             "encrypted", is_pdf=True)
    except Exception as exc:  # noqa: BLE001 - corrupt/unreadable; retried next run
        return _WorkerResult(path_str, digest, st.st_size, st.st_mtime,
                             "failed", error=f"{type(exc).__name__}: {exc}")


def index_folder(conn, root: Path, *, ocr_config: OcrConfig | None = None,
                 run_optimize: bool = True, max_workers: int | None = None,
                 on_progress: ProgressCB | None = None) -> IndexStats:
    """Incrementally index every supported file under `root`. Returns counts.

    If `on_progress` is given it is called once after the initial scan (with
    totals) and again after each file, so a caller can render a progress bar/ETA.

    `max_workers` controls extraction parallelism: ``None`` auto-sizes to the CPU
    count, ``1`` forces the serial path. DB writes are always single-threaded.
    """
    root = Path(root).resolve()
    ocr_config = ocr_config or OcrConfig()
    stats = IndexStats()
    progress = Progress(stats=stats)

    def emit():
        if on_progress:
            on_progress(progress)

    existing = {
        row["path"]: row
        for row in conn.execute(
            "SELECT path, file_hash, size_bytes, modified_at FROM documents"
        )
    }
    seen: set[str] = set()

    # --- Scan pass: split fast-skips from real work, and size up the work. ---
    progress.phase = "scanning"
    emit()
    candidates: list[Path] = []
    for doc in discover_documents(root):
        path_str = str(doc)
        seen.add(path_str)
        try:
            stat = doc.stat()
        except OSError:
            continue
        prior = existing.get(path_str)
        if prior and prior["size_bytes"] == stat.st_size \
                and abs(prior["modified_at"] - stat.st_mtime) < 1e-6:
            stats.skipped += 1            # unchanged size+mtime => no work
            continue
        candidates.append(doc)

    page_counts = {str(p): formats.page_count(p) for p in candidates}
    progress.total_files = len(candidates)
    progress.total_pages = sum(page_counts.values())
    progress.phase = "indexing"
    emit()

    # --- Work pass: hash + extract (parallel), then apply (serial). ---
    def consume(res: _WorkerResult) -> None:
        progress.current_file = Path(res.path_str).name
        emit()
        _apply_result(conn, res, stats, ocr_config)
        conn.commit()
        progress.files_done += 1
        progress.pages_done += page_counts.get(res.path_str, 0)
        emit()

    def _prior_hash(path_str: str) -> str | None:
        prior = existing.get(path_str)
        return prior["file_hash"] if prior is not None else None

    tasks = [(str(p), _prior_hash(str(p))) for p in candidates]
    workers = _resolve_workers(len(tasks), max_workers)

    if workers <= 1:
        for path_str, prior_hash in tasks:
            consume(_extract_worker(path_str, prior_hash))
    else:
        _run_parallel(tasks, workers, consume)

    # Remove documents whose files are gone (pages + FTS cascade/triggers handle rest).
    for path_str in set(existing) - seen:
        conn.execute("DELETE FROM documents WHERE path=?", (path_str,))
        stats.deleted += 1
        db.log_event(conn, "deleted", path_str)
    conn.commit()

    if run_optimize and stats.indexed:
        db.optimize(conn)
    progress.phase = "done"
    progress.current_file = ""
    emit()
    return stats


def _resolve_workers(n_tasks: int, requested: int | None) -> int:
    if requested is not None:
        return max(1, requested)
    if n_tasks < _PARALLEL_MIN_FILES:
        return 1
    return max(1, min(os.cpu_count() or 1, n_tasks, _MAX_WORKERS_CAP))


def _run_parallel(tasks, workers, consume) -> None:
    """Extract in a process pool, applying results on this thread as they land.

    Falls back to serial extraction for any not-yet-applied work if the pool
    can't spawn or breaks mid-run, so an index never aborts on a hostile
    environment. DB/apply errors from `consume` are *not* swallowed — they
    propagate, since they indicate a real problem, not a worker issue.
    """
    pending = {path_str: prior_hash for path_str, prior_hash in tasks}
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_extract_worker, path_str, prior_hash): path_str
                for path_str, prior_hash in tasks
            }
            for fut in as_completed(futures):
                path_str = futures[fut]
                try:
                    res = fut.result()
                except BrokenProcessPool:
                    raise  # finish the rest serially below
                except Exception as exc:  # noqa: BLE001 - isolate one bad file
                    res = _WorkerResult(path_str, None, 0, 0.0, "failed",
                                        error=f"{type(exc).__name__}: {exc}")
                consume(res)
                pending.pop(path_str, None)
        return
    except (BrokenProcessPool, OSError):
        pass  # spawn refused or pool died — finish whatever is left serially
    for path_str, prior_hash in list(pending.items()):
        consume(_extract_worker(path_str, prior_hash))


# --- Apply (runs on the calling thread; owns all DB + OCR work) ---------------

def _apply_result(conn, res: _WorkerResult, stats: IndexStats,
                  ocr_config: OcrConfig) -> None:
    path = Path(res.path_str)
    if res.kind == "unchanged":
        # Content identical (e.g. just touched) — refresh metadata only.
        conn.execute(
            "UPDATE documents SET modified_at=?, size_bytes=? WHERE path=?",
            (res.mtime, res.size, res.path_str),
        )
        stats.skipped += 1
        return
    if res.kind == "encrypted":
        # Stable failure: record a tracked, page-less row so it shows up as
        # encrypted and isn't re-attempted on every reindex.
        _record_unindexable(conn, path, res.size, res.mtime, res.digest, "encrypted")
        stats.encrypted += 1
        db.log_event(conn, "encrypted", res.path_str)
        return
    if res.kind == "failed":
        stats.failed += 1
        db.log_event(conn, "failed", res.path_str, res.error)
        return

    # kind == "ok"
    pages = res.pages
    method = "native"
    ocr_status = "none"
    if res.is_pdf and res.scanned:
        stats.scanned_pdfs.append(path)
        ocr_status = "required"
        if ocr_config.enabled:
            ocr_status, pages, method = _try_ocr(
                conn, path, res.digest, pages, stats, ocr_config,
            )
    for p in pages:
        p.extraction_method = method
    _insert_document(conn, path, res.size, res.mtime, res.digest, pages, ocr_status)
    stats.indexed += 1
    db.log_event(conn, "indexed", res.path_str)


def _record_unindexable(conn, path: Path, size: int, mtime: float, digest: str,
                        status: str) -> None:
    """Insert a page-less document row for a file we can't index (e.g. encrypted).

    Tracking it (with hash + mtime) means the fast-skip path won't keep retrying
    it, while it stays visible in stats/queries.
    """
    conn.execute("DELETE FROM documents WHERE path=?", (str(path),))
    conn.execute(
        """INSERT INTO documents
           (path, filename, folder, file_hash, size_bytes, modified_at,
            page_count, ocr_status, last_indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(path), path.name, str(path.parent), digest, size, mtime,
         0, status, time.time()),
    )


def _insert_document(conn, path: Path, size: int, mtime: float, digest: str,
                     pages, ocr_status: str) -> None:
    """Replace any prior rows for this path (cascade clears old pages/FTS)."""
    conn.execute("DELETE FROM documents WHERE path=?", (str(path),))
    cur = conn.execute(
        """INSERT INTO documents
           (path, filename, folder, file_hash, size_bytes, modified_at,
            page_count, ocr_status, last_indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(path), path.name, str(path.parent), digest, size, mtime,
         len(pages), ocr_status, time.time()),
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
