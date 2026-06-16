"""Phase 1 CLI: index a folder, search it, show stats.

Each indexed folder keeps its own database; commands default to the active
index (the folder most recently indexed). Use ``--index <folder|id>`` to target
another, ``--db <path>`` to point at a database directly, or ``indexes`` to list.

    python -m app.cli index <folder>
    python -m app.cli search "<query>"
    python -m app.cli stats
    python -m app.cli indexes
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import db, indexer, ocr, registry, search


def _read_db(args) -> Path | None:
    """Database to read from: explicit --db, else --index, else the active one."""
    if args.db:
        return Path(args.db)
    if getattr(args, "index", None):
        entry = registry.find(args.index)
        return registry.db_path(entry) if entry else None
    return registry.active_db_path()


def _cmd_index(args) -> int:
    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a folder: {root}", file=sys.stderr)
        return 2
    ocr_config = indexer.OcrConfig(enabled=args.ocr, language=args.ocr_lang)
    if args.ocr and not ocr.ocr_available():
        print("warning: --ocr requested but ocrmypdf/tesseract not found on PATH; "
              "scanned files will be flagged 'required' but left un-OCR'd.",
              file=sys.stderr)
    # Index into this folder's own database (created on first use, reused on
    # re-index) unless an explicit --db path overrides it.
    if args.db:
        db_path, index_id = Path(args.db), None
    else:
        entry = registry.resolve_for_folder(str(root))
        db_path, index_id = registry.db_path(entry), entry["id"]
    conn = db.connect(db_path)
    db.init_schema(conn)
    t0 = time.perf_counter()
    stats = indexer.index_folder(conn, root, ocr_config=ocr_config,
                                 max_workers=args.workers)
    if index_id:
        registry.update_counts(index_id, conn)
    dt = time.perf_counter() - t0
    print(f"{stats.summary()}  ({dt:.2f}s)")
    if stats.scanned_pdfs:
        label = "OCR pending" if not args.ocr else "still un-OCR'd"
        print(f"  {len(stats.scanned_pdfs)} file(s) look scanned ({label}):")
        for p in stats.scanned_pdfs[:10]:
            print(f"    - {p}")
    return 0


def _cmd_search(args) -> int:
    db_path = _read_db(args)
    if db_path is None:
        print("error: no index found. Run: python -m app.cli index <folder>",
              file=sys.stderr)
        return 2
    conn = db.connect(db_path)
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
    db_path = _read_db(args)
    if db_path is None:
        print("error: no index found. Run: python -m app.cli index <folder>",
              file=sys.stderr)
        return 2
    conn = db.connect(db_path)
    db.init_schema(conn)
    docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    ocr = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE ocr_status='required'"
    ).fetchone()[0]
    print(f"documents={docs} pages={pages} ocr_pending={ocr}")
    return 0


def _cmd_indexes(args) -> int:
    snap = registry.snapshot()
    if not snap["indexes"]:
        print("No indexes yet. Run: python -m app.cli index <folder>")
        return 0
    for e in snap["indexes"]:
        mark = "*" if e["id"] == snap["active"] else " "
        print(f"{mark} {e['id']}  {e['documents']:>6} docs  {e['folder']}")
    print("\n(* = active; the index commands default to it)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Offline PDF search (Phase 1)")
    parser.add_argument("--db", default=None,
                        help="SQLite DB path (overrides the per-folder index)")
    parser.add_argument("--index", default=None,
                        help="read from a specific index by folder path or id "
                             "(search/stats only; default: the active index)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="index a folder of PDFs")
    p_index.add_argument("folder")
    p_index.add_argument("--ocr", action="store_true",
                         help="OCR scanned PDFs via ocrmypdf (slower)")
    p_index.add_argument("--ocr-lang", default="eng",
                         help="Tesseract language(s), e.g. 'eng' or 'eng+deu'")
    p_index.add_argument("--workers", type=int, default=None,
                         help="extraction worker processes (default: auto; 1 = serial)")
    p_index.set_defaults(func=_cmd_index)

    p_search = sub.add_parser("search", help="search the index (FTS5 syntax)")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.set_defaults(func=_cmd_search)

    p_stats = sub.add_parser("stats", help="show index counts")
    p_stats.set_defaults(func=_cmd_stats)

    p_indexes = sub.add_parser("indexes", help="list indexed folders")
    p_indexes.set_defaults(func=_cmd_indexes)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
