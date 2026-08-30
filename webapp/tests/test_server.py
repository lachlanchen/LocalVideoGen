from __future__ import annotations

import io
import json
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
from webapp.series_runner import LALACHAN_REFERENCE_LABELS
from webapp.server import ASSETS_KEY, MISSING_JOB_GRACE_MS, SERIES_KEY, create_app
from webapp.workflows import AUX_DEVICE_ENV, UploadedAsset


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
        config = await response.json()
        self.assertEqual(config["series_api_version"], 1)
        self.assertEqual(
            config["series"]["lalachan_picture_labels"][:2],
            ["Words card", "Zhuangzi Robot"],
        )
        self.assertIn("world_travel", config["series"]["templates"])
        self.assertEqual(
            config["series"]["world_travel_scene_reference_per_shot"], 1
        )
        policy = config["series"]["shot_reference_policy"]
        self.assertEqual(policy["field"], "omit_shared_image_labels")
        self.assertTrue(policy["logical_picture_tags_remapped"])
        self.assertEqual(
            policy["recommended_omissions_after_first"],
            ["Words card", "LightMind glasses", "Patchwork notebook"],
        )
        self.assertIn("paused", policy["editable_states"])
        capability = config["series"]["capabilities"]["world_travel"]
        self.assertEqual(capability["template"], "world_travel")
        self.assertEqual(capability["render_mode"], "r2v")
        self.assertEqual(capability["recommended_continuity_seconds"], 2)
        self.assertEqual(
            capability["maximum_quality_profile"], "quality_bf16_dual"
        )
        picture_slots = capability["picture_slots"]
        self.assertEqual(
            picture_slots["shared"],
            [
                {"slot": index, "label": label}
                for index, label in enumerate(
                    LALACHAN_REFERENCE_LABELS, start=1
                )
            ],
        )
        self.assertEqual(picture_slots["scene"]["slot"], 8)
        self.assertTrue(picture_slots["scene"]["required"])
        self.assertEqual(picture_slots["continuity_final_frame"]["slot"], 9)
        self.assertTrue(
            picture_slots["continuity_final_frame"]["sha256_required"]
        )
        self.assertEqual(capability["continuity_tail"]["maximum_slot"], 3)
        self.assertTrue(capability["continuity_tail"]["sha256_required"])
        self.assertEqual(
            capability["continuity_recovery_requires"],
            ["video_path", "video_sha256", "image_path", "image_sha256"],
        )
        quality_profile = next(
            profile
            for profile in config["profiles"]
            if profile["id"] == capability["maximum_quality_profile"]
        )
        self.assertEqual(quality_profile["precision"], "bf16")
        self.assertTrue(quality_profile["dual_gpu"])
        self.assertFalse(quality_profile["turbo"])
        self.assertEqual(quality_profile["steps_ref"], 25)
        uploads = config["uploads"]
        self.assertEqual(uploads["multipart_field"], "file")
        self.assertEqual(uploads["kinds"], ["image", "video", "audio"])
        self.assertEqual(uploads["normalized_max_bytes"], 99 * 1024 * 1024)
        self.assertEqual(
            uploads["image"]["extensions"],
            [".bmp", ".jpeg", ".jpg", ".png", ".webp"],
        )
        self.assertEqual(uploads["image"]["source_max_bytes"], 30 * 1024 * 1024)
        self.assertEqual(uploads["image"]["source_max_edge"], 8192)
        self.assertEqual(uploads["image"]["source_max_pixels"], 40_000_000)
        self.assertTrue(uploads["image"]["single_frame"])
        self.assertEqual(uploads["image"]["normalized"]["format"], "PNG")
        self.assertEqual(uploads["video"]["source_max_bytes"], 600 * 1024 * 1024)
        self.assertEqual(
            uploads["video"]["duration_seconds"], {"min": 2.0, "max": 15.0}
        )
        self.assertEqual(uploads["video"]["source_fps"], {"min": 1.0, "max": 240.0})
        self.assertEqual(uploads["video"]["source_audio_sample_rate_max"], 384_000)
        self.assertEqual(uploads["video"]["source_audio_channels_max"], 32)
        self.assertEqual(uploads["video"]["source_streams_max"], 32)
        self.assertEqual(uploads["video"]["normalized"]["max_edge"], 2048)
        self.assertEqual(uploads["video"]["normalized"]["fps"], 24)
        self.assertEqual(uploads["video"]["normalized"]["audio_codec"], "AAC")
        self.assertEqual(uploads["audio"]["source_max_bytes"], 100 * 1024 * 1024)
        self.assertEqual(
            uploads["audio"]["duration_seconds"], {"min": 0.1, "max": 15.0}
        )
        self.assertEqual(uploads["audio"]["source_sample_rate_max"], 384_000)
        self.assertEqual(uploads["audio"]["source_channels_max"], 32)
        self.assertEqual(uploads["audio"]["normalized"]["codec"], "pcm_s16le")
        self.assertEqual(uploads["audio"]["normalized"]["sample_rate"], 32_000)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

        rejected = await self.client.get("/api/config", headers={"Host": "attacker.example"})
        self.assertEqual(rejected.status, 421)
        self.assertEqual(rejected.headers["X-Frame-Options"], "DENY")

        favicon = await self.client.get("/favicon.ico")
        self.assertEqual(favicon.status, 204)

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

    async def test_studio_exposes_beginner_guide_and_session_controls(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        html = await response.text()
        for element_id in (
            "quickStartTitle",
            "newSession",
            "reuseSession",
            "jobList",
            "toggleSessions",
            "systemPrompt",
            "saveSystemPrompt",
            "deleteSessionDialog",
            "confirmDeleteSession",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("Make your first clip in three steps", html)
        self.assertIn("Recent sessions", html)
        self.assertIn("Only required field", html)

        app_script = await self.client.get("/static/app.js")
        self.assertEqual(app_script.status, 200)
        self.assertEqual(app_script.headers["Cache-Control"], "no-cache")
        script = await app_script.text()
        self.assertIn("function resetSingleSession()", script)
        self.assertIn("function reuseActiveSession()", script)
        self.assertIn("sessionStatusLabel", script)
        self.assertIn("state.jobs.slice(0, 6)", script)
        self.assertIn("function deletePendingSession()", script)
        self.assertIn("function savePreferredDuration", script)
        self.assertIn("function saveRememberedRequirements()", script)
        self.assertNotIn("function saveSystemPrompt()", script)
        self.assertIn('api("/api/settings"', script)

    async def test_settings_survive_deleted_session_database_and_apply_to_render(self) -> None:
        saved = await self.client.put(
            "/api/settings",
            json={
                "system_prompt": "Never add subtitles. Keep the camera steady.",
                "preferred_duration": 7.5,
            },
        )
        self.assertEqual(saved.status, 200, await saved.text())
        self.assertEqual((await saved.json())["preferred_duration"], 7.5)

        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.store.path}{suffix}")
            if candidate.exists():
                candidate.unlink()
        history = await self.client.get("/api/jobs?scope=all&limit=24")
        self.assertEqual(history.status, 200, await history.text())
        self.assertEqual((await history.json())["jobs"], [])
        settings = await self.client.get("/api/settings")
        self.assertEqual((await settings.json())["preferred_duration"], 7.5)

        payload = {
            "mode": "t2v",
            "profile": "preview_int8_turbo_dual",
            "prompt": "A paper boat crosses a puddle.",
            "width": 864,
            "height": 480,
            "duration": 2,
            "seed": "17",
        }
        response = await self.client.post("/api/renders", json=payload)
        self.assertEqual(response.status, 202, await response.text())
        body = await response.json()
        submission = self.comfy.submissions[-1]
        serialized_graph = json.dumps(submission["prompt"])
        self.assertIn("Never add subtitles. Keep the camera steady.", serialized_graph)
        self.assertIn(payload["prompt"], serialized_graph)
        metadata = self.store.get(body["id"])["metadata"]
        self.assertEqual(metadata["prompt"], payload["prompt"])
        self.assertEqual(
            metadata["system_prompt"],
            "Never add subtitles. Keep the camera steady.",
        )

    async def test_settings_validation_is_clear_and_bounded(self) -> None:
        bad_duration = await self.client.put(
            "/api/settings", json={"preferred_duration": 2.25}
        )
        self.assertEqual(bad_duration.status, 400)
        self.assertIn("0.5 second", (await bad_duration.json())["error"])
        unknown = await self.client.put("/api/settings", json={"mystery": True})
        self.assertEqual(unknown.status, 400)
        too_long = await self.client.put(
            "/api/settings", json={"system_prompt": "x" * 2001}
        )
        self.assertEqual(too_long.status, 400)

    async def test_studio_exposes_the_guided_series_workspace(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        html = await response.text()
        for element_id in (
            "singleWorkflowTab",
            "seriesWorkflowTab",
            "seriesComposer",
            "canonicalReferenceGrid",
            "seriesShotList",
            "seriesPreflight",
            "seriesTimeline",
            "seriesLibrary",
            "startSavedSeries",
            "retrySeriesFinalization",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("LALACHAN Series", html)
        self.assertIn("World Travel", html)
        self.assertIn("My Movie", html)
        self.assertIn('name="seriesTemplate" value="world_travel"', html)
        self.assertIn('id="worldTravelIdentityGuard"', html)
        self.assertIn("Only one heavy render runs at a time.", html)

        app_script = await self.client.get("/static/app.js")
        self.assertEqual(app_script.status, 200)
        script = await app_script.text()
        self.assertIn('api("/api/series"', script)
        self.assertIn('api("/api/uploads/validate"', script)
        self.assertIn("brief: elements.seriesBrief.value.trim()", script)
        self.assertIn('scene_reference: {', script)
        self.assertIn('omit_shared_image_labels: worldTravelOmissionsForShot', script)
        self.assertIn('strong.textContent = "Opening-only reference guard"', script)
        self.assertIn('template === "world_travel"', script)
        self.assertIn(
            'templateContinuity: { lalachan: "3", world_travel: "2", movie: "3" }',
            script,
        )
        self.assertIn("previous shot's exact final frame", script)
        for phrase in (
            "reference tail is context only",
            "next unseen moment",
            "within 0.5 seconds",
            "calm, natural human pace",
        ):
            self.assertIn(phrase, script)
        self.assertIn("/retry-finalization", script)
        self.assertIn("/start`, { method: \"POST\"", script)

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

        validation = await self.client.post(
            "/api/uploads/validate",
            json={
                "uploads": [
                    {"token": body["token"], "kind": "image"},
                    {"token": "expired-handle", "kind": "image"},
                ]
            },
        )
        self.assertEqual(validation.status, 200, await validation.text())
        self.assertEqual((await validation.json())["valid"], [body["token"]])

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

        overlapping = await self.client.post("/api/renders", json=payload)
        self.assertEqual(overlapping.status, 409)
        self.assertIn("Another local H3 render", (await overlapping.json())["error"])
        self.assertEqual(len(self.comfy.submissions), 1)

        history = await self.client.get("/api/jobs?scope=all&limit=24")
        self.assertEqual(history.status, 200)
        session = (await history.json())["jobs"][0]
        self.assertEqual(session["id"], body["id"])
        self.assertEqual(session["session"]["title"], payload["prompt"])
        self.assertEqual(session["session"]["mode"], "t2v")

    async def test_series_api_keeps_upload_locations_private_and_waits_for_manual_job(self) -> None:
        source = io.BytesIO()
        Image.new("RGB", (37, 23), (10, 20, 30)).save(source, "PNG")
        form = FormData()
        form.add_field("file", source.getvalue(), filename="rara.png", content_type="image/png")
        uploaded = await self.client.post("/api/uploads?kind=image", data=form)
        token = (await uploaded.json())["token"]
        payload = {
            "title": "Forest rescue",
            "template": "movie",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1024,
                "height": 768,
                "ref_image_size": "max",
                "continuity_seconds": 3,
                "advance": True,
            },
            "references": {
                "images": [{"token": token, "label": "Rara Xia"}],
                "videos": [],
                "audio": [],
            },
            "shots": [
                {"title": "Signal", "prompt": "Find the signal.", "duration": 5, "seed": 1},
                {"title": "Rescue", "prompt": "Lift the beam.", "duration": 5, "seed": 2},
            ],
        }
        created = await self.client.post("/api/series", json=payload)
        self.assertEqual(created.status, 201, await created.text())
        series = await created.json()
        self.assertEqual(series["references"]["images"][0]["label"], "Rara Xia")
        self.assertNotIn("token", str(series))
        self.assertNotIn("h3-webapp/image", str(series))
        self.assertEqual(series["status"], "ready")

        listed = await self.client.get("/api/series")
        library_item = (await listed.json())["series"][0]
        self.assertEqual(library_item["id"], series["id"])
        self.assertEqual(library_item["shot_count"], 2)
        for detail_only in ("shots", "references", "artifacts"):
            self.assertNotIn(detail_only, library_item)
        detail = await self.client.get(f"/api/series/{series['id']}")
        self.assertEqual((await detail.json())["title"], "Forest rescue")
        payload["title"] = "Forest rescue revised"
        edited = await self.client.put(f"/api/series/{series['id']}", json=payload)
        self.assertEqual(edited.status, 200, await edited.text())
        self.assertEqual((await edited.json())["title"], "Forest rescue revised")

        manual_job = str(uuid.uuid4())
        self.store.register(manual_job, {"mode": "t2v"}, status="pending")
        started = await self.client.post(f"/api/series/{series['id']}/start", json={})
        self.assertEqual(started.status, 202, await started.text())
        await __import__("asyncio").sleep(0.02)
        durable = self.client.app[SERIES_KEY].get(series["id"])
        self.assertIn(durable["status"], {"queued", "waiting"})
        self.assertEqual(self.comfy.submissions, [])

        paused = await self.client.post(f"/api/series/{series['id']}/pause", json={})
        self.assertEqual(paused.status, 202)
        self.assertEqual((await paused.json())["status"], "paused")
        resumed = await self.client.post(f"/api/series/{series['id']}/resume", json={})
        self.assertEqual(resumed.status, 202)
        self.assertEqual((await resumed.json())["status"], "queued")

    async def test_world_travel_create_and_put_resolve_private_per_shot_scenes(
        self,
    ) -> None:
        registry = self.client.app[ASSETS_KEY]

        def image_handle(name: str, index: int) -> str:
            record = registry.register(
                UploadedAsset(
                    "image",
                    f"h3-webapp/image/private-{index}.png",
                    f"{name.lower().replace(' ', '-')}.png",
                    {"sha256": f"{index:064x}"},
                ),
                100,
            )
            return record.token

        shared = [
            {
                "token": image_handle(label, index),
                "label": label,
            }
            for index, label in enumerate(LALACHAN_REFERENCE_LABELS, start=1)
        ]
        rome = image_handle("Rome scene", 8)
        venice = image_handle("Venice scene", 9)
        payload = {
            "title": "Italy route",
            "brief": "One continuous journey through Italy.",
            "template": "world_travel",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1024,
                "height": 768,
                "continuity_seconds": 3,
            },
            "references": {"images": shared, "videos": [], "audio": []},
            "shots": [
                {
                    "title": "Rome",
                    "prompt": "Meet history at <Picture 8>.",
                    "duration": 5,
                    "seed": 1,
                    "scene_reference": {
                        "token": rome,
                        "label": "Rome · Colosseum",
                    },
                },
                {
                    "title": "Venice",
                    "prompt": "Arrive naturally at <Picture 8>.",
                    "duration": 5,
                    "seed": 2,
                    "scene_reference": {
                        "token": venice,
                        "label": "Venice · Grand Canal",
                    },
                },
            ],
        }
        created = await self.client.post("/api/series", json=payload)
        self.assertEqual(created.status, 201, await created.text())
        body = await created.json()
        self.assertEqual(body["template"], "world_travel")
        self.assertEqual(
            body["shots"][0]["scene_reference"],
            {
                "kind": "image",
                "name": "rome-scene.png",
                "label": "Rome · Colosseum",
            },
        )
        for secret in (rome, venice, "h3-webapp/image", "sha256"):
            self.assertNotIn(secret, str(body))

        payload["shots"][1]["scene_reference"]["label"] = "Venice · Rialto approach"
        updated = await self.client.put(f"/api/series/{body['id']}", json=payload)
        self.assertEqual(updated.status, 200, await updated.text())
        updated_body = await updated.json()
        self.assertEqual(
            updated_body["shots"][1]["scene_reference"]["label"],
            "Venice · Rialto approach",
        )
        private = self.client.app[SERIES_KEY].get(body["id"])["document"]
        self.assertEqual(
            private["shots"][1]["scene_reference"]["path"],
            "h3-webapp/image/private-9.png",
        )
        self.assertRegex(
            private["shots"][1]["scene_reference"]["sha256"],
            r"^[0-9a-f]{64}$",
        )

        policy = await self.client.put(
            f"/api/series/{body['id']}/shots/1/reference-policy",
            json={
                "omit_shared_image_labels": [
                    "Words card",
                    "LightMind glasses",
                    "Patchwork notebook",
                ]
            },
        )
        self.assertEqual(policy.status, 200, await policy.text())
        policy_body = await policy.json()
        self.assertEqual(
            policy_body["shots"][1]["omit_shared_image_labels"],
            ["Words card", "LightMind glasses", "Patchwork notebook"],
        )
        rejected = await self.client.put(
            f"/api/series/{body['id']}/shots/1/reference-policy",
            json={"omit_shared_image_labels": ["Aya Chan"]},
        )
        self.assertEqual(rejected.status, 400)
        self.assertIn("persistent cast", (await rejected.json())["error"])

    async def test_invalid_token_shape_and_dual_gpu_gate(self) -> None:
        response = await self.client.post(
            "/api/renders",
            json={"mode": "i2v", "prompt": "x", "first_frame": {}},
        )
        self.assertEqual(response.status, 400)
        self.comfy.devices = [{"name": "gpu0"}]
        with patch.dict("os.environ", {AUX_DEVICE_ENV: "gpu:1"}):
            response = await self.client.post(
                "/api/renders",
                json={"mode": "t2v", "profile": "quality_bf16_dual", "prompt": "x"},
            )
        self.assertEqual(response.status, 409)
        self.assertIn("both RTX 4090", (await response.json())["error"])
        with patch.dict("os.environ", {AUX_DEVICE_ENV: "gpu:0"}):
            response = await self.client.post(
                "/api/renders",
                json={"mode": "t2v", "profile": "quality_bf16_dual", "prompt": "x"},
            )
        self.assertEqual(response.status, 202, await response.text())
        self.assertEqual(len(self.comfy.submissions), 1)

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

    async def test_cancel_cannot_overwrite_watcher_completion(self) -> None:
        job_id = str(uuid.uuid4())
        self.store.register(job_id, {"mode": "t2v"}, status="pending")
        self.comfy.records[job_id] = {
            "id": job_id,
            "status": "pending",
            "outputs": {},
        }
        output = {
            "id": 0,
            "filename": "precious.mp4",
            "subfolder": "video",
            "type": "output",
            "media_type": "video",
            "node_id": "42",
        }

        async def completed_during_cancel(cancelled_job_id):
            self.assertEqual(cancelled_job_id, job_id)
            self.store.update(job_id, "completed", outputs=[output])
            return {"cancelled": True}

        self.comfy.cancel = completed_during_cancel
        response = await self.client.post(f"/api/jobs/{job_id}/cancel", json={})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["already_completed"])
        stored = self.store.get(job_id)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["outputs"], [output])

        # A later stale terminal response cannot hide or empty the completion.
        self.comfy.records[job_id] = {
            "id": job_id,
            "status": "failed",
            "outputs": {},
            "execution_error": {"message": "stale failure"},
        }
        detail = await self.client.get(f"/api/jobs/{job_id}")
        body = await detail.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(len(body["outputs"]), 1)
        self.assertNotIn("error", body)

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

    async def test_delete_session_removes_allowlisted_video_and_registry_row(self) -> None:
        job_id = str(uuid.uuid4())
        folder = self.output_root / "h3"
        folder.mkdir()
        media = folder / "delete-me.mp4"
        media.write_bytes(b"temporary-video")
        self.store.register(
            job_id,
            {"mode": "t2v", "prompt": "Temporary deletion test"},
            status="completed",
        )
        self.store.update(
            job_id,
            outputs=[{
                "id": 0,
                "filename": media.name,
                "subfolder": "h3",
                "type": "output",
                "media_type": "video",
                "node_id": "42",
            }],
        )
        history = await self.client.get("/api/jobs")
        session = (await history.json())["jobs"][0]["session"]
        self.assertTrue(session["deletable"])
        self.assertFalse(session["managed_by_series"])

        response = await self.client.delete(f"/api/jobs/{job_id}")
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertEqual(body["files_deleted"], 1)
        self.assertFalse(media.exists())
        self.assertIsNone(self.store.get(job_id))

    async def test_delete_protects_active_series_and_symlinked_outputs(self) -> None:
        active_id = str(uuid.uuid4())
        self.store.register(active_id, {"prompt": "active"}, status="pending")
        active = await self.client.delete(f"/api/jobs/{active_id}")
        self.assertEqual(active.status, 409)
        self.assertTrue(self.store.owns(active_id))

        series_id = str(uuid.uuid4())
        series_job_id = str(uuid.uuid4())
        self.store.register(
            series_job_id,
            {"prompt": "shot", "series_id": series_id},
            status="completed",
        )
        series = await self.client.delete(f"/api/jobs/{series_job_id}")
        self.assertEqual(series.status, 409)
        self.assertTrue(self.store.owns(series_job_id))

        target = self.output_root / "precious.mp4"
        target.write_bytes(b"keep")
        link = self.output_root / "linked.mp4"
        link.symlink_to(target)
        linked_id = str(uuid.uuid4())
        self.store.register(linked_id, {"prompt": "linked"}, status="completed")
        self.store.update(
            linked_id,
            outputs=[{
                "id": 0,
                "filename": link.name,
                "subfolder": "",
                "type": "output",
                "media_type": "video",
                "node_id": "42",
            }],
        )
        linked = await self.client.delete(f"/api/jobs/{linked_id}")
        self.assertEqual(linked.status, 409)
        self.assertTrue(link.exists())
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertTrue(self.store.owns(linked_id))

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

    async def test_series_artifact_is_served_only_from_its_durable_allowlist(self) -> None:
        folder = self.output_root / "series"
        folder.mkdir()
        media = folder / "preserved.mp4"
        media.write_bytes(b"precious-attempt")
        series_id = str(uuid.uuid4())
        artifact_id = str(uuid.uuid4())
        document = {
            "title": "Saved series",
            "brief": "",
            "template": "movie",
            "settings": {},
            "references": {"images": [], "videos": [], "audio": []},
            "shots": [],
            "active_shot": None,
            "pause_requested": False,
            "cancel_requested": False,
            "error": None,
            "artifacts": [{
                "id": artifact_id,
                "kind": "shot",
                "label": "Preserved attempt",
                "storage": "output",
                "locator": {
                    "filename": media.name,
                    "subfolder": "series",
                    "type": "output",
                    "media_type": "video",
                },
                "mime": "video/mp4",
                "download_name": media.name,
                "metadata": {},
            }],
        }
        self.client.app[SERIES_KEY].create(series_id, document, status="completed")
        response = await self.client.get(
            f"/api/series/{series_id}/artifacts/{artifact_id}",
            headers={"Range": "bytes=9-15"},
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"attempt")
        unknown = await self.client.get(
            f"/api/series/{series_id}/artifacts/{uuid.uuid4()}"
        )
        self.assertEqual(unknown.status, 404)

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
