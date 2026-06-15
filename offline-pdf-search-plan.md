# Offline PDF Search App Plan

## Overview

This document describes a comprehensive plan for building a local-first application that performs meaningful search across a folder of PDF documents on a laptop with 16 GB RAM, no internet access, and no dependency on cloud AI services.[cite:43][cite:58]

The recommended architecture prioritizes reliability, auditability, low memory usage, and strong search ergonomics over semantic inference. The core approach is PDF text extraction, conditional OCR for scanned documents, page-level indexing, and embedded full-text search using SQLite FTS5.[cite:43][cite:58][cite:2][cite:59][cite:61][cite:63]

## Goals

The application should achieve the following:

- Search inside a folder tree of PDFs entirely offline.[cite:43]
- Run comfortably on a standard laptop with 16 GB RAM.[cite:58]
- Support both text PDFs and scanned/image-based PDFs through OCR fallback.[cite:59][cite:62]
- Return page-level matches with useful snippets and source provenance.[cite:31][cite:66]
- Allow reindexing as files are added, modified, or removed.[cite:63][cite:69]
- Remain transparent enough for internal review and compliance discussions.[cite:2][cite:43]

## Constraints

| Constraint | Implication |
|---|---|
| No internet access | All parsing, OCR, indexing, and search must run locally.[cite:43] |
| Official / restricted documents | Avoid cloud APIs, telemetry, and external dependencies at runtime.[cite:2][cite:43] |
| 16 GB RAM laptop | Prefer embedded/local components over heavyweight services or large models.[cite:58][cite:63] |
| PDF-heavy corpus | Must handle a mix of native-text PDFs and scanned PDFs.[cite:59][cite:61] |

## Recommended architecture

The recommended pipeline is:

1. Folder selection and scan.
2. File fingerprinting and change detection.
3. PDF text extraction using PyMuPDF for documents with usable text layers.[cite:61][cite:64]
4. OCR fallback using OCRmyPDF and Tesseract when the extracted text is missing or poor.[cite:59][cite:62]
5. Page-level normalization and metadata extraction.
6. Storage of pages, metadata, and extracted text in SQLite.[cite:63]
7. Indexing with SQLite FTS5 for fast full-text queries, ranking, prefix matching, and snippet generation.[cite:63][cite:66][cite:72]
8. Local UI for search, filters, result preview, and opening the source PDF to a page.

This architecture is intentionally conservative. PyMuPDF is a high-performance local PDF library, and SQLite FTS5 provides embedded full-text search features without needing a separate search server.[cite:61][cite:64][cite:63]

## Why this approach fits 16 GB RAM

A 16 GB laptop is sufficient because the design is primarily disk-backed rather than model-backed.[cite:58][cite:63] SQLite FTS5 stores the search index on disk and executes queries efficiently inside the application process, which avoids the memory cost of running external search engines or local LLM stacks.[cite:63][cite:69]

Conditional OCR also keeps the workload bounded. OCR should only run for files that do not have a valid text layer, rather than for every document on every scan.[cite:59][cite:62]

## Technology choices

### Core stack

| Layer | Recommendation | Reason |
|---|---|---|
| UI | Static HTML/JS served locally (no Node runtime shipped) | Single runtime to package and audit; trivial to air-gap. |
| Backend | Python (FastAPI + uvicorn) bound to `127.0.0.1` | Excellent PDF/OCR tooling ecosystem; one process. |
| Embedded PDF viewer | PDF.js bundled locally | Reliable offline open-to-page inside the UI (see below). |
| PDF extraction | PyMuPDF | High-performance local extraction and page access.[cite:61][cite:64] |
| OCR | OCRmyPDF + Tesseract | Mature local OCR path for scanned PDFs.[cite:59][cite:62] |
| Database | SQLite | Embedded, portable, auditable single-file storage.[cite:63] |
| Search index | SQLite FTS5 | Embedded full-text search with ranking and snippets.[cite:63][cite:66][cite:72] |

### Recommended implementation direction

The most practical build for a solo engineer targeting an offline, restricted environment is a **single Python runtime** rather than a React + Tauri/Electron + Python stack. A separate desktop wrapper means bundling and air-gapping a Node or Rust runtime alongside a Python interpreter, which is the hardest part to package and the hardest to justify to cautious reviewers. The recommended build is:

- **Backend:** Python with FastAPI served by uvicorn, bound to `127.0.0.1` only (never `0.0.0.0`). No external network listener.
- **Frontend:** Plain HTML/CSS/JS (optionally a light framework like Preact/Alpine), built at dev time into static files and served by the same FastAPI process. No Node runtime is shipped to users.
- **Embedded viewer:** PDF.js bundled locally for preview and open-to-page.
- **Storage:** single SQLite database plus an optional cache folder for OCR artifacts.
- **Packaging:** PyInstaller (onedir) to a launcher executable, or a frozen venv. Tesseract, Ghostscript, and the required `*.traineddata` language packs must be bundled or installed as documented offline dependencies.

This collapses the runtime count to one, removes the IPC boundary, and keeps the entire system inside a single auditable codebase.

### Why the local web UI also fixes "open to page"

Open-to-page was a risk in a native-viewer design because there is no reliable, internet-free way to open an arbitrary Windows PDF handler at a specific page. Serving a **bundled PDF.js viewer** inside the local UI removes that dependency entirely: the app can load the source PDF and jump to the matched page number directly in the browser view, identically across machines and with no network access. "Open in system viewer" can remain a secondary, best-effort action.

## Data model

A page-level schema is recommended because users usually need the exact page of the hit rather than only the document name.[cite:31][cite:50]

### Suggested tables

#### `documents`

| Column | Purpose |
|---|---|
| `id` | Internal document ID |
| `path` | Full local file path |
| `filename` | File name for display |
| `folder` | Folder grouping/filtering |
| `file_hash` | Change detection fingerprint |
| `modified_at` | Last modified timestamp |
| `page_count` | Total pages |
| `ocr_status` | none / required / complete / failed |
| `last_indexed_at` | Operational audit field |

#### `pages`

| Column | Purpose |
|---|---|
| `id` | Internal page ID |
| `document_id` | Parent document reference |
| `page_number` | 1-based page number |
| `text_content` | Cleaned extracted text |
| `char_count` | Debugging / quality metric |
| `extraction_method` | native / OCR |
| `quality_score` | Optional heuristic quality signal |

#### `pages_fts`

Use an FTS5 virtual table over page text to support `MATCH`, ranking, prefix search, and snippet extraction.[cite:63][cite:66][cite:72]

The following FTS5 configuration is chosen specifically to keep search fast and the database small (search speed is the primary product requirement):

- **External-content table.** `pages_fts` is created with `content='pages'` and `content_rowid='id'`, so page text is stored **once** in `pages` rather than duplicated inside the FTS index. A smaller database means more of it stays in the OS page cache, which is the dominant factor in query latency. The FTS table is kept in sync with `AFTER INSERT/UPDATE/DELETE` triggers on `pages` (issuing the FTS5 `delete` command on update/delete). `snippet()` and `highlight()` still work because they read from the content table.
- **Tokenizer: `unicode61 remove_diacritics 2`.** This preserves exact matching of clauses, reference numbers, and codes — important for official documents where users search for precise strings. Stemming (`porter`) is deliberately **not** the default because it mangles IDs and breaks exact matches; it can be offered later as an optional "broaden results" mode via a second index.
- **Prefix indexing: `prefix='2 3'`.** Pre-builds 2- and 3-character prefix indexes so partial-term/typeahead queries (`term*`) stay fast instead of scanning.
- **Ranking via `bm25()`** with column weights (e.g. boost title/filename over body), computed inside the query — no external ranking pass.

Fuzzy / OCR-tolerant matching (e.g. a `trigram` tokenizer or `spellfix1`) is intentionally left out of the default index because it multiplies index size and slows queries — it conflicts with the speed and RAM goals. If needed for poor-quality scans, add it as a **separate, secondary** index queried only on explicit user request, not on every search.

#### `index_events`

This optional table stores operational logs such as indexed, skipped, OCR-failed, or deleted. It is useful for troubleshooting and internal traceability.[cite:2][cite:43]

## Ingestion flow

### Step 1: Discover files

The user selects a root folder. The app recursively scans for PDFs and records file path, size, modified time, and a content fingerprint.

A lightweight fingerprint can be a hash of file bytes or a compound of size plus modification time for faster incremental scans. Full hashing is safer but slower for large corpora.

### Step 2: Detect changes

The app compares the current scan to indexed records:

- New files are queued for indexing.
- Modified files are reindexed.
- Deleted files are removed from the database and search index.
- Unchanged files are skipped.

This incremental strategy matters on a 16 GB laptop because it avoids repeated heavy work and keeps reindex times practical.[cite:58][cite:69]

### Step 3: Extract native text

PyMuPDF should be used first because it is fast and can extract text page by page from PDFs with valid text layers.[cite:61][cite:64]

At this stage, collect:

- Page text.
- Page count.
- Optional document metadata such as title or author if present.
- Heuristics about extraction quality, such as very low character counts or garbled output.

### Step 4: Decide whether OCR is required

OCR should run only when necessary. Common triggers include:

- Empty or near-empty extracted text.
- Repeated unreadable glyphs.
- Image-only scanned PDFs.
- Very low text density across most pages.

OCRmyPDF requires Python 3.11+ and Tesseract 4.1.1+ and can add a searchable text layer to scanned PDFs using local tooling.[cite:59] That makes it well suited to an offline indexing pipeline.

### Step 5: OCR fallback

For OCR-required files:

- Process the original PDF with OCRmyPDF.
- Store the OCR output in a controlled cache folder or replace it only if policy allows.
- Re-extract text from the OCR-processed output.
- Mark the document OCR status accordingly.

The app should allow OCR to be disabled for very large folders or run as a background queue so the UI stays responsive.

### Step 6: Normalize text

Before indexing, normalize extracted text:

- Collapse excessive whitespace.
- Standardize newlines.
- Remove obvious OCR noise where safe.
- Preserve page boundaries.
- Optionally preserve headings if they can be inferred cleanly.

The goal is not to over-clean. For search systems, aggressive normalization can destroy meaningful exact-match behavior.

### Step 7: Write to SQLite and FTS

Insert document and page rows, then update the FTS5 table. SQLite FTS5 supports ranking functions such as `bm25()` and snippet/highlight helpers that make result previews much better without external tooling.[cite:63][cite:66]

## Search behavior

A meaningful offline search tool should support more than a single free-text box.

### Query capabilities

Recommended support:

- Exact keyword search.
- Phrase search using quotes.
- Prefix matching for partial terms.[cite:66][cite:72]
- Boolean combinations for advanced users.
- Optional fuzzy matching for OCR-imperfect corpora.
- Filters by folder, filename, modified date, and OCR/native status.

### Ranking

Default ranking should combine:

- FTS BM25 relevance.[cite:66]
- Exact phrase boosts.
- Filename/title boosts.
- Recent document boost, if useful for the workflow.

This can remain rule-based and still feel powerful.

### Results

Each result should show:

- Document name.
- Page number.
- Short snippet with highlighted matches.
- Folder path or logical folder label.
- Extraction method indicator such as Native or OCR.
- Action to open the file directly on that page where supported.

Page-level hits are usually far more useful than document-level hits for operational document search.[cite:31][cite:50]

## User interface plan

### Main views

#### 1. Corpus setup

- Select root folder.
- Show current index status.
- Display counts: files found, indexed, OCR pending, failed.
- Reindex button and optional scheduled/manual modes.

#### 2. Search view

- Main query field.
- Filter panel.
- Sort options such as relevance or most recent.
- Result list with snippets.
- Preview pane if feasible.

#### 3. Document preview

- Show top matching pages.
- Highlight hits in context.
- Jump to PDF page in the system viewer or embedded viewer.

#### 4. Index operations

- Progress indicator for indexing and OCR queue.
- Failure list with retry actions.
- Logs for skipped, deleted, and changed files.

### UX details that matter

- Search should feel instant for indexed corpora.
- Snippets should show enough context to avoid opening every result.
- Filters should be sticky during the session.
- Reindexing should not block searching.
- OCR failures should be visible rather than silent.

## Performance plan

### Memory strategy

To stay comfortable on 16 GB RAM:[cite:58]

- Process one file at a time during indexing.
- Process OCR jobs in a queue with bounded concurrency.
- Do not load entire corpora into memory.
- Keep previews lazy-loaded.
- Use SQLite transactions in batches for speed without huge memory spikes.[cite:63]

### Search latency (primary requirement)

Because slow search is the failure mode the team most wants to avoid, the following are treated as hard design rules, not optional tuning:

- **`PRAGMA journal_mode=WAL`.** Readers (search) never block on writers (indexing), so search stays responsive while reindexing or OCR runs in the background. This is how the "reindexing should not block searching" promise is actually delivered.
- **`PRAGMA synchronous=NORMAL`** with WAL — safe for this workload and much faster on writes.
- **`PRAGMA mmap_size`** set generously (e.g. a few hundred MB) so SQLite memory-maps the DB and reads page data without syscall overhead.
- **`PRAGMA cache_size`** tuned to a fixed budget that fits the 16 GB envelope.
- **Periodic `optimize`** (`INSERT INTO pages_fts(pages_fts) VALUES('optimize')`) after large reindex batches to keep the FTS index from fragmenting and slowing over time.
- **Cap result work per query:** paginate (e.g. top 50), generate `snippet()` only for the returned page, and bound snippet length.
- **Keep the DB on local SSD**, never on a network share — a remote DB file silently destroys FTS latency.

### Practical optimizations

- Skip unchanged files; default to content hashing as the source of truth, using size+mtime only as a pre-filter for which files to hash.
- Reuse OCR results across moves/renames by keying on `file_hash`, so a relocated file is not re-OCR'd.
- Cache OCR outputs or OCR status.[cite:59][cite:62]
- Store page text, not raw rendered page images, unless preview generation is explicitly needed.
- Batch SQLite writes in transactions (per document, or per N pages) to avoid per-row fsync cost.
- Limit snippet length in the main list.
- Use debounce in the search box.

### Expected bottlenecks

The main bottleneck will usually be OCR, not querying.[cite:59][cite:62] Native-text extraction with PyMuPDF and searching with SQLite FTS5 should be relatively fast on a laptop, while scanned-image OCR will dominate indexing time for poor-quality document sets.[cite:61][cite:63]

## Security and compliance design

Because the tool is intended for official or restricted documents, the implementation should assume review by cautious stakeholders.[cite:2][cite:43]

Recommended controls:

- No internet calls of any kind at runtime.[cite:43]
- No analytics, telemetry, or crash reporting.[cite:43]
- No auto-update mechanism in restricted mode.
- Local-only logs.
- Clear storage locations for DB, cache, and OCR outputs.
- Optional “portable mode” where all data stays within one approved folder.
- Configurable retention and purge options for temporary OCR artifacts.

The less magical the system feels, the easier it will be to justify internally.

## Development phases

### Phase 1: Proof of concept

Deliver a command-line or minimal local UI prototype that can:

- Scan one folder.
- Extract text from native PDFs.
- Index page text into SQLite FTS5.
- Search and print matching page results.

Success criteria:

- Can search a test folder offline.
- Returns correct file and page references.
- Query latency feels acceptable for a small corpus.

### Phase 2: OCR support

Add:

- OCR detection heuristics.
- OCRmyPDF/Tesseract integration.[cite:59][cite:62]
- OCR status tracking.
- Reindex rules that avoid duplicate OCR work.

Success criteria:

- Scanned PDFs become searchable.
- OCR errors are logged and visible.
- Native-text PDFs still follow the fast path.

### Phase 3: Desktop UX

Add:

- Search interface.
- Filter sidebar.
- Snippet highlighting.
- Open-to-page workflow.
- Index statistics and progress.

Success criteria:

- Non-technical user can point the app at a folder and search it successfully.
- Search remains usable during background indexing.

### Phase 4: Hardening

Add:

- Better logging.
- Robust deletion/update handling.
- Settings export/import.
- Packaging for internal installation.
- Performance testing on realistic corpora.

Success criteria:

- Can survive messy real-world folders.
- Can be demonstrated as a controlled internal tool.

## Testing plan

### Corpus test categories

Test against at least these document types:

- Native-text PDFs.
- Scanned PDFs.
- Mixed PDFs with some OCR and some native text.
- Large PDFs with hundreds of pages.
- Poor-quality scans.
- Files with unusual filenames and folder paths.

### Functional tests

- Exact term search returns correct pages.
- Phrase search narrows correctly.
- Reindex reflects added/changed/deleted files.
- OCR fallback triggers only when needed.
- Opening to the matched page works reliably.

### Performance tests

- Initial index time on a representative corpus.
- Reindex time after small changes.
- Search latency on larger corpora.
- RAM use during native extraction and OCR-heavy runs.

### Failure tests

- Corrupted PDF.
- OCR failure.
- File moved during indexing.
- Locked file.
- Duplicate filenames in different folders.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| OCR is slow | Long first-time indexing | OCR only when needed; background queue; status visibility.[cite:59][cite:62] |
| OCR quality is poor | Missed hits | Support fuzzy search, show OCR status, allow retry or alternate settings. |
| Large corpus causes long scans | Poor UX | Use incremental indexing and skip unchanged files.[cite:69] |
| PDF parsing inconsistencies | Missing text | Use PyMuPDF fast path and maintain extraction diagnostics.[cite:61][cite:64] |
| Compliance concerns | Project blocked | Remove telemetry, document storage behavior clearly, keep everything local.[cite:2][cite:43] |

## Suggested folder structure

```text
pdf-search-app/
  app/
    main.py          # FastAPI app, binds 127.0.0.1, serves static UI + API
    indexer.py       # discovery, change detection, orchestration
    extractor.py     # PyMuPDF native text extraction
    ocr.py           # OCRmyPDF/Tesseract fallback
    search.py        # FTS5 query building, ranking, snippets
    models.py        # schema + migrations (FTS5 external-content + triggers)
    settings.py
  web/               # static frontend (HTML/CSS/JS), built at dev time
    index.html
    assets/
    vendor/pdfjs/    # bundled PDF.js viewer (offline open-to-page)
  data/
    app.db
    ocr-cache/
    logs/
  test-corpus/
  docs/
    offline-pdf-search-plan.md
```

Everything runs under one Python process, and `web/` is served as static files by the same FastAPI app — no separate frontend runtime is shipped.

## Suggested milestone plan

### Week 1

- Create SQLite schema.
- Build PDF discovery and indexing pipeline.
- Extract native text with PyMuPDF.[cite:61][cite:64]
- Build basic CLI search over FTS5.[cite:63]

### Week 2

- Add incremental reindexing.
- Add page-level snippets and ranking.[cite:66]
- Add basic local UI.

### Week 3

- Integrate OCRmyPDF/Tesseract.[cite:59][cite:62]
- Add OCR heuristics and status tracking.
- Add progress and failure reporting.

### Week 4

- Add open-to-page support.
- Polish filters and preview.
- Test on a realistic offline corpus.
- Package an internal demo build.

## Final recommendation

The best v1 for this constraint set is a local desktop-style search tool built around PyMuPDF, OCRmyPDF/Tesseract, SQLite, and FTS5.[cite:58][cite:59][cite:61][cite:63] That architecture is capable of meaningful search on a 16 GB laptop without relying on internet access or heavyweight AI infrastructure, while remaining transparent enough for restricted-document environments.[cite:43][cite:2]

The most important product decision is to optimize for trust and speed rather than simulated intelligence. If users can reliably find the correct PDF and page in a few seconds, the app will already be delivering substantial value.
