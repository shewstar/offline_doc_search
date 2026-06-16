"""FTS5 search: page-level hits, bm25 ranking, highlighted snippets."""

from __future__ import annotations

import re
from dataclasses import dataclass

# FTS5 query operators that must pass through untouched (case-sensitive).
_FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def to_match_query(q: str) -> str:
    """Normalize a user query into a valid FTS5 MATCH string.

    The unicode61 tokenizer splits on punctuation, so a term like ``2.0`` is
    indexed as the tokens ``2`` and ``0``. But a bare ``.`` is also illegal in
    FTS5 query syntax (``fts5: syntax error near "."``), so typing ``2.0``
    raises rather than matching. We wrap any token containing separator
    punctuation in double quotes, turning it into a phrase that tokenizes to the
    same adjacent tokens — so ``2.0`` matches text containing ``2.0``.

    Real FTS5 syntax is preserved: existing quoted phrases, ``AND/OR/NOT/NEAR``,
    a trailing ``*`` (prefix), a leading ``-``/``+``, and grouping/column
    syntax (``()``/``:``) are left untouched.
    """
    out: list[str] = []
    i, n = 0, len(q)
    while i < n:
        ch = q[i]
        if ch.isspace():
            out.append(ch)
            i += 1
        elif ch == '"':                      # copy quoted phrase verbatim
            j = i + 1
            while j < n and q[j] != '"':
                j += 1
            if j < n:
                j += 1                       # include the closing quote
            out.append(q[i:j])
            i = j
        else:                                # a bareword run
            j = i
            while j < n and not q[j].isspace() and q[j] != '"':
                j += 1
            out.append(_fix_word(q[i:j]))
            i = j
    return "".join(out)


def _fix_word(word: str) -> str:
    if word in _FTS_OPERATORS:
        return word
    if any(c in word for c in "():"):        # advanced syntax — leave alone
        return word
    sign, core, star = re.match(r"^([-+]?)(.*?)(\*?)$", word).groups()
    if not core or all(c == "_" or c.isalnum() for c in core):
        return word                          # already a valid bareword
    return f'{sign}"{core.replace(chr(34), chr(34) * 2)}"{star}'


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
    """Run an FTS5 MATCH query. `query` uses FTS5 syntax (quotes, *, AND/OR/NOT);
    tokens with separator punctuation (e.g. `2.0`, `file.txt`) are auto-quoted by
    `to_match_query` so they match instead of raising a syntax error.

    Optional filters: `folder` (substring match on the document's folder path)
    and `method` ('native' or 'ocr').
    """
    query = query.strip()
    if not query:
        return []
    query = to_match_query(query)

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
