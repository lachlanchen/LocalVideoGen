#!/usr/bin/env python3
"""Build an aria2 queue that excludes already validated model files."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BOOTSTRAP_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_PYTHON = BOOTSTRAP_ROOT / ".venv" / "bin" / "python"
if Path(sys.prefix).resolve() != (BOOTSTRAP_ROOT / ".venv").resolve():
    if not BOOTSTRAP_PYTHON.is_file():
        raise SystemExit(f"Missing project interpreter: {BOOTSTRAP_PYTHON}")
    os.execv(str(BOOTSTRAP_PYTHON), [str(BOOTSTRAP_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from inventory_models import EXPECTED, PROJECT_ROOT, aligned_policy_violations, inspect


SOURCE = PROJECT_ROOT / "config" / "modelscope-downloads.txt"
DESTINATION = PROJECT_ROOT / "runtime" / "model-download-queue.txt"
MODELSCOPE_BASE = "https://www.modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master/"


def blocks(path: Path) -> list[list[str]]:
    result: list[list[str]] = []
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and current:
            result.append(current)
            current = []
        if line:
            current.append(line)
    if current:
        result.append(current)
    return result


def option(block: list[str], name: str) -> str:
    prefix = f"{name}="
    values = [line.strip()[len(prefix) :] for line in block[1:] if line.strip().startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"download block must contain exactly one non-empty {name}= option")
    return values[0]


def relative_path(block: list[str]) -> str:
    relative = (Path(option(block, "dir")) / option(block, "out")).as_posix()
    if relative not in EXPECTED:
        raise ValueError(f"download destination is not in the trusted aligned manifest: {relative}")
    return relative


def validate_blocks(download_blocks: list[list[str]], *, complete_set: bool) -> None:
    seen: set[str] = set()
    for block in download_blocks:
        if not block or block[0].startswith((" ", "\t")):
            raise ValueError("download block does not start with a URL")
        relative = relative_path(block)
        if relative in seen:
            raise ValueError(f"duplicate download destination: {relative}")
        seen.add(relative)
        spec = EXPECTED[relative]
        if option(block, "checksum") != f"sha-256={spec.sha256}":
            raise ValueError(f"download checksum is not trusted for {relative}")
        repository_path = relative.removeprefix("ComfyUI/models/")
        if block[0] != MODELSCOPE_BASE + repository_path:
            raise ValueError(f"download URL is not the pinned Comfy-Org ModelScope path for {relative}")
    if complete_set and seen != set(EXPECTED):
        missing = sorted(set(EXPECTED) - seen)
        unexpected = sorted(seen - set(EXPECTED))
        raise ValueError(f"download source is not the exact nine-file set: missing={missing}, unexpected={unexpected}")


def remaining_bytes(download_blocks: list[list[str]]) -> int:
    remaining = 0
    for block in download_blocks:
        relative = relative_path(block)
        destination = PROJECT_ROOT / relative
        expected_size = EXPECTED[relative].size
        allocated = 0
        if destination.exists():
            status = destination.stat()
            allocated = min(expected_size, status.st_size, status.st_blocks * 512)
        remaining += expected_size - allocated
    return remaining


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--remaining-bytes",
        action="store_true",
        help="print estimated unallocated payload bytes for the already-built queue without rewriting it",
    )
    args = parser.parse_args()

    if args.remaining_bytes:
        queued_blocks = blocks(DESTINATION)
        validate_blocks(queued_blocks, complete_set=False)
        print(remaining_bytes(queued_blocks))
        return 0

    source_blocks = blocks(SOURCE)
    validate_blocks(source_blocks, complete_set=True)
    violations = aligned_policy_violations()
    if violations:
        names = ", ".join(str(path.relative_to(PROJECT_ROOT)) for path in violations)
        raise SystemExit(f"Refusing aligned-only download while unaligned encoder files exist: {names}")

    queued: list[list[str]] = []
    for block in source_blocks:
        destination = PROJECT_ROOT / relative_path(block)
        control = destination.with_name(destination.name + ".aria2")
        if destination.exists() and not control.exists():
            try:
                summary = inspect(destination)
            except (OSError, ValueError) as error:
                raise SystemExit(f"Refusing to overwrite invalid completed file {destination}: {error}")
            print(f"SKIP {summary}")
        else:
            queued.append(block)
            print(f"QUEUE {destination.relative_to(PROJECT_ROOT)}")

    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join("\n".join(block) for block in queued)
    if text:
        text += "\n"
    DESTINATION.write_text(text, encoding="utf-8")
    print(f"Queued {len(queued)} model file(s).")
    print(f"Estimated remaining payload: {remaining_bytes(queued):,} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
