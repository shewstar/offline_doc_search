"""FTS5 search: page-level hits, bm25 ranking, highlighted snippets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Hit:
    document_id: int
    filename: str
    path: str
    folder: str
    page_number: int
    extraction_method: str
    snippet: str
    rank: float
    modified_at: float = 0.0
    size_bytes: int = 0


_SQL = """
SELECT d.id AS document_id, d.filename, d.path, d.folder,
       d.modified_at, d.size_bytes,
       p.page_number, p.extraction_method,
       snippet(pages_fts, 0, ?, ?, ' … ', 12) AS snip,
       bm25(pages_fts) AS rank
FROM pages_fts
JOIN pages p     ON p.id = pages_fts.rowid
JOIN documents d ON d.id = p.document_id
WHERE pages_fts MATCH ?
  {folder_clause}
  {method_clause}
ORDER BY rank        -- bm25: lower is more relevant
LIMIT ?
"""


def search(conn, query: str, *, limit: int = 50,
           folder: str | None = None, method: str | None = None,
           hl_open: str = "[", hl_close: str = "]") -> list[Hit]:
    """Run an FTS5 MATCH query. `query` uses FTS5 syntax (quotes, *, AND/OR/NOT).

    Optional filters: `folder` (substring match on the document's folder path)
    and `method` ('native' or 'ocr').
    """
    query = query.strip()
    if not query:
        return []

    params: list = [hl_open, hl_close, query]
    folder_clause = ""
    if folder:
        folder_clause = "AND d.folder LIKE ?"
        params.append(f"%{folder}%")
    method_clause = ""
    if method in ("native", "ocr"):
        method_clause = "AND p.extraction_method = ?"
        params.append(method)
    params.append(limit)

    sql = _SQL.format(folder_clause=folder_clause, method_clause=method_clause)
    rows = conn.execute(sql, params).fetchall()
    return [
        Hit(
            document_id=r["document_id"],
            filename=r["filename"],
            path=r["path"],
            folder=r["folder"],
            page_number=r["page_number"],
            extraction_method=r["extraction_method"],
            snippet=r["snip"],
            rank=r["rank"],
            modified_at=r["modified_at"],
            size_bytes=r["size_bytes"],
        )
        for r in rows
    ]
