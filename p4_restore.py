"""P4 (core) — Restore, verification and cross-device pull. No web framework here.

Integrity is checked at two levels:
  * per chunk  — its bytes must hash to its name (catches bit-rot/tampering);
  * per file   — the reassembled bytes must hash to the stored file SHA-256
                 (catches wrong order / missing rows in the DB).
Restored files are written to a temp name and renamed only after both checks
pass, so a failed restore never leaves a corrupt file under the real name.
"""

import hashlib
import io
import os

import requests

import p1_backup_engine as engine

TOKEN_HEADER = "X-BackupX-Token"


class RestoreError(Exception):
    pass


# ---------------------------------------------------------------------------
# Local restore
# ---------------------------------------------------------------------------

def _chunk_list(db, fmeta):
    chunks = db.get_file_chunks(fmeta["id"])
    if len(chunks) != fmeta["chunk_count"]:
        raise RestoreError(f"{fmeta['path']}: DB has {len(chunks)} chunk rows, "
                           f"expected {fmeta['chunk_count']} (interrupted backup?)")
    return [c["sha256"] for c in chunks]


def precheck_file(db, fmeta):
    """Fail fast (before any bytes are sent/written) if a chunk is missing."""
    hashes = _chunk_list(db, fmeta)
    missing = [h for h in hashes if not engine.has_chunk(h)]
    if missing:
        raise RestoreError(f"{fmeta['path']}: {len(missing)} chunk(s) missing from vault")
    return hashes


def iter_file_bytes(db, fmeta, hashes=None):
    """Yield the file's bytes chunk by chunk, verifying everything."""
    hashes = hashes if hashes is not None else precheck_file(db, fmeta)
    whole = hashlib.sha256()
    for h in hashes:
        try:
            data = engine.read_chunk(h, verify=True)
        except (engine.MissingChunkError, engine.CorruptChunkError) as exc:
            raise RestoreError(f"{fmeta['path']}: {type(exc).__name__} {h[:12]}…") from exc
        whole.update(data)
        yield data
    if whole.hexdigest() != fmeta["sha256"]:
        raise RestoreError(f"{fmeta['path']}: whole-file SHA-256 mismatch")


def _write_verified(out_path, chunks_iter):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + engine._TMP_SUFFIX
    try:
        with open(tmp, "wb") as out:
            for data in chunks_iter:
                out.write(data)
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def restore_snapshot(db, snapshot_id, target_dir):
    """Rebuild every file of a snapshot under target_dir."""
    if not db.get_snapshot(snapshot_id):
        raise LookupError(f"Snapshot #{snapshot_id} not found")
    files = db.get_snapshot_files(snapshot_id)
    target_dir = os.path.abspath(target_dir)
    restored, failed = [], []
    for f in files:
        try:
            out_path = engine.safe_join(target_dir, f["path"])
            _write_verified(out_path, iter_file_bytes(db, f))
            restored.append(f["path"])
        except (RestoreError, ValueError) as exc:
            failed.append(str(exc))
    return {"snapshot_id": snapshot_id, "restored": len(restored), "failed": failed,
            "target_dir": target_dir, "files": restored}


def verify_snapshot(db, snapshot_id):
    """Check every chunk of a snapshot without writing anything."""
    if not db.get_snapshot(snapshot_id):
        raise LookupError(f"Snapshot #{snapshot_id} not found")
    ok, problems = 0, []
    for f in db.get_snapshot_files(snapshot_id):
        try:
            for _ in iter_file_bytes(db, f):
                pass
            ok += 1
        except RestoreError as exc:
            problems.append(str(exc))
    return {"snapshot_id": snapshot_id, "ok_files": ok, "problems": problems,
            "healthy": not problems}


def scrub(db, quarantine=True, remote_url=None, token="", session=None):
    """Check EVERY chunk any snapshot references; optionally quarantine and repair.

    * missing / corrupt chunks are listed with the snapshots and files they break;
    * quarantine=True moves corrupt chunks out of the vault, so the next backup of
      the same data rewrites them (store_chunk would otherwise skip the bad file);
    * remote_url: re-download bad chunks from another Backup-X node. Each download
      is verified against the chunk's SHA-256 before it is stored.
    Holds the backup lock, so no backup or cleanup can run mid-scrub.
    """
    session = session or requests.Session()
    with engine.BACKUP_LOCK:
        referenced = sorted(db.referenced_chunk_hashes())
        bad = {}
        for h in referenced:
            status = engine.check_chunk(h)
            if status != "ok":
                bad[h] = status

        quarantined = 0
        if quarantine or remote_url:     # repair needs the bad file out of the way
            for h, status in bad.items():
                if status == "corrupt":
                    engine.quarantine_chunk(h)
                    quarantined += 1

        repaired = []
        if remote_url and bad:
            base = remote_url.rstrip("/")
            if not base.startswith(("http://", "https://")):
                raise RestoreError("Remote URL must start with http:// or https://")
            for h in bad:
                r = _get(session, f"{base}/api/remote/chunks/{h}", token, timeout=60)
                if r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == h:
                    engine.store_chunk(h, r.content)
                    repaired.append(h)

        fixed = set(repaired)
        unrepaired = [h for h in bad if h not in fixed]
        affected = db.files_using_chunks(unrepaired)

    return {
        "chunks_checked": len(referenced),
        "corrupt": sum(1 for s in bad.values() if s == "corrupt"),
        "missing": sum(1 for s in bad.values() if s == "missing"),
        "quarantined": quarantined,
        "repaired": len(repaired),
        "unrepaired": len(unrepaired),
        "affected_files": [{"snapshot_id": s, "file_id": f, "path": p} for s, f, p in affected],
        "healthy": not unrepaired,
    }


# ---------------------------------------------------------------------------
# Cross-device pull
# ---------------------------------------------------------------------------

def _get(session, url, token, **kw):
    headers = {TOKEN_HEADER: token} if token else {}
    try:
        resp = session.get(url, headers=headers, timeout=kw.pop("timeout", 30), **kw)
    except requests.RequestException as exc:
        raise RestoreError(f"Could not reach {url}: {exc}") from exc
    return resp


def pull_remote(db, remote_url, snapshot_id, target_dir, token="", session=None):
    """Copy a snapshot from another Backup-X node, downloading only missing chunks.

    1. Fetch the remote snapshot's file list and each file's chunk list.
    2. Download only chunks this vault lacks (dedup across devices); verify each.
    3. Record it locally as a 'remote' snapshot (browsable/restorable later).
    4. Reconstruct the files into target_dir with full verification.
    """
    session = session or requests.Session()
    base = remote_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise RestoreError("Remote URL must start with http:// or https://")

    resp = _get(session, f"{base}/api/snapshots/{snapshot_id}", token)
    if resp.status_code in (401, 403):
        raise RestoreError("Remote rejected the access token")
    if resp.status_code != 200:
        raise RestoreError(f"Remote returned HTTP {resp.status_code} for snapshot #{snapshot_id}")
    remote_files = resp.json()

    entries, downloaded, reused = [], 0, 0
    for f in remote_files:
        engine.safe_join(target_dir, f["path"])         # reject hostile paths up front
        cr = _get(session, f"{base}/api/remote/files/{f['id']}/chunks", token)
        if cr.status_code == 200:
            hashes = cr.json()["chunks"]
            for h in hashes:
                if not engine.is_valid_hash(h):
                    raise RestoreError(f"Remote sent an invalid chunk id for {f['path']}")
                if engine.has_chunk(h):
                    reused += 1
                    continue
                r = _get(session, f"{base}/api/remote/chunks/{h}", token, timeout=60)
                if r.status_code != 200:
                    raise RestoreError(f"Remote is missing chunk {h[:12]}… of {f['path']}")
                if hashlib.sha256(r.content).hexdigest() != h:
                    raise RestoreError(f"Chunk {h[:12]}… of {f['path']} failed verification")
                engine.store_chunk(h, r.content)
                downloaded += 1
            entry = {"path": f["path"], "size": f["size"], "sha256": f["sha256"],
                     "chunks": hashes, "chunk_count": len(hashes), "new_chunks": 0}
        else:
            # Older remote without the chunk-list endpoint: download the whole file.
            fr = _get(session, f"{base}/api/restore/{f['id']}", token, timeout=300)
            if fr.status_code != 200:
                raise RestoreError(f"Failed to fetch {f['path']} (HTTP {fr.status_code})")
            sha, size, hashes, new, _raw, _stored = engine.ingest_stream(io.BytesIO(fr.content))
            if sha != f["sha256"]:
                raise RestoreError(f"{f['path']}: downloaded file failed SHA-256 check")
            downloaded += new
            entry = {"path": f["path"], "size": size, "sha256": sha,
                     "chunks": hashes, "chunk_count": len(hashes), "new_chunks": new}
        entries.append(entry)

    with engine.BACKUP_LOCK:   # don't let gc() race the new chunks
        local_id = db.commit_snapshot(f"{base}#snapshot-{snapshot_id}", "remote", entries)
    result = restore_snapshot(db, local_id, target_dir)
    result.update({"local_snapshot_id": local_id, "remote_snapshot_id": snapshot_id,
                   "chunks_downloaded": downloaded, "chunks_already_local": reused})
    return result
