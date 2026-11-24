"""Shared configuration for Backup-X.

Every other module imports this first, so the .env file is loaded exactly once
and before anything reads an environment variable.

All default paths are anchored to the project folder (APP_DIR), NOT the current
working directory. Previously, running `python newbackup-X/main.py` from another
folder created a second, empty vault and database there.
"""

import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path):
    """Minimal .env parser (KEY=VALUE, # comments). Real env vars win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


_load_env_file(os.path.join(APP_DIR, ".env"))


def _path(env_name, default):
    """Read a path from env; relative values are resolved against APP_DIR."""
    raw = os.getenv(env_name) or default
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(APP_DIR, raw))


# --- Storage ---------------------------------------------------------------
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", str(4 * 1024)))          # bytes
# Bounds for a user-selected chunk size. Below 1 KB the DB rows and vault files
# explode; above 64 MB a single chunk is a large in-memory read.
COMPRESS_LEVEL = max(0, min(9, int(os.getenv("COMPRESS_LEVEL", "6"))))   # zlib 1-9; 0 = off
MIN_CHUNK_SIZE = 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024
VAULT_DIR = _path("VAULT_DIR", os.path.join(".backupx_vault", "chunks"))
DB_PATH = _path("DB_PATH", "backupx.db")
WATCH_DIR = _path("WATCH_DIR", "backup_data")
RESTORE_DIR = _path("RESTORE_DIR", "restored")
STATIC_DIR = os.path.join(APP_DIR, "static")

# --- Database --------------------------------------------------------------
# "auto" tries MySQL and falls back to SQLite; "sqlite" / "mysql" force one.
DB_BACKEND = os.getenv("DB_BACKEND", "auto").lower()

# --- Server ----------------------------------------------------------------
# Default is loopback only. Set HOST=0.0.0.0 to allow cross-device access,
# and set BACKUPX_TOKEN so other machines must authenticate.
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
API_TOKEN = os.getenv("BACKUPX_TOKEN", "")

# --- Monitor ---------------------------------------------------------------
DEBOUNCE_SEC = float(os.getenv("DEBOUNCE_SEC", "2.0"))


def resolve_user_dir(path):
    """Resolve a directory typed by the user (relative paths -> APP_DIR)."""
    if not path:
        return RESTORE_DIR
    path = os.path.expanduser(path)
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(APP_DIR, path))
