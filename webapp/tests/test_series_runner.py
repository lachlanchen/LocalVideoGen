from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from webapp.comfy_client import ComfyError
from webapp.job_store import JobStore
from webapp.series_runner import SeriesRunner, build_series_document, public_series
from webapp.series_store import SeriesStore
from webapp.workflows import RequestError, UploadedAsset


class FakeClient:
    def __init__(self) -> None:
        self.submissions = []
        self.uploads = []

    async def health(self, *, inspect_nodes=False):
        return {
            "ready": True,
            "missing_nodes": [],
            "stats": {"devices": [{"name": "gpu0"}, {"name": "gpu1"}]},
        }

    async def submit(self, prompt, metadata, prompt_id):
        self.submissions.append(
            {"prompt": prompt, "metadata": metadata, "prompt_id": prompt_id}
        )
        return {"prompt_id": prompt_id, "number": len(self.submissions)}

    async def get_job(self, job_id):
        raise AssertionError("test terminalizes jobs directly in the durable registry")

    async def upload(self, *, fileobj, filename, content_type, subfolder):
        self.uploads.append((filename, fileobj.read()))
        return {"path": f"{subfolder}/{filename}"}

    async def cancel(self, job_id):
        return {"cancelled": True}

    def job_progress(self, job_id):
        return None


class FakeMedia:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()
        self.output = self.root / "render.mp4"
        self.output.write_bytes(b"render")
        self.stitched = False
        self.continuity_calls = []

    def output_path(self, locator):
        return self.output

    async def validate_video(self, path, *, width, height, expected_frames=None, **kwargs):
        return {
            "width": width,
            "height": height,
            "fps": 24.0,
            "frames": expected_frames,
            "duration": expected_frames / 24,
            "audio_sample_rate": 32000,
            "audio_channels": 2,
            "bytes": path.stat().st_size,
            "sha256": "a" * 64,
        }

    async def make_continuity(
        self,
        series_id,
        *,
        shot_index,
        attempt_number,
        source,
        source_duration,
        source_frames,
        seconds,
        width,
        height,
    ):
        self.continuity_calls.append((shot_index, attempt_number))
        tail = self.root / f"tail-{shot_index}-{attempt_number}.mp4"
        frame = self.root / f"frame-{shot_index}-{attempt_number}.png"
        tail.write_bytes(b"tail")
        frame.write_bytes(b"frame")
        return (
            {
                "id": str(uuid.uuid4()),
                "kind": "continuity_video",
                "label": "tail",
                "storage": "series",
                "relative": tail.name,
                "mime": "video/mp4",
                "download_name": tail.name,
                "metadata": {"duration": seconds},
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "final_frame",
                "label": "frame",
                "storage": "series",
                "relative": frame.name,
                "mime": "image/png",
                "download_name": frame.name,
                "metadata": {},
            },
        )

    def artifact_path(self, series_id, relative):
        return self.root / relative

    async def stitch(self, series_id, *, title, shots, width, height):
        self.stitched = True
        self.accepted = list(shots)
        return (
            {
                "id": str(uuid.uuid4()),
                "kind": "final",
                "label": "Stitched series",
                "storage": "series",
                "relative": "final.mp4",
                "mime": "video/mp4",
                "download_name": "final.mp4",
                "metadata": {"frames": sum(item["metadata"]["frames"] for item in shots)},
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "manifest",
                "label": "Manifest",
                "storage": "series",
                "relative": "manifest.json",
                "mime": "application/json",
                "download_name": "manifest.json",
                "metadata": {},
            },
        )


class SequentialSeriesRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        private = root / "private"
        private.mkdir(mode=0o700)
        self.jobs = JobStore(private / "jobs.sqlite3")
        self.series = SeriesStore(private / "series.sqlite3")
        self.client = FakeClient()
        self.media = FakeMedia(root / "artifacts")
        self.runner = SeriesRunner(
            self.series, self.jobs, self.client, self.media, poll_interval=0.01
        )
        payload = {
            "title": "Two-shot movie",
            "template": "movie",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1024,
                "height": 768,
                "continuity_seconds": 3,
                "advance": True,
            },
            "references": {"images": [], "videos": [], "audio": []},
            "shots": [
                {"title": "One", "prompt": "A person enters.", "duration": 5, "seed": 10},
                {"title": "Two", "prompt": "The person waves.", "duration": 5, "seed": 11},
            ],
        }
        document = build_series_document(payload, lambda token, kind, optional: None)
        self.series_id = str(uuid.uuid4())
        self.series.create(self.series_id, document, status="queued")

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def output_locator():
        return {
            "id": 0,
            "filename": "shot.mp4",
            "subfolder": "video",
            "type": "output",
            "media_type": "video",
            "node_id": "42",
        }

    async def test_waits_for_unrelated_job_then_chains_and_stitches(self) -> None:
        unrelated = str(uuid.uuid4())
        self.jobs.register(unrelated, {"mode": "t2v"}, status="pending")
        self.assertTrue(await self.runner.run_once())
        self.assertEqual(self.series.get(self.series_id)["status"], "waiting")
        self.assertEqual(self.client.submissions, [])

        self.jobs.update(unrelated, "completed")
        self.assertTrue(await self.runner.run_once())
        first_job = self.client.submissions[0]["prompt_id"]
        first_graph = self.client.submissions[0]["prompt"]
        self.assertIn("MiniMaxH3ImageToVideo", {node["class_type"] for node in first_graph.values()})
        self.jobs.update(first_job, "completed", outputs=[self.output_locator()])
        self.assertTrue(await self.runner.run_once())
        after_first = self.series.get(self.series_id)
        self.assertEqual(after_first["document"]["shots"][0]["status"], "completed")
        self.assertEqual(len(self.client.uploads), 2)

        self.assertTrue(await self.runner.run_once())
        second_job = self.client.submissions[1]["prompt_id"]
        second_graph = self.client.submissions[1]["prompt"]
        self.assertIn(
            "MiniMaxH3ReferenceToVideo",
            {node["class_type"] for node in second_graph.values()},
        )
        load_video_paths = [
            node["inputs"]["file"]
            for node in second_graph.values()
            if node["class_type"] == "LoadVideo"
        ]
        self.assertTrue(any("h3-webapp/series/" in path for path in load_video_paths))
        load_image_paths = [
            node["inputs"]["image"]
            for node in second_graph.values()
            if node["class_type"] == "LoadImage"
        ]
        self.assertTrue(any("h3-webapp/series/" in path for path in load_image_paths))
        self.jobs.update(second_job, "completed", outputs=[self.output_locator()])
        self.assertTrue(await self.runner.run_once())
        self.assertEqual(self.media.continuity_calls, [(0, 1)])
        self.assertTrue(await self.runner.run_once())
        completed = self.series.get(self.series_id)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(self.media.stitched)
        public = public_series(completed, self.client)
        self.assertEqual(public["progress"]["percent"], 100.0)
        self.assertEqual(public["final_artifact"]["kind"], "final")
        self.assertEqual(len(public["shots"][0]["attempts"]), 1)

    async def test_shared_admission_lock_rechecks_after_a_manual_claim_race(self) -> None:
        await self.runner.submission_lock.acquire()
        task = asyncio.create_task(self.runner.run_once())
        await asyncio.sleep(0)
        manual_job = str(uuid.uuid4())
        self.jobs.register(manual_job, {"mode": "t2v"}, status="pending")
        self.runner.submission_lock.release()
        self.assertTrue(await task)
        self.assertEqual(self.series.get(self.series_id)["status"], "waiting")
        self.assertEqual(self.client.submissions, [])

    async def test_transient_preclaim_health_failure_retries_without_stranding(self) -> None:
        healthy = {
            "ready": True,
            "missing_nodes": [],
            "stats": {"devices": [{"name": "gpu0"}, {"name": "gpu1"}]},
        }
        self.client.health = AsyncMock(
            side_effect=[ComfyError("temporarily offline"), healthy]
        )
        self.assertFalse(await self.runner.run_once())
        waiting = self.series.get(self.series_id)
        self.assertEqual(waiting["status"], "queued")
        self.assertEqual(waiting["document"]["shots"][0]["status"], "pending")
        self.assertEqual(waiting["document"]["shots"][0]["attempts"], [])
        self.assertEqual(self.client.submissions, [])
        self.assertTrue(await self.runner.run_once())
        self.assertEqual(len(self.client.submissions), 1)
        self.assertEqual(len(self.series.get(self.series_id)["document"]["shots"][0]["attempts"]), 1)

    async def test_restart_reconciles_registered_attempt_without_resubmitting(self) -> None:
        self.assertTrue(await self.runner.run_once())
        job_id = self.client.submissions[0]["prompt_id"]
        reopened_jobs = JobStore(self.jobs.path)
        reopened_series = SeriesStore(self.series.path)
        reopened_jobs.update(job_id, "completed", outputs=[self.output_locator()])
        restarted = SeriesRunner(
            reopened_series,
            reopened_jobs,
            self.client,
            self.media,
            poll_interval=0.01,
        )

        self.assertTrue(await restarted.run_once())
        recovered = reopened_series.get(self.series_id)
        self.assertEqual(recovered["document"]["shots"][0]["status"], "completed")
        self.assertEqual(recovered["document"]["shots"][0]["accepted_attempt"], 1)
        self.assertEqual(len(self.client.submissions), 1)

    async def test_pause_during_preclaim_health_cannot_be_overwritten(self) -> None:
        health_started = asyncio.Event()
        release_health = asyncio.Event()
        original_health = self.client.health

        async def blocked_health(*, inspect_nodes=False):
            health_started.set()
            await release_health.wait()
            return await original_health(inspect_nodes=inspect_nodes)

        self.client.health = blocked_health
        submission = asyncio.create_task(self.runner.run_once())
        await health_started.wait()
        paused = await self.runner.pause(self.series_id)
        self.assertEqual(paused["status"], "paused")
        release_health.set()
        self.assertTrue(await submission)
        self.assertEqual(self.series.get(self.series_id)["status"], "paused")
        self.assertEqual(self.client.submissions, [])

    async def test_false_cancel_stays_cancelling_until_engine_confirms(self) -> None:
        await self.runner.run_once()
        job_id = self.client.submissions[0]["prompt_id"]
        await self.runner.cancel(self.series_id)
        self.client.cancel = AsyncMock(return_value={"cancelled": False})
        self.client.get_job = AsyncMock(
            return_value={"id": job_id, "status": "in_progress", "outputs": {}}
        )
        self.assertFalse(await self.runner.run_once())
        still_running = self.series.get(self.series_id)
        self.assertEqual(still_running["status"], "cancelling")
        self.assertEqual(still_running["document"]["active_shot"], 0)
        self.assertIn(self.jobs.status(job_id), {"pending", "in_progress"})

        self.client.cancel = AsyncMock(return_value={"cancelled": True})
        self.assertTrue(await self.runner.run_once())
        self.assertEqual(self.series.get(self.series_id)["status"], "cancelled")
        self.assertEqual(self.jobs.status(job_id), "cancelled")

    async def test_false_cancel_preserves_job_that_completed_at_boundary(self) -> None:
        await self.runner.run_once()
        job_id = self.client.submissions[0]["prompt_id"]
        await self.runner.cancel(self.series_id)
        self.client.cancel = AsyncMock(return_value={"cancelled": False})
        locator = self.output_locator()
        self.client.get_job = AsyncMock(
            return_value={
                "id": job_id,
                "status": "completed",
                "outputs": {"42": {"videos": [locator]}},
            }
        )

        self.assertTrue(await self.runner.run_once())
        preserved = self.series.get(self.series_id)
        self.assertEqual(preserved["status"], "cancelled")
        self.assertEqual(self.jobs.status(job_id), "completed")
        shot = preserved["document"]["shots"][0]
        self.assertEqual(shot["status"], "completed")
        self.assertEqual(shot["accepted_attempt"], 1)
        self.assertEqual(len(shot["attempts"][0]["artifact_ids"]), 1)

    async def test_confirmed_cancel_cannot_overwrite_concurrent_completion(self) -> None:
        await self.runner.run_once()
        job_id = self.client.submissions[0]["prompt_id"]
        await self.runner.cancel(self.series_id)

        async def completed_while_cancelling(cancelled_job_id):
            self.assertEqual(cancelled_job_id, job_id)
            self.jobs.update(job_id, "completed", outputs=[self.output_locator()])
            return {"cancelled": True}

        self.client.cancel = completed_while_cancelling
        self.assertTrue(await self.runner.run_once())
        preserved = self.series.get(self.series_id)
        self.assertEqual(self.jobs.status(job_id), "completed")
        self.assertEqual(preserved["status"], "cancelled")
        self.assertEqual(preserved["document"]["shots"][0]["status"], "completed")

    async def test_cancel_waits_for_atomic_submit_boundary(self) -> None:
        submit_started = asyncio.Event()
        release_submit = asyncio.Event()

        async def blocked_submit(prompt, metadata, prompt_id):
            submit_started.set()
            await release_submit.wait()
            self.client.submissions.append(
                {"prompt": prompt, "metadata": metadata, "prompt_id": prompt_id}
            )
            return {"prompt_id": prompt_id, "number": 1}

        self.client.submit = blocked_submit
        submit_task = asyncio.create_task(self.runner.run_once())
        await submit_started.wait()
        cancel_task = asyncio.create_task(self.runner.cancel(self.series_id))
        await asyncio.sleep(0)
        self.assertFalse(cancel_task.done())
        release_submit.set()
        self.assertTrue(await submit_task)
        cancelling = await cancel_task
        self.assertEqual(cancelling["status"], "cancelling")
        job_id = self.client.submissions[0]["prompt_id"]
        self.assertEqual(self.jobs.status(job_id), "pending")
        self.assertTrue(await self.runner.run_once())
        self.assertEqual(self.jobs.status(job_id), "cancelled")
        self.assertEqual(self.series.get(self.series_id)["status"], "cancelled")

    async def test_cancel_during_postprocessing_preserves_completed_output(self) -> None:
        await self.runner.run_once()
        job_id = self.client.submissions[0]["prompt_id"]
        self.jobs.update(job_id, "completed", outputs=[self.output_locator()])
        validation_started = asyncio.Event()
        release_validation = asyncio.Event()
        original_validate = self.media.validate_video

        async def blocked_validation(*args, **kwargs):
            validation_started.set()
            await release_validation.wait()
            return await original_validate(*args, **kwargs)

        self.media.validate_video = blocked_validation
        processing = asyncio.create_task(self.runner.run_once())
        await validation_started.wait()
        cancelling = await self.runner.cancel(self.series_id)
        self.assertEqual(cancelling["status"], "cancelling")
        release_validation.set()
        self.assertTrue(await processing)
        completed_cancel = self.series.get(self.series_id)
        self.assertEqual(completed_cancel["status"], "cancelled")
        self.assertIsNone(completed_cancel["document"]["active_shot"])
        self.assertFalse(completed_cancel["document"]["cancel_requested"])
        shot = completed_cancel["document"]["shots"][0]
        self.assertEqual(shot["status"], "completed")
        self.assertEqual(shot["accepted_attempt"], 1)
        self.assertEqual(len(shot["attempts"][0]["artifact_ids"]), 1)
        self.assertEqual(self.jobs.status(job_id), "completed")

    async def test_reference_map_includes_audio_and_video_soundtrack_labels(self) -> None:
        assets = {
            "video": UploadedAsset("video", "h3/video.mp4", "video.mp4"),
            "video-audible": UploadedAsset(
                "video",
                "h3/video-audible.mp4",
                "video-audible.mp4",
                {"has_audio": True},
            ),
            "sound": UploadedAsset("audio", "h3/sound.wav", "sound.wav"),
            "voice": UploadedAsset("audio", "h3/voice.wav", "voice.wav"),
        }

        def resolve(token, kind, optional):
            if token is None and optional:
                return None
            asset = assets[token]
            self.assertEqual(asset.kind, kind)
            return asset

        payload = {
            "title": "Audio map",
            "brief": "Keep the rainy blue-hour mood.",
            "template": "movie",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1024,
                "height": 768,
                "continuity_seconds": 3,
            },
            "references": {
                "images": [],
                "videos": [
                    {"token": "video", "label": "Opening", "soundtrack": "sound"},
                    {"token": "video-audible", "label": "Audible scene"},
                ],
                "audio": [{"token": "voice", "label": "Hero voice"}],
            },
            "shots": [
                {"title": "One", "prompt": "Opening.", "duration": 5, "seed": 1},
                {"title": "Two", "prompt": "Continue.", "duration": 5, "seed": 2},
            ],
        }
        document = build_series_document(payload, resolve)
        _, _, labels = self.runner._references_for_shot(document, 0)
        prompt = self.runner._series_prompt(document, 0, labels)
        self.assertIn("Target output: 1024x768; 5 seconds.", prompt)
        self.assertIn("Series note: Keep the rainy blue-hour mood.", prompt)
        self.assertIn("<Audio 1> = Opening soundtrack", prompt)
        self.assertIn("<Video 1> = Opening", prompt)
        self.assertIn("<Audio 2> = original audio from Audible scene", prompt)
        self.assertIn("<Video 2> = Audible scene", prompt)
        self.assertIn("<Audio 3> = Hero voice", prompt)
        document["shots"][0]["continuity_input"] = {
            "video_path": "h3/tail.mp4",
            "video_name": "tail.mp4",
            "image_path": "h3/frame.png",
            "image_name": "frame.png",
        }
        _, _, next_labels = self.runner._references_for_shot(document, 1)
        next_prompt = self.runner._series_prompt(document, 1, next_labels)
        self.assertIn("<Audio 1> = Opening soundtrack", next_prompt)
        self.assertIn(
            "<Audio 3> = stereo audio from the previous shot continuity tail",
            next_prompt,
        )
        self.assertIn("<Video 3> = previous shot's final 3 seconds", next_prompt)
        self.assertIn("<Audio 4> = Hero voice", next_prompt)

    async def test_rejects_prompt_tag_without_matching_reference(self) -> None:
        payload = {
            "title": "Broken reference map",
            "template": "movie",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1024,
                "height": 768,
                "continuity_seconds": 0,
            },
            "references": {"images": [], "videos": [], "audio": []},
            "shots": [
                {"title": "One", "prompt": "Follow <Picture 1>.", "duration": 5, "seed": 1},
                {"title": "Two", "prompt": "Continue without it.", "duration": 5, "seed": 2},
            ],
        }
        with self.assertRaisesRegex(RequestError, "without a matching reference"):
            build_series_document(payload, lambda token, kind, optional: None)

    async def test_rejects_audio_tag_that_changes_meaning_after_continuity(self) -> None:
        voice = UploadedAsset("audio", "h3/voice.wav", "voice.wav")

        def resolve(token, kind, optional):
            if token is None and optional:
                return None
            self.assertEqual((token, kind), ("voice", "audio"))
            return voice

        payload = {
            "title": "Stable voice tag",
            "template": "movie",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1024,
                "height": 768,
                "continuity_seconds": 3,
            },
            "references": {
                "images": [],
                "videos": [],
                "audio": [{"token": "voice", "label": "Hero voice"}],
            },
            "shots": [
                {"title": "One", "prompt": "Use <Audio 1>.", "duration": 5, "seed": 1},
                {"title": "Two", "prompt": "Keep <Audio 1>.", "duration": 5, "seed": 2},
            ],
        }
        with self.assertRaisesRegex(RequestError, "changes meaning between shots"):
            build_series_document(payload, resolve)

    async def test_regenerate_from_here_preserves_attempts(self) -> None:
        # Build a completed-looking chain without running media; retry must only
        # change acceptance/status and retain all old attempts and artifacts.
        def completed(document, _):
            for index, shot in enumerate(document["shots"]):
                shot["status"] = "completed"
                shot["accepted_attempt"] = 1
                shot["attempts"] = [
                    {
                        "number": 1,
                        "job_id": str(uuid.uuid4()),
                        "status": "completed",
                        "error": None,
                        "artifact_ids": [],
                    }
                ]
            document["artifacts"] = [
                {
                    "id": str(uuid.uuid4()),
                    "kind": "final",
                    "storage": "series",
                    "relative": "old.mp4",
                }
            ]
            return document, "completed"

        self.series.mutate(self.series_id, completed)
        retried = await self.runner.retry(
            self.series_id, 0, regenerate_following=True
        )
        self.assertEqual(retried["status"], "queued")
        self.assertEqual([shot["status"] for shot in retried["document"]["shots"]], ["pending", "pending"])
        self.assertEqual(len(retried["document"]["shots"][0]["attempts"]), 1)
        self.assertTrue(retried["document"]["shots"][0]["attempts"][0]["superseded"])
        self.assertTrue(retried["document"]["artifacts"][0]["superseded"])

    async def test_retry_cannot_reorder_continuity_during_next_shot_preclaim(self) -> None:
        def between_shots(document, _):
            first = document["shots"][0]
            first["status"] = "completed"
            first["accepted_attempt"] = 1
            first["attempts"] = [
                {
                    "number": 1,
                    "job_id": str(uuid.uuid4()),
                    "status": "completed",
                    "error": None,
                    "artifact_ids": [],
                    "reference_map": [],
                }
            ]
            first["continuity_input"] = {
                "video_path": "h3/tail.mp4",
                "video_name": "tail.mp4",
                "image_path": "h3/frame.png",
                "image_name": "frame.png",
            }
            return document, "running"

        self.series.mutate(self.series_id, between_shots)
        health_started = asyncio.Event()
        release_health = asyncio.Event()
        original_health = self.client.health

        async def blocked_health(*, inspect_nodes=False):
            health_started.set()
            await release_health.wait()
            return await original_health(inspect_nodes=inspect_nodes)

        self.client.health = blocked_health
        next_submission = asyncio.create_task(self.runner.run_once())
        await health_started.wait()
        with self.assertRaisesRegex(RequestError, "paused or stopped"):
            await self.runner.retry(
                self.series_id, 0, regenerate_following=True
            )
        release_health.set()
        self.assertTrue(await next_submission)
        self.assertEqual(len(self.client.submissions), 1)
        self.assertEqual(self.client.submissions[0]["metadata"]["shot_index"], 1)

    async def test_failed_validation_still_exposes_the_precious_render_output(self) -> None:
        await self.runner.run_once()
        job_id = self.client.submissions[0]["prompt_id"]
        self.jobs.update(job_id, "completed", outputs=[self.output_locator()])
        self.media.validate_video = AsyncMock(side_effect=RuntimeError("decode failed"))
        self.assertTrue(await self.runner.run_once())
        failed = self.series.get(self.series_id)
        self.assertEqual(failed["status"], "failed")
        public = public_series(failed, self.client)
        self.assertEqual(len(public["shots"][0]["attempts"][0]["outputs"]), 1)
        self.assertEqual(
            public["shots"][0]["attempts"][0]["outputs"][0]["metadata"]["validation"],
            "failed",
        )

    async def test_retry_resumes_postprocessing_without_another_gpu_submission(self) -> None:
        await self.runner.run_once()
        job_id = self.client.submissions[0]["prompt_id"]
        self.jobs.update(job_id, "completed", outputs=[self.output_locator()])
        original_make = self.media.make_continuity
        self.media.make_continuity = AsyncMock(side_effect=RuntimeError("temporary handoff failure"))
        await self.runner.run_once()
        failed = self.series.get(self.series_id)
        artifact = failed["document"]["artifacts"][0]
        self.assertEqual(artifact["metadata"]["validation"], "passed")
        self.assertEqual(len(self.client.submissions), 1)
        self.assertEqual(len(failed["document"]["shots"][0]["attempts"]), 1)

        self.media.make_continuity = original_make
        resumed = await self.runner.retry(
            self.series_id, 0, regenerate_following=False
        )
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["document"]["active_shot"], 0)
        await self.runner.run_once()
        recovered = self.series.get(self.series_id)
        self.assertEqual(recovered["document"]["shots"][0]["status"], "completed")
        self.assertEqual(len(recovered["document"]["shots"][0]["attempts"]), 1)
        self.assertEqual(len(self.client.submissions), 1)

    async def test_retry_finalization_reuses_all_accepted_mp4s(self) -> None:
        def accepted(document, _):
            for shot in document["shots"]:
                artifact_id = str(uuid.uuid4())
                job_id = str(uuid.uuid4())
                shot["status"] = "completed"
                shot["accepted_attempt"] = 1
                shot["attempts"] = [
                    {
                        "number": 1,
                        "job_id": job_id,
                        "status": "completed",
                        "error": None,
                        "artifact_ids": [artifact_id],
                        "reference_map": [],
                    }
                ]
                document["artifacts"].append(
                    {
                        "id": artifact_id,
                        "kind": "shot",
                        "label": "accepted",
                        "storage": "output",
                        "locator": self.output_locator(),
                        "mime": "video/mp4",
                        "download_name": "shot.mp4",
                        "metadata": {
                            "frames": 124,
                            "duration": 124 / 24,
                            "sha256": "a" * 64,
                            "validation": "passed",
                        },
                    }
                )
            return document, "running"

        self.series.mutate(self.series_id, accepted)
        original_stitch = self.media.stitch
        self.media.stitch = AsyncMock(side_effect=RuntimeError("temporary concat failure"))
        await self.runner.run_once()
        failed = self.series.get(self.series_id)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(all(shot["status"] == "completed" for shot in failed["document"]["shots"]))

        self.media.stitch = original_stitch
        queued = await self.runner.retry_finalization(self.series_id)
        self.assertEqual(queued["status"], "stitching")
        await self.runner.run_once()
        completed = self.series.get(self.series_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(self.client.submissions), 0)

    async def test_retry_cannot_replace_state_during_active_stitch(self) -> None:
        def accepted(document, _):
            for shot in document["shots"]:
                artifact_id = str(uuid.uuid4())
                shot["status"] = "completed"
                shot["accepted_attempt"] = 1
                shot["attempts"] = [
                    {
                        "number": 1,
                        "job_id": str(uuid.uuid4()),
                        "status": "completed",
                        "error": None,
                        "artifact_ids": [artifact_id],
                        "reference_map": [],
                    }
                ]
                document["artifacts"].append(
                    {
                        "id": artifact_id,
                        "kind": "shot",
                        "storage": "output",
                        "locator": self.output_locator(),
                        "metadata": {
                            "frames": 124,
                            "duration": 124 / 24,
                            "sha256": "b" * 64,
                            "validation": "passed",
                        },
                    }
                )
            return document, "running"

        self.series.mutate(self.series_id, accepted)
        stitch_started = asyncio.Event()
        release_stitch = asyncio.Event()
        original_stitch = self.media.stitch

        async def blocked_stitch(*args, **kwargs):
            stitch_started.set()
            await release_stitch.wait()
            return await original_stitch(*args, **kwargs)

        self.media.stitch = blocked_stitch
        stitching = asyncio.create_task(self.runner.run_once())
        await stitch_started.wait()
        with self.assertRaisesRegex(RequestError, "paused or stopped"):
            await self.runner.retry(
                self.series_id, 0, regenerate_following=True
            )
        release_stitch.set()
        self.assertTrue(await stitching)
        finished = self.series.get(self.series_id)
        self.assertEqual(finished["status"], "completed")
        self.assertTrue(all(shot["status"] == "completed" for shot in finished["document"]["shots"]))


if __name__ == "__main__":
    unittest.main()
