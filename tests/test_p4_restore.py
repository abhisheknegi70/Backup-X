"""P4 restore: integrity, scrub/repair, cross-device pull.

Run everything with:  python -m unittest discover -s tests -v
"""

from helpers import *  # noqa: F401,F403
from helpers import _TMP_ROOT  # noqa: F401


class TestIntegrity(Base):
    def _backup_one(self, data):
        write(os.path.join(self.src, "f.bin"), data)
        res = engine.backup_directory(self.src, self.db)
        fmeta = self.db.get_snapshot_files(res["snapshot_id"])[0]
        return res["snapshot_id"], fmeta

    def test_corrupt_chunk_detected_and_no_partial_file(self):
        sid, fmeta = self._backup_one(os.urandom(CS * 3))
        victim = self.db.get_file_chunks(fmeta["id"])[1]["sha256"]
        write(os.path.join(self.vault, victim), b"bit rot")
        report = restore.verify_snapshot(self.db, sid)
        self.assertFalse(report["healthy"])
        self.assertIn("CorruptChunkError", report["problems"][0])
        r = restore.restore_snapshot(self.db, sid, self.out)
        self.assertEqual(r["restored"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, "f.bin")))
        self.assertEqual(os.listdir(self.out) if os.path.exists(self.out) else [], [])

    def test_missing_chunk_detected_before_streaming(self):
        sid, fmeta = self._backup_one(os.urandom(CS * 2))
        os.remove(os.path.join(self.vault, self.db.get_file_chunks(fmeta["id"])[0]["sha256"]))
        with self.assertRaises(restore.RestoreError):
            restore.precheck_file(self.db, fmeta)

    def test_wrong_chunk_order_caught_by_whole_file_hash(self):
        sid, fmeta = self._backup_one(os.urandom(CS) + os.urandom(CS))
        self.db.execute("UPDATE chunks SET chunk_index = 1 - chunk_index WHERE file_id = %s",
                        (fmeta["id"],))
        self.db.commit()
        report = restore.verify_snapshot(self.db, sid)
        self.assertIn("whole-file SHA-256 mismatch", report["problems"][0])

    def test_healthy_snapshot_verifies(self):
        sid, _ = self._backup_one(os.urandom(CS * 4))
        self.assertTrue(restore.verify_snapshot(self.db, sid)["healthy"])


class TestScrub(Base):
    def _corrupt(self, sha):
        path = engine.chunk_path(sha)
        blob = bytearray(read(path))
        blob[len(blob) // 2] ^= 0xFF
        write(path, bytes(blob))

    def _setup(self):
        data = os.urandom(CS * 3)
        write(os.path.join(self.src, "f.bin"), data)
        res = engine.backup_directory(self.src, self.db)
        hashes = [c["sha256"] for c in self.db.get_file_chunks(
            self.db.get_snapshot_files(res["snapshot_id"])[0]["id"])]
        return data, res["snapshot_id"], hashes

    def test_clean_vault_is_healthy(self):
        _, snap, hashes = self._setup()
        r = restore.scrub(self.db)
        self.assertTrue(r["healthy"])
        self.assertEqual((r["chunks_checked"], r["corrupt"], r["missing"]), (len(hashes), 0, 0))

    def test_detects_corrupt_and_missing_and_names_affected_files(self):
        _, snap, hashes = self._setup()
        self._corrupt(hashes[0])
        os.remove(engine.chunk_path(hashes[1]))
        r = restore.scrub(self.db, quarantine=False)
        self.assertEqual((r["corrupt"], r["missing"], r["quarantined"]), (1, 1, 0))
        self.assertFalse(r["healthy"])
        self.assertEqual([f["path"] for f in r["affected_files"]], ["f.bin"])
        self.assertTrue(os.path.exists(engine.chunk_path(hashes[0])))   # untouched: read-only mode

    def test_quarantine_then_rebackup_heals_the_vault(self):
        data, snap, hashes = self._setup()
        self._corrupt(hashes[0])
        # Without quarantine, re-backing up trusts the bad file (the old behaviour).
        engine.backup_directory(self.src, self.db, force=True)
        self.assertEqual(engine.check_chunk(hashes[0]), "corrupt")
        r = restore.scrub(self.db)                      # quarantines it
        self.assertEqual(r["quarantined"], 1)
        self.assertEqual(engine.check_chunk(hashes[0]), "missing")
        self.assertEqual(len(os.listdir(os.path.join(os.path.dirname(self.vault), "quarantine"))), 1)
        engine.backup_directory(self.src, self.db, force=True)           # rewrites the chunk
        self.assertTrue(restore.scrub(self.db)["healthy"])
        restore.restore_snapshot(self.db, snap, self.out)
        self.assertEqual(read(os.path.join(self.out, "f.bin")), data)

    def test_truncated_chunk_file_is_corrupt(self):
        _, _, hashes = self._setup()
        write(engine.chunk_path(hashes[0]), b"")
        self.assertEqual(engine.check_chunk(hashes[0]), "corrupt")


class _FakeRemote(BaseHTTPRequestHandler):
    """Implements the 4 GET endpoints a Backup-X node exposes for cross-device pull."""
    db = vault = token = None
    paths_override = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.token and self.headers.get(restore.TOKEN_HEADER) != self.token:
            return self._send(401, {"detail": "token"})
        parts = self.path.strip("/").split("/")
        if parts[:2] == ["api", "snapshots"]:
            files = self.db.get_snapshot_files(int(parts[2]))
            if self.paths_override:
                for f in files:
                    f["path"] = self.paths_override
            return self._send(200, files)
        if parts[:3] == ["api", "remote", "files"]:
            ch = [c["sha256"] for c in self.db.get_file_chunks(int(parts[3]))]
            return self._send(200, {"chunks": ch})
        if parts[:3] == ["api", "remote", "chunks"]:
            # Read straight from the REMOTE vault folder. (An earlier version swapped the
            # global engine.VAULT_DIR on this server thread, racing with the client thread.)
            sha = parts[3]
            with open(os.path.join(self.vault, sha), "rb") as f:
                blob = f.read()
            for data in engine._decode_candidates(blob):
                if hashlib.sha256(data).hexdigest() == sha:
                    return self._send(200, data, "application/octet-stream")
            return self._send(500, {"detail": "corrupt on remote"})
        return self._send(404, {"detail": "nf"})


class TestCrossDevice(Base):
    def setUp(self):
        super().setUp()
        # "Remote" node: its own vault + DB, populated while it is the active vault.
        self.r_vault = os.path.join(self.tmp, "remote_vault")
        os.makedirs(self.r_vault)
        self.r_db = DB(path=os.path.join(self.tmp, "remote.db"))
        self.shared = os.urandom(CS * 4)
        write(os.path.join(self.tmp, "rsrc", "shared.bin"), self.shared)
        write(os.path.join(self.tmp, "rsrc", "only_remote.bin"), os.urandom(CS * 3))
        engine.VAULT_DIR = self.r_vault
        self.r_snap = engine.backup_directory(os.path.join(self.tmp, "rsrc"), self.r_db)["snapshot_id"]
        engine.VAULT_DIR = self.vault
        # Local node already has shared.bin's chunks.
        write(os.path.join(self.src, "shared.bin"), self.shared)
        engine.backup_directory(self.src, self.db)

        handler = type("H", (_FakeRemote,), {"db": self.r_db, "vault": self.r_vault})
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.r_db._conn.close()
        super().tearDown()

    def test_pull_downloads_only_missing_chunks(self):
        r = restore.pull_remote(self.db, self.url, self.r_snap, self.out)
        self.assertEqual(r["failed"], [])
        self.assertEqual(r["chunks_downloaded"], 3)       # only_remote.bin
        self.assertEqual(r["chunks_already_local"], 4)    # shared.bin dedup'd
        self.assertEqual(read(os.path.join(self.out, "shared.bin")), self.shared)
        local = self.db.get_snapshot(r["local_snapshot_id"])
        self.assertEqual(local["kind"], "remote")

    def test_pull_rejects_hostile_paths(self):
        self.handler.paths_override = "../../pwned.txt"
        with self.assertRaises(ValueError):
            restore.pull_remote(self.db, self.url, self.r_snap, self.out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned.txt")))

    def test_pull_with_token(self):
        self.handler.token = "s3cret"
        with self.assertRaises(restore.RestoreError):
            restore.pull_remote(self.db, self.url, self.r_snap, self.out, token="wrong")
        r = restore.pull_remote(self.db, self.url, self.r_snap, self.out, token="s3cret")
        self.assertEqual(r["restored"], 2)

    def test_scrub_repairs_corrupt_chunk_from_remote_peer(self):
        snap = self.db.latest_full_snapshot(self.src)["id"]
        fid = self.db.get_snapshot_files(snap)[0]["id"]            # shared.bin, also on the remote
        sha = self.db.get_file_chunks(fid)[0]["sha256"]
        path = engine.chunk_path(sha)
        write(path, b"bit rot")
        # The fake remote serves from the remote vault, so don't let scrub's own
        # vault swap race the handler: scrub runs while engine.VAULT_DIR is local.
        r = restore.scrub(self.db, remote_url=self.url)
        self.assertEqual((r["corrupt"], r["repaired"], r["unrepaired"]), (1, 1, 0))
        self.assertTrue(r["healthy"])
        self.assertEqual(engine.check_chunk(sha), "ok")

    def test_unreachable_remote(self):
        with self.assertRaises(restore.RestoreError):
            restore.pull_remote(self.db, "http://127.0.0.1:9", 1, self.out)


if __name__ == "__main__":
    unittest.main()
