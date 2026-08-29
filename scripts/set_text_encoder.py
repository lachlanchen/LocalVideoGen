#!/usr/bin/env python3
"""Switch all LocalVideoGen CLIPLoader workflows between known encoders."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "text-encoder-selection.json"
MODEL_ROOT = PROJECT_ROOT / "ComfyUI" / "models" / "text_encoders"
PREPARE_SCRIPT = PROJECT_ROOT / "scripts" / "prepare_workflows.py"
VALIDATE_SCRIPT = PROJECT_ROOT / "scripts" / "validate_workflows.py"


def load_config() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    active = config.get("active")
    profiles = config.get("profiles")
    if (
        not isinstance(active, str)
        or not isinstance(profiles, dict)
        or active not in profiles
    ):
        raise RuntimeError(f"invalid selector config: {CONFIG_PATH}")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not isinstance(profile, dict):
            raise RuntimeError(f"invalid selector profile in {CONFIG_PATH}")
        if not isinstance(profile.get("filename"), str) or not isinstance(
            profile.get("sha256"), str
        ):
            raise RuntimeError(f"invalid selector profile {name!r} in {CONFIG_PATH}")
    return config


def model_path(profile: dict[str, str]) -> Path:
    path = (MODEL_ROOT / profile["filename"]).resolve()
    if path.parent != MODEL_ROOT.resolve():
        raise RuntimeError("text encoder filename escapes the model directory")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_profile(name: str, profile: dict[str, str]) -> Path:
    path = model_path(profile)
    if not path.is_file():
        raise RuntimeError(f"{name} encoder is missing: {path}")
    actual = sha256(path)
    if actual != profile["sha256"]:
        raise RuntimeError(
            f"{name} encoder checksum mismatch: expected {profile['sha256']}, got {actual}"
        )
    return path


def write_config(config: dict[str, Any]) -> None:
    payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=CONFIG_PATH.parent,
        prefix=".text-encoder-",
        delete=False,
    ) as target:
        temporary_path = Path(target.name)
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary_path, CONFIG_PATH)


def regenerate() -> None:
    subprocess.run([sys.executable, str(PREPARE_SCRIPT)], cwd=PROJECT_ROOT, check=True)
    subprocess.run([sys.executable, str(VALIDATE_SCRIPT)], cwd=PROJECT_ROOT, check=True)


def status(config: dict[str, Any]) -> None:
    print(f"active={config['active']}")
    for name, profile in config["profiles"].items():
        path = model_path(profile)
        marker = "*" if name == config["active"] else " "
        availability = "available" if path.is_file() else "missing"
        print(f"{marker} {name}: {profile['filename']} ({availability})")


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=[*sorted(config["profiles"]), "status"])
    args = parser.parse_args()

    if args.profile == "status":
        status(config)
        return 0

    selected = config["profiles"][args.profile]
    path = verify_profile(args.profile, selected)
    original = CONFIG_PATH.read_bytes()
    config["active"] = args.profile
    try:
        write_config(config)
        regenerate()
    except Exception:
        CONFIG_PATH.write_bytes(original)
        regenerate()
        raise

    print(f"active={args.profile}")
    print(f"clip_name={selected['filename']}")
    print(f"verified={path}")
    print(
        "Restart H3 Studio if it is already running; ComfyUI itself does not need a restart."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
