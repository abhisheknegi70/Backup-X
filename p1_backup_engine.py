"""P1 — Backup Engine: fixed-size chunking, SHA-256 hashing, vault storage, dedup.

Flow for every backup:
    1. scan   — hash each file into CHUNK_SIZE pieces, write unseen chunks to the
                vault (content-addressed: file name == SHA-256 of its bytes).
                Files whose path/size/mtime match an earlier backup are NOT
                re-read; their chunk list is copied from the DB instead.
    2. commit — record the snapshot + all files + all chunk rows in ONE DB
                transaction (see p3_database.commit_snapshot).

Chunks are written before the DB commit, so a crash can leave orphan chunks
(harmless; gc() removes them) but never a snapshot pointing at missing data.
"""

import hashlib
import os
import re
import threading
import uuid
import zlib

import config

CHUNK_SIZE = config.CHUNK_SIZE          # active size for new backups; see set_chunk_size()
COMPRESS_LEVEL = config.COMPRESS_LEVEL
VAULT_DIR = config.VAULT_DIR
os.makedirs(VAULT_DIR, exist_ok=True)

# Serialises backups and garbage collection. Without it, gc() could delete a
# chunk that a running backup has written but not yet committed to the DB.
BACKUP_LOCK = threading.Lock()

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Directory names never worth backing up, wherever they appear.
SKIP_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", "node_modules", ".backupx_vault"}
_TMP_SUFFIX = ".backupx-tmp"


class CorruptChunkError(Exception):
    pass


class MissingChunkError(Exception):
    pass


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _protected_paths():
    """The app's own files/folders, skipped if someone watches the project dir."""
    names = ["main.py", "config.py", "p1_backup_engine.py", "p2_monitor.py", "p3_database.py",
             "p4_restore.py", "p4_restore_web.py", "requirements.txt", "README.md",
             ".env", ".env.example", ".gitignore", "static", "tests", ".vscode"]
    paths = {os.path.join(config.APP_DIR, n) for n in names}
    db = os.path.abspath(config.DB_PATH)
    paths |= {db, db + "-wal", db + "-shm", db + "-journal",
              os.path.abspath(VAULT_DIR)}
    return {os.path.normcase(p) for p in paths}


def is_excluded(abs_path):
    """True if a path should never be backed up (component-based, not substring).

    The old code used substring checks, so a user file like "static_ip_notes.txt"
    or anything under ".github/" was silently skipped.
    """
    norm = os.path.normcase(os.path.abspath(abs_path))
    if norm.endswith(_TMP_SUFFIX):
        return True
    parts = norm.replace("\\", "/").split("/")
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    for protected in _protected_paths():
        if norm == protected or norm.startswith(protected + os.sep):
            return True
    return False


def to_rel(path, base):
    """Relative path with forward slashes, so snapshots are portable across OSes."""
    return os.path.relpath(path, base).replace("\\", "/")


def safe_join(base, rel):
    """Join a stored relative path onto base, refusing anything that escapes it.

    Paths come from the DB or from a *remote* machine during cross-device pull,
    so "../../Windows/System32/x.dll" or "C:/x" must be rejected.
    """
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts):
        raise ValueError(f"Unsafe path in snapshot: {rel!r}")
    base_abs = os.path.abspath(base)
    out = os.path.abspath(os.path.join(base_abs, *parts))
    if os.path.commonpath([base_abs, out]) != base_abs:
        raise ValueError(f"Unsafe path in snapshot: {rel!r}")
    return out


def safe_filename(name):
    """Reduce a client-supplied upload name to a plain file name."""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    if name in ("", ".", "..") or ":" in name:
        raise ValueError(f"Invalid file name: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Vault I/O
# ---------------------------------------------------------------------------

def is_valid_hash(sha256_hex):
    return bool(_HEX64.match(sha256_hex or ""))


def chunk_path(sha256_hex):
    if not is_valid_hash(sha256_hex):
        raise ValueError("Invalid chunk hash")
    return os.path.join(VAULT_DIR, sha256_hex)


def has_chunk(sha256_hex):
    return os.path.exists(chunk_path(sha256_hex))


def set_chunk_size(size):
    """Set the chunk size used by every later backup (manual, selected, upload, auto)."""
    global CHUNK_SIZE
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("Chunk size must be a whole number of bytes")
    if not config.MIN_CHUNK_SIZE <= size <= config.MAX_CHUNK_SIZE:
        raise ValueError(f"Chunk size must be between {config.MIN_CHUNK_SIZE} bytes "
                         f"and {config.MAX_CHUNK_SIZE} bytes")
    CHUNK_SIZE = size
    return CHUNK_SIZE


# On-disk chunk format. The file NAME is always the SHA-256 of the RAW bytes, so
# dedup, verification and cross-device transfer are unaffected by compression.
#   new files:     b"BXZ1" + method byte (0 = stored, 1 = zlib) + payload
#   legacy files:  the raw bytes with no header (still readable)
_MAGIC = b"BXZ1"


def _encode(data):
    """Compress a chunk; fall back to 'stored' when compression doesn't shrink it."""
    if COMPRESS_LEVEL == 0:
        return data                                   # legacy raw layout
    packed = zlib.compress(data, COMPRESS_LEVEL)
    if len(packed) < len(data):
        return _MAGIC + b"\x01" + packed
    return _MAGIC + b"\x00" + data                    # already-compressed data (zip, jpg, mp4)


def _decode_candidates(blob):
    """Yield possible raw contents of a vault file, most likely first.

    A legacy raw chunk could in theory begin with the magic bytes, so the caller
    checks each candidate against the chunk's SHA-256 name.
    """
    if blob[:4] == _MAGIC and len(blob) > 5:
        try:
            if blob[4] == 1:
                yield zlib.decompress(blob[5:])
            elif blob[4] == 0:
                yield blob[5:]
        except zlib.error:
            pass
    yield blob


def store_chunk(sha256_hex, data):
    """Write chunk if not already present. Atomic: temp file + rename.

    Returns the number of bytes written to disk, or None if the chunk already existed.
    """
    dest = chunk_path(sha256_hex)
    if os.path.exists(dest):
        return None
    blob = _encode(data)
    tmp = f"{dest}.{uuid.uuid4().hex}{_TMP_SUFFIX}"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, dest)   # a crash never leaves a truncated chunk under its real name
    return len(blob)


def read_chunk(sha256_hex, verify=True):
    """Read a chunk's raw bytes; with verify=True, confirm they hash to its name."""
    path = chunk_path(sha256_hex)
    if not os.path.exists(path):
        raise MissingChunkError(sha256_hex)
    with open(path, "rb") as f:
        blob = f.read()
    candidates = list(_decode_candidates(blob))
    if not verify:
        return candidates[0]
    for data in candidates:
        if hashlib.sha256(data).hexdigest() == sha256_hex:
            return data
    raise CorruptChunkError(sha256_hex)


def check_chunk(sha256_hex):
    """'ok', 'missing' or 'corrupt' (unreadable, truncated, or bytes don't match the name)."""
    try:
        read_chunk(sha256_hex, verify=True)
        return "ok"
    except MissingChunkError:
        return "missing"
    except (CorruptChunkError, OSError):
        return "corrupt"


def quarantine_chunk(sha256_hex):
    """Move a corrupt chunk out of the vault (kept for inspection, never deleted).

    This matters because store_chunk() skips chunks whose file already exists: a
    corrupt file left in place would be treated as 'already backed up' forever.
    Once it is gone, the next backup of the same data (or a repair) rewrites it.
    """
    src = chunk_path(sha256_hex)
    qdir = os.path.join(os.path.dirname(os.path.abspath(VAULT_DIR)), "quarantine")
    os.makedirs(qdir, exist_ok=True)
    dest = os.path.join(qdir, f"{sha256_hex}.{uuid.uuid4().hex[:8]}.corrupt")
    os.replace(src, dest)
    return dest


def vault_stats():
    """Chunk count and on-disk size. os.scandir reuses cached stat data (fast on Windows)."""
    count, size = 0, 0
    if os.path.isdir(VAULT_DIR):
        with os.scandir(VAULT_DIR) as it:
            for entry in it:
                if entry.is_file() and is_valid_hash(entry.name):
                    count += 1
                    size += entry.stat().st_size
    return {"chunk_count": count, "vault_size_bytes": size}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def chunk_stream(stream, chunk_size=None):
    """Yield (sha256_hex, data) for each chunk_size piece of a binary stream."""
    chunk_size = chunk_size or CHUNK_SIZE
    while True:
        data = stream.read(chunk_size)
        if not data:
            return
        yield hashlib.sha256(data).hexdigest(), data


def ingest_stream(stream, tee=None, chunk_size=None):
    """Chunk a stream into the vault.

    Returns (file_sha, size, chunk_hashes, new_count, new_raw_bytes, new_stored_bytes);
    the last two compare the NEW chunks' raw size with what hit the disk.

    `tee` is an optional writable file that receives a copy of the bytes
    (used by uploads, which also save the file into the watched folder).
    """
    whole = hashlib.sha256()
    hashes, new, size, new_raw, new_stored = [], 0, 0, 0, 0
    for sha, data in chunk_stream(stream, chunk_size):
        whole.update(data)
        size += len(data)
        hashes.append(sha)
        written = store_chunk(sha, data)
        if written is not None:
            new += 1
            new_raw += len(data)
            new_stored += written
        if tee is not None:
            tee.write(data)
    return whole.hexdigest(), size, hashes, new, new_raw, new_stored


def scan_file(abs_path, base_dir, db, chunk_size=None):
    """Build a snapshot entry for one file, reusing an identical earlier record."""
    chunk_size = chunk_size or CHUNK_SIZE
    st = os.stat(abs_path)
    rel = to_rel(abs_path, base_dir)
    prev = db.find_reusable_file(base_dir, rel, st.st_size, st.st_mtime_ns, chunk_size)
    # Reuse skips reading the file, so first make sure the old chunks are still in the
    # vault; if any vanished (quarantined, deleted), re-read the file and rewrite them.
    if prev and not all(has_chunk(c["sha256"]) for c in db.get_file_chunks(prev["id"])):
        prev = None
    if prev:
        return {"path": rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "sha256": prev["sha256"], "reuse_file_id": prev["id"],
                "chunk_count": prev["chunk_count"], "new_chunks": 0,
                "chunk_size": chunk_size, "new_raw_bytes": 0, "new_stored_bytes": 0}
    with open(abs_path, "rb") as f:
        sha, size, hashes, new, new_raw, new_stored = ingest_stream(f, chunk_size=chunk_size)
    return {"path": rel, "size": size, "mtime_ns": st.st_mtime_ns,
            "sha256": sha, "chunks": hashes, "chunk_count": len(hashes), "new_chunks": new,
            "chunk_size": chunk_size, "new_raw_bytes": new_raw, "new_stored_bytes": new_stored}


def compression_note(raw, stored):
    """', compressed 10.0 MB -> 3.1 MB (69% smaller)' for the new chunks, or ''."""
    if raw <= 0 or COMPRESS_LEVEL == 0:
        return ""
    def fmt(n):
        return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB"
    return f", compressed {fmt(raw)} -> {fmt(stored)} ({max(0, 100 - stored * 100 // raw)}% smaller)"


def _summary(snapshot_id, entries, chunk_size, skipped=False, message=None):
    total = sum(e["chunk_count"] for e in entries)
    new = sum(e["new_chunks"] for e in entries)
    raw = sum(e["new_raw_bytes"] for e in entries)
    stored = sum(e["new_stored_bytes"] for e in entries)
    return {
        "snapshot_id": snapshot_id,
        "chunk_size": chunk_size,
        "files": len(entries),
        "total_chunks": total,
        "new_chunks": new,
        "new_raw_bytes": raw,                 # size of the newly stored chunks before compression
        "new_stored_bytes": stored,           # what they actually occupy in the vault
        "compression_saved_bytes": raw - stored,
        "reused_files": sum(1 for e in entries if "reuse_file_id" in e),
        "skipped": skipped,
        "message": message or (
            f"Snapshot #{snapshot_id}: {len(entries)} file(s), {total} chunks ({new} new)"
            + compression_note(raw, stored)),
        "file_details": [{"path": e["path"], "size": e["size"],
                          "chunks": e["chunk_count"], "new_chunks": e["new_chunks"]}
                         for e in entries],
    }


def iter_candidate_files(dir_path):
    """Yield absolute paths of every backup-eligible file under dir_path."""
    abs_dir = os.path.abspath(dir_path)
    for root, dirs, files in os.walk(abs_dir):
        dirs[:] = sorted(d for d in dirs if not is_excluded(os.path.join(root, d)))
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            if not is_excluded(fpath) and os.path.isfile(fpath):
                yield fpath


# ---------------------------------------------------------------------------
# Public backup operations
# ---------------------------------------------------------------------------

def backup_directory(dir_path, db, kind="auto", force=False):
    """Snapshot every file under dir_path. Skips the snapshot if nothing changed."""
    abs_dir = os.path.abspath(dir_path)
    with BACKUP_LOCK:
        chunk_size = CHUNK_SIZE      # fixed for this whole backup, even if the setting changes
        entries, errors = [], []
        for fpath in iter_candidate_files(abs_dir):
            try:
                entries.append(scan_file(fpath, abs_dir, db, chunk_size))
            except (PermissionError, OSError) as exc:
                errors.append(f"{to_rel(fpath, abs_dir)}: {exc}")

        if not force:
            prev = db.latest_full_snapshot(abs_dir)
            if prev:
                # Chunk size is part of the comparison: re-backing up unchanged files
                # at a different size must produce a new snapshot, not "no changes".
                # Legacy rows (NULL chunk_size) were made at config.CHUNK_SIZE.
                old = {(f["path"], f["sha256"], f["chunk_size"] or config.CHUNK_SIZE)
                       for f in db.get_snapshot_files(prev["id"])}
                if old == {(e["path"], e["sha256"], e["chunk_size"]) for e in entries}:
                    res = _summary(None, entries, chunk_size, skipped=True,
                                   message=f"No changes since snapshot #{prev['id']}")
                    res["errors"] = errors
                    return res

        snap_id = db.commit_snapshot(abs_dir, kind, entries)
    res = _summary(snap_id, entries, chunk_size)
    res["errors"] = errors
    return res


def backup_selected_files(file_paths, db, base_dir):
    """Back up only the listed files (relative to base_dir) into a new snapshot."""
    abs_dir = os.path.abspath(base_dir)
    with BACKUP_LOCK:
        chunk_size = CHUNK_SIZE
        entries, errors = [], []
        for p in file_paths:
            try:
                fpath = safe_join(abs_dir, p)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if not os.path.isfile(fpath) or is_excluded(fpath):
                errors.append(f"{p}: not found or excluded")
                continue
            try:
                entries.append(scan_file(fpath, abs_dir, db, chunk_size))
            except (PermissionError, OSError) as exc:
                errors.append(f"{p}: {exc}")
        if not entries:
            raise ValueError("None of the selected files could be backed up: " + "; ".join(errors))
        snap_id = db.commit_snapshot(abs_dir, "selected", entries)
    res = _summary(snap_id, entries, chunk_size)
    res["errors"] = errors
    return res


def backup_uploads(uploads, db, save_dir):
    """Back up uploaded streams. `uploads` is a list of (filename, binary_stream).

    Each upload is streamed chunk-by-chunk (never fully loaded into memory) into
    the vault AND into save_dir, so it also appears in the watched folder.
    """
    os.makedirs(save_dir, exist_ok=True)
    abs_dir = os.path.abspath(save_dir)
    with BACKUP_LOCK:
        chunk_size = CHUNK_SIZE
        entries = []
        for raw_name, stream in uploads:
            name = safe_filename(raw_name)
            dest = os.path.join(abs_dir, name)
            tmp = dest + _TMP_SUFFIX
            try:
                with open(tmp, "wb") as out:
                    sha, size, hashes, new, new_raw, new_stored = ingest_stream(
                        stream, tee=out, chunk_size=chunk_size)
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            st = os.stat(dest)
            entries.append({"path": name, "size": size, "mtime_ns": st.st_mtime_ns,
                            "sha256": sha, "chunks": hashes,
                            "chunk_count": len(hashes), "new_chunks": new,
                            "chunk_size": chunk_size, "new_raw_bytes": new_raw,
                            "new_stored_bytes": new_stored})
        snap_id = db.commit_snapshot(abs_dir, "upload", entries)
    return _summary(snap_id, entries, chunk_size)


def list_workspace_files(dir_path):
    """Candidate files in a directory for the 'Select Files' tab."""
    abs_dir = os.path.abspath(dir_path)
    chunk_size = CHUNK_SIZE
    out = []
    for fpath in iter_candidate_files(abs_dir):
        try:
            size = os.path.getsize(fpath)
        except OSError:
            continue
        out.append({"path": to_rel(fpath, abs_dir), "size": size,
                    "chunks_est": (size + chunk_size - 1) // chunk_size})
    return out


def gc(db):
    """Delete vault chunks no snapshot references any more. Returns stats."""
    with BACKUP_LOCK:
        referenced = db.referenced_chunk_hashes()
        removed, freed = 0, 0
        with os.scandir(VAULT_DIR) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                stale_tmp = entry.name.endswith(_TMP_SUFFIX)
                if stale_tmp or (is_valid_hash(entry.name) and entry.name not in referenced):
                    freed += entry.stat().st_size
                    os.remove(entry.path)
                    removed += 1
    return {"removed_chunks": removed, "freed_bytes": freed}
