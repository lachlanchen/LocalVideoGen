from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from PIL import Image

from webapp.series_media import SeriesMedia, SeriesMediaError


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required",
)
class SeriesMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.output_root = root / "output"
        (self.output_root / "video").mkdir(parents=True)
        self.media = SeriesMedia(self.output_root, root / "private-artifacts")
        self.first = self.output_root / "video" / "first.mp4"
        self.second = self.output_root / "video" / "second.mp4"
        for path, color in ((self.first, "red"), (self.second, "blue")):
            source = f"color={color}:size=320x240:rate=24:duration=2"
            if path == self.first:
                source += ",drawbox=x=0:y=0:w=iw:h=ih:color=green:t=fill:enable='eq(n\\,47)'"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    source,
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=32000",
                    "-t",
                    "2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "32000",
                    "-ac",
                    "2",
                    "-shortest",
                    str(path),
                ],
                check=True,
                timeout=30,
            )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def locator(name: str):
        return {
            "filename": name,
            "subfolder": "video",
            "type": "output",
            "media_type": "video",
        }

    async def test_full_validation_continuity_and_lossless_concat(self) -> None:
        first_metadata = await self.media.validate_video(
            self.first, width=320, height=240, expected_frames=48
        )
        second_metadata = await self.media.validate_video(
            self.second, width=320, height=240, expected_frames=48
        )
        series_id = str(uuid.uuid4())
        tail, frame = await self.media.make_continuity(
            series_id,
            shot_index=0,
            attempt_number=1,
            source=self.first,
            source_duration=first_metadata["duration"],
            source_frames=first_metadata["frames"],
            seconds=2,
            width=320,
            height=240,
        )
        self.assertEqual(tail["metadata"]["frames"], 48)
        self.assertTrue(self.media.artifact_path(series_id, tail["relative"]).is_file())
        frame_path = self.media.artifact_path(series_id, frame["relative"])
        self.assertTrue(frame_path.is_file())
        with Image.open(frame_path) as image:
            red, green, blue = image.convert("RGB").getpixel((160, 120))
        self.assertGreater(green, red)
        self.assertGreater(green, blue)
        final, manifest = await self.media.stitch(
            series_id,
            title="Test series",
            shots=[
                {
                    "shot_index": 0,
                    "attempt": 1,
                    "job_id": str(uuid.uuid4()),
                    "locator": self.locator("first.mp4"),
                    "metadata": first_metadata,
                },
                {
                    "shot_index": 1,
                    "attempt": 1,
                    "job_id": str(uuid.uuid4()),
                    "locator": self.locator("second.mp4"),
                    "metadata": second_metadata,
                },
            ],
            width=320,
            height=240,
        )
        self.assertEqual(final["metadata"]["frames"], 96)
        self.assertTrue(self.media.artifact_path(series_id, final["relative"]).is_file())
        self.assertTrue(self.media.artifact_path(series_id, manifest["relative"]).is_file())

    async def test_output_locator_cannot_escape_root(self) -> None:
        with self.assertRaises(SeriesMediaError):
            self.media.output_path({"filename": "first.mp4", "subfolder": "../video"})

    async def test_validation_rejects_audio_that_ends_before_video(self) -> None:
        path = self.output_root / "video" / "short-audio.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=black:size=320x240:rate=24:duration=2",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=32000:duration=0.25",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "32000",
                "-ac",
                "2",
                str(path),
            ],
            check=True,
            timeout=30,
        )
        with self.assertRaisesRegex(SeriesMediaError, "audio ends before"):
            await self.media.validate_video(
                path, width=320, height=240, expected_frames=48
            )

    async def test_stitch_rejects_an_accepted_source_that_changed(self) -> None:
        first_metadata = await self.media.validate_video(
            self.first, width=320, height=240, expected_frames=48
        )
        second_metadata = await self.media.validate_video(
            self.second, width=320, height=240, expected_frames=48
        )
        with self.first.open("ab") as handle:
            handle.write(b"changed-after-validation")
        shots = [
            {
                "shot_index": index,
                "attempt": 1,
                "job_id": str(uuid.uuid4()),
                "locator": self.locator(filename),
                "metadata": metadata,
            }
            for index, (filename, metadata) in enumerate(
                (("first.mp4", first_metadata), ("second.mp4", second_metadata))
            )
        ]
        with self.assertRaisesRegex(SeriesMediaError, "changed after validation"):
            await self.media.stitch(
                str(uuid.uuid4()),
                title="Drift test",
                shots=shots,
                width=320,
                height=240,
            )

    async def test_validation_rejects_audio_shifted_on_the_timeline(self) -> None:
        path = self.output_root / "video" / "delayed-audio.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=black:size=320x240:rate=24:duration=2",
                "-itsoffset",
                "1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=32000:duration=2",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "32000",
                "-ac",
                "2",
                str(path),
            ],
            check=True,
            timeout=30,
        )
        with self.assertRaisesRegex(SeriesMediaError, "audio does not start"):
            await self.media.validate_video(
                path, width=320, height=240, expected_frames=48
            )


if __name__ == "__main__":
    unittest.main()
