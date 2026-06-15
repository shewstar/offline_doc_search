"""SQLite connection, PRAGMAs, and schema.

The FTS5 design follows the plan: an *external-content* table over `pages`
(text stored once), kept in sync with triggers, tokenized for exact matching
with prefix indexing. PRAGMAs are tuned so search reads never block on indexing
writes (WAL) and so the DB is memory-mapped for low-latency reads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths

# Project root for source runs; the writable data dir is frozen-aware (it lands
# beside the executable in a packaged build). See app/paths.py.
PROJECT_ROOT = paths.resource_root()
DEFAULT_DB_PATH = paths.data_root() / "app.db"

# FTS5 tokenizer: exact matching (no stemming) so reference numbers / codes
# survive; diacritics folded; 2- and 3-char prefix indexes for fast `term*`.
_FTS_TOKENIZE = "unicode61 remove_diacritics 2"
_FTS_PREFIX = "2 3"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL,
    folder        TEXT NOT NULL,
    file_hash     TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    modified_at   REAL NOT NULL,
    page_count    INTEGER NOT NULL DEFAULT 0,
    ocr_status    TEXT NOT NULL DEFAULT 'none',   -- none/required/complete/failed
    last_indexed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number   INTEGER NOT NULL,               -- 1-based
    text_content  TEXT NOT NULL DEFAULT '',
    char_count    INTEGER NOT NULL DEFAULT 0,
    extraction_method TEXT NOT NULL DEFAULT 'native',  -- native/ocr
    UNIQUE(document_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_pages_document ON pages(document_id);

-- External-content FTS: text lives in `pages`, not duplicated here.
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    text_content,
    content='pages',
    content_rowid='id',
    tokenize='{_FTS_TOKENIZE}',
    prefix='{_FTS_PREFIX}'
);

-- Keep the FTS index in sync with the content table.
CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, text_content) VALUES (new.id, new.text_content);
END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, text_content)
        VALUES ('delete', old.id, old.text_content);
END;
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, text_content)
        VALUES ('delete', old.id, old.text_content);
    INSERT INTO pages_fts(rowid, text_content) VALUES (new.id, new.text_content);
END;

-- Read-only view of the FTS vocabulary (term, #docs, #occurrences). Drives
-- search-as-you-type suggestions without storing anything extra — it reflects
-- the live pages_fts index. See app.main /api/suggest.
CREATE VIRTUAL TABLE IF NOT EXISTS pages_vocab USING fts5vocab('pages_fts', 'row');

-- Operational log for auditability/troubleshooting.
CREATE TABLE IF NOT EXISTS index_events (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    path      TEXT,
    event     TEXT NOT NULL,                       -- indexed/skipped/deleted/failed
    detail    TEXT
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with the performance/safety PRAGMAs from the plan."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")        # readers never block writers
    conn.execute("PRAGMA synchronous=NORMAL")      # safe with WAL, much faster
    conn.execute("PRAGMA foreign_keys=ON")         # enable ON DELETE CASCADE
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")     # 256 MB memory-mapped reads
    conn.execute("PRAGMA cache_size=-65536")       # ~64 MB page cache
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def optimize(conn: sqlite3.Connection) -> None:
    """Compact the FTS index — run after large reindex batches."""
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('optimize')")
    conn.commit()


def log_event(conn: sqlite3.Connection, event: str, path: str | None = None,
              detail: str | None = None) -> None:
    import time
    conn.execute(
        "INSERT INTO index_events(ts, path, event, detail) VALUES (?,?,?,?)",
        (time.time(), path, event, detail),
    )
