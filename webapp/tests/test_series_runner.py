from __future__ import annotations

import asyncio
import copy
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from webapp.comfy_client import ComfyError
from webapp.job_store import JobStore
from webapp.series_media import SeriesMediaError
from webapp.series_runner import (
    COMPLETED_OUTPUT_REFRESH_ATTEMPTS,
    LALACHAN_REFERENCE_LABELS,
    SeriesRunner,
    build_series_document,
    public_series,
)
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
                "metadata": {"duration": seconds, "sha256": "b" * 64},
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "final_frame",
                "label": "frame",
                "storage": "series",
                "relative": frame.name,
                "mime": "image/png",
                "download_name": frame.name,
                "metadata": {"sha256": "c" * 64},
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

    @staticmethod
    def world_document(input_root: Path | None = None):
        assets = {}
        labels = [*LALACHAN_REFERENCE_LABELS, "Rome Colosseum", "Florence Duomo"]
        for index, label in enumerate(labels, start=1):
            relative = f"travel/reference-{index}.png"
            content = f"trusted-{label}".encode()
            digest = hashlib.sha256(content).hexdigest()
            if input_root is not None:
                target = input_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            assets[label] = UploadedAsset(
                "image",
                relative,
                f"reference-{index}.png",
                {"sha256": digest},
            )

        def resolve(token, kind, optional):
            if token is None and optional:
                return None
            asset = assets[token]
            if asset.kind != kind:
                raise AssertionError(f"expected {kind}, got {asset.kind}")
            return asset

        payload = {
            "title": "LALACHAN World Travel · Italy",
            "brief": "Follow one northbound route through Italy; earlier episodes are voice reference only.",
            "template": "world_travel",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1024,
                "height": 768,
                "continuity_seconds": 3,
                "advance": True,
            },
            "references": {
                "images": [{"token": label, "label": label} for label in LALACHAN_REFERENCE_LABELS],
                "videos": [],
                "audio": [],
            },
            "shots": [
                {
                    "title": "Rome",
                    "prompt": "At <Picture 8>, discover a visible layer of Roman history.",
                    "duration": 5,
                    "seed": 101,
                    "scene_reference": {
                        "token": "Rome Colosseum",
                        "label": "Rome · Colosseum exterior",
                    },
                },
                {
                    "title": "Florence",
                    "prompt": "Continue north to <Picture 8>; do not copy the Rome composition.",
                    "duration": 5,
                    "seed": 102,
                    "scene_reference": {
                        "token": "Florence Duomo",
                        "label": "Florence · Duomo street view",
                    },
                },
            ],
        }
        return build_series_document(payload, resolve), payload, resolve

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

    async def test_world_travel_uses_per_shot_p8_then_continuity_p9(self) -> None:
        input_root = Path(self.temporary.name) / "input"
        document, _, _ = self.world_document(input_root)
        first_assets, first_mode, first_labels = self.runner._references_for_shot(document, 0)
        self.assertEqual(first_mode, "r2v")
        self.assertEqual(
            [asset.original_name for asset in first_assets["ref_images"]],
            [f"reference-{index}.png" for index in range(1, 9)],
        )
        self.assertEqual(
            first_labels[:7],
            [f"<Picture {index}> = {label}" for index, label in enumerate(LALACHAN_REFERENCE_LABELS, start=1)],
        )
        self.assertEqual(first_labels[7], "<Picture 8> = Rome · Colosseum exterior")
        self.assertFalse(any("<Picture 9>" in label for label in first_labels))

        document["shots"][0]["continuity_input"] = {
            "video_path": "h3/tail.mp4",
            "video_name": "tail.mp4",
            "video_sha256": "b" * 64,
            "image_path": "h3/frame.png",
            "image_name": "frame.png",
            "image_sha256": "c" * 64,
        }
        second_assets, _, second_labels = self.runner._references_for_shot(document, 1)
        self.assertEqual(second_assets["ref_images"][-2].original_name, "reference-9.png")
        self.assertEqual(second_assets["ref_images"][-1].original_name, "frame.png")
        self.assertIn("<Picture 8> = Florence · Duomo street view", second_labels)
        self.assertIn("<Picture 9> = previous shot's exact final frame", second_labels)
        prompt = self.runner._series_prompt(document, 1, second_labels)
        for required in (
            "lock every named character's identity",
            "wardrobe",
            "voice across the whole journey",
            "travel route, geography, screen direction",
            "Picture 8 is the location",
            "for this shot only",
            "never copy its country, plot, story direction",
            "Continue directly and seamlessly",
        ):
            self.assertIn(required, prompt)

    async def test_later_world_travel_shot_omits_opening_props_and_remaps_tags(
        self,
    ) -> None:
        input_root = Path(self.temporary.name) / "input"
        document, payload, resolve = self.world_document(input_root)
        payload["brief"] += (
            " The physical words card appears in the notebook only during the opening."
        )
        payload["shots"][1]["prompt"] += (
            " Exactly four friends remain; card and notebook stay off-camera. "
            "No subtitles, card, notebook, duplicate cast, or montage."
        )
        payload["shots"][1]["omit_shared_image_labels"] = [
            "Words card",
            "LightMind glasses",
            "Patchwork notebook",
        ]
        document = build_series_document(payload, resolve)
        document["shots"][0]["continuity_input"] = {
            "video_path": "h3/tail.mp4",
            "video_name": "tail.mp4",
            "video_sha256": "b" * 64,
            "image_path": "h3/frame.png",
            "image_name": "frame.png",
            "image_sha256": "c" * 64,
        }

        assets, _, labels = self.runner._references_for_shot(document, 1)

        self.assertEqual(
            [asset.original_name for asset in assets["ref_images"]],
            [
                "reference-2.png",
                "reference-5.png",
                "reference-6.png",
                "reference-7.png",
                "reference-9.png",
                "frame.png",
            ],
        )
        self.assertEqual(
            labels[:6],
            [
                "<Picture 1> = Zhuangzi Robot",
                "<Picture 2> = Rara Xia",
                "<Picture 3> = Aya Chan",
                "<Picture 4> = Sasa Kun",
                "<Picture 5> = Florence · Duomo street view",
                "<Picture 6> = previous shot's exact final frame",
            ],
        )
        prompt = self.runner._series_prompt(document, 1, labels)
        self.assertIn("Continue north to <Picture 5>", prompt)
        self.assertNotIn("<Picture 8>", prompt)
        for omitted in ("Words card", "LightMind glasses", "Patchwork notebook"):
            self.assertNotIn(omitted, prompt)
        for omitted_term in (r"\bwords card\b", r"\bcard\b", r"\bnotebook\b"):
            self.assertNotRegex(prompt.lower(), omitted_term)
        self.assertIn("Exactly four friends remain", prompt)
        self.assertIn("No subtitles, duplicate cast, or montage", prompt)
        self.assertIn("within the first second", prompt)
        self.assertIn("shot-specific location picture", prompt)

    async def test_opening_prop_scrub_preserves_negative_grammar_and_story(self) -> None:
        input_root = Path(self.temporary.name) / "input"
        _, payload, resolve = self.world_document(input_root)
        payload["brief"] = "Match the route and let the friends raise glasses together."
        payload["shots"][1]["prompt"] = (
            "Match the Deosai meadow, and keep the notebook off-camera. "
            "No card, notebook, subtitles, duplicate cast, or montage. "
            "Do not show the card, labels, or extra people."
        )
        payload["shots"][1]["omit_shared_image_labels"] = [
            "Words card",
            "LightMind glasses",
            "Patchwork notebook",
        ]
        document = build_series_document(payload, resolve)
        document["shots"][0]["continuity_input"] = {
            "video_path": "h3/tail.mp4",
            "video_name": "tail.mp4",
            "video_sha256": "b" * 64,
            "image_path": "h3/frame.png",
            "image_name": "frame.png",
            "image_sha256": "c" * 64,
        }
        _, _, labels = self.runner._references_for_shot(document, 1)

        prompt = self.runner._series_prompt(document, 1, labels)

        self.assertIn("Match the Deosai meadow", prompt)
        self.assertIn("raise glasses together", prompt)
        self.assertIn("No subtitles, duplicate cast, or montage", prompt)
        self.assertIn("Do not show labels, or extra people", prompt)
        self.assertNotRegex(prompt.lower(), r"\b(?:card|notebook)\b")

    async def test_reference_policy_preserves_attempt_history_and_cast_refs(
        self,
    ) -> None:
        document, _, _ = self.world_document()
        document["shots"][1]["attempts"] = [
            {
                "number": 1,
                "job_id": str(uuid.uuid4()),
                "status": "completed",
                "error": None,
                "artifact_ids": [str(uuid.uuid4())],
                "reference_map": ["old preserved map"],
            }
        ]
        attempts_before = copy.deepcopy(document["shots"][1]["attempts"])
        series_id = str(uuid.uuid4())
        self.series.create(series_id, document, status="paused")

        updated = await self.runner.set_shot_reference_policy(
            series_id,
            1,
            omit_shared_image_labels=[
                "Words card",
                "LightMind glasses",
                "Patchwork notebook",
            ],
        )

        shot = updated["document"]["shots"][1]
        self.assertEqual(shot["attempts"], attempts_before)
        self.assertEqual(
            shot["omit_shared_image_labels"],
            ["Words card", "LightMind glasses", "Patchwork notebook"],
        )
        with self.assertRaisesRegex(RequestError, "persistent cast"):
            await self.runner.set_shot_reference_policy(
                series_id,
                1,
                omit_shared_image_labels=["Aya Chan"],
            )
        with self.assertRaisesRegex(RequestError, "Shot 1 must keep"):
            await self.runner.set_shot_reference_policy(
                series_id,
                0,
                omit_shared_image_labels=["Words card"],
            )

    async def test_reference_policy_rejects_omitting_an_authored_picture_tag(
        self,
    ) -> None:
        _, payload, resolve = self.world_document()
        payload["shots"][1]["prompt"] += " Show <Picture 1>."
        payload["shots"][1]["omit_shared_image_labels"] = ["Words card"]
        with self.assertRaisesRegex(RequestError, "without a matching reference"):
            build_series_document(payload, resolve)

    async def test_composed_prompt_keeps_ui_titles_out_of_generation_text(self) -> None:
        document, payload, resolve = self.world_document()
        document["title"] = "SPEAK_THIS_SERIES_TITLE"
        document["shots"][0]["title"] = "SPEAK_THIS_SHOT_TITLE"
        _, _, labels = self.runner._references_for_shot(document, 0)

        prompt = self.runner._series_prompt(document, 0, labels)

        self.assertNotIn("SPEAK_THIS_SERIES_TITLE", prompt)
        self.assertNotIn("SPEAK_THIS_SHOT_TITLE", prompt)
        self.assertIn("never speak or display a series title, shot title", prompt)
        self.assertIn("Spoken content is limited to dialogue explicitly quoted", prompt)

        payload["title"] = "SPEAK_THIS_SERIES_TITLE"
        payload["shots"][0]["title"] = "SPEAK_THIS_SHOT_TITLE"
        payload["shots"][0]["scene_reference"].pop("label")
        fallback_document = build_series_document(payload, resolve)
        _, _, fallback_labels = self.runner._references_for_shot(fallback_document, 0)
        fallback_prompt = self.runner._series_prompt(
            fallback_document, 0, fallback_labels
        )
        self.assertIn("<Picture 8> = Shot 1 location reference", fallback_prompt)
        self.assertNotIn("SPEAK_THIS_SERIES_TITLE", fallback_prompt)
        self.assertNotIn("SPEAK_THIS_SHOT_TITLE", fallback_prompt)

    async def test_successor_requires_complete_hashed_continuity_before_p9(self) -> None:
        document, _, _ = self.world_document()
        valid = {
            "video_path": "h3/tail.mp4",
            "video_name": "tail.mp4",
            "video_sha256": "b" * 64,
            "image_path": "h3/frame.png",
            "image_name": "frame.png",
            "image_sha256": "c" * 64,
        }
        for missing in (
            "video_path",
            "video_sha256",
            "image_path",
            "image_sha256",
        ):
            candidate = copy.deepcopy(document)
            candidate["shots"][0]["continuity_input"] = dict(valid)
            candidate["shots"][0]["continuity_input"].pop(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(
                SeriesMediaError, "incomplete or unverified"
            ):
                self.runner._references_for_shot(candidate, 1)

        document["shots"][0]["continuity_input"] = {
            **valid,
            "image_sha256": "not-a-sha256",
        }
        with self.assertRaisesRegex(SeriesMediaError, "incomplete or unverified"):
            self.runner._references_for_shot(document, 1)

        document["shots"][0]["continuity_input"] = {
            **valid,
            "image_path": "../escaped-frame.png",
        }
        with self.assertRaisesRegex(SeriesMediaError, "incomplete or unverified"):
            self.runner._references_for_shot(document, 1)

    async def test_malformed_recovered_continuity_fails_before_gpu_submission(self) -> None:
        def recover_with_bad_handoff(document, _):
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
                "video_sha256": "b" * 64,
                "image_path": "h3/frame.png",
                "image_name": "frame.png",
                # Simulate an older/corrupt durable record with no image hash.
            }
            return document, "running"

        self.series.mutate(self.series_id, recover_with_bad_handoff)
        self.assertTrue(await self.runner.run_once())
        failed = self.series.get(self.series_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["document"]["shots"][1]["status"], "failed")
        self.assertEqual(failed["document"]["shots"][1]["attempts"], [])
        self.assertEqual(self.client.submissions, [])
        self.assertIn("before GPU submission", failed["document"]["error"])

    async def test_world_travel_scene_is_private_persistent_and_hash_guarded(
        self,
    ) -> None:
        input_root = Path(self.temporary.name) / "input"
        document, _, _ = self.world_document(input_root)
        series_id = str(uuid.uuid4())
        created = self.series.create(series_id, document, status="queued")
        reopened = SeriesStore(self.series.path).get(series_id)
        scene_private = reopened["document"]["shots"][0]["scene_reference"]
        self.assertEqual(scene_private["path"], "travel/reference-8.png")
        self.assertRegex(scene_private["sha256"], r"^[0-9a-f]{64}$")
        public = public_series(created, self.client)
        self.assertEqual(
            public["shots"][0]["scene_reference"],
            {
                "kind": "image",
                "name": "reference-8.png",
                "label": "Rome · Colosseum exterior",
            },
        )
        serialized = str(public["shots"][0]["scene_reference"])
        self.assertNotIn("travel/", serialized)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn("token", serialized)

        guarded = SeriesRunner(
            self.series,
            self.jobs,
            self.client,
            self.media,
            input_root=input_root,
        )
        fingerprints = await guarded._verify_reference_integrity(document, 0)
        self.assertEqual(len(fingerprints), 8)
        self.assertEqual(fingerprints[-1]["label"], "Rome · Colosseum exterior")
        (input_root / "travel/reference-8.png").write_bytes(b"changed scene")
        with self.assertRaisesRegex(SeriesMediaError, "Rome · Colosseum exterior.*changed after upload"):
            await guarded._verify_reference_integrity(document, 0)

    async def test_world_travel_requires_seven_canonical_images_and_each_scene(
        self,
    ) -> None:
        _, payload, resolve = self.world_document()
        payload["shots"][0].pop("scene_reference")
        with self.assertRaisesRegex(RequestError, "Shot 1 scene_reference"):
            build_series_document(payload, resolve)

        _, payload, resolve = self.world_document()
        payload["references"]["images"].append({"token": "Rome Colosseum", "label": "Unwanted shared location"})
        with self.assertRaisesRegex(RequestError, "requires exactly seven pictures"):
            build_series_document(payload, resolve)

    async def test_world_travel_submission_keeps_paths_private_from_job_metadata(
        self,
    ) -> None:
        input_root = Path(self.temporary.name) / "input"
        document, _, _ = self.world_document(input_root)
        series_id = str(uuid.uuid4())
        record = self.series.create(series_id, document, status="queued")
        runner = SeriesRunner(
            self.series,
            self.jobs,
            self.client,
            self.media,
            input_root=input_root,
        )
        self.assertTrue(
            await runner._submit_shot(
                series_id,
                document,
                0,
                expected_revision=int(record["revision"]),
            )
        )
        submission = self.client.submissions[-1]
        image_paths = [
            node["inputs"]["image"] for node in submission["prompt"].values() if node["class_type"] == "LoadImage"
        ]
        self.assertEqual(image_paths, [f"travel/reference-{index}.png" for index in range(1, 9)])
        fingerprints = submission["metadata"]["reference_fingerprints"]
        self.assertEqual(fingerprints[-1]["label"], "Rome · Colosseum exterior")
        self.assertTrue(all("path" not in item for item in fingerprints))
        durable = SeriesStore(self.series.path).get(series_id)
        attempt = durable["document"]["shots"][0]["attempts"][0]
        self.assertEqual(
            attempt["reference_map"][7],
            "<Picture 8> = Rome · Colosseum exterior",
        )

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

    async def test_completed_job_boundedly_refreshes_delayed_output_without_resubmitting(
        self,
    ) -> None:
        self.assertTrue(await self.runner.run_once())
        job_id = self.client.submissions[0]["prompt_id"]
        self.jobs.update(job_id, "completed", outputs=[])
        self.client.get_job = AsyncMock(
            side_effect=[
                {"id": job_id, "status": "completed", "outputs": {}},
                {
                    "id": job_id,
                    "status": "completed",
                    "outputs": {"42": {"videos": [self.output_locator()]}},
                },
            ]
        )

        self.assertTrue(await self.runner.run_once())
        recovered = self.series.get(self.series_id)
        shot = recovered["document"]["shots"][0]
        self.assertEqual(shot["status"], "completed")
        self.assertEqual(shot["accepted_attempt"], 1)
        self.assertEqual(len(shot["attempts"]), 1)
        self.assertEqual(len(shot["attempts"][0]["artifact_ids"]), 1)
        self.assertEqual(self.jobs.get(job_id)["outputs"], [self.output_locator()])
        self.assertEqual(self.client.get_job.await_count, 2)
        self.assertEqual(len(self.client.submissions), 1)

    async def test_stale_completed_refresh_cannot_erase_concurrent_output(self) -> None:
        self.assertTrue(await self.runner.run_once())
        job_id = self.client.submissions[0]["prompt_id"]
        self.jobs.update(job_id, "completed", outputs=[])
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        async def stale_completed_without_output(requested_job_id):
            self.assertEqual(requested_job_id, job_id)
            refresh_started.set()
            await release_refresh.wait()
            return {"id": job_id, "status": "completed", "outputs": {}}

        self.client.get_job = AsyncMock(side_effect=stale_completed_without_output)
        accepting = asyncio.create_task(self.runner.run_once())
        await refresh_started.wait()
        self.jobs.update(job_id, outputs=[self.output_locator()])
        release_refresh.set()

        self.assertTrue(await accepting)
        recovered = self.series.get(self.series_id)
        self.assertEqual(recovered["document"]["shots"][0]["status"], "completed")
        self.assertEqual(self.jobs.get(job_id)["outputs"], [self.output_locator()])
        self.assertEqual(self.client.get_job.await_count, 1)
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
        self.assertIn(
            "Silent series context, never speak or display: Keep the rainy blue-hour mood.",
            prompt,
        )
        self.assertIn("<Audio 1> = Opening soundtrack", prompt)
        self.assertIn("<Video 1> = Opening", prompt)
        self.assertIn("<Audio 2> = original audio from Audible scene", prompt)
        self.assertIn("<Video 2> = Audible scene", prompt)
        self.assertIn("<Audio 3> = Hero voice", prompt)
        document["shots"][0]["continuity_input"] = {
            "video_path": "h3/tail.mp4",
            "video_name": "tail.mp4",
            "video_sha256": "b" * 64,
            "image_path": "h3/frame.png",
            "image_name": "frame.png",
            "image_sha256": "c" * 64,
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
                "video_sha256": "b" * 64,
                "image_path": "h3/frame.png",
                "image_name": "frame.png",
                "image_sha256": "c" * 64,
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

    async def test_retry_recovers_completed_job_output_before_artifact_attachment(
        self,
    ) -> None:
        self.assertTrue(await self.runner.run_once())
        job_id = self.client.submissions[0]["prompt_id"]
        self.jobs.update(job_id, "completed", outputs=[])
        self.client.get_job = AsyncMock(
            return_value={"id": job_id, "status": "completed", "outputs": {}}
        )

        self.assertTrue(await self.runner.run_once())
        failed = self.series.get(self.series_id)
        failed_shot = failed["document"]["shots"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed_shot["status"], "failed")
        self.assertEqual(failed_shot["attempts"][0]["artifact_ids"], [])
        self.assertEqual(
            self.client.get_job.await_count,
            COMPLETED_OUTPUT_REFRESH_ATTEMPTS,
        )
        self.assertEqual(len(self.client.submissions), 1)

        self.jobs.update(job_id, outputs=[self.output_locator()])
        self.client.get_job = AsyncMock(
            side_effect=AssertionError("stored terminal output must be reused")
        )
        resumed = await self.runner.retry(
            self.series_id, 0, regenerate_following=False
        )
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["document"]["active_shot"], 0)
        self.assertEqual(len(resumed["document"]["shots"][0]["attempts"]), 1)

        self.assertTrue(await self.runner.run_once())
        recovered = self.series.get(self.series_id)
        recovered_shot = recovered["document"]["shots"][0]
        self.assertEqual(recovered_shot["status"], "completed")
        self.assertEqual(recovered_shot["accepted_attempt"], 1)
        self.assertEqual(len(recovered_shot["attempts"]), 1)
        self.assertEqual(len(recovered_shot["attempts"][0]["artifact_ids"]), 1)
        self.client.get_job.assert_not_awaited()
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
