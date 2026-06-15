# Offline PDF Search

Local-first, fully offline search across a folder of PDFs. Page-level results,
exact/phrase/prefix queries, BM25 ranking, highlighted snippets — backed by
SQLite FTS5 and PyMuPDF. No network, no cloud, no telemetry.

See [offline-pdf-search-plan.md](offline-pdf-search-plan.md) for the full design.

## Status: Phases 1–3

- **Phase 1** — native-text extraction (PyMuPDF) + FTS5 indexing + CLI search.
- **Phase 2** — OCR fallback for scanned PDFs via the OCRmyPDF/Tesseract CLI,
  cached by content hash; degrades gracefully when the toolchain is absent.
- **Phase 3** — local web UI (FastAPI, bound to `127.0.0.1`) with debounced
  search, folder/source filters, highlighted snippets, index controls, and an
  embedded PDF viewer that opens to the matched page. Results are grouped by
  document (ranked by each document's best-matching page, with its top pages
  nested). Indexing runs as a background job with a live progress bar and a
  page-weighted ETA (polled via `/api/index/status`).

OCR requires `ocrmypdf` + `tesseract` on PATH (optional). Phase 4 hardening
(bundled PDF.js viewer, async index progress, packaging) is not done yet.

## Setup

```sh
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt
```

Requires Python 3.11+ and an SQLite build with FTS5 (the CPython default on
Windows/macOS includes it).

## Usage

### Web UI

```sh
python -m app.main          # serves http://127.0.0.1:8765
```

Open the URL, point the index box at a folder of PDFs, then search. Results show
page-level hits with highlighted snippets and a native/OCR badge; clicking a
result previews that page in the embedded viewer.

### CLI

```sh
python -m app.cli index <folder>          # scan + index a folder tree of PDFs
python -m app.cli index <folder> --ocr    # also OCR scanned PDFs (needs toolchain)
python -m app.cli search "<query>"        # FTS5 syntax: "exact phrase", term*, AND/OR/NOT
python -m app.cli stats                   # document / page counts
```

The index lives in `data/app.db` (override with `--db <path>`). Indexing is
incremental: unchanged files are skipped, changed files reindexed, and removed
files dropped from the index.

### Query examples

```sh
python -m app.cli search "termination"
python -m app.cli search '"retention period"'   # exact phrase
python -m app.cli search "docu*"                 # prefix
python -m app.cli search '"SA-2024-0093"'        # exact reference code
```

## Layout

| Path | Role |
|---|---|
| `app/db.py` | Schema, PRAGMAs (WAL/mmap), FTS5 external-content table + triggers |
| `app/extractor.py` | PyMuPDF text extraction + scanned-PDF heuristic |
| `app/ocr.py` | OCRmyPDF/Tesseract fallback, content-hash cache |
| `app/indexer.py` | Discovery, hash-based change detection, orchestration |
| `app/search.py` | FTS5 MATCH, BM25 ranking, snippets, filters |
| `app/cli.py` | Command-line interface |
| `app/main.py` | FastAPI app: web UI + JSON API + PDF serving |
| `web/index.html` | Single-file frontend (search, filters, viewer) |
