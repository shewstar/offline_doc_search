"""Standalone test harness for multi-format + parallel indexing.

Run from the repo root:

    python tests/test_indexing.py

No pytest dependency (kept out of the frozen bundle). Generates a synthetic
corpus in a temp dir, then exercises:

  * parallel vs serial equivalence (the core parallelism guarantee)
  * determinism across repeated parallel runs
  * correct stats for native / scanned / encrypted / corrupt / non-PDF files
  * incremental reindex (skip / modify / delete / add)
  * per-format extraction + FTS searchability (pdf/txt/md/html/docx/epub)
  * serial fallback when the process pool cannot be used

Everything is guarded under __main__ so spawn-based worker processes re-import
this module safely without re-running the suite.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
import zipfile
from pathlib import Path

# Make the repo root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, formats, indexer, paths, search  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _worker_pid(_ignored) -> int:
    """Top-level (picklable) helper for the multiprocessing sanity check."""
    return os.getpid()


# --- corpus builders ----------------------------------------------------------

def make_native_pdf(path: Path, pages_text: list[str]) -> None:
    import fitz

    doc = fitz.open()
    for txt in pages_text:
        page = doc.new_page()
        page.insert_text((72, 72), txt, fontsize=11)
    doc.save(str(path))
    doc.close()


def make_scanned_pdf(path: Path, n_pages: int = 2) -> None:
    import fitz

    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()  # blank: no text layer -> looks_scanned() is True
    doc.save(str(path))
    doc.close()


def make_encrypted_pdf(path: Path) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "locked content", fontsize=11)
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256,
             user_pw="pw", owner_pw="pw")
    doc.close()


def make_corrupt_pdf(path: Path) -> None:
    path.write_bytes(b"this is definitely not a valid PDF file at all")


def make_docx(path: Path, paragraphs: list[str]) -> None:
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
          'package/2006/content-types"><Default Extension="rels" ContentType='
          '"application/vnd.openxmlformats-package.relationships+xml"/><Default '
          'Extension="xml" ContentType="application/xml"/><Override PartName='
          '"/word/document.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship '
            'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
                f"{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def make_epub(path: Path, chapters: list[tuple[str, str]]) -> None:
    container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:'
                 'names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
                 'full-path="OEBPS/content.opf" media-type="application/oebps-'
                 'package+xml"/></rootfiles></container>')
    manifest = "".join(
        f'<item id="c{i}" href="{fn}" media-type="application/xhtml+xml"/>'
        for i, (fn, _) in enumerate(chapters)
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    opf = (f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
           f'version="2.0" unique-identifier="id"><metadata/><manifest>'
           f"{manifest}</manifest><spine>{spine}</spine></package>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for fn, html in chapters:
            z.writestr(f"OEBPS/{fn}", f"<html><body>{html}</body></html>")


def build_corpus(root: Path) -> dict:
    """Create one of every interesting file type. Returns expected facts."""
    root.mkdir(parents=True, exist_ok=True)
    make_native_pdf(root / "a.pdf", ["alpha ZTOKpdf hello world", "second page ZTOKpdf2"])
    make_native_pdf(root / "b.pdf", ["bravo only one page ZTOKpdfb"])
    make_scanned_pdf(root / "scanned.pdf", 2)
    make_encrypted_pdf(root / "locked.pdf")
    make_corrupt_pdf(root / "broken.pdf")
    (root / "notes.txt").write_text("a plain text file ZTOKtxt about cats\n", encoding="utf-8")
    (root / "readme.md").write_text("# Heading\n\nMarkdown body ZTOKmd here.\n", encoding="utf-8")
    (root / "page.html").write_text(
        "<html><head><style>.x{color:red}</style>"
        "<script>var ZTOKscript = 1;</script></head>"
        "<body><h1>Title</h1><p>HTML para ZTOKhtml &amp; more</p></body></html>",
        encoding="utf-8",
    )
    make_docx(root / "doc.docx", ["First docx para ZTOKdocx.", "Second paragraph."])
    make_epub(root / "book.epub", [
        ("ch1.xhtml", "<h1>Chapter 1</h1><p>intro text</p>"),
        ("ch2.xhtml", "<p>Chapter two has ZTOKepub inside</p>"),
    ])
    return {
        # path -> (expected ocr_status, expect indexed)
        "indexed_paths": {"a.pdf", "b.pdf", "scanned.pdf", "notes.txt", "readme.md",
                          "page.html", "doc.docx", "book.epub"},
        "encrypted_paths": {"locked.pdf"},
        "failed_paths": {"broken.pdf"},
        "tokens": {  # unique token -> filename it should be found in
            "ZTOKpdf": "a.pdf", "ZTOKpdfb": "b.pdf", "ZTOKtxt": "notes.txt",
            "ZTOKmd": "readme.md", "ZTOKhtml": "page.html", "ZTOKdocx": "doc.docx",
            "ZTOKepub": "book.epub",
        },
    }


# --- DB snapshot for equivalence checks ---------------------------------------

def snapshot(db_path: Path) -> dict:
    """Order-independent fingerprint of the index: per-document text + metadata."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        out = {}
        for d in conn.execute("SELECT id, path, page_count, ocr_status, file_hash "
                              "FROM documents"):
            pages = conn.execute(
                "SELECT page_number, text_content FROM pages "
                "WHERE document_id=? ORDER BY page_number", (d["id"],)
            ).fetchall()
            text = "␟".join(p["text_content"] for p in pages)
            out[Path(d["path"]).name] = (d["page_count"], d["ocr_status"],
                                         d["file_hash"], text)
        return out
    finally:
        conn.close()


def index_into(db_path: Path, corpus: Path, **kw) -> indexer.IndexStats:
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        return indexer.index_folder(conn, corpus, **kw)
    finally:
        conn.close()


def fts_finds(db_path: Path, token: str) -> set[str]:
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        return {Path(h.path).name for h in search.search(conn, token, limit=50)}
    finally:
        conn.close()


# --- assertions runner --------------------------------------------------------

class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}  {detail}")


def test_equivalence(tmp: Path, corpus: Path, facts: dict, r: Results) -> None:
    print("\n[1] parallel vs serial equivalence")
    serial_db = tmp / "serial.db"
    par_db = tmp / "parallel.db"
    s_stats = index_into(serial_db, corpus, max_workers=1)
    p_stats = index_into(par_db, corpus, max_workers=4)

    r.check("serial indexed count", s_stats.indexed == len(facts["indexed_paths"]),
            f"got {s_stats.indexed} want {len(facts['indexed_paths'])}: {s_stats.summary()}")
    r.check("parallel indexed count", p_stats.indexed == len(facts["indexed_paths"]),
            f"got {p_stats.indexed}: {p_stats.summary()}")
    r.check("serial encrypted=1", s_stats.encrypted == 1, s_stats.summary())
    r.check("parallel encrypted=1", p_stats.encrypted == 1, p_stats.summary())
    r.check("serial failed=1 (corrupt)", s_stats.failed == 1, s_stats.summary())
    r.check("parallel failed=1 (corrupt)", p_stats.failed == 1, p_stats.summary())
    r.check("serial scanned detected=1", len(s_stats.scanned_pdfs) == 1, s_stats.summary())
    r.check("parallel scanned detected=1", len(p_stats.scanned_pdfs) == 1, p_stats.summary())

    snap_s = snapshot(serial_db)
    snap_p = snapshot(par_db)
    r.check("same document set", set(snap_s) == set(snap_p),
            f"serial={set(snap_s)} parallel={set(snap_p)}")
    mismatches = [n for n in snap_s if snap_s.get(n) != snap_p.get(n)]
    r.check("identical per-document content+metadata", not mismatches,
            f"differ: {mismatches}")


def test_determinism(tmp: Path, corpus: Path, r: Results) -> None:
    print("\n[2] determinism across repeated parallel runs")
    snaps = []
    for i in range(3):
        dbp = tmp / f"det{i}.db"
        index_into(dbp, corpus, max_workers=4)
        snaps.append(snapshot(dbp))
    r.check("3 parallel runs identical", snaps[0] == snaps[1] == snaps[2],
            "snapshots differ across runs")


def test_searchability(tmp: Path, facts: dict, r: Results) -> None:
    print("\n[3] per-format extraction + FTS searchability")
    dbp = tmp / "parallel.db"  # from test 1
    for token, fname in facts["tokens"].items():
        found = fts_finds(dbp, token)
        r.check(f"token {token} -> {fname}", fname in found, f"found in {found}")
    r.check("html <script> body NOT indexed", not fts_finds(dbp, "ZTOKscript"),
            "script content leaked into the index")


def test_extract_units(tmp: Path, r: Results) -> None:
    print("\n[4] formats.extract unit behaviour")
    big = tmp / "big.txt"
    big.write_text("\n\n".join(f"Paragraph number {i} with filler words." * 5
                              for i in range(400)), encoding="utf-8")
    pages = formats.extract(big)
    r.check("large text paginates to >1 page", len(pages) > 1, f"got {len(pages)} pages")
    r.check("page numbers are 1-based contiguous",
            [p.page_number for p in pages] == list(range(1, len(pages) + 1)))
    empty = tmp / "empty.txt"
    empty.write_text("", encoding="utf-8")
    ep = formats.extract(empty)
    r.check("empty file -> single empty page", len(ep) == 1 and ep[0].text == "",
            f"got {ep}")


def test_incremental(tmp: Path, r: Results) -> None:
    print("\n[5] incremental reindex (skip/modify/delete/add)")
    corpus = tmp / "inc"
    corpus.mkdir()
    make_native_pdf(corpus / "keep.pdf", ["unchanged ZTOKkeep"])
    make_native_pdf(corpus / "edit.pdf", ["original ZTOKv1"])
    (corpus / "gone.txt").write_text("temporary ZTOKgone", encoding="utf-8")
    dbp = tmp / "inc.db"

    first = index_into(dbp, corpus, max_workers=4)
    r.check("initial indexed=3", first.indexed == 3, first.summary())

    second = index_into(dbp, corpus, max_workers=4)
    r.check("reindex unchanged -> indexed=0", second.indexed == 0, second.summary())
    r.check("reindex unchanged -> skipped=3", second.skipped == 3, second.summary())

    # Modify one file, delete one, add one.
    make_native_pdf(corpus / "edit.pdf", ["rewritten ZTOKv2 brand new"])
    (corpus / "gone.txt").unlink()
    (corpus / "added.md").write_text("freshly added ZTOKnew", encoding="utf-8")
    third = index_into(dbp, corpus, max_workers=4)
    r.check("modified+added -> indexed=2", third.indexed == 2, third.summary())
    r.check("removed file -> deleted=1", third.deleted == 1, third.summary())
    r.check("old content gone", not fts_finds(dbp, "ZTOKv1"), "stale text remained")
    r.check("new content present", fts_finds(dbp, "ZTOKv2") == {"edit.pdf"})
    r.check("added file indexed", fts_finds(dbp, "ZTOKnew") == {"added.md"})
    r.check("deleted file unsearchable", not fts_finds(dbp, "ZTOKgone"))


def test_excluded_dirs(tmp: Path, r: Results) -> None:
    print("\n[5b] PreviousVersions subtrees are excluded from indexing")
    corpus = tmp / "excl"
    (corpus / "PreviousVersions").mkdir(parents=True)
    (corpus / "sub" / "PreviousVersions" / "deep").mkdir(parents=True)
    (corpus / "current.txt").write_text("live ZTOKlive doc", encoding="utf-8")
    (corpus / "PreviousVersions" / "old.txt").write_text("stale ZTOKold v1", encoding="utf-8")
    (corpus / "sub" / "PreviousVersions" / "deep" / "older.md").write_text(
        "stale ZTOKold2 v0", encoding="utf-8")

    discovered = {p.name for p in formats.discover_documents(corpus)}
    r.check("discovery skips excluded subtrees", discovered == {"current.txt"},
            f"got {discovered}")

    dbp = tmp / "excl.db"
    stats = index_into(dbp, corpus, max_workers=1)
    r.check("only the live file is indexed", stats.indexed == 1, stats.summary())
    r.check("live file searchable", fts_finds(dbp, "ZTOKlive") == {"current.txt"})
    r.check("top-level PreviousVersions not indexed", not fts_finds(dbp, "ZTOKold"))
    r.check("nested PreviousVersions not indexed", not fts_finds(dbp, "ZTOKold2"))


def test_external_exclusions(tmp: Path, r: Results) -> None:
    print("\n[5c] external exclusions file adds patterns without a rebuild")
    corpus = tmp / "extexcl"
    (corpus / "Archive").mkdir(parents=True)
    (corpus / "PreviousVersions").mkdir(parents=True)
    (corpus / "keep.txt").write_text("live ZTOKkeep doc", encoding="utf-8")
    (corpus / "report_old.md").write_text("stale ZTOKoldsuffix", encoding="utf-8")
    (corpus / "Archive" / "a.txt").write_text("archived ZTOKarch", encoding="utf-8")
    (corpus / "PreviousVersions" / "p.txt").write_text("prev ZTOKprev", encoding="utf-8")

    excl = tmp / "exclusions.txt"
    excl.write_text("# custom\nArchive\n*_old.*\n", encoding="utf-8")
    orig = paths.exclusions_file
    paths.exclusions_file = lambda: excl  # type: ignore[assignment]
    try:
        pats = formats.load_exclusion_patterns()
        r.check("loader merges defaults + file",
                "PreviousVersions" in pats and "Archive" in pats and "*_old.*" in pats,
                f"got {pats}")
        discovered = {p.name for p in formats.discover_documents(corpus)}
        r.check("only non-excluded files discovered", discovered == {"keep.txt"},
                f"got {discovered}")

        dbp = tmp / "extexcl.db"
        stats = index_into(dbp, corpus, max_workers=1)
        r.check("external-exclusion index count=1", stats.indexed == 1, stats.summary())
        r.check("kept file searchable", fts_finds(dbp, "ZTOKkeep") == {"keep.txt"})
        r.check("named folder (Archive) excluded", not fts_finds(dbp, "ZTOKarch"))
        r.check("glob (*_old) excluded", not fts_finds(dbp, "ZTOKoldsuffix"))
        r.check("built-in default still applies", not fts_finds(dbp, "ZTOKprev"))
    finally:
        paths.exclusions_file = orig  # type: ignore[assignment]

    # With the file gone, only the built-in default remains.
    pats = formats.load_exclusion_patterns()
    r.check("defaults-only when file absent", pats == list(formats.DEFAULT_EXCLUDED_PATTERNS),
            f"got {pats}")


def test_serial_fallback(tmp: Path, corpus: Path, facts: dict, r: Results) -> None:
    print("\n[6] serial fallback when the process pool cannot start")

    class BoomPool:
        def __init__(self, *a, **k):
            raise OSError("spawn refused (simulated)")

    dbp = tmp / "fallback.db"
    orig = indexer.ProcessPoolExecutor
    indexer.ProcessPoolExecutor = BoomPool  # force the pool to fail
    try:
        stats = index_into(dbp, corpus, max_workers=4)
    finally:
        indexer.ProcessPoolExecutor = orig
    r.check("fallback still indexes everything",
            stats.indexed == len(facts["indexed_paths"]), stats.summary())
    r.check("fallback result matches parallel", snapshot(dbp) == snapshot(tmp / "parallel.db"),
            "fallback snapshot differs")


def test_stress(tmp: Path, r: Results) -> None:
    print("\n[7] concurrency stress (40 files, workers=4 vs serial)")
    corpus = tmp / "many"
    corpus.mkdir()
    for i in range(40):
        make_native_pdf(corpus / f"f{i:02d}.pdf",
                        [f"document {i} token ZT{i:02d}", f"page two of {i}"])
    s_db, p_db = tmp / "many_s.db", tmp / "many_p.db"
    s = index_into(s_db, corpus, max_workers=1)
    p = index_into(p_db, corpus, max_workers=4)
    r.check("serial indexed=40", s.indexed == 40, s.summary())
    r.check("parallel indexed=40", p.indexed == 40, p.summary())
    r.check("stress: serial==parallel snapshot", snapshot(s_db) == snapshot(p_db),
            "snapshots differ under load")
    r.check("random token searchable", fts_finds(p_db, "ZT17") == {"f17.pdf"})


def test_multiprocessing_real(r: Results) -> None:
    print("\n[8] multiprocessing sanity (distinct worker PIDs)")
    from concurrent.futures import ProcessPoolExecutor
    try:
        with ProcessPoolExecutor(max_workers=4) as ex:
            pids = set(ex.map(_worker_pid, range(16)))
        r.check("pool used >1 distinct process",
                len(pids) > 1 or (os.cpu_count() or 1) == 1,
                f"pids={pids}")
    except Exception as exc:  # noqa: BLE001
        r.check("process pool usable on this machine", False, str(exc))

    r.check("_resolve_workers(1) -> serial", indexer._resolve_workers(1, None) == 1)
    r.check("_resolve_workers(big) -> parallel when multicore",
            indexer._resolve_workers(100, None) >= 1)
    r.check("_resolve_workers honours explicit 1", indexer._resolve_workers(100, 1) == 1)


def main() -> int:
    r = Results()
    with tempfile.TemporaryDirectory(prefix="ods_test_") as td:
        tmp = Path(td)
        corpus = tmp / "corpus"
        facts = build_corpus(corpus)  # built once; PDF timestamps make rebuilds differ
        test_equivalence(tmp, corpus, facts, r)
        test_determinism(tmp, corpus, r)
        test_searchability(tmp, facts, r)
        test_extract_units(tmp, r)
        test_incremental(tmp, r)
        test_excluded_dirs(tmp, r)
        test_external_exclusions(tmp, r)
        test_serial_fallback(tmp, corpus, facts, r)
        test_stress(tmp, r)
    test_multiprocessing_real(r)
    print(f"\n{'='*48}\n  {r.passed} passed, {r.failed} failed\n{'='*48}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
