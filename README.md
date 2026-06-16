# Offline PDF Search

Local-first, fully offline search across a folder of documents — PDFs, plain
text, Office/HTML/ebook formats, and common source-code files. Page-level
results, exact/phrase/prefix queries, BM25 ranking, highlighted snippets —
backed by SQLite FTS5 and PyMuPDF. No network, no cloud, no telemetry.

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

- **Phase 4 (in progress)** — robustness for messy corpora: encrypted PDFs are
  detected (after trying the empty password), recorded as `encrypted`, and not
  retried each run; corrupted/locked files fail without aborting the batch.
  Problem files are surfaced via `/api/issues` and an "issues" link in the UI. A
  folder picker (`/api/browse`) lets you browse to a folder instead of typing a
  path.

- **Phase 4 (cont.)** — offline packaging: a PyInstaller onedir build produces a
  self-contained, network-free bundle (no Python install needed on the target).
  Read-only UI assets are bundled; the writable index lives in a `data/` folder
  beside the executable. The launcher starts the localhost server and opens the
  browser. See [PACKAGING.md](PACKAGING.md).

OCR requires `ocrmypdf` + `tesseract` on PATH (optional). Remaining Phase 4 work:
settings export/import.

**Optional Ask mode** — natural-language questions with cited answers via a local
GGUF model dropped into `models/` (off by default; see [PACKAGING.md](PACKAGING.md)).

## Setup

```sh
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt
# Optional — enable Ask mode:
pip install -r requirements-llm.txt
```

Requires Python 3.11+ and an SQLite build with FTS5 (the CPython default on
Windows/macOS includes it).

## Usage

### Web UI

```sh
python -m app.main          # serves http://127.0.0.1:8765
```

Open the URL, point the index box at a folder of documents, then search. Supported
formats include PDF, `.txt`/`.md`, `.html`, `.docx`, `.epub`, and source code
(`.c`, `.cxx`, `.for`, `.css`, `.py`, and related extensions). Results show
page-level hits with highlighted snippets and a native/OCR badge; clicking a
result previews that page in the embedded viewer. Switch to **Ask** mode to pose
natural-language questions (requires a local GGUF model in `models/`).

### CLI

```sh
python -m app.cli index <folder>          # scan + index a folder tree
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
| `app/formats.py` | Multi-format discovery, extraction, and synthetic pagination |
| `app/indexer.py` | Hash-based change detection, parallel orchestration |
| `app/search.py` | FTS5 MATCH, BM25 ranking, snippets, filters |
| `app/ask.py` | Optional local LLM Ask mode (query expansion + cited RAG) |
| `app/cli.py` | Command-line interface |
| `app/main.py` | FastAPI app: web UI + JSON API + PDF serving |
| `app/paths.py` | Frozen-aware resource/data dirs (source vs PyInstaller build) |
| `app/launcher.py` | Packaged entry point: starts server, opens browser |
| `run_app.py` | Top-level entry script (PyInstaller target) |
| `web/index.html` | Single-file frontend (search, filters, viewer) |
| `packaging/` | PyInstaller spec + build script (see [PACKAGING.md](PACKAGING.md)) |
