from __future__ import annotations

import unittest
from unittest.mock import patch

from webapp.workflows import (
    AUDIO_VAE,
    AUX_DEVICE_ENV,
    PROFILES,
    TEXT_ENCODER,
    VIDEO_VAE,
    RequestError,
    UploadedAsset,
    aligned_frame_count,
    compile_prompt,
    parse_render_spec,
    public_config,
)


IMAGE = UploadedAsset("image", "h3-webapp/image/image.png", "image.png")
VIDEO = UploadedAsset("video", "h3-webapp/video/video.mp4", "video.mp4")
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


class RequestValidationTests(unittest.TestCase):
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
