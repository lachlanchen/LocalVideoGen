from __future__ import annotations

import math
import os
import sqlite3
import stat
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from webapp.job_store import (
    JobAlreadyExistsError,
    JobNotFoundError,
    JobStore,
    JobStoreCorruptionError,
    JobStoreError,
    JobStoreValidationError,
)


def new_id() -> str:
    return str(uuid.uuid4())


def output_item(index: int = 0) -> dict[str, object]:
    return {
        "id": index,
        "filename": f"render-{index}.mp4",
        "subfolder": "h3/finished",
        "type": "output",
        "media_type": "video",
        "node_id": "42",
    }


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.path = self.runtime / "webapp-jobs.sqlite3"
        self.store = JobStore(self.path, max_terminal_jobs=2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_private_wal_database(self):
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)

    def test_register_update_get_and_status(self):
        job_id = new_id()
        created = self.store.register(job_id, {"mode": "t2v", "seed": "7"})
        self.assertEqual(created["id"], job_id)
        self.assertEqual(created["status"], "submitting")
        self.assertEqual(created["metadata"], {"mode": "t2v", "seed": "7"})
        self.assertEqual(created["outputs"], [])
        self.assertIsNone(created["error"])
        self.assertTrue(self.store.owns(job_id))
        self.assertEqual(self.store.status(job_id), "submitting")

        raw_output = {**output_item(), "url": "/must-not-be-persisted", "download_url": "/also-no"}
        updated = self.store.update(
            job_id,
            "completed",
            outputs=[raw_output],
            error=None,
        )
        self.assertEqual(updated["status"], "completed")
        self.assertGreater(updated["updated_ms"], updated["created_ms"])
        self.assertEqual(updated["outputs"], [output_item()])
        self.assertNotIn("url", updated["outputs"][0])
        self.assertEqual(self.store.get(job_id), updated)

    def test_metadata_update_and_error_clear(self):
        job_id = new_id()
        self.store.register(job_id, {"mode": "t2v"}, status="queued")
        failed = self.store.update(job_id, "failed", error="GPU error")
        self.assertEqual(failed["error"], "GPU error")
        retried = self.store.update(
            job_id,
            "pending",
            metadata={"mode": "t2v", "retry": 1},
            outputs=[],
            error=None,
        )
        self.assertEqual(retried["metadata"]["retry"], 1)
        self.assertIsNone(retried["error"])

    def test_uuid_duplicate_and_missing_guards(self):
        job_id = new_id()
        self.store.register(job_id, {})
        with self.assertRaises(JobAlreadyExistsError):
            self.store.register(job_id, {})
        with self.assertRaises(JobNotFoundError):
            self.store.update(new_id(), "pending")
        with self.assertRaises(JobStoreValidationError):
            self.store.get(job_id.upper())
        self.assertFalse(self.store.owns("not-a-uuid"))
        self.assertIsNone(self.store.get(new_id()))

    def test_json_and_status_validation(self):
        with self.assertRaises(JobStoreValidationError):
            self.store.register(new_id(), {"bad": object()})
        with self.assertRaises(JobStoreValidationError):
            self.store.register(new_id(), {"bad": math.nan})
        with self.assertRaises(JobStoreValidationError):
            self.store.register(new_id(), {}, status="whatever")
        job_id = new_id()
        self.store.register(job_id, {})
        with self.assertRaises(JobStoreValidationError):
            self.store.update(job_id, error="x" * 8193)

    def test_unsafe_outputs_are_rejected(self):
        job_id = new_id()
        self.store.register(job_id, {})
        unsafe = (
            {**output_item(), "filename": "../secret.mp4"},
            {**output_item(), "filename": "secret\\file.mp4"},
            {**output_item(), "subfolder": "../../outside"},
            {**output_item(), "subfolder": "/absolute"},
            {**output_item(), "type": "input"},
            {**output_item(), "media_type": "video/../../bad"},
        )
        for item in unsafe:
            with self.subTest(item=item):
                with self.assertRaises(JobStoreValidationError):
                    self.store.update(job_id, outputs=[item])
        with self.assertRaises(JobStoreValidationError):
            self.store.update(job_id, outputs=[output_item(0), output_item(0)])

    def test_active_history_and_order(self):
        first = new_id()
        second = new_id()
        third = new_id()
        self.store.register(first, {}, status="pending")
        self.store.register(second, {}, status="running")
        self.store.register(third, {}, status="completed")
        self.assertEqual([item["id"] for item in self.store.active()], [second, first])
        self.assertEqual([item["id"] for item in self.store.list(scope="history")], [third])
        self.assertEqual([item["id"] for item in self.store.list(limit=2)], [third, second])

    def test_terminal_pruning_never_removes_active_jobs(self):
        active = new_id()
        terminal = [new_id() for _ in range(3)]
        self.store.register(active, {}, status="pending")
        for job_id in terminal:
            self.store.register(job_id, {}, status="completed")
        self.assertTrue(self.store.owns(active))
        self.assertFalse(self.store.owns(terminal[0]))
        self.assertTrue(self.store.owns(terminal[1]))
        self.assertTrue(self.store.owns(terminal[2]))
        self.assertEqual(len(self.store.list(scope="history")), 2)

    def test_thread_safe_short_transactions(self):
        identifiers = [new_id() for _ in range(32)]

        def register_and_update(job_id: str) -> str:
            self.store.register(job_id, {"worker": job_id}, status="queued")
            self.store.update(job_id, "running")
            return self.store.status(job_id) or ""

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(register_and_update, identifiers))
        self.assertEqual(statuses, ["running"] * len(identifiers))
        self.assertEqual(len(self.store.active(limit=100)), len(identifiers))

    def test_invalid_persisted_json_fails_closed(self):
        job_id = new_id()
        self.store.register(job_id, {"mode": "t2v"})
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE jobs SET metadata_json = '{' WHERE job_id = ?", (job_id,))
        with self.assertRaises(JobStoreCorruptionError):
            self.store.get(job_id)

    def test_non_finite_persisted_json_fails_closed(self):
        job_id = new_id()
        self.store.register(job_id, {"mode": "t2v"})
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE jobs SET metadata_json = '{\"bad\":NaN}' WHERE job_id = ?", (job_id,))
        with self.assertRaises(JobStoreCorruptionError):
            self.store.get(job_id)

    def test_corrupt_database_has_typed_error(self):
        corrupt = self.runtime / "corrupt.sqlite3"
        descriptor = os.open(corrupt, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, b"this is not sqlite")
        finally:
            os.close(descriptor)
        with self.assertRaises(JobStoreCorruptionError):
            JobStore(corrupt)

    def test_public_parent_is_rejected(self):
        public = Path(self.temporary.name) / "public"
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        with self.assertRaises(JobStoreError):
            JobStore(public / "jobs.sqlite3")


if __name__ == "__main__":
    unittest.main()
