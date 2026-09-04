from __future__ import annotations

import unittest
from unittest.mock import patch

from webapp.workflows import (
    AUDIO_VAE,
    AUX_DEVICE_ENV,
    LONG_REFERENCE_FRAME_PIXEL_LIMIT,
    LONG_REFERENCE_SAFE_PROFILE,
    PROFILES,
    TEXT_ENCODER,
    VIDEO_VAE,
    RequestError,
    UploadedAsset,
    aligned_frame_count,
    compile_prompt,
    parse_render_spec,
    profile_requires_two_devices,
    public_config,
)


IMAGE = UploadedAsset("image", "h3-webapp/image/image.png", "image.png")
VIDEO = UploadedAsset(
    "video",
    "h3-webapp/video/video.mp4",
    "video.mp4",
    {"width": 576, "height": 1024},
)
SAFE_VIDEO = UploadedAsset(
    "video",
    "h3-webapp/video/safe.mp4",
    "safe.mp4",
    {"width": 576, "height": 1024},
)
UNSAFE_VIDEO = UploadedAsset(
    "video",
    "h3-webapp/video/unsafe.mp4",
    "unsafe.mp4",
    {"width": 736, "height": 1312},
)
AUDIO = UploadedAsset("audio", "h3-webapp/audio/audio.wav", "audio.wav")


def graph_for(mode: str, profile: str):
    payload = {
        "mode": mode,
        "profile": profile,
        "prompt": "A deliberate camera move. Audio: quiet room tone.",
        "width": 1344,
        "height": 768,
        "duration": 5,
        "seed": "18446744073709551615",
    }
    assets = {
        "first_frame": IMAGE if mode == "i2v" else None,
        "last_frame": None,
        "ref_images": [IMAGE] if mode == "r2v" else [],
        "ref_videos": [VIDEO] if mode == "r2v" else [],
        "ref_video_audios": [AUDIO] if mode == "r2v" else [],
        "ref_audios": [AUDIO] if mode == "r2v" else [],
    }
    return compile_prompt(parse_render_spec(payload, assets))


class DurationTests(unittest.TestCase):
    def test_official_frame_grid(self):
        self.assertEqual(aligned_frame_count(2), 56)
        self.assertEqual(aligned_frame_count(5), 124)
        self.assertEqual(aligned_frame_count(15), 362)
        for value in (2, 2.5, 5, 7.5, 15):
            self.assertEqual(aligned_frame_count(value) % 17, 5)

    def test_full_profiles_enforce_trained_duration(self):
        with self.assertRaisesRegex(RequestError, "between 5 and 15"):
            parse_render_spec(
                {"mode": "t2v", "profile": "quality_bf16_dual", "prompt": "x", "duration": 2},
                {},
            )
        spec = parse_render_spec(
            {"mode": "t2v", "profile": "preview_int8_turbo_dual", "prompt": "x", "duration": 2},
            {},
        )
        self.assertEqual(spec.length, 56)

    def test_beginner_defaults_use_the_small_fast_preview(self):
        config = public_config()
        self.assertEqual(
            config["defaults"],
            {
                "mode": "t2v",
                "profile": "preview_int8_turbo_dual",
                "width": 864,
                "height": 480,
                "duration": 2,
            },
        )
        self.assertEqual(config["modes"][0]["label"], "Start with words")
        preview = next(
            profile
            for profile in config["profiles"]
            if profile["id"] == "preview_int8_turbo_dual"
        )
        self.assertIn("recommended", preview["label"].lower())

    def test_long_reference_config_is_separate_and_exact(self):
        safe = public_config()["long_reference"]
        self.assertEqual(safe["label"], "Long reference · 24 GiB safe")
        self.assertEqual(safe["profile"], "quality_int8_offload")
        self.assertEqual(safe["ref_image_size"], "match")
        self.assertEqual((safe["duration"], safe["length"]), (14, 345))
        self.assertEqual(
            (safe["portrait"]["width"], safe["portrait"]["height"]),
            (704, 1248),
        )
        self.assertEqual(safe["video_reference"]["max_pixels"], 576 * 1024)
        self.assertEqual(safe["frame_pixel_limit"], 510_000_000)


class GraphMatrixTests(unittest.TestCase):
    allowed_classes = {
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "SelectModelDevice",
        "SelectCLIPDevice",
        "SelectVAEDevice",
        "LoraLoaderModelOnly",
        "LoadImage",
        "LoadVideo",
        "LoadAudio",
        "GetVideoComponents",
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
        "RandomNoise",
        "KSamplerSelect",
        "BasicScheduler",
        "BasicGuider",
        "SamplerCustomAdvanced",
        "VAEDecode",
        "VAEDecodeAudio",
        "CreateVideo",
        "SaveVideo",
    }

    def test_every_mode_and_profile_is_closed_and_allowlisted(self):
        for mode in ("t2v", "i2v", "r2v"):
            for profile in PROFILES:
                with self.subTest(mode=mode, profile=profile):
                    graph = graph_for(mode, profile)
                    self.assertTrue(graph)
                    node_ids = set(graph)
                    for node in graph.values():
                        self.assertIn(node["class_type"], self.allowed_classes)
                        for value in node["inputs"].values():
                            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                                self.assertIn(value[0], node_ids)
                    self.assertEqual(sum(n["class_type"] == "SaveVideo" for n in graph.values()), 1)

    def test_models_steps_schedulers_and_devices(self):
        for mode in ("t2v", "i2v", "r2v"):
            for profile_id, profile in PROFILES.items():
                graph = graph_for(mode, profile_id)
                by_class: dict[str, list[dict]] = {}
                for node in graph.values():
                    by_class.setdefault(node["class_type"], []).append(node["inputs"])
                self.assertEqual(by_class["CLIPLoader"][0]["clip_name"], TEXT_ENCODER)
                self.assertEqual({item["vae_name"] for item in by_class["VAELoader"]}, {VIDEO_VAE, AUDIO_VAE})
                diffusion = by_class["UNETLoader"][0]["unet_name"]
                self.assertIn("ref2va" if mode == "r2v" else "fl2va", diffusion)
                self.assertIn("bf16" if profile.precision == "bf16" else "int8_convrot", diffusion)
                scheduler = by_class["BasicScheduler"][0]
                self.assertEqual(scheduler["steps"], profile.steps_ref if mode == "r2v" else profile.steps_fl)
                self.assertEqual(scheduler["scheduler"], "beta" if mode == "r2v" and not profile.turbo else "simple")
                self.assertEqual(bool(by_class.get("LoraLoaderModelOnly")), profile.turbo)
                if profile.dual_gpu:
                    self.assertEqual(by_class["SelectModelDevice"][0]["device"], "gpu:0")
                    self.assertEqual(by_class["SelectCLIPDevice"][0]["device"], "gpu:0")
                    self.assertEqual([item["device"] for item in by_class["SelectVAEDevice"]], ["gpu:0", "gpu:0"])
                else:
                    self.assertNotIn("SelectModelDevice", by_class)

    def test_shared_workstation_can_keep_auxiliary_stages_on_gpu_zero(self):
        with patch.dict("os.environ", {AUX_DEVICE_ENV: "gpu:0"}):
            graph = graph_for("r2v", "quality_bf16_dual")
            by_class: dict[str, list[dict]] = {}
            for node in graph.values():
                by_class.setdefault(node["class_type"], []).append(node["inputs"])
            self.assertEqual(by_class["SelectModelDevice"][0]["device"], "gpu:0")
            self.assertEqual(by_class["SelectCLIPDevice"][0]["device"], "gpu:0")
            self.assertEqual(
                [item["device"] for item in by_class["SelectVAEDevice"]],
                ["gpu:0", "gpu:0"],
            )
            maximum = next(
                profile
                for profile in public_config()["profiles"]
                if profile["id"] == "quality_bf16_dual"
            )
            self.assertFalse(maximum["effective_dual_gpu"])
            self.assertFalse(maximum["requires_two_gpus"])
            self.assertFalse(profile_requires_two_devices(PROFILES["quality_bf16_dual"]))
            self.assertEqual(
                maximum["device_layout"],
                {
                    "model": "gpu:0",
                    "conditioning": "gpu:0",
                    "final_decode": "gpu:0",
                },
            )

    def test_exclusive_workstation_can_move_auxiliary_stages_to_gpu_one(self):
        with patch.dict("os.environ", {AUX_DEVICE_ENV: "gpu:1"}):
            graph = graph_for("r2v", "quality_bf16_dual")
            by_class: dict[str, list[dict]] = {}
            for node in graph.values():
                by_class.setdefault(node["class_type"], []).append(node["inputs"])
            self.assertEqual(by_class["SelectModelDevice"][0]["device"], "gpu:0")
            self.assertEqual(by_class["SelectCLIPDevice"][0]["device"], "gpu:1")
            self.assertEqual(
                [item["device"] for item in by_class["SelectVAEDevice"]],
                ["gpu:1", "gpu:1"],
            )
            maximum = next(
                profile
                for profile in public_config()["profiles"]
                if profile["id"] == "quality_bf16_dual"
            )
            self.assertTrue(maximum["effective_dual_gpu"])
            self.assertTrue(maximum["requires_two_gpus"])
            self.assertTrue(profile_requires_two_devices(PROFILES["quality_bf16_dual"]))

    def test_invalid_auxiliary_device_is_rejected(self):
        with patch.dict("os.environ", {AUX_DEVICE_ENV: "gpu:9"}):
            with self.assertRaisesRegex(RuntimeError, AUX_DEVICE_ENV):
                graph_for("r2v", "quality_bf16_dual")

    def test_dual_profile_decodes_from_default_gpu_zero_vae_wrappers(self):
        graph = graph_for("r2v", "quality_bf16_dual")
        loaders = {
            node["inputs"]["vae_name"]: node_id
            for node_id, node in graph.items()
            if node["class_type"] == "VAELoader"
        }
        video_decode = next(
            node for node in graph.values() if node["class_type"] == "VAEDecode"
        )
        audio_decode = next(
            node for node in graph.values() if node["class_type"] == "VAEDecodeAudio"
        )
        self.assertEqual(video_decode["inputs"]["vae"][0], loaders[VIDEO_VAE])
        self.assertEqual(audio_decode["inputs"]["vae"][0], loaders[AUDIO_VAE])

        conditioning = next(
            node
            for node in graph.values()
            if node["class_type"] == "MiniMaxH3ReferenceToVideo"
        )
        selected_vae_ids = {
            node_id
            for node_id, node in graph.items()
            if node["class_type"] == "SelectVAEDevice"
        }
        self.assertIn(conditioning["inputs"]["vae"][0], selected_vae_ids)
        self.assertIn(conditioning["inputs"]["audio_vae"][0], selected_vae_ids)

    def test_r2v_autogrow_and_audio_pairing(self):
        graph = graph_for("r2v", "quality_bf16_dual")
        conditioning = next(node for node in graph.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
        keys = conditioning["inputs"]
        self.assertIn("ref_images.ref_image_0", keys)
        self.assertIn("ref_videos.ref_video_0", keys)
        self.assertIn("ref_video_audios.ref_video_audio_0", keys)
        self.assertIn("ref_audios.ref_audio_0", keys)
        self.assertFalse(any(key.endswith("_1") for key in keys if key.startswith("ref_")))

    def test_video_audio_is_wired_only_when_trusted_or_explicit(self):
        payload = {
            "mode": "r2v",
            "profile": "quality_bf16_dual",
            "prompt": "Continue the scene.",
            "width": 1024,
            "height": 768,
            "duration": 5,
        }
        silent = UploadedAsset(
            "video", "h3/silent.mp4", "silent.mp4", {"width": 864, "height": 640}
        )
        audible = UploadedAsset(
            "video",
            "h3/audible.mp4",
            "audible.mp4",
            {"width": 864, "height": 640, "has_audio": True},
        )
        for video, expected in ((silent, False), (audible, True)):
            with self.subTest(expected=expected):
                graph = compile_prompt(
                    parse_render_spec(payload, {"ref_videos": [video]})
                )
                conditioning = next(
                    node
                    for node in graph.values()
                    if node["class_type"] == "MiniMaxH3ReferenceToVideo"
                )
                self.assertEqual(
                    "ref_video_audios.ref_video_audio_0" in conditioning["inputs"],
                    expected,
                )


class RequestValidationTests(unittest.TestCase):
    def test_long_reference_24gb_safe_portrait_contract(self):
        spec = parse_render_spec(
            {
                "mode": "r2v",
                "prompt": "Keep the rider and add the four buddies.",
                "width": 704,
                "height": 1248,
                "duration": 14,
            },
            {"ref_videos": [SAFE_VIDEO]},
        )
        self.assertEqual(spec.profile.id, LONG_REFERENCE_SAFE_PROFILE)
        self.assertEqual(spec.ref_image_size, "match")
        self.assertEqual(spec.length, 345)
        self.assertEqual(
            spec.length
            * (spec.width * spec.height + 576 * 1024),
            506_603_520,
        )
        self.assertLessEqual(506_603_520, LONG_REFERENCE_FRAME_PIXEL_LIMIT)

    def test_prechange_reference_dimensions_are_not_trusted(self):
        with self.assertRaisesRegex(RequestError, "normalized H3 contract"):
            parse_render_spec(
                {
                    "mode": "r2v",
                    "profile": "quality_int8_offload",
                    "prompt": "Unsafe old full-resolution reference.",
                    "width": 736,
                    "height": 1312,
                    "duration": 14,
                    "ref_image_size": "match",
                },
                {"ref_videos": [UNSAFE_VIDEO]},
            )

    def test_long_visual_video_requires_the_exact_safe_profile_and_fidelity(self):
        for profile in PROFILES:
            for fidelity in ("match", "max"):
                if profile == LONG_REFERENCE_SAFE_PROFILE and fidelity == "match":
                    continue
                with self.subTest(profile=profile, fidelity=fidelity):
                    with self.assertRaisesRegex(
                        RequestError,
                        "require (?:the quality_int8_offload profile|ref_image_size=match)",
                    ):
                        parse_render_spec(
                            {
                                "mode": "r2v",
                                "profile": profile,
                                "prompt": "A long visual-video reference.",
                                "width": 704,
                                "height": 1248,
                                "duration": 14,
                                "ref_image_size": fidelity,
                            },
                            {"ref_videos": [SAFE_VIDEO]},
                        )

    def test_video_reference_missing_dimensions_fails_closed_at_every_duration(self):
        missing = UploadedAsset(
            "video", "h3-webapp/video/missing.mp4", "missing.mp4"
        )
        with self.assertRaisesRegex(RequestError, "trusted width and height"):
            parse_render_spec(
                {
                    "mode": "r2v",
                    "prompt": "Old reference without durable metadata.",
                    "width": 704,
                    "height": 1248,
                    "duration": 14,
                },
                {"ref_videos": [missing]},
            )
        with self.assertRaisesRegex(RequestError, "trusted width and height"):
            parse_render_spec(
                {
                    "mode": "r2v",
                    "prompt": "Short old reference without durable metadata.",
                    "duration": 5,
                },
                {"ref_videos": [missing]},
            )

    def test_long_reference_budget_sums_every_visual_video(self):
        second = UploadedAsset(
            "video",
            "h3-webapp/video/safe-2.mp4",
            "safe-2.mp4",
            {"width": 576, "height": 1024},
        )
        with self.assertRaisesRegex(RequestError, "reduce duration/canvas/reference count"):
            parse_render_spec(
                {
                    "mode": "r2v",
                    "prompt": "Two visual video references.",
                    "width": 704,
                    "height": 1248,
                    "duration": 14,
                },
                {"ref_videos": [SAFE_VIDEO, second]},
            )

    def test_long_reference_budget_charges_each_still_beyond_calibrated_one(self):
        with self.assertRaisesRegex(RequestError, "510,117,888 combined frame-pixels"):
            parse_render_spec(
                {
                    "mode": "r2v",
                    "prompt": "One video with five identity stills.",
                    "width": 704,
                    "height": 1248,
                    "duration": 14,
                },
                {"ref_videos": [SAFE_VIDEO], "ref_images": [IMAGE] * 5},
            )

    def test_long_reference_audio_cap_counts_only_effective_audio(self):
        silent = SAFE_VIDEO
        audible = UploadedAsset(
            "video",
            SAFE_VIDEO.path,
            SAFE_VIDEO.original_name,
            {"width": 576, "height": 1024, "has_audio": True},
        )
        payload = {
            "mode": "r2v",
            "prompt": "A long scene with one voice guide.",
            "width": 704,
            "height": 1248,
            "duration": 14,
        }
        parse_render_spec(payload, {"ref_videos": [silent], "ref_audios": [AUDIO]})
        with self.assertRaisesRegex(RequestError, "at most one audio"):
            parse_render_spec(
                payload, {"ref_videos": [audible], "ref_audios": [AUDIO]}
            )

    def test_short_multi_video_reference_cannot_bypass_global_budget(self):
        videos = [
            UploadedAsset(
                "video",
                f"h3-webapp/video/safe-{index}.mp4",
                f"safe-{index}.mp4",
                {"width": 576, "height": 1024},
            )
            for index in range(3)
        ]
        with self.assertRaisesRegex(RequestError, "618,133,504 combined frame-pixels"):
            parse_render_spec(
                {
                    "mode": "r2v",
                    "profile": "quality_bf16_dual",
                    "prompt": "Two short but oversized visual references.",
                    "width": 736,
                    "height": 1312,
                    "duration": 9,
                    "ref_image_size": "max",
                },
                {"ref_videos": videos},
            )

    def test_explicit_bf16_max_remains_available_when_video_load_is_short_and_safe(self):
        spec = parse_render_spec(
            {
                "mode": "r2v",
                "profile": "quality_bf16_dual",
                "prompt": "A genuinely short visual reference.",
                "width": 736,
                "height": 1312,
                "duration": 5,
                "ref_image_size": "max",
            },
            {"ref_videos": [SAFE_VIDEO]},
        )
        self.assertEqual(spec.profile.id, "quality_bf16_dual")
        self.assertEqual(spec.ref_image_size, "max")

    def test_short_video_plus_max_size_still_remains_explicitly_available(self):
        spec = parse_render_spec(
            {
                "mode": "r2v",
                "profile": "quality_bf16_dual",
                "prompt": "A short mixed reference.",
                "width": 736,
                "height": 1312,
                "duration": 5,
                "ref_image_size": "max",
            },
            {"ref_videos": [SAFE_VIDEO], "ref_images": [IMAGE]},
        )
        self.assertEqual(spec.ref_image_size, "max")

    def test_explicit_bf16_max_remains_available_for_long_image_only_r2v(self):
        spec = parse_render_spec(
            {
                "mode": "r2v",
                "profile": "quality_bf16_dual",
                "prompt": "A long image-only identity reference.",
                "width": 768,
                "height": 1024,
                "duration": 14,
                "ref_image_size": "max",
            },
            {"ref_images": [IMAGE]},
        )
        self.assertEqual(spec.profile.id, "quality_bf16_dual")
        self.assertEqual(spec.ref_image_size, "max")

    def test_short_reference_uses_safe_r2v_canvas_defaults(self):
        spec = parse_render_spec(
            {
                "mode": "r2v",
                "prompt": "A short reference clip.",
                "duration": 5,
            },
            {"ref_videos": [VIDEO]},
        )
        self.assertEqual((spec.width, spec.height), (1248, 704))

    def test_nonvideo_r2v_keeps_backward_compatible_quality_defaults(self):
        for assets in ({"ref_images": [IMAGE]}, {"ref_audios": [AUDIO]}):
            with self.subTest(assets=tuple(assets)):
                spec = parse_render_spec(
                    {"mode": "r2v", "prompt": "Use this reference."}, assets
                )
                self.assertEqual(spec.profile.id, "quality_bf16_dual")
                self.assertEqual((spec.width, spec.height), (1344, 768))
                self.assertEqual(spec.duration, 5)

    def test_resolution_and_seed_bounds(self):
        base = {"mode": "t2v", "prompt": "x", "width": 1344, "height": 768, "duration": 5}
        for change, message in (
            ({"width": 1333}, "multiples of 32"),
            ({"width": 1344, "height": 800}, "resolution exceeds"),
            ({"seed": str(1 << 64)}, "seed must be between"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(RequestError, message):
                parse_render_spec({**base, **change}, {})

    def test_mode_media_contracts(self):
        with self.assertRaisesRegex(RequestError, "does not accept reference"):
            parse_render_spec({"mode": "t2v", "prompt": "x"}, {"ref_images": [IMAGE]})
        with self.assertRaisesRegex(RequestError, "requires a first-frame"):
            parse_render_spec({"mode": "i2v", "prompt": "x"}, {})
        with self.assertRaisesRegex(RequestError, "requires at least one"):
            parse_render_spec({"mode": "r2v", "prompt": "x"}, {})


if __name__ == "__main__":
    unittest.main()
