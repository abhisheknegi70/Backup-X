"""Live API tests — start two real Backup-X servers and exercise the HTTP API.

Needs the project's requirements installed (FastAPI, uvicorn, requests).
Run from the project folder:

    python -m unittest tests.test_api_live -v

Skipped automatically if FastAPI is not installed.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import fastapi  # noqa: F401
    import requests
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Node:
    """One Backup-X server with its own folder, vault and database."""

    def __init__(self, root, name):
        self.dir = os.path.join(root, name)
        self.watch = os.path.join(self.dir, "data")
        os.makedirs(self.watch)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ,
                   HOST="127.0.0.1", PORT=str(self.port), DB_BACKEND="sqlite",
                   WATCH_DIR=self.watch,
                   VAULT_DIR=os.path.join(self.dir, "vault"),
                   DB_PATH=os.path.join(self.dir, "backupx.db"),
                   RESTORE_DIR=os.path.join(self.dir, "restored"),
                   DEBOUNCE_SEC="0.5")
        self.log = open(os.path.join(self.dir, "server.log"), "w")
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "main.py")],
                                     cwd=self.dir, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if requests.get(self.url + "/api/status", timeout=1).ok:
                    return
            except requests.RequestException:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"server {name} did not start; see {self.log.name}")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()

    def get(self, path, **kw):
        return requests.get(self.url + path, timeout=60, **kw)

    def post(self, path, **kw):
        return requests.post(self.url + path, timeout=120, **kw)


@unittest.skipUnless(HAVE_DEPS, "FastAPI/requests not installed")
class TestLiveAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="backupx-live-")
        cls.a = Node(cls.tmp, "a")
        cls.b = Node(cls.tmp, "b")

    @classmethod
    def tearDownClass(cls):
        cls.a.stop()
        cls.b.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write_a(self, rel, data):
        path = os.path.join(self.a.watch, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_01_dashboard_served(self):
        r = self.a.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Backup-X", r.text)

    def test_02_full_backup_then_no_changes(self):
        self.write_a("doc.txt", b"hello\n" * 2000)
        self.write_a("sub/img.bin", os.urandom(50_000))
        self.write_a("empty.txt", b"")
        r1 = self.a.post("/api/backup", params={"force": "true"}).json()
        self.assertEqual(r1["files"], 3)
        r2 = self.a.post("/api/backup").json()
        self.assertTrue(r2["skipped"])

    def test_03_download_matches_including_empty_file(self):
        snap = self.a.get("/api/snapshots").json()[0]["id"]
        files = {f["path"]: f for f in self.a.get(f"/api/snapshots/{snap}").json()}
        for rel in ("doc.txt", "sub/img.bin", "empty.txt"):
            body = self.a.get(f"/api/restore/{files[rel]['id']}").content
            with open(os.path.join(self.a.watch, rel), "rb") as f:
                self.assertEqual(body, f.read(), rel)

    def test_04_upload_sanitises_name(self):
        r = self.a.post("/api/upload-backup",
                        files=[("files", ("../../evil.txt", b"payload", "text/plain"))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(os.path.exists(os.path.join(self.a.watch, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.a.dir, "evil.txt")))

    def test_05_verify_and_restore_snapshot(self):
        # Newest snapshot that has files (snapshot #1 is the empty catch-up backup at startup).
        snap = next(s["id"] for s in self.a.get("/api/snapshots").json() if s["file_count"] > 0)
        self.assertTrue(self.a.get(f"/api/verify/{snap}").json()["healthy"])
        r = self.a.post(f"/api/restore-snapshot/{snap}").json()
        self.assertEqual(r["failed"], [])
        self.assertGreater(r["restored"], 0)

    def test_06_bad_inputs_rejected(self):
        self.assertEqual(self.a.get("/api/snapshots/99999").status_code, 404)
        self.assertEqual(self.a.get("/api/remote/chunks/not-a-hash").status_code, 400)
        self.assertEqual(self.a.get("/api/restore/99999").status_code, 404)

    def test_07_cross_device_pull(self):
        snap = [s for s in self.a.get("/api/snapshots").json() if s["kind"] == "manual"][0]["id"]
        r = self.b.post("/api/remote/pull",
                        json={"remote_url": self.a.url, "snapshot_id": snap}).json()
        self.assertEqual(r["failed"], [])
        self.assertGreater(r["chunks_downloaded"], 0)
        restored = os.path.join(self.b.dir, "restored", "sub", "img.bin")
        with open(restored, "rb") as f1, open(os.path.join(self.a.watch, "sub", "img.bin"), "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        again = self.b.post("/api/remote/pull",
                            json={"remote_url": self.a.url, "snapshot_id": snap}).json()
        self.assertEqual(again["chunks_downloaded"], 0)   # dedup across devices

    def test_08_delete_and_gc(self):
        snaps = self.b.get("/api/snapshots").json()
        newest = snaps[0]["id"]
        r = requests.delete(self.b.url + f"/api/snapshots/{newest}", timeout=30)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.b.post("/api/gc").status_code, 200)


if __name__ == "__main__":
    unittest.main()
