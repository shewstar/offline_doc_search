"""Local web app: FastAPI serving the search UI + JSON API + PDF bytes.

Single runtime — the same db/indexer/search/ocr modules the CLI uses. Bind to
127.0.0.1 only; this never listens on an external interface.

Run:  python -m app.main         (or: uvicorn app.main:app)
"""

from __future__ import annotations

import html
import time
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


@app.post("/api/index")
def api_index(req: IndexRequest) -> dict:
    root = Path(req.folder).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"not a folder: {root}")
    conn = _conn()
    try:
        cfg = indexer.OcrConfig(enabled=req.ocr, language=req.ocr_lang)
        ocr_warning = None
        if req.ocr and not ocr.ocr_available():
            ocr_warning = "ocrmypdf/tesseract not on PATH; scanned files left un-OCR'd"
        t0 = time.perf_counter()
        stats = indexer.index_folder(conn, root, ocr_config=cfg)
        return {
            "took_s": round(time.perf_counter() - t0, 2),
            "indexed": stats.indexed,
            "skipped": stats.skipped,
            "deleted": stats.deleted,
            "failed": stats.failed,
            "ocr_done": stats.ocr_done,
            "ocr_failed": stats.ocr_failed,
            "ocr_unavailable": stats.ocr_unavailable,
            "scanned_detected": len(stats.scanned_pdfs),
            "ocr_warning": ocr_warning,
        }
    finally:
        conn.close()


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
