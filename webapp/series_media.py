"""Bounded media validation and continuity/final artifact creation for series."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import stat
import uuid
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any

from .media import bounded_video_dimensions


class SeriesMediaError(RuntimeError):
    """A generated artifact failed validation or local processing."""


class SeriesMedia:
    """Own derived artifacts without moving or deleting original H3 outputs."""

    def __init__(
        self,
        output_root: str | Path,
        artifact_root: str | Path,
        *,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.ffmpeg = shutil.which(ffmpeg or "ffmpeg")
        self.ffprobe = shutil.which(ffprobe or "ffprobe")
        self.available = self.ffmpeg is not None and self.ffprobe is not None
        self.timeout = timeout
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(self.artifact_root.stat().st_mode) & 0o077:
            raise SeriesMediaError("the series artifact directory must be private")

    async def _run(self, arguments: list[str], *, timeout: float | None = None) -> bytes:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C"},
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self.timeout
            )
        except asyncio.TimeoutError as exc:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            raise SeriesMediaError("a bounded local media tool timed out") from exc
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        except OSError as exc:
            raise SeriesMediaError("a bounded local media tool failed to run") from exc
        if len(stdout) > 2 * 1024 * 1024 or len(stderr) > 2 * 1024 * 1024:
            raise SeriesMediaError("local media tool diagnostics exceeded the safe limit")
        if process.returncode != 0:
            diagnostic = stderr.decode("utf-8", "replace").strip()[-1000:]
            raise SeriesMediaError(
                "generated video failed local media processing"
                + (f": {diagnostic}" if diagnostic else "")
            )
        return stdout

    def output_path(self, locator: Mapping[str, Any]) -> Path:
        """Resolve an allowlisted JobStore output locator below output_root."""

        try:
            filename = str(locator["filename"])
            subfolder = str(locator.get("subfolder") or "")
            if PurePosixPath(filename).name != filename or "\\" in filename:
                raise ValueError
            folder = PurePosixPath(subfolder)
            if folder.is_absolute() or any(part in {"", ".", ".."} for part in folder.parts):
                if subfolder:
                    raise ValueError
            candidate = (self.output_root / Path(*folder.parts) / filename).resolve(strict=True)
            candidate.relative_to(self.output_root)
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise SeriesMediaError("generated output is missing or outside the output directory") from exc
        if not candidate.is_file():
            raise SeriesMediaError("generated output is not a regular file")
        return candidate

    def artifact_path(self, series_id: str, relative: str) -> Path:
        try:
            relative_path = PurePosixPath(relative)
            if (
                relative_path.is_absolute()
                or not relative_path.parts
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or "\\" in relative
            ):
                raise ValueError
            root = (self.artifact_root / series_id).resolve(strict=True)
            candidate = (root / Path(*relative_path.parts)).resolve(strict=True)
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SeriesMediaError("series artifact is missing or unsafe") from exc
        if not candidate.is_file():
            raise SeriesMediaError("series artifact is not a regular file")
        return candidate

    def _series_dir(self, series_id: str) -> Path:
        target = self.artifact_root / series_id
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        resolved = target.resolve(strict=True)
        resolved.relative_to(self.artifact_root)
        return resolved

    async def probe(self, path: Path) -> dict[str, Any]:
        output = await self._run(
            [
                self.ffprobe or "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            timeout=min(self.timeout, 60.0),
        )
        try:
            document = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SeriesMediaError("ffprobe returned invalid generated-media metadata") from exc
        if not isinstance(document, dict):
            raise SeriesMediaError("ffprobe returned invalid generated-media metadata")
        return document

    @staticmethod
    def _fraction(value: Any) -> float:
        try:
            result = float(Fraction(str(value)))
        except (ValueError, ZeroDivisionError) as exc:
            raise SeriesMediaError("generated video has invalid frame-rate metadata") from exc
        if not math.isfinite(result):
            raise SeriesMediaError("generated video has invalid frame-rate metadata")
        return result

    @staticmethod
    def _duration(document: Mapping[str, Any], stream: Mapping[str, Any]) -> float:
        raw = stream.get("duration")
        if raw in {None, "N/A"} and isinstance(document.get("format"), Mapping):
            raw = document["format"].get("duration")
        try:
            duration = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SeriesMediaError("generated video has no trustworthy duration") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise SeriesMediaError("generated video has no trustworthy duration")
        return duration

    @classmethod
    def _audio_duration(cls, stream: Mapping[str, Any]) -> float:
        """Return stream-local audio duration without a misleading container fallback."""

        raw = stream.get("duration")
        if raw in {None, "N/A"}:
            duration_ts = stream.get("duration_ts")
            time_base = stream.get("time_base")
            if duration_ts not in {None, "N/A"} and time_base not in {None, "N/A"}:
                try:
                    raw = int(duration_ts) * cls._fraction(time_base)
                except (TypeError, ValueError, OverflowError):
                    raw = None
        if raw in {None, "N/A"}:
            samples = stream.get("nb_samples")
            sample_rate = stream.get("sample_rate")
            try:
                raw = int(samples) / int(sample_rate)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                raw = None
        try:
            duration = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SeriesMediaError(
                "generated video audio has no trustworthy duration"
            ) from exc
        if not math.isfinite(duration) or duration <= 0:
            raise SeriesMediaError("generated video audio has no trustworthy duration")
        return duration

    @classmethod
    def _stream_start(cls, stream: Mapping[str, Any]) -> float:
        raw = stream.get("start_time")
        if raw in {None, "N/A"}:
            start_pts = stream.get("start_pts")
            time_base = stream.get("time_base")
            if start_pts not in {None, "N/A"} and time_base not in {None, "N/A"}:
                try:
                    raw = int(start_pts) * cls._fraction(time_base)
                except (TypeError, ValueError, OverflowError):
                    raw = None
        # A genuinely absent stream timestamp is treated as the conventional
        # zero origin; malformed or non-finite timestamps still fail closed.
        if raw in {None, "N/A"}:
            return 0.0
        try:
            start = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SeriesMediaError(
                "generated video has invalid stream timing metadata"
            ) from exc
        if not math.isfinite(start):
            raise SeriesMediaError("generated video has invalid stream timing metadata")
        return start

    async def validate_video(
        self,
        path: Path,
        *,
        width: int,
        height: int,
        expected_frames: int | None = None,
        expected_duration: float | None = None,
        duration_tolerance: float = 0.30,
    ) -> dict[str, Any]:
        """Probe, exact-stream-check, then fully decode video and stereo audio."""

        document = await self.probe(path)
        streams = document.get("streams")
        if not isinstance(streams, list):
            raise SeriesMediaError("generated video has no decodable streams")
        videos = [item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"]
        audios = [item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"]
        if len(videos) != 1 or len(audios) != 1:
            raise SeriesMediaError(
                "generated video must contain one video stream and one stereo audio stream"
            )
        video = videos[0]
        audio = audios[0]
        if int(video.get("width") or 0) != width or int(video.get("height") or 0) != height:
            raise SeriesMediaError("generated video resolution does not match the series canvas")
        fps = self._fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        if abs(fps - 24.0) > 0.01:
            raise SeriesMediaError("generated video is not constant 24 fps")
        if int(audio.get("channels") or 0) != 2:
            raise SeriesMediaError("generated video audio is not stereo")
        if int(audio.get("sample_rate") or 0) != 32_000:
            raise SeriesMediaError("generated video audio is not 32000 Hz")
        duration = self._duration(document, video)
        audio_duration = self._audio_duration(audio)
        video_start = self._stream_start(video)
        audio_start = self._stream_start(audio)
        video_end = video_start + duration
        audio_end = audio_start + audio_duration
        # AAC packets can add a small encoder-delay/padding discrepancy.  They
        # cannot account for missing dialogue over a material part of a shot.
        audio_tolerance = 0.15
        if abs(audio_start - video_start) > audio_tolerance:
            raise SeriesMediaError("generated video audio does not start with the video")
        if audio_end + audio_tolerance < video_end:
            raise SeriesMediaError("generated video audio ends before the video")
        if audio_end > video_end + 0.50:
            raise SeriesMediaError("generated video audio materially exceeds the video")
        raw_frames = video.get("nb_read_frames") or video.get("nb_frames")
        try:
            frames = int(raw_frames)
        except (TypeError, ValueError):
            frames = round(duration * fps)
        if expected_frames is not None and frames != expected_frames:
            raise SeriesMediaError(
                f"generated video frame count is {frames}, expected {expected_frames}"
            )
        target_duration = expected_duration
        if target_duration is None and expected_frames is not None:
            target_duration = expected_frames / 24.0
        if target_duration is not None and abs(duration - target_duration) > duration_tolerance:
            raise SeriesMediaError("generated video duration is outside the expected tolerance")
        await self._run(
            [
                self.ffmpeg or "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ]
        )
        digest = await asyncio.to_thread(self.sha256, path)
        return {
            "width": width,
            "height": height,
            "fps": 24.0,
            "frames": frames,
            "duration": duration,
            "video_start": video_start,
            "audio_duration": audio_duration,
            "audio_start": audio_start,
            "audio_sample_rate": 32_000,
            "audio_channels": 2,
            "bytes": path.stat().st_size,
            "sha256": digest,
        }

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    async def make_continuity(
        self,
        series_id: str,
        *,
        shot_index: int,
        attempt_number: int,
        source: Path,
        source_duration: float,
        source_frames: int,
        seconds: int,
        width: int,
        height: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if seconds not in {2, 3, 4}:
            raise SeriesMediaError("continuity handoff must be 2, 3, or 4 seconds")
        if source_duration + 0.05 < seconds:
            raise SeriesMediaError("shot is too short for its continuity handoff")
        if source_frames < 1:
            raise SeriesMediaError("shot has no frame available for continuity")
        folder = self._series_dir(series_id)
        nonce = uuid.uuid4().hex[:10]
        stem = f"shot-{shot_index + 1:02d}-attempt-{attempt_number:02d}-{nonce}"
        tail = folder / f"{stem}-tail-{seconds}s.mp4"
        frame = folder / f"{stem}-final-frame.png"
        start = max(0.0, source_duration - seconds)
        tail_width, tail_height = bounded_video_dimensions(width, height)
        await self._run(
            [
                self.ffmpeg or "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                "-ss",
                f"{start:.6f}",
                "-t",
                str(seconds),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-vf",
                f"fps=24,scale={tail_width}:{tail_height}:flags=lanczos,setsar=1",
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "12",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "32000",
                "-ac",
                "2",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(tail),
            ]
        )
        await self._run(
            [
                self.ffmpeg or "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                f"select=eq(n\\,{source_frames - 1})",
                "-frames:v",
                "1",
                str(frame),
            ]
        )
        tail_metadata = await self.validate_video(
            tail,
            width=tail_width,
            height=tail_height,
            expected_frames=seconds * 24,
            expected_duration=float(seconds),
        )
        return (
            {
                "id": str(uuid.uuid4()),
                "kind": "continuity_video",
                "label": f"Shot {shot_index + 1} continuity tail",
                "storage": "series",
                "relative": tail.name,
                "mime": "video/mp4",
                "download_name": tail.name,
                "metadata": tail_metadata,
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "final_frame",
                "label": f"Shot {shot_index + 1} final frame",
                "storage": "series",
                "relative": frame.name,
                "mime": "image/png",
                "download_name": frame.name,
                "metadata": {
                    "bytes": frame.stat().st_size,
                    "sha256": await asyncio.to_thread(self.sha256, frame),
                },
            },
        )

    async def stitch(
        self,
        series_id: str,
        *,
        title: str,
        shots: Sequence[Mapping[str, Any]],
        width: int,
        height: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if len(shots) < 2:
            raise SeriesMediaError("a series needs at least two accepted shots")
        folder = self._series_dir(series_id)
        nonce = uuid.uuid4().hex[:10]
        final = folder / f"series-final-{nonce}.mp4"
        concat_file = folder / f"series-concat-{nonce}.ffconcat"
        manifest_path = folder / f"series-manifest-{nonce}.json"
        paths: list[Path] = []
        total_frames = 0
        manifest_shots: list[dict[str, Any]] = []
        for item in shots:
            locator = item.get("locator")
            metadata = item.get("metadata")
            if not isinstance(locator, Mapping) or not isinstance(metadata, Mapping):
                raise SeriesMediaError("accepted shot metadata is incomplete")
            path = self.output_path(locator)
            try:
                accepted_frames = int(metadata["frames"])
                accepted_duration = float(metadata["duration"])
                accepted_bytes = int(metadata["bytes"])
                accepted_sha256 = str(metadata["sha256"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise SeriesMediaError("accepted shot metadata is incomplete") from exc
            current_metadata = await self.validate_video(
                path,
                width=width,
                height=height,
                expected_frames=accepted_frames,
                expected_duration=accepted_duration,
            )
            if (
                int(current_metadata["bytes"]) != accepted_bytes
                or str(current_metadata["sha256"]) != accepted_sha256
            ):
                raise SeriesMediaError(
                    "an accepted shot changed after validation; refusing to stitch"
                )
            paths.append(path)
            total_frames += accepted_frames
            manifest_shots.append(
                {
                    "shot_index": int(item["shot_index"]),
                    "attempt": int(item["attempt"]),
                    "job_id": str(item["job_id"]),
                    "filename": path.name,
                    "duration": accepted_duration,
                    "frames": accepted_frames,
                    "bytes": accepted_bytes,
                    "sha256": accepted_sha256,
                }
            )
        for path in paths:
            if any(character in str(path) for character in ("'", "\n", "\r")):
                raise SeriesMediaError("an accepted output path cannot be represented safely")
        concat_text = "ffconcat version 1.0\n" + "".join(
            f"file '{path}'\n" for path in paths
        )
        descriptor = os.open(concat_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(concat_text)
        await self._run(
            [
                self.ffmpeg or "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(final),
            ]
        )
        final_metadata = await self.validate_video(
            final,
            width=width,
            height=height,
            expected_frames=total_frames,
            expected_duration=total_frames / 24.0,
            duration_tolerance=0.50,
        )
        manifest = {
            "schema": 1,
            "series_id": series_id,
            "title": title,
            "lossless_concat": True,
            "video": {
                "filename": final.name,
                **final_metadata,
            },
            "shots": manifest_shots,
        }
        descriptor = os.open(
            manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        return (
            {
                "id": str(uuid.uuid4()),
                "kind": "final",
                "label": "Stitched series",
                "storage": "series",
                "relative": final.name,
                "mime": "video/mp4",
                "download_name": final.name,
                "metadata": final_metadata,
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "manifest",
                "label": "Validation manifest",
                "storage": "series",
                "relative": manifest_path.name,
                "mime": "application/json",
                "download_name": manifest_path.name,
                "metadata": {
                    "bytes": manifest_path.stat().st_size,
                    "sha256": await asyncio.to_thread(self.sha256, manifest_path),
                },
            },
        )
