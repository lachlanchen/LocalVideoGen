from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.submit_h3_test import (
    DEFAULT_DURATION,
    DEFAULT_HEIGHT,
    DEFAULT_PROFILE,
    DEFAULT_SEED,
    DEFAULT_WIDTH,
    RenderOptions,
    RuntimeTarget,
    SubmitRenderError,
    build_parser,
    resolve_output_path,
    submit_test_render,
    verify_project_runtime,
)


_UNSET = object()


class FakeStore:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.updates: list[tuple[str, str | None]] = []

    def register(
        self,
        job_id: str,
        metadata: Mapping[str, Any],
        *,
        status: str = "submitting",
    ) -> dict[str, Any]:
        if job_id in self.records:
            raise AssertionError("duplicate test job")
        record = {
            "id": job_id,
            "status": status,
            "metadata": dict(metadata),
            "outputs": [],
            "error": None,
        }
        self.records[job_id] = record
        return dict(record)

    def update(
        self,
        job_id: str,
        status: str | None = None,
        *,
        metadata: Mapping[str, Any] | None = None,
        outputs: Sequence[Mapping[str, Any]] | None = None,
        error: Any = _UNSET,
    ) -> dict[str, Any]:
        record = self.records[job_id]
        if status is not None:
            record["status"] = status
        if metadata is not None:
            record["metadata"] = dict(metadata)
        if outputs is not None:
            record["outputs"] = [dict(item) for item in outputs]
        if error is not _UNSET:
            record["error"] = error
        self.updates.append((job_id, status))
        return dict(record)


class FakeClient:
    def __init__(self, base_url: str, *, timeout: float, jobs: list[dict[str, Any]]) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.jobs = jobs
        self.opened = False
        self.closed = False
        self.submissions: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.devices = [
            {"name": "cuda:0 NVIDIA GeForce RTX 4090"},
            {"name": "cuda:1 NVIDIA GeForce RTX 4090"},
        ]

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def health(self, *, inspect_nodes: bool = False) -> dict[str, Any]:
        return {
            "connected": True,
            "ready": inspect_nodes,
            "missing_nodes": [],
            "stats": {"devices": self.devices},
        }

    async def submit(
        self,
        prompt: Mapping[str, Any],
        metadata: Mapping[str, Any],
        prompt_id: str,
    ) -> dict[str, Any]:
        self.submissions.append(
            {"prompt": dict(prompt), "metadata": dict(metadata), "prompt_id": prompt_id}
        )
        return {"prompt_id": prompt_id, "number": 1}

    async def get_job(self, job_id: str) -> dict[str, Any]:
        if len(self.jobs) > 1:
            return self.jobs.pop(0)
        return self.jobs[0]

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self.cancelled.append(job_id)
        return {"cancelled": True}


def runtime_target(root: Path, *, pid: int = 777) -> RuntimeTarget:
    return RuntimeTarget(
        base_url="http://127.0.0.1:8188",
        pid=pid,
        instance="11111111-1111-4111-8111-111111111111",
        port=8188,
        start_ticks=12345,
        boot_id="boot-id",
        cwd=str((root / "ComfyUI").resolve()),
        argv=(
            str(root / ".venv" / "bin" / "python"),
            "-u",
            str(root / "ComfyUI" / "main.py"),
            "--listen",
            "127.0.0.1",
            "--port",
            "8188",
        ),
    )


class RuntimeVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "ComfyUI").mkdir()
        (self.root / "ComfyUI" / "main.py").touch()
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        self.state_path = runtime / "comfyui-state.json"
        self.state = {
            "pid": os.getpid(),
            "uid": os.getuid(),
            "start_ticks": 12345,
            "boot_id": "test-boot-id",
            "instance": "11111111-1111-4111-8111-111111111111",
            "cwd": str((self.root / "ComfyUI").resolve()),
            "argv": [
                str(self.root / ".venv" / "bin" / "python"),
                "-u",
                str(self.root / "ComfyUI" / "main.py"),
                "--listen",
                "127.0.0.1",
                "--port",
                "8188",
            ],
            "port": 8188,
        }
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.state_path.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runtime_identity_command_and_stable_loopback_state_are_required(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            self.assertEqual(kwargs["cwd"], self.root.resolve())
            self.assertTrue(kwargs["capture_output"])
            return subprocess.CompletedProcess(command, 0, "PID verified\n", "")

        target = verify_project_runtime(self.root, runner=runner)
        self.assertEqual(target.base_url, "http://127.0.0.1:8188")
        self.assertEqual(target.pid, os.getpid())
        self.assertEqual(calls[0][-1], "verify")
        self.assertTrue(calls[0][-2].endswith("scripts/runtime_identity.py"))

    def test_state_change_during_runtime_identity_check_is_rejected(self) -> None:
        def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            changed = {**self.state, "port": 8189}
            self.state_path.write_text(json.dumps(changed), encoding="utf-8")
            self.state_path.chmod(0o600)
            return subprocess.CompletedProcess(command, 0, "verified\n", "")

        with self.assertRaisesRegex(SubmitRenderError, "changed during"):
            verify_project_runtime(self.root, runner=runner)


class DirectRenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        output = self.root / "ComfyUI" / "output" / "video"
        output.mkdir(parents=True)
        self.artifact = output / "forest.mp4"
        self.artifact.write_bytes(b"mock-media")
        self.store = FakeStore()
        self.job_id = "22222222-2222-4222-8222-222222222222"
        self.target = runtime_target(self.root)
        self.progress: list[str] = []

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def completed_job(self) -> dict[str, Any]:
        return {
            "id": self.job_id,
            "workflow_id": "local-video-gen-minimax-h3-webapp",
            "status": "completed",
            "outputs": {
                "20": {
                    "images": [
                        {
                            "filename": self.artifact.name,
                            "subfolder": "video",
                            "type": "output",
                        }
                    ]
                }
            },
        }

    async def test_exact_prompt_allowlisted_graph_tracking_and_media_result(self) -> None:
        client = FakeClient(
            self.target.base_url,
            timeout=30,
            jobs=[
                {
                    "id": self.job_id,
                    "workflow_id": "local-video-gen-minimax-h3-webapp",
                    "status": "in_progress",
                },
                self.completed_job(),
            ],
        )
        verifier_calls: list[Path] = []

        def verifier(root: Path) -> RuntimeTarget:
            verifier_calls.append(root)
            return self.target

        async def probe(path: Path, *, timeout: float) -> dict[str, Any]:
            self.assertEqual(path, self.artifact.resolve())
            self.assertEqual(timeout, 7)
            return {
                "format": {"format_name": "mov,mp4", "duration": "2.333333"},
                "video": {
                    "codec_type": "video",
                    "width": 640,
                    "height": 352,
                    "avg_frame_rate": "24/1",
                    "nb_read_frames": "56",
                },
                "audio": {"codec_type": "audio", "channels": 2},
            }

        async def no_wait(_: float) -> None:
            return None

        exact_prompt = "a forest with fairy tales"
        result = await submit_test_render(
            RenderOptions(
                prompt=exact_prompt,
                timeout=2,
                poll_interval=0.001,
                probe_timeout=7,
            ),
            project_root=self.root,
            runtime_verifier=verifier,
            client_factory=lambda base_url, timeout: client,
            store=self.store,
            probe=probe,
            job_id_factory=lambda: self.job_id,
            progress=self.progress.append,
            sleep=no_wait,
        )

        self.assertEqual(
            len(verifier_calls),
            3,
            "identity must be rechecked before submit and again before trusting output",
        )
        self.assertTrue(client.opened)
        self.assertTrue(client.closed)
        self.assertEqual(client.base_url, "http://127.0.0.1:8188")
        self.assertEqual(len(client.submissions), 1)
        submission = client.submissions[0]
        self.assertEqual(submission["metadata"]["prompt"], exact_prompt)
        conditioning = next(
            node
            for node in submission["prompt"].values()
            if node["class_type"] == "MiniMaxH3ImageToVideo"
        )
        self.assertEqual(conditioning["inputs"]["prompt"], exact_prompt)
        self.assertEqual(conditioning["inputs"]["width"], 640)
        self.assertEqual(conditioning["inputs"]["height"], 352)
        self.assertEqual(conditioning["inputs"]["length"], 56)
        self.assertEqual(
            sum(node["class_type"] == "SaveVideo" for node in submission["prompt"].values()),
            1,
        )
        self.assertEqual(result["artifact"], str(self.artifact.resolve()))
        self.assertEqual(result["render"]["profile"], DEFAULT_PROFILE)
        self.assertEqual(self.store.records[self.job_id]["status"], "completed")
        self.assertEqual(self.store.records[self.job_id]["outputs"][0]["media_type"], "video")

    async def test_changed_runtime_is_failed_before_network_submission(self) -> None:
        client = FakeClient(self.target.base_url, timeout=30, jobs=[self.completed_job()])
        targets = iter((self.target, runtime_target(self.root, pid=778)))
        with self.assertRaisesRegex(SubmitRenderError, "changed after readiness"):
            await submit_test_render(
                RenderOptions(prompt="a forest with fairy tales"),
                project_root=self.root,
                runtime_verifier=lambda _: next(targets),
                client_factory=lambda base_url, timeout: client,
                store=self.store,
                probe=lambda *args, **kwargs: None,  # Never reached.
                job_id_factory=lambda: self.job_id,
                progress=lambda _: None,
            )
        self.assertEqual(client.submissions, [])
        self.assertEqual(self.store.records[self.job_id]["status"], "failed")
        self.assertTrue(client.closed)

    async def test_timeout_cancels_only_the_owned_registered_job(self) -> None:
        client = FakeClient(
            self.target.base_url,
            timeout=30,
            jobs=[{
                "id": self.job_id,
                "workflow_id": "local-video-gen-minimax-h3-webapp",
                "status": "pending",
            }],
        )
        with self.assertRaisesRegex(SubmitRenderError, "exceeded"):
            await submit_test_render(
                RenderOptions(
                    prompt="a forest with fairy tales",
                    timeout=0.01,
                    poll_interval=0.05,
                ),
                project_root=self.root,
                runtime_verifier=lambda _: self.target,
                client_factory=lambda base_url, timeout: client,
                store=self.store,
                probe=lambda *args, **kwargs: None,  # Never reached.
                job_id_factory=lambda: self.job_id,
                progress=lambda _: None,
            )
        self.assertEqual(client.cancelled, [self.job_id])
        self.assertEqual(self.store.records[self.job_id]["status"], "cancelled")
        self.assertTrue(client.closed)

    async def test_unsafe_completed_output_is_failed_without_path_access(self) -> None:
        job = self.completed_job()
        job["outputs"]["20"]["images"][0]["filename"] = "../outside.mp4"
        client = FakeClient(self.target.base_url, timeout=30, jobs=[job])
        with self.assertRaisesRegex(SubmitRenderError, "safe video output"):
            await submit_test_render(
                RenderOptions(prompt="a forest with fairy tales"),
                project_root=self.root,
                runtime_verifier=lambda _: self.target,
                client_factory=lambda base_url, timeout: client,
                store=self.store,
                probe=lambda *args, **kwargs: None,  # Never reached.
                job_id_factory=lambda: self.job_id,
                progress=lambda _: None,
            )
        self.assertEqual(client.cancelled, [])
        self.assertEqual(self.store.records[self.job_id]["status"], "failed")

    async def test_wrong_gpu_inventory_blocks_before_store_or_submit(self) -> None:
        client = FakeClient(self.target.base_url, timeout=30, jobs=[self.completed_job()])
        client.devices = [{"name": "NVIDIA GeForce RTX 4090"}]
        with patch.dict("os.environ", {"H3_AUX_DEVICE": "gpu:1"}):
            with self.assertRaisesRegex(SubmitRenderError, "requires both"):
                await submit_test_render(
                    RenderOptions(prompt="a forest with fairy tales"),
                    project_root=self.root,
                    runtime_verifier=lambda _: self.target,
                    client_factory=lambda base_url, timeout: client,
                    store=self.store,
                    probe=lambda *args, **kwargs: None,  # Never reached.
                    job_id_factory=lambda: self.job_id,
                    progress=lambda _: None,
                )
        self.assertEqual(self.store.records, {})
        self.assertEqual(client.submissions, [])


class ArgumentAndPathTests(unittest.TestCase):
    def test_prompt_is_explicit_and_defaults_are_the_small_turbo_test(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([])
        args = build_parser().parse_args(["--prompt", "a forest with fairy tales"])
        self.assertEqual(args.prompt, "a forest with fairy tales")
        self.assertEqual(args.profile, DEFAULT_PROFILE)
        self.assertEqual((args.width, args.height), (DEFAULT_WIDTH, DEFAULT_HEIGHT))
        self.assertEqual(args.duration, DEFAULT_DURATION)
        self.assertEqual(args.seed, DEFAULT_SEED)

    def test_output_path_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ComfyUI" / "output").mkdir(parents=True)
            with self.assertRaises(SubmitRenderError):
                resolve_output_path(
                    root,
                    {
                        "filename": "secret.mp4",
                        "subfolder": "../outside",
                        "type": "output",
                        "media_type": "video",
                    },
                )


if __name__ == "__main__":
    unittest.main()
