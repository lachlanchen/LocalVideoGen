#!/usr/bin/env python3
"""Fail-closed burned-subtitle preflight for short H3 reference videos.

This command never edits its input.  A successful result requires both an
explicit operator declaration and a complete OCR scan of the lower caption
region.  Missing tools, missing OCR languages, decode failures, an input that
changes during inspection, or detected text all return a non-zero status.

Exit status:
  0: declared subtitle-free and no caption-like text was detected
  1: caption-like text was detected
  2: inspection was not conclusive (including a missing declaration)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_LANGUAGES = "chi_sim+chi_tra+eng"
OCR_PAGE_SEGMENTATION_MODES = (11, 7, 6)
OCR_SCALE = 3
OCR_BAND_FRACTION = 0.30
MIN_REFERENCE_SECONDS = 2.0
MAX_REFERENCE_SECONDS = 15.25
MAX_REFERENCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_SCALED_SAMPLE_PIXELS = 500_000_000
MAX_OCR_INVOCATIONS = 1_500
MAX_CANDIDATES_PER_SAMPLE = 512
TEXT_CHARACTER = re.compile(
    r"[0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
CJK_CHARACTER = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
COMMON_OCR_NOISE = frozenset("一二三十人个了的")


class PreflightError(RuntimeError):
    """The reference could not be inspected conclusively."""


@dataclass(frozen=True)
class TextCandidate:
    text: str
    confidence: float
    x: int
    y: int
    width: int
    height: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 2),
            "box": {
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": self.height,
            },
        }


def _finite_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PreflightError(f"ffprobe returned an invalid {label}") from exc
    if not math.isfinite(result):
        raise PreflightError(f"ffprobe returned an invalid {label}")
    return result


def _run(
    arguments: Sequence[str],
    *,
    timeout: float,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[bytes]:
    try:
        # Keep tool output off the Python heap.  Tesseract and ffprobe are
        # local, trusted programs, but a malformed source must not make their
        # diagnostics consume unbounded RAM before the size check runs.
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            completed = subprocess.run(
                list(arguments),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
                pass_fds=tuple(pass_fds),
            )
            stdout_size = os.fstat(stdout_file.fileno()).st_size
            stderr_size = os.fstat(stderr_file.fileno()).st_size
            if (
                stdout_size > MAX_TOOL_OUTPUT_BYTES
                or stderr_size > MAX_TOOL_OUTPUT_BYTES
            ):
                raise PreflightError("local inspection diagnostics exceeded the safe limit")
            stdout_file.seek(0)
            stderr_file.seek(0)
            result = subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                stdout_file.read(),
                stderr_file.read(),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError("a bounded local inspection tool failed to run") from exc
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", "replace").strip()[-800:]
        raise PreflightError(
            "a local inspection tool rejected the reference"
            + (f": {diagnostic}" if diagnostic else "")
        )
    return result


def _tool(name: str) -> str:
    result = shutil.which(name)
    if result is None:
        raise PreflightError(f"required inspection tool is unavailable: {name}")
    return result


def _available_languages(tesseract: str) -> set[str]:
    result = _run([tesseract, "--list-langs"], timeout=15)
    return {
        line.strip()
        for line in result.stdout.decode("utf-8", "replace").splitlines()
        if line.strip() and not line.lower().startswith("list of available")
    }


def _probe(ffprobe: str, fd_path: str, fd: int) -> dict[str, Any]:
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            fd_path,
        ],
        timeout=30,
        pass_fds=(fd,),
    )
    try:
        document = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("ffprobe returned invalid JSON") from exc
    streams = document.get("streams") if isinstance(document, Mapping) else None
    if not isinstance(streams, list):
        raise PreflightError("reference has no decodable video stream")
    videos = [
        item
        for item in streams
        if isinstance(item, Mapping) and item.get("codec_type") == "video"
    ]
    if len(videos) != 1:
        raise PreflightError("reference must contain exactly one video stream")
    video = videos[0]
    try:
        width = int(video.get("width"))
        height = int(video.get("height"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise PreflightError("reference has invalid video dimensions") from exc
    if width < 32 or height < 32 or width > 8192 or height > 8192:
        raise PreflightError("reference dimensions are outside the safe inspection range")
    duration_value = video.get("duration")
    if duration_value in {None, "N/A"} and isinstance(document.get("format"), Mapping):
        duration_value = document["format"].get("duration")
    duration = _finite_float(duration_value, "duration")
    if duration < MIN_REFERENCE_SECONDS:
        raise PreflightError("reference is shorter than the 2 second H3 upload window")
    if duration > MAX_REFERENCE_SECONDS:
        raise PreflightError(
            "reference is longer than the H3 upload window; cut the exact 2–15 second segment first"
        )
    return {"width": width, "height": height, "duration": duration}


def _sha256(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _meaningful_text(text: str) -> tuple[str, int, bool]:
    characters = TEXT_CHARACTER.findall(text)
    normalized = "".join(characters)
    return normalized, len(characters), bool(CJK_CHARACTER.search(normalized))


def _expected_sample_count(duration: float, sample_fps: float) -> int:
    expected = max(1, math.ceil(duration * sample_fps))
    if expected < 2:
        raise PreflightError(
            "sample rate and duration provide fewer than two OCR observations; "
            "increase --sample-fps"
        )
    return expected


def candidates_from_tsv(
    tsv: str,
    *,
    frame_width: int,
    crop_y: int,
    minimum_confidence: float,
    coordinate_scale: int = 1,
) -> list[TextCandidate]:
    """Return conservative caption-like OCR lines from one Tesseract TSV."""

    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    try:
        rows: Iterable[Mapping[str, str]] = csv.DictReader(
            tsv.splitlines(), delimiter="\t"
        )
        for row in rows:
            text = str(row.get("text") or "").strip()
            normalized, count, _ = _meaningful_text(text)
            if not normalized or count == 0:
                continue
            try:
                confidence = float(row.get("conf") or -1)
                left = int(row.get("left") or 0)
                top = int(row.get("top") or 0)
                width = int(row.get("width") or 0)
                height = int(row.get("height") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if confidence < minimum_confidence or width <= 0 or height <= 0:
                continue
            key = tuple(str(row.get(name) or "0") for name in (
                "page_num",
                "block_num",
                "par_num",
                "line_num",
            ))
            groups[key].append(
                {
                    "text": text,
                    "confidence": confidence,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                }
            )
    except csv.Error as exc:
        raise PreflightError("Tesseract returned invalid TSV") from exc

    candidates: list[TextCandidate] = []
    for words in groups.values():
        words.sort(key=lambda item: item["left"])
        text = " ".join(str(item["text"]) for item in words)
        normalized, character_count, has_cjk = _meaningful_text(text)
        # Two CJK characters can be a real short subtitle (for example “来了”).
        # Latin-only noise needs four characters to avoid blocking on one speck.
        if character_count < (2 if has_cjk else 4):
            continue
        left = round(min(int(item["left"]) for item in words) / coordinate_scale)
        top = round(min(int(item["top"]) for item in words) / coordinate_scale)
        right = round(
            max(int(item["left"]) + int(item["width"]) for item in words)
            / coordinate_scale
        )
        bottom = round(
            max(int(item["top"]) + int(item["height"]) for item in words)
            / coordinate_scale
        )
        if right - left < max(8, round(frame_width * 0.025)):
            continue
        weighted_area = sum(max(1, int(item["width"]) * int(item["height"])) for item in words)
        confidence = sum(
            float(item["confidence"])
            * max(1, int(item["width"]) * int(item["height"]))
            for item in words
        ) / weighted_area
        candidates.append(
            TextCandidate(
                text=normalized,
                confidence=confidence,
                x=left,
                y=crop_y + top,
                width=right - left,
                height=bottom - top,
            )
        )
    return sorted(candidates, key=lambda item: (item.y, item.x, item.text))


def persistent_candidate_pair(
    previous: Sequence[TextCandidate],
    current: Sequence[TextCandidate],
    *,
    frame_width: int,
) -> tuple[TextCandidate, TextCandidate] | None:
    """Find subtitle-like text that persists in adjacent sampled frames."""

    for before in previous:
        for after in current:
            before_center_y = before.y + before.height / 2
            after_center_y = after.y + after.height / 2
            if abs(before_center_y - after_center_y) > max(
                32, before.height * 1.5, after.height * 1.5
            ):
                continue
            overlap = max(
                0,
                min(before.x + before.width, after.x + after.width)
                - max(before.x, after.x),
            )
            before_center_x = before.x + before.width / 2
            after_center_x = after.x + after.width / 2
            if overlap < 0.2 * min(before.width, after.width) and abs(
                before_center_x - after_center_x
            ) > frame_width * 0.15:
                continue
            # Stable short Chinese captions can consist entirely of common
            # characters (for example “一个人”).  Exact adjacent recognition
            # at the same position is stronger evidence than the noise-word
            # suppression below and must fail closed.
            stable_geometry = (
                overlap >= 0.5 * min(before.width, after.width)
                and abs(before_center_x - after_center_x)
                <= max(24, frame_width * 0.04)
                and abs(before_center_y - after_center_y)
                <= max(12, min(before.height, after.height) * 0.75)
            )
            if before.text == after.text and stable_geometry:
                return before, after
            if stable_geometry and all(
                CJK_CHARACTER.fullmatch(character)
                for character in before.text + after.text
            ):
                shared_characters = sum(
                    (Counter(before.text) & Counter(after.text)).values()
                )
                required_shared = max(
                    2, math.ceil(0.5 * min(len(before.text), len(after.text)))
                )
                if shared_characters >= required_shared:
                    return before, after
            common = set(before.text) & set(after.text)
            meaningful_common = common - COMMON_OCR_NOISE
            both_include_cjk = bool(
                CJK_CHARACTER.search(before.text)
                and CJK_CHARACTER.search(after.text)
            )
            if (both_include_cjk and meaningful_common) or (
                not both_include_cjk and len(meaningful_common) >= 2
            ):
                return before, after
    return None


def _dedupe_candidates(candidates: Iterable[TextCandidate]) -> list[TextCandidate]:
    unique: dict[tuple[str, int, int, int, int], TextCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.text,
            candidate.x,
            candidate.y,
            candidate.width,
            candidate.height,
        )
        current = unique.get(key)
        if current is None or candidate.confidence > current.confidence:
            unique[key] = candidate
    return sorted(unique.values(), key=lambda item: (item.y, item.x, item.text))


def _persistent_finding(
    samples: Sequence[Sequence[TextCandidate]],
    *,
    frame_width: int,
    sample_fps: float,
) -> dict[str, Any] | None:
    for index in range(1, len(samples)):
        persistent = persistent_candidate_pair(
            samples[index - 1],
            samples[index],
            frame_width=frame_width,
        )
        if persistent is None:
            continue
        before_candidate, after_candidate = persistent
        return {
            "sample_index": index + 1,
            "time_seconds_approx": round(index / sample_fps, 3),
            "candidates": [item.as_dict() for item in samples[index]],
            "persistence": {
                "previous_sample_index": index,
                "previous": before_candidate.as_dict(),
                "current": after_candidate.as_dict(),
            },
        }
    return None


def inspect_reference(
    path: Path,
    *,
    languages: str,
    sample_fps: float,
    bottom_fraction: float,
    minimum_confidence: float,
) -> dict[str, Any]:
    ffmpeg = _tool("ffmpeg")
    ffprobe = _tool("ffprobe")
    tesseract = _tool("tesseract")
    requested_languages = [item for item in languages.split("+") if item]
    available = _available_languages(tesseract)
    missing = sorted(set(requested_languages) - available)
    if not requested_languages or missing:
        detail = ", ".join(missing) if missing else "none requested"
        raise PreflightError(f"required Tesseract language data is unavailable: {detail}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PreflightError(f"could not safely open reference: {path}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PreflightError("reference must be a regular file")
        if before.st_size <= 0 or before.st_size > MAX_REFERENCE_BYTES:
            raise PreflightError("reference byte size is outside the safe inspection range")
        fd_path = f"/proc/self/fd/{fd}"
        media = _probe(ffprobe, fd_path, fd)
        source_sha256 = _sha256(fd)
        frame_height = int(media["height"])
        crop_height = max(32, math.ceil(frame_height * bottom_fraction))
        crop_y = frame_height - crop_height
        band_height = max(
            32,
            min(crop_height, math.ceil(frame_height * OCR_BAND_FRACTION)),
        )
        last_band_y = frame_height - band_height
        band_step = max(1, band_height // 2)
        band_ys = {crop_y, last_band_y}
        band_y = crop_y
        while band_y < last_band_y:
            band_ys.add(band_y)
            band_y += band_step
        band_ys = sorted(band_ys)
        with tempfile.TemporaryDirectory(prefix="h3-reference-text-") as temporary:
            expected_samples = _expected_sample_count(
                float(media["duration"]), sample_fps
            )
            scaled_sample_pixels = (
                expected_samples
                * len(band_ys)
                * int(media["width"])
                * band_height
                * OCR_SCALE
                * OCR_SCALE
            )
            maximum_invocations = (
                expected_samples
                * len(band_ys)
                * len(requested_languages)
                * len(OCR_PAGE_SEGMENTATION_MODES)
            )
            if scaled_sample_pixels > MAX_SCALED_SAMPLE_PIXELS:
                raise PreflightError(
                    "requested OCR scan exceeds the scaled-pixel budget; reduce sample rate, "
                    "scan region, duration, or reference resolution"
                )
            if maximum_invocations > MAX_OCR_INVOCATIONS:
                raise PreflightError(
                    "requested OCR scan exceeds the invocation budget; reduce sample rate, "
                    "scan region, duration, or requested languages"
                )
            sample_candidates: list[list[TextCandidate]] | None = None
            sample_count: int | None = None
            findings: list[dict[str, Any]] = []
            bands_examined = 0
            samples_examined = 0
            for band_index, band_y in enumerate(band_ys):
                output_pattern = str(
                    Path(temporary) / f"band-{band_index}-frame-%05d.png"
                )
                _run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-nostdin",
                        "-loglevel",
                        "error",
                        "-i",
                        fd_path,
                        "-map",
                        "0:v:0",
                        "-an",
                        "-vf",
                        (
                            f"fps={sample_fps:g},crop=iw:{band_height}:0:{band_y},"
                            f"scale=iw*{OCR_SCALE}:ih*{OCR_SCALE}:flags=lanczos,"
                            "format=gray,eq=contrast=1.35"
                        ),
                        "-frames:v",
                        str(expected_samples),
                        output_pattern,
                    ],
                    timeout=90,
                    pass_fds=(fd,),
                )
                extracted = sorted(
                    Path(temporary).glob(f"band-{band_index}-frame-*.png")
                )
                if not extracted:
                    raise PreflightError("reference produced no OCR sample frames")
                extracted_count = len(extracted)
                if extracted_count != expected_samples:
                    raise PreflightError(
                        "reference decode did not cover the declared media duration"
                    )
                if sample_count is None:
                    sample_count = extracted_count
                    sample_candidates = [[] for _ in range(sample_count)]
                elif extracted_count != sample_count:
                    raise PreflightError(
                        "reference OCR bands produced inconsistent temporal coverage"
                    )
                assert sample_candidates is not None
                bands_examined += 1
                for index, frame in enumerate(extracted):
                    samples_examined = max(samples_examined, index + 1)
                    candidates: list[TextCandidate] = []
                    for language in requested_languages:
                        for page_mode in OCR_PAGE_SEGMENTATION_MODES:
                            result = _run(
                                [
                                    tesseract,
                                    str(frame),
                                    "stdout",
                                    "-l",
                                    language,
                                    "--psm",
                                    str(page_mode),
                                    "tsv",
                                ],
                                timeout=30,
                            )
                            candidates.extend(
                                candidates_from_tsv(
                                    result.stdout.decode("utf-8", "replace"),
                                    frame_width=int(media["width"]),
                                    crop_y=band_y,
                                    minimum_confidence=minimum_confidence,
                                    coordinate_scale=OCR_SCALE,
                                )
                            )
                    frame.unlink()
                    sample_candidates[index] = _dedupe_candidates(
                        [*sample_candidates[index], *candidates]
                    )
                    if len(sample_candidates[index]) > MAX_CANDIDATES_PER_SAMPLE:
                        raise PreflightError(
                            "OCR returned too many text candidates for a conclusive scan"
                        )
                    finding = _persistent_finding(
                        sample_candidates[: index + 1],
                        frame_width=int(media["width"]),
                        sample_fps=sample_fps,
                    )
                    if finding is not None:
                        findings.append(finding)
                        # Two adjacent observations are enough to fail closed.
                        # This rejects stable caption text without treating one
                        # isolated OCR hallucination as a subtitle.
                        break
                if findings:
                    break
            if sample_count is None or sample_candidates is None:
                raise PreflightError("reference produced no OCR sample frames")
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PreflightError("reference changed during inspection")
    finally:
        os.close(fd)

    return {
        "schema": "localvideogen.reference-subtitle-preflight.v1",
        "source": str(path),
        "source_sha256": source_sha256,
        "media": media,
        "scan": {
            "languages": requested_languages,
            "sample_fps": sample_fps,
            "bottom_fraction": bottom_fraction,
            "minimum_confidence": minimum_confidence,
            "page_segmentation_modes": list(OCR_PAGE_SEGMENTATION_MODES),
            "preprocessing": {
                "scale": OCR_SCALE,
                "grayscale": True,
                "contrast": 1.35,
                "band_fraction": OCR_BAND_FRACTION,
                "band_y_positions": band_ys,
                "band_height": band_height,
            },
            "budgets": {
                "reference_bytes_limit": MAX_REFERENCE_BYTES,
                "scaled_sample_pixels": scaled_sample_pixels,
                "scaled_sample_pixels_limit": MAX_SCALED_SAMPLE_PIXELS,
                "maximum_ocr_invocations": maximum_invocations,
                "ocr_invocations_limit": MAX_OCR_INVOCATIONS,
            },
            "samples_extracted": sample_count,
            "samples_examined": samples_examined,
            "expected_samples": expected_samples,
            "bands_examined": bands_examined,
        },
        "subtitle_like_text_detected": bool(findings),
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Conservatively OCR the lower caption region of one 2–15 second H3 "
            "reference video. The source is never modified."
        )
    )
    parser.add_argument("video", type=Path)
    parser.add_argument(
        "--declare-subtitle-free",
        action="store_true",
        help="required operator declaration that the selected source should contain no burned subtitles",
    )
    parser.add_argument("--languages", default=DEFAULT_LANGUAGES)
    parser.add_argument("--sample-fps", type=float, default=3.0)
    parser.add_argument("--bottom-fraction", type=float, default=0.45)
    parser.add_argument("--minimum-confidence", type=float, default=55.0)
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print the JSON report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    if not options.declare_subtitle_free:
        print(
            "INCONCLUSIVE: pass --declare-subtitle-free only after selecting a source that should contain no burned subtitles.",
            file=sys.stderr,
        )
        return 2
    if not 0.25 <= options.sample_fps <= 12:
        print("INCONCLUSIVE: --sample-fps must be between 0.25 and 12.", file=sys.stderr)
        return 2
    if not 0.20 <= options.bottom_fraction <= 1.0:
        print("INCONCLUSIVE: --bottom-fraction must be between 0.20 and 1.0.", file=sys.stderr)
        return 2
    if not 0 <= options.minimum_confidence <= 100:
        print("INCONCLUSIVE: --minimum-confidence must be between 0 and 100.", file=sys.stderr)
        return 2
    try:
        report = inspect_reference(
            options.video,
            languages=options.languages,
            sample_fps=options.sample_fps,
            bottom_fraction=options.bottom_fraction,
            minimum_confidence=options.minimum_confidence,
        )
    except PreflightError as exc:
        print(f"INCONCLUSIVE: {exc}", file=sys.stderr)
        return 2
    report["operator_declared_subtitle_free"] = True
    report["result"] = (
        "blocked_text_detected"
        if report["subtitle_like_text_detected"]
        else "clean"
    )
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            indent=2 if options.pretty else None,
            sort_keys=True,
        )
    )
    if report["subtitle_like_text_detected"]:
        print(
            "BLOCKED: caption-like text was detected; pre-clean the exact reference segment before upload.",
            file=sys.stderr,
        )
        return 1
    print(
        "PASS: declared subtitle-free and no caption-like text was detected in the sampled lower region.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
