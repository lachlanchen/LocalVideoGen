from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts.localvideogen_series import (
    LocalVideoGenClient,
    SERIES_RECEIPT_SCHEMA,
    SeriesClientError,
    SeriesTransportError,
    WAIT_MODES,
    WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS,
    WORLD_TRAVEL_REFERENCE_LABELS,
    _install_temporary,
    build_parser,
    load_series_receipt,
    load_series_spec,
    main,
    normalize_base_url,
)


SERIES_ID = "11111111-1111-4111-8111-111111111111"
FINAL_ID = "22222222-2222-4222-8222-222222222222"
MANIFEST_ID = "33333333-3333-4333-8333-333333333333"
SHOT_ID = "44444444-4444-4444-8444-444444444444"
BAD_ID = "55555555-5555-4555-8555-555555555555"
ARTIFACT_BODIES = {
    FINAL_ID: b"precious-final-video",
    MANIFEST_ID: b'{"manifest":true}',
    SHOT_ID: b"preserved-shot-attempt",
    BAD_ID: b"tampered-download",
}


def maximum_quality_config() -> dict:
    return {
        "series_api_version": 1,
        "profiles": [
            {
                "id": "quality_bf16_dual",
                "precision": "bf16",
                "dual_gpu": True,
                "turbo": False,
                "steps_ref": 25,
            },
            {
                "id": "quality_int8_dual",
                "precision": "int8",
                "dual_gpu": True,
                "turbo": False,
                "steps_ref": 25,
            },
            {
                "id": "quality_int8_offload",
                "precision": "int8",
                "dual_gpu": False,
                "turbo": False,
                "steps_ref": 25,
            },
        ],
        "long_reference": {
            "profile": "quality_int8_offload",
            "ref_image_size": "match",
            "minimum_length": 243,
            "frame_pixel_limit": 510_000_000,
        },
        "uploads": {
            "video": {"normalized": {"max_edge": 1024, "max_pixels": 576 * 1024}}
        },
        "series": {
            "templates": ["lalachan", "movie", "world_travel"],
            "default_settings": {
                "profile": "quality_int8_offload",
                "width": 1248,
                "height": 704,
                "ref_image_size": "match",
            },
            "shot_reference_policy": {
                "field": "omit_shared_image_labels",
                "logical_picture_tags_remapped": True,
                "first_shot_must_keep_all": True,
            },
            "capabilities": {
                "world_travel": {
                    "template": "world_travel",
                    "render_mode": "r2v",
                    "maximum_quality_profile": "quality_bf16_dual",
                    "long_reference_safe_profile": "quality_int8_offload",
                    "picture_slots": {
                        "shared": [
                            {"slot": index, "label": label}
                            for index, label in enumerate(
                                WORLD_TRAVEL_REFERENCE_LABELS, start=1
                            )
                        ],
                        "scene": {
                            "slot": 8,
                            "kind": "image",
                            "scope": "shot",
                            "required": True,
                        },
                        "continuity_final_frame": {
                            "slot": 9,
                            "kind": "image",
                            "scope": "successor_shot",
                            "when_continuity_enabled": True,
                            "sha256_required": True,
                        },
                    },
                }
            },
        },
    }


def token_world_travel_spec(*, profile: str = "quality_int8_offload") -> dict:
    return {
        "title": "Italy",
        "template": "world_travel",
        "settings": {"profile": profile, "continuity_seconds": 3},
        "references": {
            "images": [
                {"token": f"token-{index}", "label": label}
                for index, label in enumerate(WORLD_TRAVEL_REFERENCE_LABELS)
            ],
            "videos": [],
            "audio": [],
        },
        "shots": [
            {
                "title": "Rome",
                "prompt": "Walk through Rome.",
                "duration": 10,
                "scene_reference": {"token": "scene-token", "label": "Rome"},
            }
        ],
    }


def artifact_record(artifact_id: str, kind: str, *, expected: bytes | None = None):
    body = ARTIFACT_BODIES[artifact_id] if expected is None else expected
    return {
        "id": artifact_id,
        "kind": kind,
        "download_url": f"/api/series/{SERIES_ID}/artifacts/{artifact_id}?download=1",
        "metadata": {
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
    }


class PreparationClient(LocalVideoGenClient):
    def __init__(self) -> None:
        super().__init__()
        self.uploaded: list[tuple[str, Path]] = []
        self.validated: list[dict[str, str]] = []
        self.config_result = maximum_quality_config()
        self.health_result = {
            "connected": True,
            "ready": True,
            "model_status": "verified",
        }
        self.config_checks = 0
        self.deep_health_checks = 0

    def config(self):
        self.config_checks += 1
        return copy.deepcopy(self.config_result)

    def health(self, *, deep=True):
        self.assert_deep = deep
        self.deep_health_checks += 1
        return copy.deepcopy(self.health_result)

    def _upload_file(self, kind: str, path: str | Path) -> dict[str, str]:
        source = Path(path).resolve()
        self.uploaded.append((kind, source))
        return {
            "kind": kind,
            "token": f"token-{kind}-{len(self.uploaded)}",
        }

    def validate_uploads(self, handles):
        self.validated = [dict(item) for item in handles]
        return [item["token"] for item in handles]


class RoutingClient(LocalVideoGenClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, object, object]] = []
        self.states: list[dict | Exception] = []
        self.config_result = maximum_quality_config()
        self.health_result = {
            "connected": True,
            "ready": True,
            "model_status": "verified",
        }

    def config(self):
        return copy.deepcopy(self.config_result)

    def health(self, *, deep=True):
        self.health_was_deep = deep
        return copy.deepcopy(self.health_result)

    def _request_json(self, method, path, payload=None, *, query=None, timeout=None):
        self.calls.append((method, path, payload, query))
        if method == "GET" and path == f"/api/series/{SERIES_ID}":
            state = self.states.pop(0)
            if isinstance(state, Exception):
                raise state
            return state
        return {"id": SERIES_ID, "status": "queued"}


class LoopbackHandler(BaseHTTPRequestHandler):
    upload_body = b""
    upload_origin = ""
    artifact_requests: list[str] = []
    health_requests = 0

    def log_message(self, _format, *args):
        return None

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).upload_body = self.rfile.read(length)
        type(self).upload_origin = self.headers.get("Origin", "")
        if self.path == "/api/uploads?kind=image":
            self._json(
                201,
                {
                    "token": "opaque-upload-token",
                    "kind": "image",
                    "name": "reference.png",
                },
            )
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/api/health?deep=1":
            type(self).health_requests += 1
            self._json(
                200,
                {"connected": True, "ready": True, "model_status": "verified"},
            )
            return
        if self.path == f"/api/series/{SERIES_ID}":
            self._json(
                200,
                {
                    "id": SERIES_ID,
                    "status": "completed",
                    "artifacts": [
                        artifact_record(FINAL_ID, "final"),
                        artifact_record(MANIFEST_ID, "manifest"),
                    ],
                    "final_artifact": artifact_record(FINAL_ID, "final"),
                    "shots": [
                        {
                            "attempts": [
                                {
                                    "outputs": [
                                        artifact_record(SHOT_ID, "shot"),
                                        artifact_record(
                                            BAD_ID,
                                            "shot",
                                            expected=b"trusted-download",
                                        ),
                                    ]
                                }
                            ],
                            "continuity": [],
                        }
                    ],
                },
            )
            return
        prefix = f"/api/series/{SERIES_ID}/artifacts/"
        if self.path.startswith(prefix):
            type(self).artifact_requests.append(self.path)
            artifact = self.path[len(prefix) :].split("?", 1)[0]
            body = ARTIFACT_BODIES.get(artifact)
            if body is None:
                self._json(404, {"error": "artifact not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "not found"})


class LocalVideoGenSeriesClientTests(unittest.TestCase):
    def test_base_url_cannot_escape_loopback_origin(self) -> None:
        self.assertEqual(
            normalize_base_url("http://localhost:8190/"),
            "http://localhost:8190",
        )
        self.assertEqual(normalize_base_url("http://[::1]:8190"), "http://[::1]:8190")
        for unsafe in (
            "https://127.0.0.1:8190",
            "http://example.com:8190",
            "http://127.0.0.1:8190/api",
            "http://user:secret@127.0.0.1:8190",
            "http://127.0.0.1:8190?next=evil",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(SeriesClientError):
                normalize_base_url(unsafe)
        with self.assertRaises(SeriesClientError):
            LocalVideoGenClient._target("//example.com/api/series")
        self.assertEqual(LocalVideoGenClient().upload_timeout, 600)
        with self.assertRaisesRegex(SeriesClientError, "upload timeout"):
            LocalVideoGenClient(upload_timeout=299)

    def test_spec_loader_rejects_non_finite_json_before_any_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bad.json"
            source.write_text('{"seed": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(SeriesClientError, "valid finite UTF-8 JSON"):
                load_series_spec(source)

    def test_preflight_enforces_v1_maximum_world_travel_contract(self) -> None:
        client = PreparationClient()
        report = client.preflight_series_spec(
            token_world_travel_spec(profile="quality_bf16_dual")
        )
        self.assertEqual(report["series_api_version"], 1)
        self.assertEqual(report["profile"], "quality_bf16_dual")
        self.assertIsNone(report["runtime"])
        self.assertEqual(client.config_checks, 1)
        self.assertEqual(client.deep_health_checks, 0)
        safe_report = client.preflight_series_spec(
            token_world_travel_spec(profile="quality_int8_offload")
        )
        self.assertEqual(safe_report["profile"], "quality_int8_offload")

        cases = (
            ("version", lambda config: config.update(series_api_version=2), "version"),
            (
                "profile shape",
                lambda config: config["profiles"][0].update(steps_ref=24),
                "25-step",
            ),
            (
                "P8",
                lambda config: config["series"]["capabilities"]["world_travel"][
                    "picture_slots"
                ]["scene"].update(slot=7),
                "P8",
            ),
            (
                "P1-P7",
                lambda config: config["series"]["capabilities"]["world_travel"][
                    "picture_slots"
                ]["shared"][0].update(label="Wrong card"),
                "P1-P7",
            ),
            (
                "P9",
                lambda config: config["series"]["capabilities"]["world_travel"][
                    "picture_slots"
                ]["continuity_final_frame"].update(slot=8),
                "P9",
            ),
            (
                "R2V",
                lambda config: config["series"]["capabilities"]["world_travel"].update(
                    render_mode="i2v"
                ),
                "R2V",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                rejected = PreparationClient()
                mutate(rejected.config_result)
                with self.assertRaisesRegex(SeriesClientError, message):
                    rejected.preflight_series_spec(token_world_travel_spec())
                self.assertEqual(rejected.uploaded, [])

        with self.assertRaisesRegex(SeriesClientError, "accepts quality_int8_offload"):
            client.preflight_series_spec(
                token_world_travel_spec(profile="quality_int8_dual")
            )
        with self.assertRaisesRegex(SeriesClientError, "not advertised"):
            client.preflight_series_spec(
                token_world_travel_spec(profile="missing-profile")
            )

    def test_every_template_requires_the_long_reference_server_contract(self) -> None:
        spec = {
            "title": "Movie",
            "template": "movie",
            "references": {"images": [], "videos": [], "audio": []},
            "shots": [
                {"title": "One", "prompt": "One.", "duration": 5},
                {"title": "Two", "prompt": "Two.", "duration": 5},
            ],
        }
        for field in ("long_reference", "uploads"):
            with self.subTest(field=field):
                client = PreparationClient()
                client.config_result.pop(field)
                with self.assertRaisesRegex(SeriesClientError, "stopped before upload"):
                    client.preflight_series_spec(spec)
                self.assertEqual(client.uploaded, [])
        client = PreparationClient()
        client.config_result["series"].pop("default_settings")
        with self.assertRaisesRegex(SeriesClientError, "stopped before upload"):
            client.preflight_series_spec(spec)

    def test_long_bf16_continuity_is_rejected_before_upload(self) -> None:
        spec = {
            "title": "Unsafe continuity",
            "template": "movie",
            "settings": {
                "profile": "quality_bf16_dual",
                "width": 1248,
                "height": 704,
                "ref_image_size": "max",
                "continuity_seconds": 3,
            },
            "references": {"images": [], "videos": [], "audio": []},
            "shots": [
                {"title": "One", "prompt": "One.", "duration": 10},
                {"title": "Two", "prompt": "Two.", "duration": 10},
            ],
        }
        client = PreparationClient()
        with self.assertRaisesRegex(SeriesClientError, "quality_int8_offload"):
            client.preflight_series_spec(spec)
        self.assertEqual(client.uploaded, [])

    def test_checked_world_travel_example_passes_client_safety_preflight(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "series-api"
            / "maximum-quality-world-travel.json"
        )
        spec = json.loads(source.read_text(encoding="utf-8"))
        for kind in ("images", "videos", "audio"):
            for index, item in enumerate(spec["references"][kind], start=1):
                item.pop("source", None)
                item["token"] = f"{kind}-{index}"
        for index, shot in enumerate(spec["shots"], start=1):
            shot["scene_reference"].pop("source", None)
            shot["scene_reference"]["token"] = f"scene-{index}"

        client = PreparationClient()
        report = client.preflight_series_spec(spec)

        self.assertEqual(report["profile"], "quality_int8_offload")
        self.assertEqual(len(report["effective_picture_layouts"]), 6)
        self.assertIsNone(report["runtime"])
        self.assertEqual(client.uploaded, [])

    def test_local_source_and_start_require_deep_ready_health(self) -> None:
        client = PreparationClient()
        local_spec = token_world_travel_spec()
        local_spec["references"]["images"][0] = {
            "source": "not-uploaded.png",
            "label": WORLD_TRAVEL_REFERENCE_LABELS[0],
        }
        client.health_result["ready"] = False
        with self.assertRaisesRegex(SeriesClientError, "deep runtime health"):
            client.preflight_series_spec(local_spec)
        self.assertEqual(client.deep_health_checks, 1)
        self.assertTrue(client.assert_deep)
        self.assertEqual(client.uploaded, [])

        starter = RoutingClient()
        public = token_world_travel_spec()
        public["id"] = SERIES_ID
        public["status"] = "ready"
        starter.states = [public]
        starter.health_result["connected"] = False
        with self.assertRaisesRegex(SeriesClientError, "deep runtime health"):
            starter.start_series(SERIES_ID)
        self.assertFalse(
            any(
                method == "POST" and path.endswith("/start")
                for method, path, _payload, _query in starter.calls
            )
        )

    def test_world_travel_spec_uploads_shared_and_per_shot_scene_references(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_paths = []
            for index in range(7):
                path = root / f"shared-{index}.png"
                path.write_bytes(f"shared-{index}".encode())
                reference_paths.append(path)
            rome = root / "rome.png"
            florence = root / "florence.png"
            rome.write_bytes(b"rome")
            florence.write_bytes(b"florence")
            spec = {
                "title": "Italy",
                "template": "world_travel",
                "references": {
                    "images": [
                        {
                            "source": path.name,
                            "label": WORLD_TRAVEL_REFERENCE_LABELS[index],
                        }
                        for index, path in enumerate(reference_paths)
                    ],
                    "videos": [],
                    "audio": [],
                },
                "shots": [
                    {
                        "title": "Rome",
                        "prompt": "Walk through Rome.",
                        "duration": 10,
                        "seed": 1,
                        "scene_reference": {"source": rome.name, "label": "Rome"},
                    },
                    {
                        "title": "Florence",
                        "prompt": "Continue to Florence.",
                        "duration": 10,
                        "seed": 2,
                        "scene_reference": {"path": florence.name, "label": "Florence"},
                    },
                ],
            }
            original = copy.deepcopy(spec)
            client = PreparationClient()
            payload = client.prepare_series_payload(spec, base_dir=root)

            self.assertEqual(spec, original)
            self.assertEqual(payload["settings"]["profile"], "quality_int8_offload")
            self.assertEqual(payload["settings"]["ref_image_size"], "match")
            self.assertEqual(payload["settings"]["continuity_seconds"], 2)
            self.assertEqual(payload["settings"]["width"], 1248)
            self.assertEqual(payload["settings"]["height"], 704)
            self.assertEqual(client.config_checks, 1)
            self.assertEqual(client.deep_health_checks, 1)
            self.assertEqual(len(client.uploaded), 9)
            self.assertEqual(len(client.validated), 9)
            self.assertEqual(payload["shots"][0]["scene_reference"]["label"], "Rome")
            self.assertIn("token", payload["shots"][0]["scene_reference"])
            self.assertNotIn("source", json.dumps(payload))
            self.assertNotIn(str(root), json.dumps(payload))

    def test_world_travel_image_omission_is_preflighted_and_preserved(self) -> None:
        spec = token_world_travel_spec()
        spec["shots"].append(
            {
                "title": "Florence",
                "prompt": "Continue to <Picture 8>.",
                "duration": 10,
                "scene_reference": {
                    "token": "florence-token",
                    "label": "Florence",
                },
                "omit_shared_image_labels": [
                    "Words card",
                    "LightMind glasses",
                    "Patchwork notebook",
                ],
            }
        )
        client = PreparationClient()
        report = client.preflight_series_spec(spec)
        self.assertEqual(report["template"], "world_travel")

        payload = client.prepare_series_payload(spec)
        self.assertEqual(payload["settings"]["continuity_seconds"], 3)
        self.assertEqual(
            payload["shots"][1]["omit_shared_image_labels"],
            ["Words card", "LightMind glasses", "Patchwork notebook"],
        )

        old_server = PreparationClient()
        old_server.config_result["series"].pop("shot_reference_policy")
        with self.assertRaisesRegex(SeriesClientError, "does not advertise"):
            old_server.preflight_series_spec(spec)
        invalid = copy.deepcopy(spec)
        invalid["shots"][1]["omit_shared_image_labels"] = ["Aya Chan"]
        with self.assertRaisesRegex(SeriesClientError, "persistent cast"):
            client.preflight_series_spec(invalid)

    def test_world_travel_defaults_and_preflights_effective_picture_layout(
        self,
    ) -> None:
        spec = token_world_travel_spec()
        spec["shots"].append(
            {
                "title": "Florence",
                "prompt": "Continue through <Picture 8> from <Picture 9>.",
                "duration": 10,
                "scene_reference": {
                    "token": "florence-token",
                    "label": "Florence",
                },
            }
        )
        original = copy.deepcopy(spec)
        client = PreparationClient()

        report = client.preflight_series_spec(spec)

        self.assertEqual(spec, original)
        second = report["effective_picture_layouts"][1]
        self.assertEqual(
            second["omit_shared_image_labels"],
            list(WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS),
        )
        self.assertEqual(
            [item["label"] for item in second["effective_pictures"]],
            [
                "Zhuangzi Robot",
                "Rara Xia",
                "Aya Chan",
                "Sasa Kun",
                "Florence",
                "previous shot's exact final frame",
            ],
        )
        self.assertEqual(
            [
                (item["logical_slot"], item["physical_slot"])
                for item in second["effective_pictures"]
            ],
            [(2, 1), (5, 2), (6, 3), (7, 4), (8, 5), (9, 6)],
        )

        payload = client.prepare_series_payload(spec)
        self.assertEqual(
            payload["shots"][1]["omit_shared_image_labels"],
            list(WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS),
        )
        self.assertEqual(spec, original)

    def test_world_travel_explicit_keep_all_override_is_preserved(self) -> None:
        spec = token_world_travel_spec()
        spec["shots"].append(
            {
                "title": "Closing card",
                "prompt": "Use <Picture 1> at <Picture 8>.",
                "duration": 10,
                "scene_reference": {"token": "closing-token", "label": "Closing"},
                "omit_shared_image_labels": [],
            }
        )
        client = PreparationClient()

        report = client.preflight_series_spec(spec)
        self.assertEqual(
            report["effective_picture_layouts"][1][
                "omit_shared_image_labels"
            ],
            [],
        )
        payload = client.prepare_series_payload(spec)
        self.assertEqual(payload["shots"][1]["omit_shared_image_labels"], [])

    def test_direct_token_create_materializes_later_shot_defaults(self) -> None:
        spec = token_world_travel_spec()
        spec["shots"].append(
            {
                "title": "Later",
                "prompt": "Continue at <Picture 8>.",
                "duration": 10,
                "scene_reference": {"token": "later-token", "label": "Later"},
            }
        )
        original = copy.deepcopy(spec)
        client = RoutingClient()

        client.create_series(spec)

        create = next(
            call
            for call in client.calls
            if call[0] == "POST" and call[1] == "/api/series"
        )
        self.assertEqual(
            create[2]["shots"][1]["omit_shared_image_labels"],
            list(WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS),
        )
        self.assertEqual(spec, original)

    def test_world_travel_omitted_authored_tag_fails_before_upload(self) -> None:
        spec = token_world_travel_spec()
        spec["shots"].append(
            {
                "title": "Unsafe repeat",
                "prompt": "Show <Picture 1> again beside <Picture 8>.",
                "duration": 10,
                "scene_reference": {"token": "later-token", "label": "Later"},
            }
        )
        client = PreparationClient()

        with self.assertRaisesRegex(
            SeriesClientError, "effective reference policy omits Words card"
        ):
            client.prepare_series_payload(spec)
        self.assertEqual(client.uploaded, [])

    def test_world_travel_omission_preflight_canonicalizes_label_whitespace(
        self,
    ) -> None:
        spec = token_world_travel_spec()
        spec["shots"].append(
            {
                "title": "Unsafe padded omission",
                "prompt": "Show <Picture 1> again beside <Picture 8>.",
                "duration": 10,
                "scene_reference": {"token": "later-token", "label": "Later"},
                "omit_shared_image_labels": [" Words card "],
            }
        )
        client = PreparationClient()

        with self.assertRaisesRegex(
            SeriesClientError, "effective reference policy omits Words card"
        ):
            client.prepare_series_payload(spec)
        self.assertEqual(client.uploaded, [])

    def test_world_travel_string_zero_disables_continuity_in_preflight(self) -> None:
        spec = token_world_travel_spec()
        spec["settings"]["continuity_seconds"] = "0"
        spec["shots"].append(
            {
                "title": "Independent second shot",
                "prompt": "Continue at <Picture 8>.",
                "duration": 10,
                "scene_reference": {"token": "later-token", "label": "Later"},
            }
        )
        client = PreparationClient()

        report = client.preflight_series_spec(spec)

        self.assertNotIn(
            "previous shot's exact final frame",
            [
                item["label"]
                for item in report["effective_picture_layouts"][1][
                    "effective_pictures"
                ]
            ],
        )

    def test_start_preflights_durable_effective_references_before_post(self) -> None:
        series = token_world_travel_spec()
        series.update(id=SERIES_ID, status="ready")
        series["shots"].append(
            {
                "title": "Later",
                "prompt": "Accidentally repeat <Picture 1> at <Picture 8>.",
                "duration": 10,
                "scene_reference": {"kind": "image", "label": "Later"},
                "omit_shared_image_labels": ["Words card"],
            }
        )
        client = RoutingClient()
        client.states = [series]

        with self.assertRaisesRegex(
            SeriesClientError, "effective reference policy omits Words card"
        ):
            client.start_series(SERIES_ID)
        self.assertFalse(
            any(
                method == "POST" and path.endswith("/start")
                for method, path, _payload, _query in client.calls
            )
        )

    def test_video_soundtrack_source_is_uploaded_as_an_audio_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "motion.mp4"
            soundtrack = root / "voice.m4a"
            video.write_bytes(b"video")
            soundtrack.write_bytes(b"audio")
            client = PreparationClient()
            payload = client.prepare_series_payload(
                {
                    "title": "Soundtrack source",
                    "template": "movie",
                    "references": {
                        "images": [],
                        "videos": [
                            {
                                "source": video.name,
                                "soundtrack_source": soundtrack.name,
                                "label": "Character movement",
                            }
                        ],
                        "audio": [],
                    },
                    "shots": [
                        {"title": "One", "prompt": "one", "duration": 5},
                        {"title": "Two", "prompt": "two", "duration": 5},
                    ],
                },
                base_dir=root,
            )
            self.assertEqual([kind for kind, _ in client.uploaded], ["video", "audio"])
            video_record = payload["references"]["videos"][0]
            self.assertIn("soundtrack", video_record)
            self.assertNotIn("soundtrack_source", json.dumps(payload))

    def test_world_travel_scene_reference_is_required_before_api_submission(
        self,
    ) -> None:
        client = PreparationClient()
        with self.assertRaisesRegex(SeriesClientError, "scene_reference"):
            client.prepare_series_payload(
                {
                    "title": "Missing scene",
                    "template": "world_travel",
                    "references": {
                        "images": [
                            {"token": f"token-{index}", "label": label}
                            for index, label in enumerate(WORLD_TRAVEL_REFERENCE_LABELS)
                        ],
                        "videos": [],
                        "audio": [],
                    },
                    "shots": [{"title": "One", "prompt": "x"}],
                }
            )

    def test_lifecycle_routes_and_polling_match_server_contract(self) -> None:
        client = RoutingClient()
        client.retry_shot(SERIES_ID, 2, regenerate_following=True)
        self.assertEqual(
            client.calls[-1],
            (
                "POST",
                f"/api/series/{SERIES_ID}/shots/2/retry",
                {"regenerate_following": True},
                None,
            ),
        )
        client.pause_series(SERIES_ID)
        self.assertEqual(client.calls[-1][1], f"/api/series/{SERIES_ID}/pause")
        client.resume_series(SERIES_ID)
        self.assertEqual(client.calls[-1][1], f"/api/series/{SERIES_ID}/resume")
        client.retry_finalization(SERIES_ID)
        self.assertEqual(
            client.calls[-1][1], f"/api/series/{SERIES_ID}/retry-finalization"
        )
        client.set_shot_reference_policy(
            SERIES_ID,
            3,
            omit_shared_image_labels=["Words card", "Patchwork notebook"],
        )
        self.assertEqual(
            client.calls[-1],
            (
                "PUT",
                f"/api/series/{SERIES_ID}/shots/3/reference-policy",
                {
                    "omit_shared_image_labels": [
                        "Words card",
                        "Patchwork notebook",
                    ]
                },
                None,
            ),
        )

        client.states = [
            {"id": SERIES_ID, "status": "running", "progress": {}},
            {"id": SERIES_ID, "status": "completed", "progress": {}},
        ]
        updates = []
        with patch("scripts.localvideogen_series.time.sleep"):
            result = client.wait_for_series(
                SERIES_ID,
                interval=0.2,
                timeout=10,
                on_update=updates.append,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["status"] for item in updates], ["running", "completed"])

        retry_notices = []
        client.states = [
            SeriesTransportError("offline once"),
            SeriesTransportError("offline twice"),
            {"id": SERIES_ID, "status": "paused", "progress": {}},
        ]
        sleeps = []
        with patch(
            "scripts.localvideogen_series.time.sleep", side_effect=sleeps.append
        ):
            paused = client.wait_for_series(
                SERIES_ID,
                interval=0.2,
                timeout=10,
                stop_statuses=WAIT_MODES["terminal-or-paused"],
                on_transport_error=lambda error, delay: retry_notices.append(
                    (str(error), delay)
                ),
            )
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(len(retry_notices), 2)

        with patch.object(
            client,
            "_request_json",
            side_effect=SeriesTransportError("write response unknown"),
        ) as request, patch.object(
            client, "get_series", return_value=token_world_travel_spec()
        ), patch.object(client, "preflight_series_spec", return_value={}):
            with self.assertRaises(SeriesTransportError):
                client.start_series(SERIES_ID)
            request.assert_called_once()

    def test_recover_receipt_is_bounded_read_only_and_state_aware(self) -> None:
        client = RoutingClient()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "series-receipt.json"
            receipt.write_text(
                json.dumps(
                    {
                        "schema": SERIES_RECEIPT_SCHEMA,
                        "series_id": SERIES_ID,
                        "base_url": client.base_url,
                        "ignored": "receipt fields are not trusted as server state",
                    }
                ),
                encoding="utf-8",
            )
            parsed, parsed_path = load_series_receipt(receipt)
            self.assertEqual(parsed["series_id"], SERIES_ID)
            self.assertEqual(parsed_path, receipt.resolve())

            for status, expected in (
                ("ready", "start"),
                ("running", "wait"),
                ("paused", "review_then_resume"),
                ("failed", "inspect_then_retry"),
                ("completed", "download_verified_artifacts"),
            ):
                with self.subTest(status=status):
                    client.states = [{"id": SERIES_ID, "status": status}]
                    before = len(client.calls)
                    recovered = client.recover_from_receipt(receipt)
                    self.assertEqual(
                        recovered["recommended_next_action"]["action"], expected
                    )
                    new_calls = client.calls[before:]
                    self.assertEqual(
                        new_calls,
                        [("GET", f"/api/series/{SERIES_ID}", None, None)],
                    )

            receipt.write_text(
                json.dumps(
                    {
                        "schema": "untrusted.receipt.v9",
                        "series_id": SERIES_ID,
                        "base_url": client.base_url,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SeriesClientError, "schema"):
                load_series_receipt(receipt)

    def test_run_overwrite_controls_are_independent(self) -> None:
        parser = build_parser()
        recovered = parser.parse_args(["recover", "receipt.json"])
        self.assertEqual(recovered.command, "recover")
        self.assertEqual(recovered.receipt, Path("receipt.json"))
        parsed = parser.parse_args(
            [
                "run",
                "spec.json",
                "--output-dir",
                "out",
                "--overwrite-downloads",
            ]
        )
        self.assertFalse(parsed.overwrite_receipt)
        self.assertTrue(parsed.overwrite_downloads)
        parsed = parser.parse_args(
            [
                "run",
                "spec.json",
                "--output-dir",
                "out",
                "--overwrite-receipt",
            ]
        )
        self.assertTrue(parsed.overwrite_receipt)
        self.assertFalse(parsed.overwrite_downloads)

    def test_run_writes_receipt_before_start_failure_interrupt_or_pause(self) -> None:
        class FakeRunClient:
            base_url = "http://127.0.0.1:8190"

            def __init__(self, outcome):
                self.outcome = outcome
                self.wait_kwargs = None

            def create_series_from_spec(self, spec, *, base_dir):
                return {
                    "id": SERIES_ID,
                    "title": "Recoverable series",
                    "status": "ready",
                    "revision": 1,
                    "created_ms": 123,
                }

            def start_series(self, series_id):
                if isinstance(self.outcome, BaseException):
                    raise self.outcome
                return {"id": series_id, "status": "queued"}

            def wait_for_series(self, series_id, **kwargs):
                self.wait_kwargs = kwargs
                return {"id": series_id, "status": self.outcome, "progress": {}}

            def download_artifact(self, *args, **kwargs):
                raise AssertionError("a paused review series must not download a final")

        for name, outcome, expected_code in (
            ("failure", SeriesTransportError("start response unknown"), 1),
            ("interrupt", KeyboardInterrupt(), 130),
            ("paused", "paused", 0),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                spec = root / "spec.json"
                spec.write_text("{}", encoding="utf-8")
                output = root / "output"
                fake = FakeRunClient(outcome)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch(
                        "scripts.localvideogen_series.LocalVideoGenClient",
                        return_value=fake,
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = main(["run", str(spec), "--output-dir", str(output)])
                self.assertEqual(code, expected_code)
                receipt = output / f"{SERIES_ID}-receipt.json"
                self.assertTrue(receipt.is_file())
                self.assertEqual(
                    json.loads(receipt.read_text())["series_id"], SERIES_ID
                )
                self.assertIn(SERIES_ID, stderr.getvalue())
                if outcome == "paused":
                    self.assertIn("paused", fake.wait_kwargs["stop_statuses"])
                    self.assertEqual(json.loads(stdout.getvalue())["downloads"], {})

    def test_streaming_upload_sets_same_origin_and_download_uses_public_allowlist(
        self,
    ) -> None:
        LoopbackHandler.upload_body = b""
        LoopbackHandler.upload_origin = ""
        LoopbackHandler.artifact_requests = []
        LoopbackHandler.health_requests = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            client = LocalVideoGenClient(base_url)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "reference.png"
                source.write_bytes(b"stable-image-bytes")
                uploaded = client.upload("image", source)
                self.assertEqual(uploaded["token"], "opaque-upload-token")
                self.assertEqual(LoopbackHandler.health_requests, 1)
                self.assertEqual(LoopbackHandler.upload_origin, base_url)
                self.assertIn(b"stable-image-bytes", LoopbackHandler.upload_body)

                target = root / "final.mp4"
                receipt = client.download_artifact(SERIES_ID, "final", target)
                self.assertEqual(target.read_bytes(), b"precious-final-video")
                self.assertEqual(receipt["size"], len(b"precious-final-video"))
                self.assertEqual(len(receipt["sha256"]), 64)
                self.assertEqual(
                    LoopbackHandler.artifact_requests,
                    [f"/api/series/{SERIES_ID}/artifacts/{FINAL_ID}?download=1"],
                )
                with self.assertRaisesRegex(SeriesClientError, "already exists"):
                    client.download_artifact(SERIES_ID, "final", target)
                replaced = client.download_artifact(
                    SERIES_ID, "final", target, overwrite=True
                )
                self.assertEqual(
                    replaced["sha256"], hashlib.sha256(target.read_bytes()).hexdigest()
                )
                bad_target = root / "bad.mp4"
                with self.assertRaisesRegex(SeriesClientError, "SHA-256"):
                    client.download_artifact(SERIES_ID, BAD_ID, bad_target)
                self.assertFalse(bad_target.exists())
                with self.assertRaisesRegex(SeriesClientError, "durable allowlist"):
                    client.download_artifact(
                        SERIES_ID, str(uuid.uuid4()), root / "unknown"
                    )
                self.assertEqual(len(LoopbackHandler.artifact_requests), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_atomic_no_overwrite_install_does_not_clobber_a_racing_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / ".movie.part"
            target = root / "movie.mp4"
            staged.write_bytes(b"our-verified-download")

            def racing_link(source, destination, *, follow_symlinks):
                self.assertFalse(follow_symlinks)
                target.write_bytes(b"racing-writer")
                raise FileExistsError

            with (
                patch("scripts.localvideogen_series.os.link", side_effect=racing_link),
                self.assertRaisesRegex(SeriesClientError, "target already exists"),
            ):
                _install_temporary(staged, target, overwrite=False)
            self.assertEqual(target.read_bytes(), b"racing-writer")
            self.assertEqual(staged.read_bytes(), b"our-verified-download")


if __name__ == "__main__":
    unittest.main()
