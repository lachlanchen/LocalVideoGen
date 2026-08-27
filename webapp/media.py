"""Safe preparation of image, video, and audio uploads for ComfyUI.

The public :func:`prepare_upload` function copies an already-spooled web
upload into a private temporary directory, validates its actual contents, and
normalizes it to a small set of trusted formats.  The returned
:class:`PreparedUpload` owns that directory and must be closed after ComfyUI
has consumed the file.

No user-controlled filename is used on disk or passed to a subprocess.  Media
tools are invoked with argument arrays, bounded runtimes, and bounded captured
output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
import warnings
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO, Literal, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError


MediaKind = Literal["image", "video", "audio"]
MIB = 1024 * 1024


class MediaError(ValueError):
    """Base class for safe, user-facing media preparation failures."""


class MediaValidationError(MediaError):
    """The uploaded bytes are not valid or are outside supported limits."""


class MediaToolUnavailable(MediaError):
    """A required local ffmpeg tool is unavailable."""


class MediaToolError(MediaError):
    """A bounded ffmpeg/ffprobe invocation failed."""

    def __init__(self, message: str, *, diagnostic: str = "") -> None:
        super().__init__(message)
        # Kept out of str(exc), which is suitable for a browser response.
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class MediaLimits:
    """Limits chosen for H3 references and ComfyUI's pinned 100 MiB request."""

    max_image_source_bytes: int = 30 * MIB
    max_video_source_bytes: int = 600 * MIB
    max_audio_source_bytes: int = 100 * MIB
    # Leave room for multipart framing under ComfyUI's 100 MiB request cap.
    max_comfy_file_bytes: int = 99 * MIB

    max_image_edge: int = 8192
    max_image_pixels: int = 40_000_000
    max_source_video_edge: int = 8192
    max_source_video_pixels: int = 40_000_000
    normalized_video_edge: int = 2048

    video_min_seconds: float = 2.0
    video_max_seconds: float = 15.0
    audio_min_seconds: float = 0.10
    audio_max_seconds: float = 15.0
    # Containers commonly round a boundary by a frame or packet.  The output
    # is still explicitly clipped to the exact maximum below.
    duration_probe_slack: float = 0.25

    video_fps: int = 24
    min_source_video_fps: float = 1.0
    max_source_video_fps: float = 240.0
    audio_sample_rate: int = 32_000
    max_source_audio_sample_rate: int = 384_000
    max_audio_channels: int = 32
    max_streams: int = 32

    probe_timeout: float = 20.0
    normalize_timeout: float = 180.0
    max_tool_output_bytes: int = 256 * 1024
    copy_chunk_bytes: int = MIB

    def source_limit(self, kind: MediaKind) -> int:
        if kind == "image":
            return self.max_image_source_bytes
        if kind == "video":
            return self.max_video_source_bytes
        if kind == "audio":
            return self.max_audio_source_bytes
        raise MediaValidationError("upload kind must be image, video, or audio")


DEFAULT_LIMITS = MediaLimits()


@dataclass
class PreparedUpload:
    """A trusted, seekable upload artifact with explicit temporary ownership."""

    kind: MediaKind
    path: Path
    filename: str
    content_type: str
    size: int
    metadata: dict[str, Any]
    _workspace: Path = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def open(self) -> BinaryIO:
        """Open the normalized artifact at offset zero for multipart upload."""

        if self._closed:
            raise RuntimeError("prepared upload has already been cleaned up")
        handle = self.path.open("rb")
        if not handle.seekable():  # pragma: no cover - regular files are seekable
            handle.close()
            raise RuntimeError("prepared upload is unexpectedly not seekable")
        handle.seek(0)
        return handle

    def cleanup(self) -> None:
        """Remove the private workspace.  Safe to call more than once."""

        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._workspace, ignore_errors=True)

    close = cleanup

    def __enter__(self) -> PreparedUpload:
        if self._closed:
            raise RuntimeError("prepared upload has already been cleaned up")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.cleanup()

    async def __aenter__(self) -> PreparedUpload:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.cleanup()


def _safe_display_name(value: str) -> str:
    value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    value = re.sub(r"[\x00-\x1f\x7f]", "_", value)
    return (value or "upload")[:160]


def _source_limit_message(kind: MediaKind, maximum: int) -> str:
    return f"{kind} upload exceeds the {maximum // MIB} MiB source limit"


def _copy_source(
    source: BinaryIO | str | os.PathLike[str],
    destination: Path,
    maximum: int,
    chunk_size: int,
) -> int:
    close_source = False
    if isinstance(source, (str, os.PathLike)):
        source_handle = open(source, "rb")
        close_source = True
    else:
        source_handle = source
        try:
            source_handle.seek(0)
        except (AttributeError, OSError) as exc:
            raise MediaValidationError("upload spool must be seekable") from exc

    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source_handle.read(chunk_size)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise MediaValidationError(
                        "upload spool did not return binary data"
                    )
                size += len(chunk)
                if size > maximum:
                    raise MediaValidationError("upload exceeds its bounded source size")
                output.write(chunk)
    finally:
        if close_source:
            source_handle.close()

    if size == 0:
        raise MediaValidationError("upload is empty")
    return size


def _normalize_image(
    source: Path, destination: Path, limits: MediaLimits
) -> dict[str, Any]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as candidate:
                source_format = (candidate.format or "").upper()
                if source_format not in {"PNG", "JPEG", "WEBP", "BMP"}:
                    raise MediaValidationError(
                        "image bytes are not PNG, JPEG, WebP, or BMP"
                    )
                if int(getattr(candidate, "n_frames", 1)) != 1:
                    raise MediaValidationError(
                        "animated images are not accepted as H3 still frames"
                    )
                width, height = candidate.size
                _validate_image_dimensions(width, height, limits)
                candidate.verify()

            # verify() intentionally invalidates the decoder, so reopen and
            # force a complete decode before writing a metadata-free PNG.
            with Image.open(source) as decoded:
                decoded.load()
                decoded = ImageOps.exif_transpose(decoded)
                width, height = decoded.size
                _validate_image_dimensions(width, height, limits)
                has_alpha = "A" in decoded.getbands() or "transparency" in decoded.info
                normalized = decoded.convert("RGBA" if has_alpha else "RGB")
                normalized.save(
                    destination, format="PNG", compress_level=6, optimize=False
                )
    except MediaValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaValidationError("image dimensions are unsafe") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MediaValidationError("image is corrupt or unsupported") from exc

    return {
        "source_format": source_format.lower(),
        "width": width,
        "height": height,
        "frames": 1,
    }


def _validate_image_dimensions(width: Any, height: Any, limits: MediaLimits) -> None:
    if (
        not isinstance(width, int)
        or not isinstance(height, int)
        or width < 1
        or height < 1
    ):
        raise MediaValidationError("image has invalid dimensions")
    if width > limits.max_image_edge or height > limits.max_image_edge:
        raise MediaValidationError(
            f"image dimensions exceed {limits.max_image_edge}px per edge"
        )
    if width * height > limits.max_image_pixels:
        raise MediaValidationError(
            f"image exceeds the {limits.max_image_pixels:,}-pixel decode limit"
        )


def _resolve_tool(explicit: str | os.PathLike[str] | None, default: str) -> str:
    requested = os.fspath(explicit) if explicit is not None else default
    resolved = shutil.which(requested)
    if resolved is None:
        raise MediaToolUnavailable(f"{default} is required for local media validation")
    return resolved


class _ToolOutputLimit(RuntimeError):
    pass


async def _read_bounded(stream: asyncio.StreamReader | None, maximum: int) -> bytes:
    if stream is None:
        return b""
    data = bytearray()
    while True:
        chunk = await stream.read(min(64 * 1024, maximum + 1))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > maximum:
            raise _ToolOutputLimit


async def _run_tool(
    arguments: list[str], *, timeout: float, output_limit: int
) -> tuple[bytes, bytes]:
    """Run one local media tool without a shell and with bounded diagnostics."""

    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise MediaToolUnavailable("local media tool could not be started") from exc

    stdout_task = asyncio.create_task(_read_bounded(process.stdout, output_limit))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, output_limit))

    async def collect() -> tuple[bytes, bytes, int]:
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return stdout, stderr, await process.wait()

    try:
        stdout, stderr, returncode = await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise MediaToolError("local media validation timed out") from exc
    except _ToolOutputLimit as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise MediaToolError(
            "local media tool produced excessive diagnostic output"
        ) from exc
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise

    if returncode != 0:
        diagnostic = stderr.decode("utf-8", "replace").strip()
        diagnostic = re.sub(r"\s+", " ", diagnostic)[:1000]
        raise MediaToolError(
            "local media tool rejected the upload", diagnostic=diagnostic
        )
    return stdout, stderr


async def _ffprobe(path: Path, executable: str, limits: MediaLimits) -> dict[str, Any]:
    arguments = [
        executable,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration,sample_rate,channels",
        "-of",
        "json",
        "-protocol_whitelist",
        "file",
        str(path),
    ]
    stdout, _ = await _run_tool(
        arguments,
        timeout=limits.probe_timeout,
        output_limit=limits.max_tool_output_bytes,
    )
    try:
        document = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaToolError("ffprobe returned invalid metadata") from exc
    if not isinstance(document, dict):
        raise MediaToolError("ffprobe returned invalid metadata")
    return document


def _number(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaValidationError(f"media has no reliable {field_name}") from exc
    if not math.isfinite(result) or result <= 0:
        raise MediaValidationError(f"media has no reliable {field_name}")
    return result


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise MediaValidationError(f"media has invalid {field_name}")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaValidationError(f"media has invalid {field_name}") from exc
    if result <= 0:
        raise MediaValidationError(f"media has invalid {field_name}")
    return result


def _fraction(value: Any) -> float | None:
    try:
        fraction = Fraction(str(value))
        result = float(fraction)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _streams(document: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    raw = document.get("streams")
    if not isinstance(raw, list):
        raise MediaValidationError("media metadata has no stream list")
    return [
        item
        for item in raw
        if isinstance(item, Mapping) and item.get("codec_type") == kind
    ]


def _validate_container(
    document: Mapping[str, Any], allowed: frozenset[str], label: str
) -> str:
    container = document.get("format")
    if not isinstance(container, Mapping):
        raise MediaValidationError(f"{label} has no recognized container")
    format_name = str(container.get("format_name") or "").lower()
    names = {part.strip() for part in format_name.split(",") if part.strip()}
    if not names.intersection(allowed):
        raise MediaValidationError(f"{label} container is unsupported")
    return format_name


def _duration(document: Mapping[str, Any], stream: Mapping[str, Any]) -> float:
    # Stream duration describes the selected media track more accurately than
    # a container with a long unrelated attachment.  Fall back to format.
    value = stream.get("duration")
    try:
        return _number(value, "duration")
    except MediaValidationError:
        container = document.get("format")
        if not isinstance(container, Mapping):
            raise MediaValidationError("media has no reliable duration")
        return _number(container.get("duration"), "duration")


def validate_video_probe(
    document: Mapping[str, Any],
    *,
    limits: MediaLimits = DEFAULT_LIMITS,
    normalized: bool = False,
) -> dict[str, Any]:
    """Validate ffprobe JSON and return a sanitized video metadata mapping."""

    raw_streams = document.get("streams")
    if not isinstance(raw_streams, list) or len(raw_streams) > limits.max_streams:
        raise MediaValidationError("media contains too many streams")
    videos = _streams(document, "video")
    if not videos:
        raise MediaValidationError("reference video has no decodable video stream")
    _validate_container(
        document,
        frozenset(
            {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi"}
        ),
        "reference video",
    )
    stream = videos[0]
    duration = _duration(document, stream)
    minimum = limits.video_min_seconds
    maximum = limits.video_max_seconds
    slack = 0.05 if normalized else limits.duration_probe_slack
    if duration + 1e-6 < minimum:
        raise MediaValidationError(
            f"reference video must be at least {minimum:g} seconds"
        )
    if duration > maximum + slack:
        raise MediaValidationError(
            f"reference video must be no longer than {maximum:g} seconds"
        )

    width = _integer(stream.get("width"), "video width")
    height = _integer(stream.get("height"), "video height")
    if width < 16 or height < 16:
        raise MediaValidationError("reference video dimensions are too small")
    edge_limit = (
        limits.normalized_video_edge if normalized else limits.max_source_video_edge
    )
    pixel_limit = (
        edge_limit * edge_limit if normalized else limits.max_source_video_pixels
    )
    if width > edge_limit or height > edge_limit or width * height > pixel_limit:
        raise MediaValidationError(
            "reference video dimensions exceed the safe decode limit"
        )

    fps = _fraction(stream.get("avg_frame_rate")) or _fraction(
        stream.get("r_frame_rate")
    )
    if fps is None:
        raise MediaValidationError("reference video has no reliable frame rate")
    if (
        not normalized
        and not limits.min_source_video_fps <= fps <= limits.max_source_video_fps
    ):
        raise MediaValidationError(
            "reference video frame rate is outside the safe decode range"
        )
    if normalized and not math.isclose(
        fps, limits.video_fps, rel_tol=0.0, abs_tol=0.01
    ):
        raise MediaValidationError(
            f"normalized reference video is not constant {limits.video_fps} fps"
        )

    audio_streams = _streams(document, "audio")
    audio_rate = None
    audio_channels = None
    if audio_streams:
        audio_rate = _integer(audio_streams[0].get("sample_rate"), "audio sample rate")
        audio_channels = _integer(
            audio_streams[0].get("channels"), "audio channel count"
        )
        if audio_rate > limits.max_source_audio_sample_rate:
            raise MediaValidationError(
                "reference video audio sample rate is outside the safe decode range"
            )
        if audio_channels > limits.max_audio_channels:
            raise MediaValidationError("reference video audio has too many channels")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": bool(audio_streams),
        "audio_sample_rate": audio_rate,
        "audio_channels": audio_channels,
        "codec": str(stream.get("codec_name") or ""),
    }


def validate_audio_probe(
    document: Mapping[str, Any],
    *,
    limits: MediaLimits = DEFAULT_LIMITS,
    normalized: bool = False,
) -> dict[str, Any]:
    """Validate ffprobe JSON and return a sanitized audio metadata mapping."""

    raw_streams = document.get("streams")
    if not isinstance(raw_streams, list) or len(raw_streams) > limits.max_streams:
        raise MediaValidationError("media contains too many streams")
    audio_streams = _streams(document, "audio")
    if not audio_streams:
        raise MediaValidationError("reference audio has no decodable audio stream")
    _validate_container(
        document,
        frozenset(
            {
                "wav",
                "mp3",
                "flac",
                "ogg",
                "aac",
                "mov",
                "mp4",
                "m4a",
                "3gp",
                "3g2",
                "mj2",
                "matroska",
                "webm",
            }
        ),
        "reference audio",
    )
    stream = audio_streams[0]
    duration = _duration(document, stream)
    minimum = limits.audio_min_seconds
    maximum = limits.audio_max_seconds
    slack = 0.05 if normalized else limits.duration_probe_slack
    if duration + 1e-6 < minimum:
        raise MediaValidationError(
            f"reference audio must be at least {minimum:g} seconds"
        )
    if duration > maximum + slack:
        raise MediaValidationError(
            f"reference audio must be no longer than {maximum:g} seconds"
        )

    sample_rate = _integer(stream.get("sample_rate"), "audio sample rate")
    channels = _integer(stream.get("channels"), "audio channel count")
    if sample_rate > limits.max_source_audio_sample_rate:
        raise MediaValidationError(
            "reference audio sample rate is outside the safe decode range"
        )
    if channels > limits.max_audio_channels:
        raise MediaValidationError("reference audio has too many channels")
    if normalized and sample_rate != limits.audio_sample_rate:
        raise MediaValidationError(
            f"normalized audio is not {limits.audio_sample_rate} Hz"
        )
    if normalized and channels != 2:
        raise MediaValidationError("normalized audio is not stereo")

    return {
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "codec": str(stream.get("codec_name") or ""),
    }


def _check_output(path: Path, limits: MediaLimits) -> int:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MediaToolError("media normalization produced no output") from exc
    if size <= 0:
        raise MediaToolError("media normalization produced an empty output")
    if size > limits.max_comfy_file_bytes:
        raise MediaValidationError(
            f"normalized media exceeds the {limits.max_comfy_file_bytes // MIB} MiB ComfyUI upload budget"
        )
    return size


def _sha256(path: Path, chunk_size: int = MIB) -> str:
    """Fingerprint normalized bytes before their temporary workspace is removed."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


async def _normalize_video(
    source: Path,
    destination: Path,
    ffmpeg: str,
    limits: MediaLimits,
    source_info: Mapping[str, Any],
) -> None:
    duration = min(float(source_info["duration"]), limits.video_max_seconds)
    scale = (
        f"fps={limits.video_fps},"
        f"scale=w='min({limits.normalized_video_edge},iw)':"
        f"h='min({limits.normalized_video_edge},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1"
    )
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-max_alloc",
        str(512 * MIB),
        "-probesize",
        str(20 * MIB),
        "-analyzeduration",
        str(20 * MIB),
        "-protocol_whitelist",
        "file",
        "-i",
        str(source),
        "-t",
        f"{duration:.6f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-vf",
        scale,
        "-fps_mode",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-maxrate",
        "24M",
        "-bufsize",
        "48M",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        str(limits.audio_sample_rate),
        "-ac",
        "2",
        "-threads",
        "4",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    await _run_tool(
        arguments,
        timeout=limits.normalize_timeout,
        output_limit=limits.max_tool_output_bytes,
    )


async def _normalize_audio(
    source: Path,
    destination: Path,
    ffmpeg: str,
    limits: MediaLimits,
    source_info: Mapping[str, Any],
) -> None:
    duration = min(float(source_info["duration"]), limits.audio_max_seconds)
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-max_alloc",
        str(512 * MIB),
        "-probesize",
        str(20 * MIB),
        "-analyzeduration",
        str(20 * MIB),
        "-protocol_whitelist",
        "file",
        "-i",
        str(source),
        "-t",
        f"{duration:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(limits.audio_sample_rate),
        "-ac",
        "2",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        str(destination),
    ]
    await _run_tool(
        arguments,
        timeout=limits.normalize_timeout,
        output_limit=limits.max_tool_output_bytes,
    )


async def prepare_upload(
    source: BinaryIO | str | os.PathLike[str],
    *,
    kind: MediaKind,
    original_name: str = "upload",
    limits: MediaLimits = DEFAULT_LIMITS,
    ffmpeg: str | os.PathLike[str] | None = None,
    ffprobe: str | os.PathLike[str] | None = None,
) -> PreparedUpload:
    """Validate and normalize one web upload into a private temporary file.

    The caller owns the returned object and should use it as a context manager
    or call :meth:`PreparedUpload.cleanup` after the local ComfyUI multipart
    upload completes.  On every failure this function cleans its workspace.
    """

    if kind not in {"image", "video", "audio"}:
        raise MediaValidationError("upload kind must be image, video, or audio")

    workspace = Path(tempfile.mkdtemp(prefix="h3-media-"))
    source_path = workspace / "source.bin"
    display_name = _safe_display_name(original_name)
    try:
        source_maximum = limits.source_limit(kind)
        try:
            source_size = await asyncio.to_thread(
                _copy_source,
                source,
                source_path,
                source_maximum,
                limits.copy_chunk_bytes,
            )
        except MediaValidationError as exc:
            if str(exc) == "upload exceeds its bounded source size":
                raise MediaValidationError(
                    _source_limit_message(kind, source_maximum)
                ) from exc
            raise

        common_metadata: dict[str, Any] = {
            "original_name": display_name,
            "source_size": source_size,
            "normalized": True,
        }

        if kind == "image":
            destination = workspace / "normalized.png"
            media_metadata = await asyncio.to_thread(
                _normalize_image, source_path, destination, limits
            )
            size = _check_output(destination, limits)
            return PreparedUpload(
                kind="image",
                path=destination,
                filename=f"{uuid.uuid4().hex}.png",
                content_type="image/png",
                size=size,
                metadata={
                    **common_metadata,
                    **media_metadata,
                    "size": size,
                    "sha256": await asyncio.to_thread(_sha256, destination),
                },
                _workspace=workspace,
            )

        ffprobe_executable = _resolve_tool(ffprobe, "ffprobe")
        ffmpeg_executable = _resolve_tool(ffmpeg, "ffmpeg")
        source_probe = await _ffprobe(source_path, ffprobe_executable, limits)

        if kind == "video":
            source_info = validate_video_probe(source_probe, limits=limits)
            destination = workspace / "normalized.mp4"
            await _normalize_video(
                source_path, destination, ffmpeg_executable, limits, source_info
            )
            size = _check_output(destination, limits)
            normalized_probe = await _ffprobe(destination, ffprobe_executable, limits)
            normalized_info = validate_video_probe(
                normalized_probe, limits=limits, normalized=True
            )
            metadata = {
                **common_metadata,
                "size": size,
                "duration": normalized_info["duration"],
                "width": normalized_info["width"],
                "height": normalized_info["height"],
                "fps": normalized_info["fps"],
                "has_audio": normalized_info["has_audio"],
                "audio_sample_rate": normalized_info["audio_sample_rate"],
                "audio_channels": normalized_info["audio_channels"],
                "source_duration": source_info["duration"],
                "source_width": source_info["width"],
                "source_height": source_info["height"],
                "source_fps": source_info["fps"],
                "sha256": await asyncio.to_thread(_sha256, destination),
            }
            return PreparedUpload(
                kind="video",
                path=destination,
                filename=f"{uuid.uuid4().hex}.mp4",
                content_type="video/mp4",
                size=size,
                metadata=metadata,
                _workspace=workspace,
            )

        source_info = validate_audio_probe(source_probe, limits=limits)
        destination = workspace / "normalized.wav"
        await _normalize_audio(
            source_path, destination, ffmpeg_executable, limits, source_info
        )
        size = _check_output(destination, limits)
        normalized_probe = await _ffprobe(destination, ffprobe_executable, limits)
        normalized_info = validate_audio_probe(
            normalized_probe, limits=limits, normalized=True
        )
        metadata = {
            **common_metadata,
            "size": size,
            "duration": normalized_info["duration"],
            "sample_rate": normalized_info["sample_rate"],
            "channels": normalized_info["channels"],
            "source_duration": source_info["duration"],
            "source_sample_rate": source_info["sample_rate"],
            "source_channels": source_info["channels"],
            "sha256": await asyncio.to_thread(_sha256, destination),
        }
        return PreparedUpload(
            kind="audio",
            path=destination,
            filename=f"{uuid.uuid4().hex}.wav",
            content_type="audio/wav",
            size=size,
            metadata=metadata,
            _workspace=workspace,
        )
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


__all__ = [
    "DEFAULT_LIMITS",
    "MediaError",
    "MediaLimits",
    "MediaToolError",
    "MediaToolUnavailable",
    "MediaValidationError",
    "PreparedUpload",
    "prepare_upload",
    "validate_audio_probe",
    "validate_video_probe",
]
