"""Local web app: FastAPI serving the search UI + JSON API + PDF bytes.

Single runtime — the same db/indexer/search/ocr modules the CLI uses. Bind to
127.0.0.1 only; this never listens on an external interface.

Run:  python -m app.main         (or: uvicorn app.main:app)
"""

from __future__ import annotations

import html
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import db, indexer, ocr, search

WEB_DIR = db.PROJECT_ROOT / "web"

# Sentinel markers FTS wraps around matches; we HTML-escape the snippet first,
# then swap these for <mark> so user text can never inject markup.
_HL_OPEN = "\x02"
_HL_CLOSE = "\x03"

app = FastAPI(title="Offline PDF Search")


def _conn():
    conn = db.connect()
    db.init_schema(conn)
    return conn


def _snippet_html(raw: str) -> str:
    escaped = html.escape(raw)
    return escaped.replace(_HL_OPEN, "<mark>").replace(_HL_CLOSE, "</mark>")


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/stats")
def api_stats() -> dict:
    conn = _conn()
    try:
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        ocr_pending = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE ocr_status='required'"
        ).fetchone()[0]
        return {"documents": docs, "pages": pages, "ocr_pending": ocr_pending}
    finally:
        conn.close()


# How many page hits to scan before grouping, and how many pages to show per doc.
_PAGE_SCAN = 500
_PAGES_PER_DOC = 4


def _group_by_document(hits, doc_limit: int) -> list[dict]:
    """Collapse ranked page hits into documents, best-matching document first.

    Order is preserved from the bm25-ranked hits, so the first page seen for a
    document is its strongest match and sets the document's position.
    """
    order: list[int] = []
    groups: dict[int, dict] = {}
    for h in hits:
        g = groups.get(h.document_id)
        if g is None:
            g = {
                "document_id": h.document_id,
                "filename": h.filename,
                "folder": h.folder,
                "method": h.extraction_method,
                "matched_pages": 0,
                "pages": [],
            }
            groups[h.document_id] = g
            order.append(h.document_id)
        g["matched_pages"] += 1
        if len(g["pages"]) < _PAGES_PER_DOC:
            g["pages"].append({
                "page_number": h.page_number,
                "method": h.extraction_method,
                "snippet_html": _snippet_html(h.snippet),
            })
    docs = []
    for doc_id in order[:doc_limit]:
        g = groups[doc_id]
        g["more"] = g["matched_pages"] - len(g["pages"])  # extra pages not shown
        docs.append(g)
    return docs


@app.get("/api/search")
def api_search(q: str = Query(""), limit: int = Query(50, ge=1, le=500),
               folder: str | None = None, method: str | None = None) -> dict:
    conn = _conn()
    try:
        t0 = time.perf_counter()
        try:
            hits = search.search(conn, q, limit=_PAGE_SCAN, folder=folder,
                                 method=method, hl_open=_HL_OPEN, hl_close=_HL_CLOSE)
        except Exception as exc:  # malformed FTS query -> 400, not 500
            raise HTTPException(status_code=400, detail=f"bad query: {exc}")
        docs = _group_by_document(hits, doc_limit=limit)
        took_ms = (time.perf_counter() - t0) * 1000
        return {
            "took_ms": round(took_ms, 1),
            "doc_count": len(docs),
            "page_count": len(hits),
            "documents": docs,
        }
    finally:
        conn.close()


class IndexRequest(BaseModel):
    folder: str
    ocr: bool = False
    ocr_lang: str = "eng"


# --- Background indexing jobs -------------------------------------------------
# Indexing (especially OCR) can take minutes, so it runs in a worker thread and
# the UI polls /api/index/status for progress + ETA.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _index_worker(job_id: str, folder: Path, cfg: indexer.OcrConfig) -> None:
    # Own connection: SQLite objects can't cross threads.
    conn = db.connect()
    db.init_schema(conn)
    started = time.perf_counter()

    def on_progress(p: indexer.Progress) -> None:
        elapsed = time.perf_counter() - started
        eta = None
        if p.phase == "indexing" and p.pages_done and p.pages_done < p.total_pages:
            eta = (elapsed / p.pages_done) * (p.total_pages - p.pages_done)
        _set_job(
            job_id,
            phase=p.phase,
            total_files=p.total_files,
            total_pages=p.total_pages,
            files_done=p.files_done,
            pages_done=p.pages_done,
            current_file=p.current_file,
            indexed=p.stats.indexed,
            skipped=p.stats.skipped,
            deleted=p.stats.deleted,
            failed=p.stats.failed,
            encrypted=p.stats.encrypted,
            ocr_done=p.stats.ocr_done,
            ocr_failed=p.stats.ocr_failed,
            ocr_unavailable=p.stats.ocr_unavailable,
            scanned_detected=len(p.stats.scanned_pdfs),
            elapsed_s=round(elapsed, 1),
            eta_s=round(eta, 1) if eta is not None else None,
        )

    try:
        indexer.index_folder(conn, folder, ocr_config=cfg, on_progress=on_progress)
        _set_job(job_id, state="done", phase="done",
                 elapsed_s=round(time.perf_counter() - started, 1), eta_s=0)
    except Exception as exc:  # noqa: BLE001
        _set_job(job_id, state="error", phase="error",
                 error=f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


@app.post("/api/index")
def api_index(req: IndexRequest) -> dict:
    root = Path(req.folder).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"not a folder: {root}")

    # One index job at a time keeps writers from colliding.
    with _jobs_lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(status_code=409, detail="an index job is already running")

    ocr_warning = None
    if req.ocr and not ocr.ocr_available():
        ocr_warning = "ocrmypdf/tesseract not on PATH; scanned files left un-OCR'd"

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"state": "running", "phase": "starting", "error": None,
                         "ocr_warning": ocr_warning, "total_files": 0,
                         "total_pages": 0, "files_done": 0, "pages_done": 0,
                         "current_file": "", "elapsed_s": 0, "eta_s": None,
                         "failed": 0, "encrypted": 0}
    cfg = indexer.OcrConfig(enabled=req.ocr, language=req.ocr_lang)
    threading.Thread(target=_index_worker, args=(job_id, root, cfg),
                     daemon=True).start()
    return {"job_id": job_id, "ocr_warning": ocr_warning}


@app.get("/api/index/status")
def api_index_status(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return {"job_id": job_id, **job}


@app.get("/api/file")
def api_file(id: int) -> FileResponse:
    """Serve a document's PDF bytes for the embedded viewer.

    Only paths recorded in the index are servable — no arbitrary filesystem access.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT path, filename FROM documents WHERE id=?", (id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown document")
    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="file no longer on disk")
    return FileResponse(path, media_type="application/pdf", filename=row["filename"])


@app.get("/api/browse")
def api_browse(path: str = "") -> dict:
    """List subfolders for the folder picker.

    This app is single-user and localhost-bound, so browsing the local
    filesystem is the intended behaviour. Empty path lists drive roots (Windows)
    or `/` (POSIX).
    """
    import os
    import string

    if not path:
        if os.name == "nt":
            roots = [f"{d}:\\" for d in string.ascii_uppercase
                     if Path(f"{d}:\\").exists()]
            return {"path": "", "parent": None,
                    "dirs": [{"name": r, "path": r} for r in roots], "pdf_count": 0}
        path = "/"

    p = Path(path)
    if not p.is_dir():
        raise HTTPException(status_code=404, detail="not a directory")
    p = p.resolve()

    try:
        subdirs = sorted(
            (e for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name.lower(),
        )
        dirs = [{"name": e.name, "path": str(e)} for e in subdirs]
    except (PermissionError, OSError):
        dirs = []

    try:
        pdf_count = sum(1 for _ in p.glob("*.pdf"))
    except (PermissionError, OSError):
        pdf_count = 0

    # At a filesystem/drive root, "up" goes to the roots listing ("").
    parent = "" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": dirs, "pdf_count": pdf_count}


@app.get("/api/issues")
def api_issues(limit: int = Query(50, ge=1, le=500)) -> dict:
    """Recent problem events (failed / encrypted / ocr_failed) for visibility."""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT ts, path, event, detail FROM index_events
               WHERE event IN ('failed','encrypted','ocr_failed')
               ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return {"issues": [
            {"ts": r["ts"], "path": r["path"], "event": r["event"],
             "detail": r["detail"]} for r in rows
        ]}
    finally:
        conn.close()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
