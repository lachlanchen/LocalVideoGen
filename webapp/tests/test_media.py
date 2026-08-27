from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from webapp.media import (
    DEFAULT_LIMITS,
    MediaToolUnavailable,
    MediaValidationError,
    prepare_upload,
    validate_audio_probe,
    validate_video_probe,
)


def _video_probe(
    *, duration: str = "4.0", width: int = 1920, height: int = 1080, fps: str = "30/1"
):
    return {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": width,
                "height": height,
                "avg_frame_rate": fps,
                "r_frame_rate": fps,
                "duration": duration,
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "44100",
                "channels": 2,
                "duration": duration,
            },
        ],
        "format": {"duration": duration, "size": "12345", "format_name": "mov,mp4"},
    }


def _audio_probe(
    *, duration: str = "3.0", sample_rate: str = "44100", channels: int = 1
):
    return {
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "flac",
                "sample_rate": sample_rate,
                "channels": channels,
                "duration": duration,
            }
        ],
        "format": {"duration": duration, "size": "1234", "format_name": "flac"},
    }


class ProbeValidationTests(unittest.TestCase):
    def test_video_probe_is_sanitized(self):
        result = validate_video_probe(_video_probe())
        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        self.assertEqual(result["fps"], 30.0)
        self.assertTrue(result["has_audio"])
        self.assertEqual(result["audio_sample_rate"], 44100)
        self.assertEqual(result["audio_channels"], 2)

    def test_video_requires_a_video_stream(self):
        with self.assertRaisesRegex(MediaValidationError, "no decodable video stream"):
            validate_video_probe(_audio_probe())

    def test_video_duration_bounds_are_enforced(self):
        with self.assertRaisesRegex(MediaValidationError, "at least 2 seconds"):
            validate_video_probe(_video_probe(duration="1.5"))
        with self.assertRaisesRegex(MediaValidationError, "no longer than 15 seconds"):
            validate_video_probe(_video_probe(duration="16"))

    def test_video_dimensions_are_bounded_before_decode(self):
        with self.assertRaisesRegex(MediaValidationError, "safe decode limit"):
            validate_video_probe(_video_probe(width=9000, height=9000))

    def test_normalized_video_must_be_24fps(self):
        with self.assertRaisesRegex(MediaValidationError, "constant 24 fps"):
            validate_video_probe(_video_probe(fps="24000/1001"), normalized=True)

    def test_audio_requires_stream_and_bounded_duration(self):
        with self.assertRaisesRegex(MediaValidationError, "no decodable audio stream"):
            validate_audio_probe(
                {
                    "streams": [_video_probe()["streams"][0]],
                    "format": {
                        "duration": "3",
                        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    },
                }
            )
        with self.assertRaisesRegex(MediaValidationError, "no longer than 15 seconds"):
            validate_audio_probe(_audio_probe(duration="16"))

    def test_playlist_like_container_is_rejected(self):
        document = _video_probe()
        document["format"]["format_name"] = "hls"
        with self.assertRaisesRegex(MediaValidationError, "container is unsupported"):
            validate_video_probe(document)

    def test_normalized_audio_schema_is_strict(self):
        wrong_rate = _audio_probe(sample_rate="44100", channels=2)
        with self.assertRaisesRegex(MediaValidationError, "not 32000 Hz"):
            validate_audio_probe(wrong_rate, normalized=True)
        wrong_channels = _audio_probe(sample_rate="32000", channels=1)
        with self.assertRaisesRegex(MediaValidationError, "not stereo"):
            validate_audio_probe(wrong_channels, normalized=True)


class ImagePreparationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _image_bytes(size=(64, 32), *, image_format="JPEG") -> io.BytesIO:
        buffer = io.BytesIO()
        Image.new("RGB", size, (210, 40, 30)).save(buffer, format=image_format)
        buffer.seek(0)
        return buffer

    async def test_image_magic_decode_and_lossless_normalization(self):
        prepared = await prepare_upload(
            self._image_bytes(),
            kind="image",
            # Deliberately untrusted/misleading: output identity comes from bytes.
            original_name="../portrait.txt",
        )
        path = prepared.path
        try:
            self.assertEqual(prepared.content_type, "image/png")
            self.assertTrue(prepared.filename.endswith(".png"))
            self.assertEqual(prepared.metadata["source_format"], "jpeg")
            self.assertEqual(
                (prepared.metadata["width"], prepared.metadata["height"]), (64, 32)
            )
            self.assertEqual(prepared.metadata["original_name"], "portrait.txt")
            with prepared.open() as handle:
                self.assertTrue(handle.seekable())
                self.assertEqual(handle.tell(), 0)
                self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")
        finally:
            prepared.cleanup()
        self.assertFalse(path.exists())

    async def test_image_rejects_declared_extension_without_image_magic(self):
        with self.assertRaisesRegex(MediaValidationError, "corrupt or unsupported"):
            await prepare_upload(
                io.BytesIO(b"not an image"), kind="image", original_name="fake.png"
            )

    async def test_image_dimension_cap_runs_before_normalized_output(self):
        limits = replace(DEFAULT_LIMITS, max_image_edge=8, max_image_pixels=64)
        with self.assertRaisesRegex(MediaValidationError, "dimensions exceed"):
            await prepare_upload(self._image_bytes((9, 7)), kind="image", limits=limits)

    async def test_source_copy_limit_is_enforced(self):
        limits = replace(DEFAULT_LIMITS, max_image_source_bytes=4)
        with self.assertRaisesRegex(MediaValidationError, "source limit"):
            await prepare_upload(io.BytesIO(b"12345"), kind="image", limits=limits)

    async def test_missing_media_tools_are_reported_before_video_execution(self):
        with self.assertRaises(MediaToolUnavailable):
            await prepare_upload(
                io.BytesIO(b"video bytes"),
                kind="video",
                ffprobe="h3-definitely-missing-ffprobe",
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required",
)
class FfmpegPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="h3-media-tests-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _run_ffmpeg(self, *arguments: str) -> None:
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            *arguments,
        ]
        subprocess.run(
            command,
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def _make_video(self) -> Path:
        path = self.root / "source-30fps.mp4"
        self._run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2.2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        )
        return path

    def _make_audio(self) -> Path:
        path = self.root / "source-mono-44100.wav"
        self._run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100",
            "-t",
            "0.5",
            "-ac",
            "1",
            "-c:a",
            "pcm_s24le",
            str(path),
        )
        return path

    async def test_video_becomes_bounded_constant_24fps_mp4(self):
        source = await asyncio.to_thread(self._make_video)
        prepared = await prepare_upload(
            source, kind="video", original_name="camera.MOV"
        )
        path = prepared.path
        try:
            self.assertEqual(prepared.content_type, "video/mp4")
            self.assertTrue(prepared.filename.endswith(".mp4"))
            self.assertAlmostEqual(prepared.metadata["source_fps"], 30.0, places=3)
            self.assertAlmostEqual(prepared.metadata["fps"], 24.0, places=3)
            self.assertGreaterEqual(prepared.metadata["duration"], 2.0)
            self.assertLessEqual(prepared.metadata["duration"], 15.05)
            self.assertLessEqual(
                prepared.metadata["width"], DEFAULT_LIMITS.normalized_video_edge
            )
            self.assertLessEqual(
                prepared.metadata["height"], DEFAULT_LIMITS.normalized_video_edge
            )
            self.assertEqual(prepared.metadata["width"] % 2, 0)
            self.assertEqual(prepared.metadata["height"] % 2, 0)
            self.assertTrue(prepared.metadata["has_audio"])
            self.assertEqual(prepared.metadata["audio_sample_rate"], 32000)
            self.assertEqual(prepared.metadata["audio_channels"], 2)
            self.assertLess(prepared.size, DEFAULT_LIMITS.max_comfy_file_bytes)
            with prepared.open() as handle:
                handle.seek(10)
                self.assertEqual(handle.tell(), 10)
        finally:
            prepared.cleanup()
        self.assertFalse(path.exists())

    async def test_audio_becomes_bounded_stereo_32khz_wav(self):
        source = await asyncio.to_thread(self._make_audio)
        prepared = await prepare_upload(
            source, kind="audio", original_name="voice.flac"
        )
        try:
            self.assertEqual(prepared.content_type, "audio/wav")
            self.assertTrue(prepared.filename.endswith(".wav"))
            self.assertEqual(prepared.metadata["source_sample_rate"], 44100)
            self.assertEqual(prepared.metadata["source_channels"], 1)
            self.assertEqual(prepared.metadata["sample_rate"], 32000)
            self.assertEqual(prepared.metadata["channels"], 2)
            self.assertGreaterEqual(prepared.metadata["duration"], 0.1)
            self.assertLessEqual(prepared.metadata["duration"], 15.05)
            with prepared.open() as handle:
                self.assertEqual(handle.read(4), b"RIFF")
        finally:
            prepared.cleanup()

    async def test_audio_only_file_is_rejected_as_video(self):
        source = await asyncio.to_thread(self._make_audio)
        with self.assertRaisesRegex(MediaValidationError, "no decodable video stream"):
            await prepare_upload(source, kind="video")


if __name__ == "__main__":
    unittest.main()
