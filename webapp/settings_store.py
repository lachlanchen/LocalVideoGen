"""Small private preferences store independent of disposable render history."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Final


SETTINGS_VERSION: Final = 1
MAX_SYSTEM_PROMPT_CHARS: Final = 2_000
MIN_DURATION: Final = 2.0
MAX_DURATION: Final = 15.0
_UNSET: Final = object()


class SettingsStoreError(RuntimeError):
    """The private settings file cannot be safely read or written."""


class SettingsStoreValidationError(ValueError):
    """A settings update contains an invalid value."""


def _validated_system_prompt(value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsStoreValidationError("always-remember requirements must be text")
    prompt = value.strip()
    if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
        raise SettingsStoreValidationError(
            f"always-remember requirements are longer than {MAX_SYSTEM_PROMPT_CHARS} characters"
        )
    return prompt


def _validated_duration(value: Any) -> float:
    if isinstance(value, bool):
        raise SettingsStoreValidationError("preferred video length must be a number")
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SettingsStoreValidationError("preferred video length must be a number") from exc
    if not math.isfinite(duration) or not MIN_DURATION <= duration <= MAX_DURATION:
        raise SettingsStoreValidationError(
            f"preferred video length must be between {MIN_DURATION:g} and {MAX_DURATION:g} seconds"
        )
    doubled = round(duration * 2)
    if not math.isclose(duration * 2, doubled, abs_tol=1e-9):
        raise SettingsStoreValidationError("preferred video length must use 0.5 second steps")
    return doubled / 2


class SettingsStore:
    """Atomic JSON settings that survive deletion of the session SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._prepare_parent()
        with self._lock:
            if not self.path.exists():
                self._write_locked(self._defaults())
            else:
                self._read_locked()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "version": SETTINGS_VERSION,
            "system_prompt": "",
            "preferred_duration": 2.0,
            "updated_ms": int(time.time() * 1000),
        }

    def _prepare_parent(self) -> None:
        parent_existed = self.path.parent.exists()
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not parent_existed:
                os.chmod(self.path.parent, 0o700)
            if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
                raise SettingsStoreError("the settings directory must be private")
            if self.path.is_symlink():
                raise SettingsStoreError("the settings file cannot be a symbolic link")
        except SettingsStoreError:
            raise
        except OSError as exc:
            raise SettingsStoreError("could not prepare the private settings directory") from exc

    def _decode(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("version") != SETTINGS_VERSION:
            raise SettingsStoreError("the private settings file has an unsupported format")
        try:
            system_prompt = _validated_system_prompt(raw.get("system_prompt"))
            preferred_duration = _validated_duration(raw.get("preferred_duration"))
        except SettingsStoreValidationError as exc:
            raise SettingsStoreError("the private settings file has invalid values") from exc
        updated_ms = raw.get("updated_ms")
        if isinstance(updated_ms, bool) or not isinstance(updated_ms, int) or updated_ms < 0:
            raise SettingsStoreError("the private settings file has an invalid timestamp")
        expected = {
            "version": SETTINGS_VERSION,
            "system_prompt": system_prompt,
            "preferred_duration": preferred_duration,
            "updated_ms": updated_ms,
        }
        if raw != expected:
            raise SettingsStoreError("the private settings file is not canonical")
        return expected

    def _read_locked(self) -> dict[str, Any]:
        self._prepare_parent()
        if not self.path.exists():
            defaults = self._defaults()
            self._write_locked(defaults)
            return defaults
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags)
            try:
                if os.fstat(descriptor).st_size > 16 * 1024:
                    raise SettingsStoreError("the private settings file is too large")
                raw_bytes = os.read(descriptor, 16 * 1024 + 1)
            finally:
                os.close(descriptor)
            raw = json.loads(raw_bytes.decode("utf-8"))
            os.chmod(self.path, 0o600)
            return self._decode(raw)
        except SettingsStoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SettingsStoreError("the private settings file is unavailable or corrupt") from exc

    def _write_locked(self, settings: dict[str, Any]) -> None:
        self._prepare_parent()
        encoded = (
            json.dumps(settings, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        descriptor: int | None = None
        temporary_path: str | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise SettingsStoreError("could not save the private settings file") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._read_locked())

    def update(
        self,
        *,
        system_prompt: Any = _UNSET,
        preferred_duration: Any = _UNSET,
    ) -> dict[str, Any]:
        with self._lock:
            current = self._read_locked()
            if system_prompt is not _UNSET:
                current["system_prompt"] = _validated_system_prompt(system_prompt)
            if preferred_duration is not _UNSET:
                current["preferred_duration"] = _validated_duration(preferred_duration)
            current["updated_ms"] = max(
                int(time.time() * 1000), int(current["updated_ms"]) + 1
            )
            self._write_locked(current)
            return dict(current)
