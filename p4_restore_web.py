"""P4 (web) — FastAPI routes. Business logic lives in p1_backup_engine and p4_restore."""

import os
import secrets
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import p1_backup_engine as engine
import p2_monitor as monitor
import p4_restore as restore

app = FastAPI(title="Backup-X", docs_url="/docs")
db = None   # injected by main.py

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


# ---------------------------------------------------------------------------
# Auth: if BACKUPX_TOKEN is set, non-local clients must present it.
# ---------------------------------------------------------------------------

@app.middleware("http")
async def require_token(request: Request, call_next):
    if config.API_TOKEN and request.url.path.startswith("/api/"):
        client = request.client.host if request.client else ""
        if client not in LOOPBACK:
            supplied = (request.headers.get(restore.TOKEN_HEADER)
                        or request.query_params.get("token") or "")
            if not secrets.compare_digest(supplied, config.API_TOKEN):
                return JSONResponse({"detail": "Missing or invalid access token"}, status_code=401)
    return await call_next(request)


class SelectedBackupRequest(BaseModel):
    files: List[str]


class WatchDirRequest(BaseModel):
    path: str


class ScrubRequest(BaseModel):
    quarantine: bool = True
    remote_url: Optional[str] = ""
    token: Optional[str] = ""


class ChunkSizeRequest(BaseModel):
    chunk_size: int     # bytes


class RemotePullRequest(BaseModel):
    remote_url: str
    snapshot_id: int
    target_dir: Optional[str] = None
    token: Optional[str] = ""


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
def serve_ui():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


# ---------------------------------------------------------------------------
# Status, watch dir, file listing
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status():
    last = monitor.last_result
    return {
        "monitoring": monitor.is_monitoring(),
        "db_dialect": db.dialect,
        "chunk_size": engine.CHUNK_SIZE,
        "min_chunk_size": config.MIN_CHUNK_SIZE,
        "max_chunk_size": config.MAX_CHUNK_SIZE,
        "compress_level": engine.COMPRESS_LEVEL,
        "watch_dir": monitor.get_watch_dir(),
        "default_restore_dir": config.RESTORE_DIR,
        "last_auto_backup": last["message"] if last else None,
        **engine.vault_stats(),
    }


@app.post("/api/chunk-size")
def update_chunk_size(req: ChunkSizeRequest):
    """Choose the chunk size for all later backups. Existing snapshots are untouched
    and stay restorable: each file records its own chunk list."""
    try:
        return {"status": "ok", "chunk_size": engine.set_chunk_size(req.chunk_size)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/watch-dir")
def get_watch_directory():
    return {"watch_dir": monitor.get_watch_dir()}


@app.post("/api/watch-dir")
def update_watch_directory(req: WatchDirRequest):
    target = config.resolve_user_dir(req.path.strip())
    if engine.is_excluded(target):
        raise HTTPException(400, "That folder is part of Backup-X itself")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Cannot use directory: {exc}")
    return {"status": "ok", "watch_dir": monitor.set_watch_dir(target, db)}


@app.get("/api/files")
def list_workspace_files():
    return engine.list_workspace_files(monitor.get_watch_dir())


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

@app.post("/api/backup")
def trigger_backup(force: bool = False):
    """Full-folder backup, queued on the monitor's worker so it never overlaps."""
    try:
        return monitor.submit_backup(db, kind="manual", force=force).result(timeout=1800)
    except Exception as exc:
        raise HTTPException(500, f"Backup failed: {exc}")


@app.post("/api/backup-selected")
def backup_selected(req: SelectedBackupRequest):
    if not req.files:
        raise HTTPException(400, "No files selected for backup")
    try:
        return engine.backup_selected_files(req.files, db, monitor.get_watch_dir())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/upload-backup")
def upload_and_backup(files: List[UploadFile] = File(...)):
    """Stream uploads into the vault and the watched folder (sync def -> threadpool)."""
    if not files:
        raise HTTPException(400, "No files uploaded")
    wd = monitor.get_watch_dir()
    try:
        names = [engine.safe_filename(f.filename) for f in files]
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if len(set(names)) != len(names):
        raise HTTPException(400, "Two uploaded files have the same name")
    # The upload snapshot already captures these files; don't let the watcher
    # immediately create a second, redundant full snapshot.
    monitor.suppress([os.path.join(wd, n) for n in names], seconds=config.DEBOUNCE_SEC + 5)
    res = engine.backup_uploads([(n, f.file) for n, f in zip(names, files)], db, wd)
    res["message"] = (f"Uploaded and backed up {res['files']} file(s) into "
                      f"Snapshot #{res['snapshot_id']} ({res['total_chunks']} chunks, "
                      f"{res['new_chunks']} new)"
                      + engine.compression_note(res["new_raw_bytes"], res["new_stored_bytes"]))
    return res


@app.post("/api/gc")
def garbage_collect():
    return engine.gc(db)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@app.get("/api/snapshots")
def list_snapshots():
    return db.list_snapshots()


@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int):
    if not db.get_snapshot(snapshot_id):
        raise HTTPException(404, "Snapshot not found")
    return db.get_snapshot_files(snapshot_id)


@app.delete("/api/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: int):
    if not db.delete_snapshot(snapshot_id):
        raise HTTPException(404, "Snapshot not found")
    return {"status": "ok", "message": f"Snapshot #{snapshot_id} deleted. "
                                       "Run 'Clean up vault' to reclaim disk space."}


@app.post("/api/scrub")
def scrub_vault(req: ScrubRequest):
    """Check every referenced chunk; quarantine corrupt ones; optionally repair from a peer."""
    try:
        return restore.scrub(db, quarantine=req.quarantine,
                             remote_url=(req.remote_url or "").strip() or None,
                             token=(req.token or "").strip())
    except restore.RestoreError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/verify/{snapshot_id}")
def verify_snapshot(snapshot_id: int):
    try:
        return restore.verify_snapshot(db, snapshot_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

@app.get("/api/restore/{file_id}")
def restore_file(file_id: int):
    """Stream one file back. Missing chunks are detected BEFORE headers are sent;
    corruption found mid-stream aborts the transfer, and Content-Length lets the
    browser see the download as incomplete rather than silently saving bad data."""
    fmeta = db.get_file(file_id)
    if not fmeta:
        raise HTTPException(404, "File not found")
    try:
        hashes = restore.precheck_file(db, fmeta)
    except restore.RestoreError as exc:
        raise HTTPException(500, str(exc))
    if fmeta["size"] == 0:
        body = Response(b"", media_type="application/octet-stream")
    else:
        body = StreamingResponse(restore.iter_file_bytes(db, fmeta, hashes),
                                 media_type="application/octet-stream")
        body.headers["Content-Length"] = str(fmeta["size"])
    filename = os.path.basename(fmeta["path"]).replace('"', "")
    body.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return body


@app.post("/api/restore-snapshot/{snapshot_id}")
def restore_snapshot(snapshot_id: int, target_dir: Optional[str] = None):
    target = config.resolve_user_dir(target_dir)
    try:
        res = restore.restore_snapshot(db, snapshot_id, target)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    if res["failed"] and not res["restored"]:
        raise HTTPException(500, "; ".join(res["failed"]))
    return res


# ---------------------------------------------------------------------------
# Cross-device
# ---------------------------------------------------------------------------

@app.get("/api/remote/files/{file_id}/chunks")
def serve_chunk_list(file_id: int):
    fmeta = db.get_file(file_id)
    if not fmeta:
        raise HTTPException(404, "File not found")
    return {"file_id": file_id, "sha256": fmeta["sha256"], "size": fmeta["size"],
            "chunks": [c["sha256"] for c in db.get_file_chunks(file_id)]}


@app.get("/api/remote/chunks/{sha256}")
def serve_chunk(sha256: str):
    if not engine.is_valid_hash(sha256):
        raise HTTPException(400, "Invalid chunk id")
    try:
        data = engine.read_chunk(sha256, verify=True)
    except engine.MissingChunkError:
        raise HTTPException(404, "Chunk not found")
    except engine.CorruptChunkError:
        raise HTTPException(500, "Chunk is corrupt on this node")
    return Response(data, media_type="application/octet-stream")


@app.post("/api/remote/pull")
def pull_remote(req: RemotePullRequest):
    target = config.resolve_user_dir(req.target_dir)
    try:
        return restore.pull_remote(db, req.remote_url, req.snapshot_id, target,
                                   token=(req.token or "").strip())
    except (restore.RestoreError, ValueError) as exc:
        raise HTTPException(502, str(exc))
