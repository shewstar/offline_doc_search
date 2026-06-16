"""Local web app: FastAPI serving the search UI + JSON API + PDF bytes.

Single runtime — the same db/indexer/search/ocr modules the CLI uses. Bind to
127.0.0.1 only; this never listens on an external interface.

Run:  python -m app.main         (or: uvicorn app.main:app)
"""

from __future__ import annotations

import html
import mimetypes
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ask, db, indexer, ocr, paths, registry, search

WEB_DIR = paths.WEB_DIR

# ES modules must be served as JavaScript or browsers refuse to execute them;
# Python's mimetypes doesn't know .mjs on all platforms (notably Windows).
mimetypes.add_type("text/javascript", ".mjs")

# Sentinel markers FTS wraps around matches; we HTML-escape the snippet first,
# then swap these for <mark> so user text can never inject markup.
_HL_OPEN = "\x02"
_HL_CLOSE = "\x03"

app = FastAPI(title="Offline PDF Search")


def _active_path() -> Path:
    """Path of the active index's DB, or an empty placeholder.

    Falls back to the placeholder when there is no active index *or* the active
    index lives on a device that isn't currently mounted — so reads return
    zeros/no rows instead of crashing. Connecting to the placeholder yields a
    valid, empty schema. The UI surfaces the disconnected-device case separately.
    """
    entry = registry.get_active()
    if entry is None or not registry.is_available(entry):
        return registry.indexes_dir() / "_empty.db"
    return registry.db_path(entry)


def _conn():
    conn = db.connect(_active_path())
    db.init_schema(conn)
    return conn


def _public_index(entry: dict) -> dict:
    """Registry entry trimmed to what the UI needs (no internal db filename)."""
    return {
        "id": entry["id"],
        "folder": entry["folder"],
        "documents": entry["documents"],
        "pages": entry["pages"],
        "last_indexed_at": entry["last_indexed_at"],
        "location": entry.get("location"),
        "available": registry.is_available(entry),
        "referenced": bool(entry.get("referenced")),
    }


def _snippet_html(raw: str) -> str:
    escaped = html.escape(raw)
    return escaped.replace(_HL_OPEN, "<mark>").replace(_HL_CLOSE, "</mark>")


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


# Static assets (the vendored PDF.js viewer + its worker/fonts/cmaps, and
# pdfviewer.html) are served from here. Mounted on a subpath so it never
# shadows "/" or the "/api/*" routes.
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


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


# How many page hits to scan before grouping into documents.
_PAGE_SCAN = 500

# Sort keys the search UI offers. "relevance" keeps bm25 order (with a
# filename-match boost); the rest reorder the grouped documents by metadata.
_SORTS = {"relevance", "name", "newest", "oldest", "largest", "smallest"}


def _plain_query_terms(q: str) -> list[str]:
    """Plain words from an FTS query — quoted phrases kept, operators dropped.

    Mirrors the UI's highlightTerms() so a document whose *name* matches what
    the user typed can be boosted, even if the term is rare in its body text.
    """
    terms: list[str] = []
    for m in re.finditer(r'"([^"]+)"', q):
        phrase = m.group(1).strip().lower()
        if phrase:
            terms.append(phrase)
    for tok in re.sub(r'"[^"]*"', " ", q).split():
        tok = tok.replace("*", "").lstrip("-+").strip().lower()
        if len(tok) >= 2 and tok.upper() not in ("AND", "OR", "NOT", "NEAR"):
            terms.append(tok)
    return terms


def _group_by_document(hits, terms: list[str]) -> list[dict]:
    """Collapse ranked page hits into documents, best-matching document first.

    Order is preserved from the bm25-ranked hits, so the first page seen for a
    document is its strongest match and sets the document's position. Every
    matched page (within the scan cap) is returned so the UI can show them all.

    `filename_match` flags documents whose name or folder contains a query term;
    callers use it to float likely "I searched the title" hits to the top.
    """
    order: list[int] = []
    groups: dict[int, dict] = {}
    for h in hits:
        g = groups.get(h.document_id)
        if g is None:
            hay = f"{h.filename}\n{h.folder}".lower()
            g = {
                "document_id": h.document_id,
                "filename": h.filename,
                "folder": h.folder,
                "method": h.extraction_method,
                "modified_at": h.modified_at,
                "size_bytes": h.size_bytes,
                "filename_match": any(t in hay for t in terms),
                "matched_pages": 0,
                "pages": [],
            }
            groups[h.document_id] = g
            order.append(h.document_id)
        g["matched_pages"] += 1
        g["pages"].append({
            "page_number": h.page_number,
            "method": h.extraction_method,
            "snippet_html": _snippet_html(h.snippet),
        })
    return [groups[doc_id] for doc_id in order]


def _sort_documents(docs: list[dict], sort: str) -> list[dict]:
    """Reorder grouped documents. `docs` arrives in bm25-relevance order."""
    if sort == "name":
        return sorted(docs, key=lambda d: d["filename"].lower())
    if sort == "newest":
        return sorted(docs, key=lambda d: d["modified_at"], reverse=True)
    if sort == "oldest":
        return sorted(docs, key=lambda d: d["modified_at"])
    if sort == "largest":
        return sorted(docs, key=lambda d: d["size_bytes"], reverse=True)
    if sort == "smallest":
        return sorted(docs, key=lambda d: d["size_bytes"])
    # relevance: stable sort floats filename/folder matches up, bm25 order kept.
    return sorted(docs, key=lambda d: 0 if d["filename_match"] else 1)


def _levenshtein(a: str, b: str, cutoff: int) -> int | None:
    """Edit distance, short-circuited to None once it provably exceeds `cutoff`."""
    if abs(len(a) - len(b)) > cutoff:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            row_min = min(row_min, v)
        if row_min > cutoff:
            return None
        prev = cur
    return prev[-1] if prev[-1] <= cutoff else None


def _closest_term(conn, term: str) -> str | None:
    """Nearest vocabulary term to `term` by edit distance, tie-broken by frequency.

    Candidates are limited to terms sharing the first character and a similar
    length, which keeps the scan small. (Typos rarely change the first letter;
    that's an accepted limitation for the common case.)
    """
    cutoff = 1 if len(term) <= 4 else 2
    first = term[0]
    upper = first[:-1] + chr(ord(first[-1]) + 1) if first else first
    rows = conn.execute(
        """SELECT term, doc FROM pages_vocab
           WHERE term >= ? AND term < ?
             AND length(term) BETWEEN ? AND ?
           ORDER BY doc DESC LIMIT 600""",
        (first, upper, max(1, len(term) - 2), len(term) + 2),
    ).fetchall()
    best: str | None = None
    best_key = (cutoff + 1, 0)
    for r in rows:
        cand = r["term"]
        if cand == term:
            return None  # already a real term — nothing to correct
        d = _levenshtein(term, cand, cutoff)
        if d is None:
            continue
        key = (d, -r["doc"])  # closest first, then most common
        if key < best_key:
            best_key, best = key, cand
    return best


def _did_you_mean(conn, q: str) -> str | None:
    """A corrected query string when each unknown word has a near vocab match.

    Only triggers on plain word queries (no phrases/operators) so we never
    rewrite something the user expressed deliberately.
    """
    raw_terms = re.findall(r"[^\W\d_]{3,}", q, re.UNICODE)
    if not raw_terms or len(raw_terms) > 4:
        return None
    if re.search(r'["*]|\b(AND|OR|NOT|NEAR)\b', q):
        return None
    suggestion = q
    changed = False
    for term in raw_terms:
        cand = _closest_term(conn, term.lower())
        if cand:
            suggestion = re.sub(r"(?i)\b" + re.escape(term) + r"\b", cand, suggestion)
            changed = True
    return suggestion if changed and suggestion.lower() != q.lower() else None


@app.get("/api/search")
def api_search(q: str = Query(""), limit: int = Query(50, ge=1, le=500),
               folder: str | None = None, method: str | None = None,
               sort: str = Query("relevance")) -> dict:
    if sort not in _SORTS:
        sort = "relevance"
    conn = _conn()
    try:
        t0 = time.perf_counter()
        try:
            hits = search.search(conn, q, limit=_PAGE_SCAN, folder=folder,
                                 method=method, hl_open=_HL_OPEN, hl_close=_HL_CLOSE)
        except Exception as exc:  # malformed FTS query -> 400, not 500
            raise HTTPException(status_code=400, detail=f"bad query: {exc}")
        docs = _group_by_document(hits, _plain_query_terms(q))
        docs = _sort_documents(docs, sort)[:limit]
        # Only spend the vocab lookup when a real query found nothing.
        did_you_mean = _did_you_mean(conn, q) if (q.strip() and not docs) else None
        took_ms = (time.perf_counter() - t0) * 1000
        return {
            "took_ms": round(took_ms, 1),
            "doc_count": len(docs),
            "page_count": len(hits),
            "documents": docs,
            "did_you_mean": did_you_mean,
        }
    finally:
        conn.close()


@app.get("/api/suggest")
def api_suggest(q: str = Query(""), limit: int = Query(8, ge=1, le=20)) -> dict:
    """Term completions for search-as-you-type, ranked by document frequency.

    Reads the live FTS vocabulary (pages_vocab). Terms are already lowercased and
    diacritic-folded by the tokenizer, so we match a lowercased prefix range.
    """
    prefix = "".join(c for c in q.strip().lower() if c.isalnum())
    if len(prefix) < 2:
        return {"suggestions": []}
    # Half-open prefix range [prefix, prefix++) — all terms starting with prefix.
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT term FROM pages_vocab
               WHERE term >= ? AND term < ? AND term <> ?
               ORDER BY doc DESC, term ASC LIMIT ?""",
            (prefix, upper, prefix, limit),
        ).fetchall()
        return {"suggestions": [r["term"] for r in rows]}
    finally:
        conn.close()


# --- Optional Ask (local LLM) -------------------------------------------------

class AskRequest(BaseModel):
    question: str
    folder: str | None = None
    method: str | None = None


_ask_jobs: dict[str, dict] = {}
_ask_lock = threading.Lock()


def _ask_worker(job_id: str, question: str, folder: str | None, method: str | None) -> None:
    conn = db.connect(_active_path())
    db.init_schema(conn)
    try:
        result = ask.ask(conn, question, folder=folder, method=method)
        with _ask_lock:
            _ask_jobs[job_id].update({
                "state": "done",
                "answer": result.answer,
                "sources": result.sources,
                "search_queries": result.search_queries,
                "took_ms": result.took_ms,
            })
    except Exception as exc:  # noqa: BLE001
        with _ask_lock:
            _ask_jobs[job_id].update({
                "state": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
    finally:
        conn.close()


@app.get("/api/ask/status")
def api_ask_status(job_id: str | None = None) -> dict:
    """Model availability (no job_id) or background Ask job status."""
    if job_id is None:
        return ask.status()
    with _ask_lock:
        job = _ask_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return {"job_id": job_id, **job}


@app.post("/api/ask")
def api_ask(req: AskRequest) -> dict:
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="empty question")

    if not ask.model_available():
        st = ask.status()
        if not st["llama_installed"]:
            detail = (
                "llama-cpp-python not installed — "
                "pip install -r requirements-llm.txt"
            )
        else:
            detail = (
                f"no GGUF model in {st['models_dir']} — "
                "drop a *.gguf instruct model there (see PACKAGING.md)"
            )
        raise HTTPException(status_code=503, detail=detail)

    with _ask_lock:
        if any(j["state"] == "running" for j in _ask_jobs.values()):
            raise HTTPException(status_code=409, detail="an ask job is already running")

    job_id = uuid.uuid4().hex[:12]
    with _ask_lock:
        _ask_jobs[job_id] = {"state": "running", "error": None}
    threading.Thread(
        target=_ask_worker,
        args=(job_id, question, req.folder, req.method),
        daemon=True,
    ).start()
    return {"job_id": job_id}


class IndexRequest(BaseModel):
    folder: str
    ocr: bool = False
    ocr_lang: str = "eng"
    # Parallel extraction helps on most machines but can be slower on some
    # (few cores, slow disk, AV scanning each spawned process). Off => serial.
    parallel: bool = True
    # Optional directory (e.g. an encrypted/export-safe device) to store this
    # index's database on. Blank => the local data folder. Only applied when the
    # index is first created.
    location: str | None = None


# --- Background indexing jobs -------------------------------------------------
# Indexing (especially OCR) can take minutes, so it runs in a worker thread and
# the UI polls /api/index/status for progress + ETA.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _index_worker(job_id: str, folder: Path, cfg: indexer.OcrConfig,
                  max_workers: int | None, db_path: Path, index_id: str) -> None:
    # Own connection: SQLite objects can't cross threads. Each folder has its
    # own database, so indexing one never disturbs another.
    conn = db.connect(db_path)
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
        indexer.index_folder(conn, folder, ocr_config=cfg,
                              max_workers=max_workers, on_progress=on_progress)
        registry.update_counts(index_id, conn)
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

    # Optional export-safe storage device for this index's database. It must be
    # mounted now (we're about to write to it).
    location = (req.location or "").strip() or None
    if location:
        locp = Path(location).expanduser()
        if not locp.is_dir():
            raise HTTPException(status_code=400,
                                detail=f"storage location not found: {locp}")
        location = str(locp.resolve())

    # Resolve (or create) this folder's own index and make it active. Indexing
    # the same folder again reuses its database; a new folder gets a fresh one.
    entry = registry.resolve_for_folder(str(root), location=location)
    db_path = registry.db_path(entry)

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"state": "running", "phase": "starting", "error": None,
                         "ocr_warning": ocr_warning, "total_files": 0,
                         "total_pages": 0, "files_done": 0, "pages_done": 0,
                         "current_file": "", "elapsed_s": 0, "eta_s": None,
                         "failed": 0, "encrypted": 0}
    cfg = indexer.OcrConfig(enabled=req.ocr, language=req.ocr_lang)
    # Keep OCR-derived content (full document text) on the same device as the
    # index it belongs to, so a controlled index's data never lands locally.
    if entry.get("location"):
        cfg.cache_dir = Path(entry["location"]) / "ocr-cache"
    # None => auto-size to CPU count; 1 => serial extraction.
    max_workers = None if req.parallel else 1
    threading.Thread(target=_index_worker,
                     args=(job_id, root, cfg, max_workers, db_path, entry["id"]),
                     daemon=True).start()
    return {"job_id": job_id, "ocr_warning": ocr_warning,
            "index": _public_index(entry)}


# --- Index management (one database per indexed folder) -----------------------

class ActiveIndexRequest(BaseModel):
    id: str


@app.get("/api/indexes")
def api_indexes() -> dict:
    """List every indexed folder and which one is active."""
    snap = registry.snapshot()
    return {"active": snap["active"],
            "indexes": [_public_index(e) for e in snap["indexes"]]}


@app.post("/api/indexes/active")
def api_set_active_index(req: ActiveIndexRequest) -> dict:
    """Switch which index search/stats/Ask read from."""
    entry = registry.set_active(req.id)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown index")
    return {"active": req.id, "index": _public_index(entry)}


@app.delete("/api/indexes/{index_id}")
def api_remove_index(index_id: str) -> dict:
    """Delete a saved index (its database), leaving the folder's files untouched."""
    with _jobs_lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(status_code=409, detail="an index job is running")
    if not registry.remove(index_id):
        raise HTTPException(status_code=404, detail="unknown index")
    return {"active": registry.snapshot()["active"]}


def _export_filename(entry: dict) -> str:
    leaf = Path(entry["folder"]).name or "index"
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", leaf).strip("_") or "index"
    return f"{safe}.db"


@app.get("/api/indexes/{index_id}/export")
def api_export_index(index_id: str) -> FileResponse:
    """Download an index as a single self-contained .db file, to share."""
    entry = registry.find(index_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown index")
    if not registry.is_available(entry):
        raise HTTPException(status_code=409,
                            detail="this index's storage device is not connected")
    path = registry.db_path(entry)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="index database is missing")
    # Fold the WAL back into the .db so the downloaded file is complete.
    conn = db.connect(path)
    try:
        db.checkpoint(conn)
    finally:
        conn.close()
    return FileResponse(path, media_type="application/octet-stream",
                        filename=_export_filename(entry))


def _validate_index_db(path: Path) -> str | None:
    """Confirm `path` is one of our index databases; return its source folder.

    Raises 400 if the file isn't a usable Offline-Doc-Search index. The source
    folder (from the `meta` table) may be None for indexes built before that
    table existed.
    """
    conn = sqlite3.connect(path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if not {"documents", "pages", "pages_fts"}.issubset(names):
            raise HTTPException(status_code=400,
                                detail="file is not an Offline-Doc-Search index")
        if "meta" in names:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='source_folder'").fetchone()
            return row[0] if row else None
        return None
    except sqlite3.DatabaseError:
        raise HTTPException(status_code=400, detail="not a valid index file")
    finally:
        conn.close()


@app.post("/api/indexes/import")
async def api_import_index(request: Request, location: str = Query("")) -> dict:
    """Add a shared index file (streamed as the raw request body) as a new index.

    Optional `location` stores the imported index on a device (e.g. an
    export-safe drive) instead of the local data folder.
    """
    loc = location.strip() or None
    if loc:
        locp = Path(loc).expanduser()
        if not locp.is_dir():
            raise HTTPException(status_code=400,
                                detail=f"storage location not found: {locp}")
        loc = str(locp.resolve())

    tmp = registry.indexes_dir() / f"_import_{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        if tmp.stat().st_size == 0:
            raise HTTPException(status_code=400, detail="no file received")
        folder = _validate_index_db(tmp)
        entry = registry.import_db(tmp, folder, location=loc)
    finally:
        tmp.unlink(missing_ok=True)

    return _finalize_added(entry)


def _finalize_added(entry: dict) -> dict:
    """Cache an added index's counts (for the switcher label) and return it."""
    conn = db.connect(registry.db_path(entry))
    try:
        db.init_schema(conn)
        registry.update_counts(entry["id"], conn)
    finally:
        conn.close()
    return {"active": entry["id"],
            "index": _public_index(registry.find(entry["id"]))}


class RegisterIndexRequest(BaseModel):
    # aliased to JSON "copy"; the attribute avoids shadowing BaseModel.copy().
    model_config = {"populate_by_name": True}
    path: str
    # False (default) references the .db where it already is — nothing is
    # copied, so a controlled index never lands in the local data folder. True
    # copies it into local storage.
    copy_local: bool = Field(False, alias="copy")


@app.post("/api/indexes/register")
def api_register_index(req: RegisterIndexRequest) -> dict:
    """Add an existing index .db by path — referenced in place, or copied local."""
    src = Path(req.path).expanduser()
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"file not found: {src}")
    folder = _validate_index_db(src)
    if req.copy_local:
        entry = registry.import_db(src, folder)        # copy into local data dir
    else:
        entry = registry.register_existing(src, folder)  # reference where it is
    return _finalize_added(entry)


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
    # inline (not attachment) so the browser renders the PDF in the iframe
    # instead of downloading it. filename still sets a sensible name for "open raw".
    return FileResponse(path, media_type="application/pdf", filename=row["filename"],
                        content_disposition_type="inline")


@app.get("/api/page")
def api_page(id: int, page: int = Query(1, ge=1)) -> dict:
    """Stored text of one page — backs the text viewer for non-PDF documents."""
    conn = _conn()
    try:
        doc = conn.execute(
            "SELECT filename, page_count FROM documents WHERE id=?", (id,)
        ).fetchone()
        if doc is None:
            raise HTTPException(status_code=404, detail="unknown document")
        row = conn.execute(
            "SELECT text_content FROM pages WHERE document_id=? AND page_number=?",
            (id, page),
        ).fetchone()
        return {
            "filename": doc["filename"],
            "page_number": page,
            "page_count": doc["page_count"],
            "text": row["text_content"] if row else "",
        }
    finally:
        conn.close()


@app.get("/api/browse")
def api_browse(path: str = "", files: str = "") -> dict:
    """List subfolders for the folder picker.

    This app is single-user and localhost-bound, so browsing the local
    filesystem is the intended behaviour. Empty path lists drive roots (Windows)
    or `/` (POSIX). When ``files=db`` the response also lists ``*.db`` files in
    the folder, so the picker can be used to choose an index to import.
    """
    import os
    import string

    if not path:
        if os.name == "nt":
            roots = [f"{d}:\\" for d in string.ascii_uppercase
                     if Path(f"{d}:\\").exists()]
            return {"path": "", "parent": None,
                    "dirs": [{"name": r, "path": r} for r in roots],
                    "files": [], "pdf_count": 0}
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

    db_files: list[dict] = []
    if files == "db":
        try:
            db_files = [{"name": e.name, "path": str(e)}
                        for e in sorted(p.glob("*.db"), key=lambda e: e.name.lower())
                        if e.is_file()]
        except (PermissionError, OSError):
            db_files = []

    try:
        pdf_count = sum(1 for _ in p.glob("*.pdf"))
    except (PermissionError, OSError):
        pdf_count = 0

    # At a filesystem/drive root, "up" goes to the roots listing ("").
    parent = "" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": dirs,
            "files": db_files, "pdf_count": pdf_count}


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
