"""Per-folder index registry.

Each indexed folder gets its *own* SQLite database under ``data/indexes/``, so
indexing a new folder never overwrites another one. A small JSON file
(``data/indexes/registry.json``) tracks the set of indexes and which one is
currently *active* — the active index is the one search / stats / suggest / Ask
read from.

Re-indexing the *same* folder deliberately reuses its existing database (keyed
by the normalized folder path), so the incremental skip/delete behaviour in
:func:`app.indexer.index_folder` is preserved. Pointing the indexer at a
*different* folder creates a new database instead of clobbering the old one.

The registry is the only place that knows the on-disk file naming; everything
else works in terms of opaque entries / ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import paths

# Registry mutations are read-modify-write on a single JSON file. The web app
# touches it from both the request thread and the indexing worker thread, so a
# process-local lock keeps those from interleaving.
_lock = threading.RLock()


def indexes_dir() -> Path:
    """Directory holding the per-folder databases and the registry file."""
    d = paths.data_root() / "indexes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _registry_path() -> Path:
    return indexes_dir() / "registry.json"


def _norm(folder: str) -> str:
    """Canonical key for a folder: resolved, OS-normalized (case-folded on NT)."""
    return os.path.normcase(str(Path(folder).resolve()))


def _index_id(folder: str) -> str:
    """Stable short id derived from the normalized folder path."""
    return hashlib.sha256(_norm(folder).encode("utf-8")).hexdigest()[:8]


def _slug(folder: str) -> str:
    """Filesystem-safe, human-recognizable stem for the database filename."""
    p = Path(folder)
    name = p.name or p.drive.rstrip(":\\/") or "index"
    name = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("_")
    return (name or "index")[:40]


def _rand_id() -> str:
    return os.urandom(4).hex()


def _unique_db_name(slug: str, iid: str, dest_dir: Path | None = None) -> str:
    """A ``<slug>-<id>.db`` filename not already present in ``dest_dir``."""
    d = dest_dir or indexes_dir()
    base = f"{slug}-{iid}"
    if not (d / f"{base}.db").exists():
        return f"{base}.db"
    n = 2
    while (d / f"{base}-{n}.db").exists():
        n += 1
    return f"{base}-{n}.db"


def _load() -> dict:
    p = _registry_path()
    if not p.exists():
        return {"active": None, "indexes": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active": None, "indexes": []}
    data.setdefault("active", None)
    data.setdefault("indexes", [])
    return data


def _save(data: dict) -> None:
    p = _registry_path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)  # atomic on the same filesystem


# --- Public read API ----------------------------------------------------------

def snapshot() -> dict:
    """Whole registry: ``{"active": id|None, "indexes": [entry, ...]}``."""
    return _load()


def list_indexes() -> list[dict]:
    return _load()["indexes"]


def get_active() -> dict | None:
    data = _load()
    return _find_in(data, data.get("active"))


def db_path(entry: dict) -> Path:
    """Absolute path of an entry's database file.

    Defaults to ``indexes_dir()``; an entry with a ``location`` lives on that
    directory instead (e.g. an external / export-safe device).
    """
    loc = entry.get("location")
    base = Path(loc) if loc else indexes_dir()
    return base / entry["db"]


def is_available(entry: dict) -> bool:
    """True if the entry's storage is reachable (its device/folder is mounted).

    Default-location indexes are always available; a relocated index is only
    available when its location directory currently exists.
    """
    loc = entry.get("location")
    return True if not loc else Path(loc).is_dir()


def active_db_path() -> Path | None:
    e = get_active()
    return db_path(e) if e else None


def find(index_id_or_folder: str) -> dict | None:
    """Look up an entry by its id, or by a folder path (resolved to its id)."""
    data = _load()
    entry = _find_in(data, index_id_or_folder)
    if entry is not None:
        return entry
    try:
        return _find_in(data, _index_id(index_id_or_folder))
    except OSError:
        return None


def _find_in(data: dict, index_id: str | None) -> dict | None:
    if not index_id:
        return None
    return next((e for e in data["indexes"] if e["id"] == index_id), None)


# --- Public write API ---------------------------------------------------------

def resolve_for_folder(folder: str, location: str | None = None) -> dict:
    """Return the registry entry for ``folder``, creating it if new.

    Either way the returned index becomes the active one. Same folder -> same
    entry (its database is reused); a new folder -> a fresh database.

    ``location`` (a directory, e.g. an external/export-safe device) is recorded
    only when the entry is *first* created — it fixes where that index's
    database lives. Re-indexing the same folder keeps its original location; to
    move an index, remove it and index it again.
    """
    iid = _index_id(folder)
    disp = str(Path(folder).resolve())
    loc = str(Path(location).resolve()) if location else None
    with _lock:
        data = _load()
        entry = _find_in(data, iid)
        if entry is None:
            entry = {
                "id": iid,
                "folder": disp,
                "db": f"{_slug(folder)}-{iid}.db",
                "location": loc,
                "created_at": time.time(),
                "last_indexed_at": None,
                "documents": 0,
                "pages": 0,
            }
            data["indexes"].append(entry)
        else:
            entry["folder"] = disp  # refresh display path if it moved/relettered
        data["active"] = iid
        _save(data)
        return entry


def import_db(src: Path, folder: str | None, location: str | None = None) -> dict:
    """Register an externally produced index database as a new entry.

    ``src`` is a complete, checkpointed SQLite file (already validated by the
    caller); it is copied to ``location`` (if given, e.g. an export-safe device)
    or into ``indexes_dir`` otherwise, under a fresh filename. ``folder`` is the
    database's original source folder, used as the label — and, when it doesn't
    collide with an existing index, as the id (so that re-indexing that same
    folder locally later would update this entry in place). The imported index
    becomes active.
    """
    loc = str(Path(location).resolve()) if location else None
    dest_dir = Path(loc) if loc else indexes_dir()
    with _lock:
        data = _load()
        iid = _index_id(folder) if folder else _rand_id()
        if _find_in(data, iid) is not None:
            iid = _rand_id()                  # never clobber an existing index
        slug = _slug(folder) if folder else "imported"
        db_name = _unique_db_name(slug, iid, dest_dir)
        shutil.copyfile(src, dest_dir / db_name)
        entry = {
            "id": iid,
            "folder": folder or "(imported index)",
            "db": db_name,
            "location": loc,
            "created_at": time.time(),
            "last_indexed_at": None,
            "documents": 0,
            "pages": 0,
        }
        data["indexes"].append(entry)
        data["active"] = iid
        _save(data)
        return entry


def register_existing(db_file: Path, folder: str | None) -> dict:
    """Register an index database *in place* — without copying it.

    The entry points straight at ``db_file`` wherever it already lives (e.g. a
    shared/export-safe drive), so the data is never duplicated into the local
    data folder. Implemented as a located entry whose location is the file's
    parent directory; if that directory later disappears (device unmounted) the
    index simply reports unavailable. The active index becomes this one.
    """
    db_file = Path(db_file).resolve()
    with _lock:
        data = _load()
        iid = _index_id(folder) if folder else _rand_id()
        if _find_in(data, iid) is not None:
            iid = _rand_id()                  # never clobber an existing index
        entry = {
            "id": iid,
            "folder": folder or "(imported index)",
            "db": db_file.name,
            "location": str(db_file.parent),
            "referenced": True,   # points at an external file we must not delete
            "created_at": time.time(),
            "last_indexed_at": None,
            "documents": 0,
            "pages": 0,
        }
        data["indexes"].append(entry)
        data["active"] = iid
        _save(data)
        return entry


def set_active(index_id: str) -> dict | None:
    """Make ``index_id`` the active index. Returns the entry, or None if unknown."""
    with _lock:
        data = _load()
        entry = _find_in(data, index_id)
        if entry is None:
            return None
        data["active"] = index_id
        _save(data)
        return entry


def update_counts(index_id: str, conn) -> None:
    """Refresh cached document/page counts (and last-indexed time) from ``conn``."""
    docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    with _lock:
        data = _load()
        entry = _find_in(data, index_id)
        if entry is not None:
            entry["documents"] = docs
            entry["pages"] = pages
            entry["last_indexed_at"] = time.time()
            _save(data)


def remove(index_id: str) -> bool:
    """Remove an index from the registry.

    For an owned index, its database files are deleted too. For a *referenced*
    index (registered in place from an external/shared location) only the
    registry entry is dropped — the original file is left untouched. If the
    removed index was active, the active pointer falls back to the first
    remaining index (or None). The indexed folder's actual files are never
    touched in any case.
    """
    with _lock:
        data = _load()
        entry = _find_in(data, index_id)
        if entry is None:
            return False
        data["indexes"] = [e for e in data["indexes"] if e["id"] != index_id]
        if data.get("active") == index_id:
            data["active"] = data["indexes"][0]["id"] if data["indexes"] else None
        _save(data)
        if not entry.get("referenced"):
            base = db_path(entry)
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(base) + suffix).unlink()
                except OSError:
                    pass  # absent or locked — best-effort cleanup
        return True
