# Offline PDF Search

Local-first, fully offline search across a folder of PDFs. Page-level results,
exact/phrase/prefix queries, BM25 ranking, highlighted snippets — backed by
SQLite FTS5 and PyMuPDF. No network, no cloud, no telemetry.

See [offline-pdf-search-plan.md](offline-pdf-search-plan.md) for the full design.

## Status: Phase 1 (proof of concept)

Native-text PDF extraction + FTS5 indexing + CLI search. OCR (Phase 2) and the
local web UI (Phase 3) are not built yet; scanned PDFs are detected and flagged
(`ocr_status='required'`) but not yet OCR'd.

## Setup

```sh
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt
```

Requires Python 3.11+ and an SQLite build with FTS5 (the CPython default on
Windows/macOS includes it).

## Usage

```sh
python -m app.cli index <folder>      # scan + index a folder tree of PDFs
python -m app.cli search "<query>"    # FTS5 syntax: "exact phrase", term*, AND/OR/NOT
python -m app.cli stats               # document / page counts
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
| `app/indexer.py` | Discovery, hash-based change detection, orchestration |
| `app/search.py` | FTS5 MATCH, BM25 ranking, snippets |
| `app/cli.py` | Phase 1 command-line interface |
