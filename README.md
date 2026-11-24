# Backup-X

Backup-X is a local file-backup application. It watches a folder, splits each file into fixed-size chunks, stores each unique chunk once, and records a snapshot in a database. A web dashboard can back up, verify, restore, scan the vault for corruption, and pull a snapshot from another Backup-X machine.

The program is split into four modules:

| Module | File | Role |
|--------|------|------|
| **P1** | `p1_backup_engine.py` | Chunking, hashing, compression, vault storage, deduplication, garbage collection |
| **P2** | `p2_monitor.py` | Folder watcher and automatic backups |
| **P3** | `p3_database.py` | SQLite or MySQL storage for snapshot metadata |
| **P4** | `p4_restore.py`, `p4_restore_web.py` | Verified restore, scrub, cross-device pull, and the HTTP API |

`main.py` opens the database, starts the watcher, and serves the API. `config.py` loads `.env` once. Paths in `.env` are resolved from the project folder, not from the current working directory.

## Features

### P1 — Backup engine

Implemented in `p1_backup_engine.py`:

- Fixed-size chunking. The default is 4,096 bytes. The live size can be changed from 1 KB to 64 MB.
- Each chunk is named by the SHA-256 hash of its raw bytes.
- A chunk is written only if that file is not already in the vault (deduplication).
- New chunks are compressed with zlib before they are written. The name stays the hash of the uncompressed bytes.
- Unchanged files are not read again when path, size, modification time, and chunk size match an earlier record and those chunks are still in the vault.
- Three backup operations: the whole folder, a chosen list of files, and uploaded streams.
- Garbage collection deletes vault files that no snapshot references, and leftover `*.backupx-tmp` files.
- A corrupt chunk can be moved to `.backupx_vault/quarantine/` so a later backup can store a good copy.

### P2 — File monitor

Implemented in `p2_monitor.py`:

- `watchdog` watches the folder recursively.
- Events are debounced (`DEBOUNCE_SEC`, default 2 seconds) so a burst of saves becomes one backup.
- Automatic backups and **Backup Entire Folder** run on one worker thread.
- Files the application itself just wrote (uploads) are ignored for a few seconds, so they are not snapshotted twice.
- On startup, `main.py` queues one catch-up backup for changes made while the app was stopped.

### P3 — Database

Implemented in `p3_database.py`:

- One wrapper speaks SQLite and MySQL. SQL uses `%s` placeholders and is translated to `?` for SQLite.
- A snapshot, its file rows, and its chunk rows are committed in one transaction.
- SQLite runs in WAL mode. One shared connection is guarded by a re-entrant lock.
- Older databases gain missing columns on startup (`chunk_count`, `mtime_ns`, `chunk_size`, `kind`).

### P4 — Restore and web

Implemented in `p4_restore.py` and `p4_restore_web.py`:

- Restore and single-file download reassemble chunks and check hashes before treating the file as good.
- `verify` checks one snapshot. `scrub` checks every chunk any snapshot references.
- Scrub can quarantine corrupt chunks and, if a remote URL is given, download replacements.
- Cross-device pull copies a remote snapshot, downloading only chunks this vault does not already have.
- FastAPI serves `static/index.html` and the `/api/` routes below.

## Technology Stack

| Piece | Used for |
|-------|----------|
| Python 3 | Application language |
| FastAPI | HTTP API and dashboard |
| Uvicorn | ASGI server (`python main.py`) |
| python-multipart | File uploads |
| watchdog | Folder events |
| requests | Cross-device download and scrub repair |
| SQLite (`sqlite3`) | Default database, in the standard library |
| zlib, hashlib | Compression and SHA-256, in the standard library |
| mysql-connector-python | Optional. Used only when `DB_BACKEND` is `auto` or `mysql` and MySQL accepts the connection |

There is no frontend build step. The dashboard is one HTML file with plain CSS and JavaScript.

## Project Structure

```
newbackup-X/
├── main.py                 # start DB, watcher, and web server
├── config.py               # .env and path settings
├── p1_backup_engine.py     # P1 vault and backup
├── p2_monitor.py           # P2 watcher
├── p3_database.py          # P3 database
├── p4_restore.py           # P4 restore, verify, scrub, pull
├── p4_restore_web.py       # P4 FastAPI routes
├── requirements.txt
├── .env.example
├── static/
│   └── index.html          # dashboard
├── tests/
│   ├── test_core.py        # engine, DB, monitor, restore
│   └── test_api_live.py    # HTTP tests against real servers
├── backup_data/            # default watched folder
├── .backupx_vault/chunks/  # created at runtime; one file per chunk
├── backupx.db              # created at runtime when SQLite is used
└── restored/               # default restore destination (created when used)
```

`.backupx_vault/`, `backupx.db`, `restored/`, and `.env` are gitignored. Quarantined chunks, when any exist, are stored in `.backupx_vault/quarantine/`.

## Installation and Setup

### Requirements

- Python 3.10 or newer, with `pip` and the standard-library `sqlite3` module.
- A terminal with access to the project folder.
- No MySQL server, Node.js, frontend build tools, or administrator access is required for a normal local run.

Check that Python is installed before continuing:

```text
python --version
```

On macOS or Linux, use `python3 --version` if `python` is not available. On Windows, install Python from [python.org](https://www.python.org/downloads/) and enable the option to add Python to `PATH`, or use the Python launcher (`py -3`).

### Create an environment and install dependencies

Run these commands from the project folder (the folder containing `main.py` and `requirements.txt`). A virtual environment keeps this project's packages separate from other Python projects.

**Windows — PowerShell:**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `py` is not recognized, replace `py -3` with `python` in the first command.

**macOS or Linux:**

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

If the virtual environment command reports that `venv` or `ensurepip` is missing, install the venv component for your Python distribution and repeat the commands. For example, Debian/Ubuntu users can install `python3-venv` with their system package manager.

### Configure and run

SQLite is included with Python and is the recommended database for a single-machine setup. To explicitly select it, copy `.env.example` to `.env` and set `DB_BACKEND=sqlite`:

**Windows — PowerShell:**

```powershell
Copy-Item .env.example .env
```

**macOS or Linux:**

```sh
cp .env.example .env
```

Edit `.env` and change `DB_BACKEND=auto` to `DB_BACKEND=sqlite`. You can also skip creating `.env`: by default the app tries MySQL and falls back to SQLite if MySQL is not available. No MySQL setup is needed in either case.

Start the app from the project folder:

**Windows — PowerShell:**

```powershell
.\.venv\Scripts\python.exe main.py
```

**macOS or Linux:**

```sh
./.venv/bin/python main.py
```

Keep the terminal open while using the app. It prints the actual dashboard URL after startup; open that URL in a browser. The default is `http://localhost:8000`, and API documentation is at `http://localhost:8000/docs`. If port 8000 is occupied, the app tries the next 49 ports and prints the URL it selected. Stop it with Ctrl+C in the terminal.

The app creates the SQLite database and chunk vault as needed. Its default watched folder is `backup_data`, which is included in the project. On startup it queues a backup of that folder, so the first run may take a while for large files. Keep enough free disk space for the database and vault.

### Troubleshooting

| Problem | What to try |
|---------|-------------|
| `python`, `python3`, or `py` is not recognized | Install Python 3.10+ and reopen the terminal. On Windows, use `py -3` if `python` is not on `PATH`. |
| `No module named venv` or `ensurepip` | Install the venv package supplied by your Python distribution (for example, `python3-venv` on Debian/Ubuntu), then recreate `.venv`. |
| `No module named fastapi`, `uvicorn`, or another dependency | Install dependencies using the Python executable inside `.venv` and `-m pip install -r requirements.txt`; make sure the command is run from the project folder. |
| PowerShell blocks virtual-environment activation | Activation is not required; run `.\.venv\Scripts\python.exe` directly as shown above. |
| MySQL connection error | For local use, set `DB_BACKEND=sqlite` in `.env`. Select `mysql` only when a MySQL server and valid credentials are configured. |
| Browser cannot connect | Use the exact URL printed by the app. If a custom `HOST` or `PORT` is set, check that the address is reachable and the port is allowed by the local firewall. |
| Permission error creating the database or vault | Move the project to a folder your user can write to, or set `DB_PATH` and `VAULT_DIR` in `.env` to writable locations. |
| Dashboard or static files are missing | Start `main.py` from the project folder and make sure the `static` directory is present. |

`.env` is optional. With no `.env`, the app listens on `127.0.0.1:8000`, watches `backup_data`, uses 4,096-byte chunks, and tries MySQL then falls back to SQLite.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT` | `8000` | Preferred web port |
| `HOST` | `127.0.0.1` | Set `0.0.0.0` so other machines can connect |
| `BACKUPX_TOKEN` | empty | Required from non-local clients when set |
| `WATCH_DIR` | `backup_data` | Folder to watch |
| `RESTORE_DIR` | `restored` | Default restore folder |
| `VAULT_DIR` | `.backupx_vault/chunks` | Chunk store |
| `DB_PATH` | `backupx.db` | SQLite file |
| `CHUNK_SIZE` | `4096` | Starting chunk size in bytes (1,024 to 67,108,864) |
| `COMPRESS_LEVEL` | `6` | zlib level 1–9; `0` stores raw bytes |
| `DEBOUNCE_SEC` | `2.0` | Quiet time before an automatic backup |
| `DB_BACKEND` | `auto` | `auto`, `sqlite`, or `mysql` |
| `DB_HOST`, `DB_USER`, `DB_PASS`, `DB_NAME` | `localhost`, `root`, empty, `backupx` | Used only for MySQL |

`DB_BACKEND=mysql` stops the process if MySQL cannot be reached. `auto` prints nothing and uses SQLite instead.

A chunk size chosen in the dashboard is kept only until the process exits. The next start uses `CHUNK_SIZE` again.

## Running the Application

```powershell
.venv\Scripts\activate
python main.py
```

Open `http://localhost:8000`. Interactive API docs are at `http://localhost:8000/docs`.

If the preferred port is busy, `main.py` binds the next free port in a range of 50 and prints the URL. If another program already answers on port 8000, set `PORT=8010` in `.env` and open `http://localhost:8010`.

On startup the watcher starts and one automatic backup is queued. An empty watched folder still gets a snapshot, because “no files” is a valid folder state.

Stop the server with Ctrl+C. The watcher is stopped in a `finally` block.

## API Endpoints

All of these are implemented in `p4_restore_web.py`. If `BACKUPX_TOKEN` is set, clients that are not loopback must send `X-BackupX-Token` or `?token=`.

| Method | Route | Purpose |
|--------|-------|---------|
| `GET` | `/` | Dashboard |
| `GET` | `/api/status` | Watcher state, database type, chunk size, compression level, vault stats |
| `POST` | `/api/chunk-size` | Body `{"chunk_size": <bytes>}`. 400 if outside 1 KB–64 MB |
| `GET` | `/api/watch-dir` | Current watched folder |
| `POST` | `/api/watch-dir` | Body `{"path": "<folder>"}`. Restarts the watcher |
| `GET` | `/api/files` | Files eligible for backup, with estimated chunk counts |
| `POST` | `/api/backup?force=false` | Full-folder backup. Skipped when nothing changed unless `force=true` |
| `POST` | `/api/backup-selected` | Body `{"files": ["a.txt", "sub/b.txt"]}` |
| `POST` | `/api/upload-backup` | Multipart field `files`. Streams each file into the vault and the watched folder |
| `POST` | `/api/gc` | Delete unreferenced chunks |
| `GET` | `/api/snapshots` | Snapshots, newest first |
| `GET` | `/api/snapshots/{id}` | File rows for one snapshot |
| `DELETE` | `/api/snapshots/{id}` | Delete that snapshot’s rows. Chunk files stay until `/api/gc` |
| `GET` | `/api/verify/{id}` | Re-hash every chunk of one snapshot |
| `POST` | `/api/scrub` | Body `{"quarantine": true, "remote_url": "", "token": ""}` |
| `GET` | `/api/restore/{file_id}` | Download one file |
| `POST` | `/api/restore-snapshot/{id}?target_dir=` | Restore every file in the snapshot |
| `GET` | `/api/remote/files/{file_id}/chunks` | Ordered chunk hashes for cross-device pull |
| `GET` | `/api/remote/chunks/{sha256}` | Raw chunk bytes, verified before they are sent |
| `POST` | `/api/remote/pull` | Body `remote_url`, `snapshot_id`, optional `target_dir` and `token` |

Example (PowerShell), after the server is running:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/status
Invoke-RestMethod http://127.0.0.1:8000/api/backup -Method POST
Invoke-RestMethod http://127.0.0.1:8000/api/snapshots
```

Set a chunk size (bytes, not kilobytes):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/chunk-size -Method POST `
  -ContentType "application/json" -Body '{"chunk_size": 1048576}'
```

The dashboard preset list is 4 KB, 64 KB, 256 KB, 1 MB, 4 MB, and 16 MB. Custom input is in KB and is converted to bytes before this call.

## How the Backup Process Works

`backup_directory` in P1 does the following under `BACKUP_LOCK`:

1. Read the active chunk size once, so one snapshot does not mix sizes.
2. Walk the folder. Skip excluded paths (see Security).
3. For each file, `scan_file` looks up an earlier row with the same watched directory, relative path, size, modification time (`mtime_ns`), and chunk size. If that row’s chunks are all still in the vault, their hash list is copied. Otherwise the file is read in `chunk_size` pieces.
4. Each piece is hashed, then `store_chunk` writes it if it is new.
5. Unless `force` is true, the new set of `(path, file hash, chunk size)` is compared with the latest whole-folder snapshot (`kind` of `full`, `auto`, or `manual`). An identical set does not create a snapshot.
6. Otherwise P3 inserts the snapshot, file rows, and chunk rows in one transaction.

Chunk bytes are written before that transaction commits. A crash can leave extra chunk files. It cannot leave a snapshot that points at chunks that were never written. `POST /api/gc` removes chunks no row references.

Snapshot `kind` values written by this code:

| Kind | Created by |
|------|------------|
| `auto` | Watcher, including the startup backup |
| `manual` | `POST /api/backup` |
| `selected` | `POST /api/backup-selected` |
| `upload` | `POST /api/upload-backup` |
| `remote` | `POST /api/remote/pull` |
| `full` | Rows from older versions of this project. Still treated as a whole-folder snapshot |

Uploads are read in chunks, not loaded fully into memory. A temp file in the watched folder is renamed into place only after the stream has been ingested. The watcher is told to ignore that path so it does not immediately take a second snapshot.

## How Deduplication Works

A chunk’s vault file name is the SHA-256 of its uncompressed bytes:

```python
# p1_backup_engine.py — store only if this hash is not already a file
dest = chunk_path(sha256_hex)
if os.path.exists(dest):
    return None
```

The same 4 KB sequence in two files, or in two versions of one file, produces the same hash and is stored once. The database stores the ordered list of hashes for each file. Restore concatenates those chunks.

New chunks are compressed after hashing:

1. Hash the raw piece.
2. If the vault already has that name, stop. Nothing is compressed.
3. Otherwise zlib-compress it. If the result is smaller, write a header `BXZ1`, a method byte `1`, and the compressed payload. If it is not smaller, write `BXZ1`, method byte `0`, and the raw payload.
4. `COMPRESS_LEVEL=0` writes the raw bytes with no header.

Reading accepts a legacy file that is only raw bytes. If the file starts with `BXZ1`, the reader also tries the raw bytes, and keeps the interpretation whose SHA-256 matches the file name.

Fixed-size chunking has a limit: inserting bytes near the start of a file moves later boundaries, so later chunks get new hashes. Content-defined chunking is not implemented.

Changing the chunk size does not rewrite old snapshots. Each file row stores the size it was split with (`chunk_size`; `NULL` means the configured default from an older database). Reuse and the “no changes” check include that size, so a new size produces a new snapshot even when the file bytes did not change.

## Database Models / Schema

Three tables. Deleting a snapshot deletes its file rows, and deleting a file row deletes its chunk rows (`ON DELETE CASCADE`). The application also deletes those rows explicitly.

```
snapshots                         files                                chunks
---------                         -----                                ------
id                                id                                   id
timestamp                         snapshot_id  -> snapshots.id         file_id      -> files.id
watched_dir                       path                                 chunk_index
kind                              size                                 sha256
                                  sha256          (whole file)
                                  chunk_count
                                  mtime_ns
                                  chunk_size      (NULL on older rows)
```

Indexes: `files(snapshot_id)`, `files(path)`, `chunks(file_id, chunk_index)`, `chunks(sha256)`.

Chunk bytes are not in the database. They are files under `VAULT_DIR`.

## Recovery and Restoration Process

**One snapshot** (`restore_snapshot`):

1. Load the file rows. For each file, the number of chunk rows must equal `chunk_count`.
2. Every chunk must already exist in the vault. This check runs before a download sends headers, and before a restored file is written.
3. Chunks are read in `chunk_index` order, decompressed, and checked: SHA-256 of the bytes must equal the chunk name.
4. The concatenated bytes must match the file’s stored SHA-256.
5. Output is written to a `*.backupx-tmp` file and renamed onto the real path only after both checks pass.

One bad file does not stop the others. The response lists restored paths and failures. If every file fails, the HTTP handler returns 500.

**Verify** runs the same checks and writes nothing.

**Scrub** (`POST /api/scrub`), while holding `BACKUP_LOCK`:

- Every distinct chunk hash referenced by any snapshot is classified `ok`, `missing`, or `corrupt`.
- With `quarantine` true (the default), corrupt files are moved to `.backupx_vault/quarantine/<sha256>.<8 hex chars>.corrupt`. Missing chunks have no file to move.
- If `remote_url` is set, each bad chunk is downloaded from `GET /api/remote/chunks/{sha256}` on that node, checked against the hash, and stored.
- The response names snapshots and files that still reference unrepaired chunks.

The dashboard scrub box sends the token saved in the browser. That value is empty on a normal localhost session. The Cross-Device form has its own token field for the remote machine.

**Cross-device pull**:

1. On the machine that holds the backup, set `HOST=0.0.0.0` and `BACKUPX_TOKEN`, then restart.
2. On the receiving machine, `POST /api/remote/pull` with the remote URL, snapshot id, token, and destination folder.
3. The receiver loads the remote file list and each file’s chunk list, and downloads only hashes it does not have.
4. It stores a local snapshot with `kind` `remote`, then runs the normal verified restore.

If the remote has no chunk-list route, the receiver downloads the whole file from `GET /api/restore/{file_id}` and checks the file hash.

## Error Handling

| Situation | What the code does |
|-----------|--------------------|
| Chunk file missing | `MissingChunkError`. Restore and verify fail that file with `RestoreError` |
| Chunk bytes do not match the name, or the file cannot be decoded | `CorruptChunkError`. Scrub reports it as `corrupt` |
| Chunk row count does not match `chunk_count` | `RestoreError` before any output |
| Whole-file hash mismatch after reassembly | `RestoreError`. The temp file is removed and the destination name is not replaced |
| Permission or OS error on one file during a folder backup | That path is listed in `errors`. Other files are still snapshotted |
| Selected backup where every path is missing or excluded | `ValueError`, returned as HTTP 400 |
| Unsafe snapshot path or upload name | `ValueError` from `safe_join` or `safe_filename` |
| Unknown snapshot or file id | HTTP 404 |
| Chunk size out of range, or not an integer | HTTP 400 |
| Remote node unreachable, bad token, or bad chunk | `RestoreError`, returned as HTTP 502 |
| Database error inside `transaction()` | Rollback. The snapshot is not half-written |
| Exception on the backup worker | Printed and re-raised. The worker thread stays available for the next job |
| Port in use | Next port in `PORT` … `PORT+49`. Exit if none is free |

`store_chunk` writes to a temp name and uses `os.replace`. A crash during the write does not leave a short file under the real chunk name.

## Security and Data Integrity

Implemented:

- The server binds to `127.0.0.1` unless `HOST` is changed. If it binds to a non-loopback address with no token, `main.py` prints a warning.
- Non-loopback `/api/` requests are checked with `secrets.compare_digest` when `BACKUPX_TOKEN` is set. Localhost is not asked for the token.
- `safe_join` rejects `..`, empty paths, and any path segment containing `:`, and rejects a result outside the destination folder. This applies to restore and to paths received from another machine.
- `safe_filename` keeps only the base name of an upload, so `../../evil.txt` is stored as `evil.txt`.
- Chunk ids used as file names must match `^[0-9a-f]{64}$`.
- These names are never backed up, as path components: `.git`, `__pycache__`, `.venv`, `venv`, `node_modules`, `.backupx_vault`. The database file, the vault, and the application’s own source files are also skipped.
- The dashboard HTML-escapes names before inserting them into the page.
- Integrity is the chunk hash, the whole-file hash, and an on-demand scrub of every referenced chunk. Cross-device downloads are hashed before they are stored.

Not implemented:

- No encryption of chunk files, the database, or HTTP. Use a trusted network, or put the server behind HTTPS.
- No second copy or parity. A corrupt chunk can be repaired only from the original file or from another node that still has that chunk.
- Scrub does not check the database file itself.

On Windows, `safe_filename("x:y")` does not reject the colon. `os.path.basename` runs first and yields `y`. `safe_join` does reject a `:` inside a stored relative path.

## Development Notes

- P1, P2, P3, and `p4_restore.py` do not import FastAPI. Only `p4_restore_web.py` and `main.py` do. Core tests can run without the web stack.
- `BACKUP_LOCK` is shared by folder backup, selected backup, upload, garbage collection, and scrub.
- Select Files and Upload do not go through the watcher queue. They take the same lock on the request that started them.
- Relative paths inside snapshots use forward slashes.
- The reuse shortcut trusts size and modification time. A change that keeps both the same is not detected.
- `COMPRESS_LEVEL` is read at startup. It does not recompress chunks already in the vault.
- Backup summaries report `new_raw_bytes`, `new_stored_bytes`, and `compression_saved_bytes` for chunks written in that run only.

Run the tests from the project folder with the virtual environment's Python:

**Windows — PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**macOS or Linux:**

```sh
./.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_core.py` covers chunking, dedup, compression, scrub, restore, and the monitor. `tests/test_api_live.py` starts two servers and calls the HTTP API, including a cross-device pull. That file is skipped when FastAPI is not installed.

## Future Enhancements

These are not in the current code:

- Encrypt chunks and traffic.
- Content-defined chunking, so an insert near the start of a file does not change every later chunk hash.
- A retention rule that deletes old snapshots automatically.
- Persist the dashboard chunk size across restarts.
- Recompress or re-chunk existing vault data when settings change.
- Redundant storage (a second vault, or parity) so scrub can repair a chunk without the original file or another node.
- Per-user accounts. The token is a single shared secret.
