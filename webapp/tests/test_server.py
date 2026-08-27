from __future__ import annotations

import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from webapp.comfy_client import ComfyError
from webapp.job_store import JobStore
from webapp.server import MISSING_JOB_GRACE_MS, create_app


class FakeProxyContent:
    async def iter_chunked(self, _: int):
        yield b"<script>should-not-be-html</script>"


class FakeProxyResponse:
    status = 200
    headers = {"Content-Type": "text/html"}
    content = FakeProxyContent()

    def release(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeComfy:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.devices = [{"name": "gpu0"}, {"name": "gpu1"}]
        self.records: dict[str, dict] = {}
        self.submissions: list[dict] = []
        self.upload_bytes = b""
        self.offline = False
        self.proxy_response = None

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def health(self, *, inspect_nodes: bool = False) -> dict:
        if self.offline:
            raise ComfyError("offline")
        result = {"connected": True, "stats": {"devices": self.devices}}
        if inspect_nodes:
            result.update(ready=True, missing_nodes=[])
        return result

    async def upload(self, *, fileobj, filename: str, content_type: str, subfolder: str) -> dict:
        if self.offline:
            raise ComfyError("offline")
        self.upload_bytes = fileobj.read()
        return {"path": f"{subfolder}/{filename}"}

    async def submit(self, prompt, metadata, prompt_id: str) -> dict:
        if self.offline:
            raise ComfyError("offline")
        self.submissions.append({"prompt": prompt, "metadata": metadata, "prompt_id": prompt_id})
        self.records[prompt_id] = {
            "id": prompt_id,
            "workflow_id": "local-video-gen-minimax-h3-webapp",
            "status": "pending",
            "outputs": {},
        }
        return {"prompt_id": prompt_id, "number": 1}

    async def list_jobs(self, *, scope: str = "all", limit: int = 40) -> dict:
        if self.offline:
            raise ComfyError("offline")
        # Deliberately omit outputs, matching ComfyUI's summary response.
        jobs = [{key: value for key, value in item.items() if key != "outputs"} for item in self.records.values()]
        return {"jobs": jobs[-limit:], "pagination": {}}

    async def get_job(self, job_id: str) -> dict:
        if self.offline:
            raise ComfyError("offline")
        try:
            return self.records[job_id]
        except KeyError as exc:
            raise ComfyError("not found", status=404) from exc

    async def cancel(self, job_id: str) -> dict:
        if self.offline:
            raise ComfyError("offline")
        if job_id not in self.records:
            return {"cancelled": False}
        self.records.pop(job_id)
        return {"cancelled": True}

    async def output_response(self, *args, **kwargs):
        if self.proxy_response is not None:
            return self.proxy_response
        raise AssertionError("local persisted output should be served directly")

    def job_progress(self, job_id: str):
        return None


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        private = root / "private"
        private.mkdir(mode=0o700)
        self.output_root = root / "output"
        self.output_root.mkdir()
        self.store = JobStore(private / "jobs.sqlite3", max_terminal_jobs=20)
        self.comfy = FakeComfy()
        self.model_status = patch(
            "webapp.server._local_model_status",
            new=AsyncMock(return_value=("verified", "test bundle")),
        )
        self.runtime_status = patch(
            "webapp.server._project_comfy_status",
            new=AsyncMock(return_value=(True, "test runtime")),
        )
        self.model_status.start()
        self.runtime_status_mock = self.runtime_status.start()
        self.client = TestClient(
            TestServer(create_app(self.comfy, job_store=self.store, output_root=self.output_root))
        )
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.runtime_status.stop()
        self.model_status.stop()
        self.temporary.cleanup()

    async def test_config_loopback_and_error_security_headers(self) -> None:
        response = await self.client.get("/api/config")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

        rejected = await self.client.get("/api/config", headers={"Host": "attacker.example"})
        self.assertEqual(rejected.status, 421)
        self.assertEqual(rejected.headers["X-Frame-Options"], "DENY")

        invalid = await self.client.get("/api/jobs/not-a-uuid")
        self.assertEqual(invalid.status, 400)
        self.assertEqual(invalid.headers["X-Content-Type-Options"], "nosniff")

        cross_site = await self.client.post(
            "/api/renders",
            json={"mode": "t2v", "prompt": "x"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(cross_site.status, 403)
        self.assertEqual(cross_site.headers["X-Frame-Options"], "DENY")

    async def test_studio_exposes_accessible_persistent_theme_control(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        html = await response.text()
        self.assertIn('for="themeSelect"', html)
        self.assertIn('id="themeSelect" aria-label="Color theme"', html)
        for preference in ("system", "light", "dark"):
            self.assertIn(f'<option value="{preference}">', html)
        self.assertIn('src="/static/theme-init.js"', html)

        theme_script = await self.client.get("/static/theme-init.js")
        self.assertEqual(theme_script.status, 200)
        script = await theme_script.text()
        self.assertIn('localStorage.setItem(storageKey, preference)', script)
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', script)

    async def test_non_multipart_upload_is_a_safe_client_error(self) -> None:
        response = await self.client.post(
            "/api/uploads?kind=image",
            data=b"not multipart",
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status, 400)
        self.assertIn("multipart/form-data", (await response.json())["error"])

    async def test_image_upload_is_decoded_normalized_and_opaque(self) -> None:
        source = io.BytesIO()
        Image.new("RGB", (37, 23), (10, 20, 30)).save(source, "JPEG")
        form = FormData()
        form.add_field("file", source.getvalue(), filename="scene.jpg", content_type="image/jpeg")
        response = await self.client.post("/api/uploads?kind=image", data=form)
        self.assertEqual(response.status, 201, await response.text())
        body = await response.json()
        self.assertEqual(body["kind"], "image")
        self.assertEqual(body["metadata"]["width"], 37)
        self.assertEqual(body["metadata"]["height"], 23)
        self.assertNotIn("path", body)
        self.assertTrue(self.comfy.upload_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_render_registration_native_graph_and_history(self) -> None:
        payload = {
            "mode": "t2v",
            "profile": "quality_bf16_dual",
            "prompt": "A quiet room. Audio: rain.",
            "width": 1344,
            "height": 768,
            "duration": 5,
            "seed": "9",
        }
        response = await self.client.post("/api/renders", json=payload)
        self.assertEqual(response.status, 202, await response.text())
        body = await response.json()
        self.assertTrue(self.store.owns(body["id"]))
        self.assertEqual(self.store.status(body["id"]), "pending")
        graph = self.comfy.submissions[0]["prompt"]
        self.assertEqual(sum(node["class_type"] == "SaveVideo" for node in graph.values()), 1)
        self.assertEqual(self.store.get(body["id"])["metadata"]["prompt"], payload["prompt"])

        history = await self.client.get("/api/jobs?scope=all&limit=24")
        self.assertEqual(history.status, 200)
        self.assertEqual((await history.json())["jobs"][0]["id"], body["id"])

    async def test_invalid_token_shape_and_dual_gpu_gate(self) -> None:
        response = await self.client.post(
            "/api/renders",
            json={"mode": "i2v", "prompt": "x", "first_frame": {}},
        )
        self.assertEqual(response.status, 400)
        self.comfy.devices = [{"name": "gpu0"}]
        response = await self.client.post(
            "/api/renders",
            json={"mode": "t2v", "profile": "quality_bf16_dual", "prompt": "x"},
        )
        self.assertEqual(response.status, 409)
        self.assertIn("both RTX 4090", (await response.json())["error"])

    async def test_cancel_becomes_durably_terminal(self) -> None:
        job_id = str(uuid.uuid4())
        self.store.register(job_id, {"mode": "t2v"}, status="pending")
        self.comfy.records[job_id] = {"id": job_id, "status": "pending", "outputs": {}}
        response = await self.client.post(f"/api/jobs/{job_id}/cancel", json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.status(job_id), "cancelled")

        # A lagging upstream active record must not resurrect a locally terminal cancellation.
        self.comfy.records[job_id] = {"id": job_id, "status": "in_progress", "outputs": {}}
        detail = await self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual((await detail.json())["status"], "cancelled")
        self.assertEqual(self.store.status(job_id), "cancelled")

        missing_id = str(uuid.uuid4())
        self.store.register(missing_id, {"mode": "t2v"}, status="pending")
        response = await self.client.post(f"/api/jobs/{missing_id}/cancel", json={})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["already_missing"])
        self.assertEqual(self.store.status(missing_id), "cancelled")

    async def test_stale_active_job_missing_from_reachable_engine_is_closed(self) -> None:
        job_id = str(uuid.uuid4())
        stored = self.store.register(job_id, {"mode": "t2v"}, status="submitting")
        later = (stored["updated_ms"] + MISSING_JOB_GRACE_MS + 1) / 1000
        with patch("webapp.server.time.time", return_value=later):
            response = await self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["status"], "failed")
        self.assertIn("no longer has this job", body["error"])

    async def test_offline_history_and_range_output_survive_engine_restart(self) -> None:
        job_id = str(uuid.uuid4())
        folder = self.output_root / "h3"
        folder.mkdir()
        media = folder / "finished.mp4"
        media.write_bytes(b"0123456789")
        self.store.register(job_id, {"mode": "t2v", "width": 256, "height": 256}, status="pending")
        self.store.update(
            job_id,
            "completed",
            outputs=[{
                "id": 0,
                "filename": media.name,
                "subfolder": "h3",
                "type": "output",
                "media_type": "video",
                "node_id": "42",
            }],
        )
        self.comfy.records[job_id] = {"id": job_id, "status": "completed"}
        online_history = await self.client.get("/api/jobs")
        self.assertEqual(online_history.status, 200)
        self.assertEqual(len(self.store.get(job_id)["outputs"]), 1)
        self.comfy.offline = True

        history = await self.client.get("/api/jobs")
        self.assertEqual(history.status, 200)
        self.assertFalse((await history.json())["engine_connected"])
        detail = await self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(detail.status, 200)
        self.assertEqual((await detail.json())["outputs"][0]["media_type"], "video")
        ranged = await self.client.get(
            f"/api/jobs/{job_id}/outputs/0",
            headers={"Range": "bytes=2-5"},
        )
        self.assertEqual(ranged.status, 206)
        self.assertEqual(await ranged.read(), b"2345")

    async def test_stream_proxy_forces_media_type_and_wire_security_headers(self) -> None:
        job_id = str(uuid.uuid4())
        self.store.register(job_id, {"mode": "t2v"}, status="completed")
        self.store.update(
            job_id,
            outputs=[{
                "id": 0,
                "filename": "missing-locally.mp4",
                "subfolder": "h3",
                "type": "output",
                "media_type": "video",
                "node_id": "42",
            }],
        )
        self.comfy.offline = True
        self.comfy.proxy_response = FakeProxyResponse()
        response = await self.client.get(f"/api/jobs/{job_id}/outputs/0")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "video/mp4")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(await response.read(), b"<script>should-not-be-html</script>")

    async def test_unknown_job_is_not_owned(self) -> None:
        response = await self.client.get(f"/api/jobs/{uuid.uuid4()}")
        self.assertEqual(response.status, 404)

    async def test_unverified_backend_is_never_used_for_jobs_or_outputs(self) -> None:
        job_id = str(uuid.uuid4())
        self.store.register(job_id, {"mode": "t2v"}, status="pending")
        self.comfy.records[job_id] = {"id": job_id, "status": "in_progress", "outputs": {}}
        self.runtime_status_mock.return_value = (False, "foreign backend")

        health = await self.client.get("/api/health?deep=1")
        self.assertFalse((await health.json())["connected"])
        jobs = await self.client.get("/api/jobs")
        jobs_body = await jobs.json()
        self.assertFalse(jobs_body["engine_connected"])
        self.assertEqual(jobs_body["jobs"][0]["status"], "pending")
        cancel = await self.client.post(f"/api/jobs/{job_id}/cancel", json={})
        self.assertEqual(cancel.status, 409)
        self.assertIn(job_id, self.comfy.records)

        self.store.update(
            job_id,
            "completed",
            outputs=[{
                "id": 0,
                "filename": "foreign.mp4",
                "subfolder": "h3",
                "type": "output",
                "media_type": "video",
                "node_id": "42",
            }],
        )
        self.comfy.proxy_response = FakeProxyResponse()
        output = await self.client.get(f"/api/jobs/{job_id}/outputs/0")
        self.assertEqual(output.status, 409)


if __name__ == "__main__":
    unittest.main()
