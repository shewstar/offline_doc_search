"""Phase 1 CLI: index a folder, search it, show stats.

    python -m app.cli index <folder>
    python -m app.cli search "<query>"
    python -m app.cli stats
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import db, indexer, search


def _cmd_index(args) -> int:
    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a folder: {root}", file=sys.stderr)
        return 2
    conn = db.connect(args.db)
    db.init_schema(conn)
    t0 = time.perf_counter()
    stats = indexer.index_folder(conn, root)
    dt = time.perf_counter() - t0
    print(f"{stats.summary()}  ({dt:.2f}s)")
    if stats.scanned_pdfs:
        print(f"  {len(stats.scanned_pdfs)} file(s) look scanned (OCR pending, Phase 2):")
        for p in stats.scanned_pdfs[:10]:
            print(f"    - {p}")
    return 0


def _cmd_search(args) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    t0 = time.perf_counter()
    hits = search.search(conn, args.query, limit=args.limit)
    dt = (time.perf_counter() - t0) * 1000
    if not hits:
        print(f"No matches.  ({dt:.1f} ms)")
        return 0
    for h in hits:
        tag = "OCR" if h.extraction_method == "ocr" else "native"
        print(f"\n{h.filename}  p.{h.page_number}  [{tag}]")
        print(f"  {h.path}")
        print(f"  {h.snippet}")
    print(f"\n{len(hits)} hit(s).  ({dt:.1f} ms)")
    return 0


def _cmd_stats(args) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    ocr = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE ocr_status='required'"
    ).fetchone()[0]
    print(f"documents={docs} pages={pages} ocr_pending={ocr}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Offline PDF search (Phase 1)")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="SQLite DB path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="index a folder of PDFs")
    p_index.add_argument("folder")
    p_index.set_defaults(func=_cmd_index)

    p_search = sub.add_parser("search", help="search the index (FTS5 syntax)")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.set_defaults(func=_cmd_search)

    p_stats = sub.add_parser("stats", help="show index counts")
    p_stats.set_defaults(func=_cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
