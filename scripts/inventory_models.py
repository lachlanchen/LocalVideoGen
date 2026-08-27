#!/usr/bin/env python3
"""Validate safetensors structure without loading tensor payloads."""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
if __name__ == "__main__" and Path(sys.prefix).resolve() != (PROJECT_ROOT / ".venv").resolve():
    if not VENV_PYTHON.is_file():
        raise SystemExit(f"Missing project interpreter: {VENV_PYTHON}")
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from safetensors import SafetensorError, safe_open


MANIFEST = PROJECT_ROOT / "config" / "model-manifest.sha256"


@dataclass(frozen=True)
class ModelSpec:
    sha256: str
    size: int
    header_size: int
    tensor_count: int
    dtypes: dict[str, int]


EXPECTED: dict[str, ModelSpec] = {
    "ComfyUI/models/vae/minimax_h3_audio_vae_fp32.safetensors": ModelSpec(
        "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
        605_254_808,
        105_520,
        917,
        {"F32": 917},
    ),
    "ComfyUI/models/vae/minimax_h3_video_vae_fp16.safetensors": ModelSpec(
        "7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522",
        5_207_808_496,
        66_328,
        562,
        {"F16": 562},
    ),
    "ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": ModelSpec(
        "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6",
        15_687_142_551,
        231_400,
        2_054,
        {"F32": 351, "BF16": 651, "F8_E4M3": 350, "I8": 1, "U8": 701},
    ),
    "ComfyUI/models/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors": ModelSpec(
        "2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e",
        1_956_193_000,
        73_632,
        624,
        {"F32": 208, "BF16": 416},
    ),
    "ComfyUI/models/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors": ModelSpec(
        "5b9ab5ade15d0775676d01a907268a69a1468dc6033b3b0d3ded5502f3ebb84c",
        1_956_193_000,
        73_632,
        624,
        {"F32": 208, "BF16": 416},
    ),
    "ComfyUI/models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors": ModelSpec(
        "e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a",
        20_970_379_616,
        95_416,
        932,
        {"F32": 210, "BF16": 220, "F16": 102, "I8": 200, "U8": 200},
    ),
    "ComfyUI/models/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors": ModelSpec(
        "9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779",
        20_970_379_616,
        95_416,
        932,
        {"F32": 210, "BF16": 220, "F16": 102, "I8": 200, "U8": 200},
    ),
    "ComfyUI/models/diffusion_models/minimax_h3_fl2va_pruned_bf16.safetensors": ModelSpec(
        "a32572fb90b5508b201ec7c2eddcc184b13ddfd3c6f6d2cf06a0b46535d541b4",
        40_225_724_176,
        55_976,
        532,
        {"F32": 10, "BF16": 420, "F16": 102},
    ),
    "ComfyUI/models/diffusion_models/minimax_h3_ref2va_pruned_bf16.safetensors": ModelSpec(
        "37c0da793e20ca735272ec2be655f08a2e10f97a3ec8fdfb40f5b39a736ed6fe",
        40_225_724_176,
        55_976,
        532,
        {"F32": 10, "BF16": 420, "F16": 102},
    ),
}

DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "U16": 2,
    "I16": 2,
    "F16": 2,
    "BF16": 2,
    "U32": 4,
    "I32": 4,
    "F32": 4,
    "U64": 8,
    "I64": 8,
    "F64": 8,
}

UNALIGNED_NAME_MARKERS = ("heretic", "abliterat")


def manifest_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, line in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            raise ValueError(f"manifest line {line_number}: expected SHA-256 and relative path")
        digest, relative = fields
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"manifest line {line_number}: invalid lowercase SHA-256")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.as_posix() != relative:
            raise ValueError(f"manifest line {line_number}: unsafe or non-canonical path {relative!r}")
        if relative in entries:
            raise ValueError(f"manifest line {line_number}: duplicate path {relative}")
        entries[relative] = digest

    trusted = {relative: spec.sha256 for relative, spec in EXPECTED.items()}
    if entries != trusted:
        missing = sorted(set(trusted) - set(entries))
        unexpected = sorted(set(entries) - set(trusted))
        changed = sorted(
            relative for relative in set(entries) & set(trusted) if entries[relative] != trusted[relative]
        )
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unexpected:
            details.append(f"unexpected={unexpected}")
        if changed:
            details.append(f"wrong_checksum={changed}")
        raise ValueError("manifest is not the trusted aligned nine-file set: " + "; ".join(details))
    return entries


def manifest_paths() -> list[Path]:
    return [PROJECT_ROOT / relative for relative in manifest_entries()]


def model_key(path: Path) -> str:
    try:
        relative = path.resolve(strict=True).relative_to(PROJECT_ROOT.resolve()).as_posix()
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"model path is not a regular project file: {path}") from error
    if relative not in EXPECTED:
        raise ValueError(f"model path is not in the trusted aligned manifest: {relative}")
    return relative


def aligned_policy_violations() -> list[Path]:
    encoder_root = PROJECT_ROOT / "ComfyUI" / "models" / "text_encoders"
    if not encoder_root.is_dir():
        return []
    return sorted(
        path
        for path in encoder_root.rglob("*")
        if path.is_file()
        and any(
            marker in path.relative_to(encoder_root).as_posix().casefold()
            for marker in UNALIGNED_NAME_MARKERS
        )
    )


def read_header(path: Path, expected_header_size: int | None = None) -> tuple[dict[str, Any], int]:
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ValueError("file is shorter than the safetensors prefix")
        header_length = struct.unpack("<Q", prefix)[0]
        if header_length == 0 or header_length > 1024**3:
            raise ValueError(f"implausible header length: {header_length}")
        if expected_header_size is not None and header_length != expected_header_size:
            raise ValueError(f"header is {header_length:,}, expected {expected_header_size:,} bytes")
        raw_header = stream.read(header_length)
        if len(raw_header) != header_length:
            raise ValueError("truncated safetensors header")
    header = json.loads(raw_header)
    if not isinstance(header, dict):
        raise ValueError("safetensors header root is not an object")
    metadata = header.get("__metadata__")
    if metadata is not None and (
        not isinstance(metadata, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in metadata.items())
    ):
        raise ValueError("__metadata__ is not a string-to-string object")
    return header, header_length


def inspect(path: Path) -> str:
    relative = model_key(path)
    spec = EXPECTED[relative]
    file_size = path.stat().st_size
    if file_size != spec.size:
        raise ValueError(f"size is {file_size:,}, expected {spec.size:,}")
    header, header_length = read_header(path, spec.header_size)
    tensors = {key: value for key, value in header.items() if key != "__metadata__"}
    ranges: list[tuple[int, int, str]] = []
    dtypes: Counter[str] = Counter()
    elements = 0

    for key, value in tensors.items():
        if not isinstance(value, dict):
            raise ValueError(f"{key}: tensor record is not an object")
        dtype = value.get("dtype")
        shape = value.get("shape")
        offsets = value.get("data_offsets")
        if not isinstance(dtype, str):
            raise ValueError(f"{key}: invalid dtype")
        if not isinstance(shape, list) or not all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in shape
        ):
            raise ValueError(f"{key}: invalid shape")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in offsets)
            or offsets[0] > offsets[1]
        ):
            raise ValueError(f"{key}: invalid data offsets")
        tensor_elements = 1
        for dimension in shape:
            tensor_elements *= dimension
        element_size = DTYPE_BYTES.get(dtype)
        if element_size is None:
            raise ValueError(f"{key}: unsupported dtype {dtype!r}")
        stored_bytes = offsets[1] - offsets[0]
        expected_bytes = tensor_elements * element_size
        if stored_bytes != expected_bytes:
            raise ValueError(
                f"{key}: payload is {stored_bytes:,} bytes, shape/dtype require {expected_bytes:,}"
            )
        elements += tensor_elements
        dtypes[dtype] += 1
        ranges.append((offsets[0], offsets[1], key))

    ranges.sort()
    data_size = file_size - 8 - header_length
    if data_size < 0:
        raise ValueError("header extends beyond file")
    cursor = 0
    for start, end, key in ranges:
        if start != cursor:
            raise ValueError(f"{key}: payload gap or overlap at byte {cursor:,}")
        if end > data_size:
            raise ValueError(f"{key}: payload extends beyond file")
        cursor = end
    if cursor != data_size:
        raise ValueError(f"payload covers {cursor:,} of {data_size:,} bytes")

    try:
        with safe_open(str(path), framework="numpy") as safe_file:
            library_keys = set(safe_file.keys())
            safe_file.metadata()
    except SafetensorError as error:
        raise ValueError(f"safetensors library rejected the file: {error}") from error
    if library_keys != set(tensors):
        raise ValueError("safetensors library tensor keys disagree with parsed header")

    if len(tensors) != spec.tensor_count:
        raise ValueError(f"found {len(tensors):,} tensors, expected {spec.tensor_count:,}")
    if dict(dtypes) != spec.dtypes:
        raise ValueError(f"dtype histogram is {dict(dtypes)}, expected {spec.dtypes}")

    dtype_text = ", ".join(f"{name}:{count}" for name, count in sorted(dtypes.items()))
    return (
        f"{relative} | {file_size / 1024**3:.2f} GiB | "
        f"header {header_length:,} B | {len(tensors):,} tensors | {elements:,} stored elements | {dtype_text}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="inspect completed files and report missing/in-progress files without failing",
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        metavar="RELATIVE_PATH",
        help="inspect only this exact manifest path (repeatable); the full manifest is still authenticated",
    )
    args = parser.parse_args()

    try:
        paths = manifest_paths()
    except (OSError, ValueError) as error:
        print(f"INVALID MANIFEST: {error}")
        return 1

    if args.paths:
        unknown = sorted(set(args.paths) - set(EXPECTED))
        if unknown:
            print(f"INVALID SELECTION: paths are not in the trusted aligned manifest: {unknown}")
            return 1
        selected = set(args.paths)
        paths = [path for path in paths if path.relative_to(PROJECT_ROOT).as_posix() in selected]

    invalid = False
    unavailable = False
    violations = aligned_policy_violations()
    for path in violations:
        print(f"UNALIGNED {path.relative_to(PROJECT_ROOT)}")
        invalid = True

    for path in paths:
        if not path.exists():
            print(f"MISSING {path.relative_to(PROJECT_ROOT)}")
            unavailable = True
            continue
        if path.with_name(path.name + ".aria2").exists():
            print(f"IN PROGRESS {path.relative_to(PROJECT_ROOT)}")
            unavailable = True
            continue
        try:
            print(f"OK {inspect(path)}")
        except (OSError, ValueError, json.JSONDecodeError, SafetensorError) as error:
            print(f"INVALID {path.relative_to(PROJECT_ROOT)}: {error}")
            invalid = True

    if invalid:
        return 1
    if unavailable and not args.allow_missing:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
