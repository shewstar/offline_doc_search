"""FTS5 search: page-level hits, bm25 ranking, highlighted snippets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Hit:
    filename: str
    path: str
    page_number: int
    extraction_method: str
    snippet: str
    rank: float


_SQL = """
SELECT d.filename, d.path, p.page_number, p.extraction_method,
       snippet(pages_fts, 0, ?, ?, ' … ', 12) AS snip,
       bm25(pages_fts) AS rank
FROM pages_fts
JOIN pages p     ON p.id = pages_fts.rowid
JOIN documents d ON d.id = p.document_id
WHERE pages_fts MATCH ?
ORDER BY rank        -- bm25: lower is more relevant
LIMIT ?
"""


def search(conn, query: str, *, limit: int = 50,
           hl_open: str = "[", hl_close: str = "]") -> list[Hit]:
    """Run an FTS5 MATCH query. `query` uses FTS5 syntax (quotes, *, AND/OR/NOT)."""
    query = query.strip()
    if not query:
        return []
    rows = conn.execute(_SQL, (hl_open, hl_close, query, limit)).fetchall()
    return [
        Hit(
            filename=r["filename"],
            path=r["path"],
            page_number=r["page_number"],
            extraction_method=r["extraction_method"],
            snippet=r["snip"],
            rank=r["rank"],
        )
        for r in rows
    ]
