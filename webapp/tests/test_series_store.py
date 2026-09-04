from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from webapp.series_runner import build_series_document
from webapp.series_store import SeriesStore, SeriesStoreValidationError
from webapp.workflows import RequestError, UploadedAsset


class SeriesStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.private = Path(self.temporary.name) / "private"
        self.private.mkdir(mode=0o700)
        self.path = self.private / "series.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_mutate_and_reopen_are_durable(self) -> None:
        series_id = str(uuid.uuid4())
        store = SeriesStore(self.path)
        created = store.create(series_id, {"title": "Episode", "shots": []})
        self.assertEqual(created["status"], "ready")
        updated = store.mutate(
            series_id,
            lambda document, _: ({**document, "title": "Episode two"}, "queued"),
        )
        self.assertEqual(updated["revision"], 2)
        reopened = SeriesStore(self.path).get(series_id)
        self.assertEqual(reopened["document"]["title"], "Episode two")
        self.assertEqual(reopened["status"], "queued")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_identifiers_and_finite_json_are_strict(self) -> None:
        store = SeriesStore(self.path)
        with self.assertRaises(SeriesStoreValidationError):
            store.create("not-a-uuid", {})
        with self.assertRaises(SeriesStoreValidationError):
            store.create(str(uuid.uuid4()), {"bad": float("nan")})


class SeriesPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assets = {
            "image-token": UploadedAsset("image", "h3-webapp/image/cast.png", "cast.png"),
            "video-token": UploadedAsset(
                "video",
                "h3-webapp/video/opening.mp4",
                "opening.mp4",
                {"width": 576, "height": 1024, "has_audio": False},
            ),
            "audio-token": UploadedAsset("audio", "h3-webapp/audio/voice.flac", "voice.flac"),
        }

    def resolve(self, token, kind, optional):
        if token in {None, ""} and optional:
            return None
        asset = self.assets.get(token)
        if asset is None or asset.kind != kind:
            raise RequestError(f"invalid {kind} token")
        return asset

    def payload(self):
        return {
            "title": "Forest signal",
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
                "images": [{"token": "image-token", "label": "Rara Xia"}],
                "videos": [
                    {
                        "token": "video-token",
                        "label": "Previous episode",
                        "soundtrack": "audio-token",
                    }
                ],
                "audio": [],
            },
            "shots": [
                {"title": "Signal", "prompt": "Find the signal.", "duration": 5, "seed": 1},
                {"title": "Rescue", "prompt": "Lift the beam.", "duration": 5, "seed": 2},
            ],
        }

    def test_tokens_resolve_to_trusted_durable_assets(self) -> None:
        document = build_series_document(self.payload(), self.resolve)
        image = document["references"]["images"][0]
        self.assertEqual(image["path"], "h3-webapp/image/cast.png")
        self.assertEqual(image["label"], "Rara Xia")
        self.assertNotIn("token", image)
        self.assertEqual(
            document["references"]["videos"][0]["soundtrack"]["path"],
            "h3-webapp/audio/voice.flac",
        )
        self.assertEqual(len(document["shots"]), 2)
        self.assertAlmostEqual(document["shots"][0]["actual_duration"], 5.1666666667)

    def test_shot_and_reference_limits_are_enforced(self) -> None:
        payload = self.payload()
        payload["shots"] = payload["shots"][:1]
        with self.assertRaisesRegex(RequestError, "between 2 and 12"):
            build_series_document(payload, self.resolve)
        payload = self.payload()
        payload["settings"]["continuity_seconds"] = 1
        with self.assertRaisesRegex(RequestError, "0, 2, 3, or 4"):
            build_series_document(payload, self.resolve)

    def test_composed_prompt_limit_is_checked_before_series_is_saved(self) -> None:
        payload = self.payload()
        payload["brief"] = "b" * 2_000
        payload["shots"][1]["prompt"] = "p" * 10_000
        with self.assertRaisesRegex(RequestError, "composed prompt for Shot 2"):
            build_series_document(payload, self.resolve)
        payload["shots"][1]["prompt"] = "p" * 7_000
        document = build_series_document(payload, self.resolve)
        self.assertEqual(len(document["shots"][1]["prompt"]), 7_000)

    def test_lalachan_requires_the_seven_canonical_picture_slots(self) -> None:
        payload = self.payload()
        payload["template"] = "lalachan"
        with self.assertRaisesRegex(RequestError, "first seven pictures"):
            build_series_document(payload, self.resolve)

        payload["references"]["images"] = [
            {"token": "image-token", "label": label}
            for label in (
                "Words card",
                "Zhuangzi Robot",
                "LightMind glasses",
                "Patchwork notebook",
                "Rara Xia",
                "Aya Chan",
                "Sasa Kun",
            )
        ]
        document = build_series_document(payload, self.resolve)
        self.assertEqual(len(document["references"]["images"]), 7)

    def test_nonfinal_shot_must_cover_continuity_handoff(self) -> None:
        payload = self.payload()
        payload["settings"]["profile"] = "preview_int8_turbo_dual"
        payload["settings"]["continuity_seconds"] = 4
        payload["shots"][0]["duration"] = 2
        with self.assertRaisesRegex(RequestError, "Shot 1 is too short"):
            build_series_document(payload, self.resolve)

        payload["shots"][0]["duration"] = 5
        payload["shots"][1]["duration"] = 2
        document = build_series_document(payload, self.resolve)
        self.assertLess(document["shots"][1]["actual_duration"], 4)


if __name__ == "__main__":
    unittest.main()
