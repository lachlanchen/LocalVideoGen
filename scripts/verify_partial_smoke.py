#!/usr/bin/env python3
"""Verify the fixed official model subset used by an early H3 test render.

This deliberately does not create ``runtime/models.verified``: unrelated H3
weights may still be incomplete, so this receipt must never be confused with a
verified full bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
if Path(sys.prefix).resolve() != (PROJECT_ROOT / ".venv").resolve():
    if not PROJECT_PYTHON.is_file():
        raise SystemExit(f"Missing project interpreter: {PROJECT_PYTHON}")
    os.execv(str(PROJECT_PYTHON), [str(PROJECT_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from inventory_models import EXPECTED, aligned_policy_violations, inspect  # noqa: E402


PROFILE = "fl-int8-turbo"
REQUIRED = (
    "ComfyUI/models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "ComfyUI/models/vae/minimax_h3_video_vae_fp16.safetensors",
    "ComfyUI/models/vae/minimax_h3_audio_vae_fp32.safetensors",
    "ComfyUI/models/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
)


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(32 * 1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def verify() -> None:
    violations = aligned_policy_violations()
    if violations:
        names = ", ".join(str(path.relative_to(PROJECT_ROOT)) for path in violations)
        raise RuntimeError(f"unaligned encoder file(s) present: {names}")
    for relative in REQUIRED:
        specification = EXPECTED.get(relative)
        if specification is None:
            raise RuntimeError(f"required path is absent from the pinned manifest: {relative}")
        path = PROJECT_ROOT / relative
        control = path.with_name(path.name + ".aria2")
        if control.exists():
            raise RuntimeError(f"required file is still downloading: {relative}")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"required regular file is missing: {relative}")
        if path.stat().st_size != specification.size:
            raise RuntimeError(f"required file has the wrong size: {relative}")
        summary = inspect(path)
        actual = digest(path)
        if actual != specification.sha256:
            raise RuntimeError(f"required file failed SHA-256 verification: {relative}")
        print(f"VERIFIED {summary}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=(PROFILE,), required=True)
    parser.parse_args()
    try:
        verify()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Aligned FL INT8 Turbo subset verified; full-bundle receipt was not created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
