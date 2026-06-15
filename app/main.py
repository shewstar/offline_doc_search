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


@app.get("/api/search")
def api_search(q: str = Query(""), limit: int = Query(50, ge=1, le=500),
               folder: str | None = None, method: str | None = None) -> dict:
    conn = _conn()
    try:
        t0 = time.perf_counter()
        try:
            hits = search.search(conn, q, limit=limit, folder=folder,
                                 method=method, hl_open=_HL_OPEN, hl_close=_HL_CLOSE)
        except Exception as exc:  # malformed FTS query -> 400, not 500
            raise HTTPException(status_code=400, detail=f"bad query: {exc}")
        took_ms = (time.perf_counter() - t0) * 1000
        return {
            "took_ms": round(took_ms, 1),
            "count": len(hits),
            "hits": [
                {
                    "document_id": h.document_id,
                    "filename": h.filename,
                    "folder": h.folder,
                    "page_number": h.page_number,
                    "method": h.extraction_method,
                    "snippet_html": _snippet_html(h.snippet),
                }
                for h in hits
            ],
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
                         "current_file": "", "elapsed_s": 0, "eta_s": None}
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


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
