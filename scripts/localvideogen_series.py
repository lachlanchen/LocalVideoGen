#!/usr/bin/env python3
"""Safe stdlib client and CLI for the loopback H3 Studio Series API.

The client never starts a service and never submits work unless ``start`` or
``run`` is explicitly requested.  Local reference paths are uploaded as
opaque handles, validated, and removed from the durable API payload.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import mimetypes
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit


DEFAULT_BASE_URL = "http://127.0.0.1:8190"
DEFAULT_HTTP_TIMEOUT = 120.0
DEFAULT_UPLOAD_TIMEOUT = 600.0
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_WAIT_TIMEOUT = 24 * 60 * 60.0
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_SPEC_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
SERIES_API_VERSION = 1
SERIES_RECEIPT_SCHEMA = "localvideogen.series-receipt.v1"
MAXIMUM_QUALITY_PROFILE = "quality_bf16_dual"
TERMINAL_SERIES_STATUSES = frozenset({"completed", "failed", "cancelled"})
SERIES_STATUSES = frozenset(
    {
        "ready",
        "queued",
        "waiting",
        "running",
        "pausing",
        "paused",
        "cancelling",
        "stitching",
        *TERMINAL_SERIES_STATUSES,
    }
)
WAIT_MODES = {
    "terminal": TERMINAL_SERIES_STATUSES,
    "terminal-or-paused": TERMINAL_SERIES_STATUSES | {"paused"},
}
REFERENCE_KINDS = frozenset({"image", "video", "audio"})
WORLD_TRAVEL_REFERENCE_LABELS = (
    "Words card",
    "Zhuangzi Robot",
    "LightMind glasses",
    "Patchwork notebook",
    "Rara Xia",
    "Aya Chan",
    "Sasa Kun",
)
WORLD_TRAVEL_PERSISTENT_IMAGE_LABELS = frozenset(
    {"Zhuangzi Robot", "Rara Xia", "Aya Chan", "Sasa Kun"}
)
WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS = (
    "Words card",
    "LightMind glasses",
    "Patchwork notebook",
)
REFERENCE_TAG_PATTERN = re.compile(
    r"<(Picture|Video|Audio)\s+([0-9]+)>", re.IGNORECASE
)


class SeriesClientError(RuntimeError):
    """A safe, user-facing client failure."""


class SeriesApiError(SeriesClientError):
    """A non-success response from H3 Studio."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"H3 Studio returned HTTP {status}: {message}")
        self.status = status
        self.message = message


class SeriesTransportError(SeriesClientError):
    """The loopback HTTP exchange failed before a trustworthy response."""


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite number: {value}")


def _canonical_uuid(value: str, label: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise SeriesClientError(f"{label} must be a canonical UUID") from exc
    if value != canonical:
        raise SeriesClientError(f"{label} must be a canonical UUID")
    return canonical


def normalize_base_url(value: str) -> str:
    """Accept only the loopback HTTP origin enforced by the server."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SeriesClientError("base URL is invalid") from exc
    if parsed.scheme != "http":
        raise SeriesClientError("base URL must use loopback HTTP")
    if parsed.username is not None or parsed.password is not None:
        raise SeriesClientError("base URL must not contain credentials")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SeriesClientError("base URL must name an explicit loopback host")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SeriesClientError("base URL must contain only a loopback origin")
    if port is not None and not 1 <= port <= 65535:
        raise SeriesClientError("base URL port is invalid")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}{f':{port}' if port is not None else ''}"


def _validate_spec_image_omissions(spec: Mapping[str, Any]) -> bool:
    """Validate per-shot label policies before any source upload."""

    references = spec.get("references") or {}
    raw_images = references.get("images") if isinstance(references, Mapping) else None
    image_labels = (
        [
            item.get("label") if isinstance(item, Mapping) else None
            for item in raw_images
        ]
        if isinstance(raw_images, list)
        else []
    )
    shots = spec.get("shots")
    if not isinstance(shots, list):
        return False
    used = False
    for index, shot in enumerate(shots):
        if not isinstance(shot, Mapping) or "omit_shared_image_labels" not in shot:
            continue
        raw = shot.get("omit_shared_image_labels")
        if isinstance(raw, (str, bytes)) or not isinstance(raw, list):
            raise SeriesClientError("omit_shared_image_labels must be a list")
        if any(not isinstance(label, str) or not label.strip() for label in raw):
            raise SeriesClientError(
                "omit_shared_image_labels must contain non-empty text labels"
            )
        labels = [label.strip() for label in raw]
        if len(set(labels)) != len(labels):
            raise SeriesClientError(
                "omit_shared_image_labels must not contain duplicates"
            )
        unknown = [label for label in labels if label not in image_labels]
        if unknown:
            raise SeriesClientError(
                "omit_shared_image_labels contains an unknown shared image: "
                + unknown[0]
            )
        if index == 0 and labels:
            raise SeriesClientError("Shot 1 must keep every shared image reference")
        if spec.get("template") == "world_travel":
            protected = WORLD_TRAVEL_PERSISTENT_IMAGE_LABELS.intersection(labels)
            if protected:
                raise SeriesClientError(
                    "world_travel cannot omit persistent cast reference: "
                    + sorted(protected)[0]
                )
        used = used or bool(labels)
    return used


def _with_world_travel_omission_defaults(
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Copy a spec and scope opening-only pictures on future travel shots.

    An explicit list, including an empty one, is always retained.  This keeps
    intentional prop use editable while making the safer behavior the default
    for supported client-created World Travel series.  Server-side durable
    records are never rewritten by this helper.
    """

    if not isinstance(spec, Mapping):
        raise SeriesClientError("series spec must be a JSON object")
    result = copy.deepcopy(dict(spec))
    if result.get("template") != "world_travel":
        return result
    shots = result.get("shots")
    if not isinstance(shots, list):
        return result
    for index, shot in enumerate(shots):
        if (
            index > 0
            and isinstance(shot, dict)
            and "omit_shared_image_labels" not in shot
        ):
            shot["omit_shared_image_labels"] = list(
                WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS
            )
    return result


def _world_travel_effective_picture_layouts(
    spec: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Preflight authored logical tags against each compact H3 image list."""

    references = spec.get("references")
    shots = spec.get("shots")
    settings = spec.get("settings") or {}
    if not isinstance(references, Mapping) or not isinstance(shots, list):
        return []
    images = references.get("images")
    if not isinstance(images, list):
        return []
    raw_continuity = (
        settings.get("continuity_seconds", 2)
        if isinstance(settings, Mapping)
        else 2
    )
    if isinstance(raw_continuity, bool):
        raise SeriesClientError("continuity_seconds must be an integer")
    try:
        continuity_seconds = int(raw_continuity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SeriesClientError("continuity_seconds must be an integer") from exc
    if isinstance(raw_continuity, float) and raw_continuity != continuity_seconds:
        raise SeriesClientError("continuity_seconds must be an integer")
    if continuity_seconds not in {0, 2, 3, 4}:
        raise SeriesClientError("continuity_seconds must be 0, 2, 3, or 4")
    continuity = bool(continuity_seconds)
    brief = str(spec.get("brief") or "")
    layouts: list[dict[str, Any]] = []
    for index, shot in enumerate(shots):
        if not isinstance(shot, Mapping):
            continue
        omitted_labels = [
            str(label).strip()
            for label in (shot.get("omit_shared_image_labels") or [])
        ]
        omitted = set(omitted_labels)
        effective: list[dict[str, Any]] = []
        logical_to_physical: dict[int, int] = {}
        for logical_slot, item in enumerate(images, start=1):
            label = str(item.get("label") or "") if isinstance(item, Mapping) else ""
            if label in omitted:
                continue
            physical_slot = len(effective) + 1
            logical_to_physical[logical_slot] = physical_slot
            effective.append(
                {
                    "logical_slot": logical_slot,
                    "physical_slot": physical_slot,
                    "label": label,
                    "scope": "shared",
                }
            )
        scene = shot.get("scene_reference")
        if isinstance(scene, Mapping):
            logical_slot = len(images) + 1
            physical_slot = len(effective) + 1
            logical_to_physical[logical_slot] = physical_slot
            effective.append(
                {
                    "logical_slot": logical_slot,
                    "physical_slot": physical_slot,
                    "label": str(scene.get("label") or f"Shot {index + 1} location"),
                    "scope": "shot",
                }
            )
        if index > 0 and continuity:
            logical_slot = len(images) + 2
            physical_slot = len(effective) + 1
            logical_to_physical[logical_slot] = physical_slot
            effective.append(
                {
                    "logical_slot": logical_slot,
                    "physical_slot": physical_slot,
                    "label": "previous shot's exact final frame",
                    "scope": "continuity",
                }
            )

        authored = f"{brief}\n{shot.get('prompt') or ''}"
        for match in REFERENCE_TAG_PATTERN.finditer(authored):
            if match.group(1).lower() != "picture":
                continue
            logical_slot = int(match.group(2))
            if logical_slot in logical_to_physical:
                continue
            omitted_label = (
                str(images[logical_slot - 1].get("label") or "")
                if 1 <= logical_slot <= len(images)
                and isinstance(images[logical_slot - 1], Mapping)
                else ""
            )
            if omitted_label in omitted:
                raise SeriesClientError(
                    f"Shot {index + 1} uses {match.group(0)} but its effective "
                    f"reference policy omits {omitted_label}"
                )
            raise SeriesClientError(
                f"Shot {index + 1} uses {match.group(0)} without a matching "
                "effective picture reference"
            )
        layouts.append(
            {
                "index": index,
                "omit_shared_image_labels": omitted_labels,
                "effective_pictures": effective,
            }
        )
    return layouts


def load_series_spec(path: str | Path) -> tuple[dict[str, Any], Path]:
    """Read a bounded JSON object and return its reference-path base directory."""

    source = Path(path).expanduser()
    try:
        if source.stat().st_size > MAX_SPEC_BYTES:
            raise SeriesClientError("series spec exceeds 2 MiB")
        raw = source.read_bytes()
    except OSError as exc:
        raise SeriesClientError(f"could not read series spec: {source}") from exc
    try:
        decoded = json.loads(raw, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SeriesClientError("series spec is not valid finite UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise SeriesClientError("series spec must contain one JSON object")
    return decoded, source.resolve().parent


def load_series_receipt(path: str | Path) -> tuple[dict[str, str], Path]:
    """Safely read the small durable-ID receipt emitted by ``run``."""

    source = Path(path).expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise SeriesClientError(
            f"could not safely open series receipt: {source}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SeriesClientError("series receipt must be a regular file")
        if info.st_size > MAX_RECEIPT_BYTES:
            raise SeriesClientError("series receipt exceeds 64 KiB")
        raw = bytearray()
        while len(raw) <= MAX_RECEIPT_BYTES:
            chunk = os.read(
                descriptor, min(16 * 1024, MAX_RECEIPT_BYTES + 1 - len(raw))
            )
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_RECEIPT_BYTES:
            raise SeriesClientError("series receipt exceeds 64 KiB")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(raw, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SeriesClientError(
            "series receipt is not valid finite UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise SeriesClientError("series receipt must contain one JSON object")
    if decoded.get("schema") != SERIES_RECEIPT_SCHEMA:
        raise SeriesClientError(
            f"series receipt schema must be {SERIES_RECEIPT_SCHEMA}"
        )
    series_id = _canonical_uuid(str(decoded.get("series_id") or ""), "series id")
    base_url = normalize_base_url(str(decoded.get("base_url") or ""))
    return {
        "schema": SERIES_RECEIPT_SCHEMA,
        "series_id": series_id,
        "base_url": base_url,
    }, source.resolve()


def _safe_upload_name(path: Path) -> str:
    suffix = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in path.suffix.lower()
    )[:20]
    stem = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in path.stem
    )
    stem = stem.strip(".")[: 150 - len(suffix)] or "reference"
    return f"{stem}{suffix}"


def _sync_directory(path: Path) -> None:
    """Best-effort durability barrier after an atomic directory entry change."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The file contents were already fsynced and installed atomically.
        # Some filesystems do not support fsync on a directory descriptor.
        return


def _install_temporary(
    temporary: str | Path, destination: str | Path, *, overwrite: bool
) -> None:
    """Install a same-directory temporary without a no-overwrite TOCTOU gap."""

    source = Path(temporary)
    target = Path(destination)
    if overwrite:
        os.replace(source, target)
    else:
        try:
            os.link(source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise SeriesClientError(f"target already exists: {target}") from exc
        try:
            os.unlink(source)
        except OSError:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
    _sync_directory(target.parent)


def atomic_write_json(
    destination: str | Path,
    value: Mapping[str, Any],
    *,
    overwrite: bool = False,
) -> Path:
    """Write finite JSON durably without clobbering an unapproved target."""

    target = Path(destination).expanduser()
    try:
        body = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, TypeError, ValueError) as exc:
        raise SeriesClientError(f"could not prepare JSON receipt: {target}") from exc
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".part",
            dir=target.parent,
            delete=False,
        ) as output:
            temporary_name = output.name
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        _install_temporary(temporary_name, target, overwrite=overwrite)
        temporary_name = None
    except SeriesClientError:
        raise
    except OSError as exc:
        raise SeriesClientError(f"could not install JSON receipt: {target}") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return target.resolve()


class LocalVideoGenClient:
    """Synchronous stdlib client for one loopback H3 Studio instance."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        if not 0 < timeout <= 3600:
            raise SeriesClientError("HTTP timeout must be between 0 and 3600 seconds")
        if not 300 <= upload_timeout <= 3600:
            raise SeriesClientError(
                "upload timeout must be between 300 and 3600 seconds"
            )
        self.timeout = float(timeout)
        self.upload_timeout = float(upload_timeout)
        parsed = urlsplit(self.base_url)
        assert parsed.hostname is not None
        self._host = parsed.hostname
        self._port = parsed.port or 80

    def _connection(self, timeout: float | None = None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            self._host,
            self._port,
            timeout=self.timeout if timeout is None else timeout,
        )

    @staticmethod
    def _target(path: str, query: Mapping[str, Any] | None = None) -> str:
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or parsed.query
            or not parsed.path.startswith("/api/")
            or "//" in parsed.path
        ):
            raise SeriesClientError("client endpoint is not an allowlisted API path")
        return parsed.path + (f"?{urlencode(query)}" if query else "")

    @staticmethod
    def _read_response(
        response: http.client.HTTPResponse, *, limit: int = MAX_JSON_BYTES
    ) -> bytes:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise SeriesClientError(
                "H3 Studio response exceeds the client safety limit"
            )
        return data

    @staticmethod
    def _response_error(status: int, data: bytes) -> SeriesApiError:
        message = "request failed"
        try:
            decoded = json.loads(data)
            if isinstance(decoded, Mapping) and isinstance(decoded.get("error"), str):
                message = decoded["error"][:2000]
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = data.decode("utf-8", errors="replace").strip()
            if text:
                message = text[:2000]
        return SeriesApiError(status, message)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        target = self._target(path, query)
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "LocalVideoGen-Series-Client/1",
        }
        if payload is not None:
            try:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            except (TypeError, ValueError) as exc:
                raise SeriesClientError("request payload is not finite JSON") from exc
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Origin": self.base_url,
                }
            )
        connection = self._connection(timeout)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            data = self._read_response(response)
            if not 200 <= response.status < 300:
                raise self._response_error(response.status, data)
        except (OSError, http.client.HTTPException) as exc:
            raise SeriesTransportError(
                f"could not reach H3 Studio at {self.base_url}"
            ) from exc
        finally:
            connection.close()
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SeriesClientError("H3 Studio returned invalid JSON") from exc

    def health(self, *, deep: bool = True) -> dict[str, Any]:
        result = self._request_json(
            "GET", "/api/health", query={"deep": "1"} if deep else None
        )
        if not isinstance(result, dict):
            raise SeriesClientError("health response is invalid")
        return result

    def config(self) -> dict[str, Any]:
        result = self._request_json("GET", "/api/config")
        if not isinstance(result, dict):
            raise SeriesClientError("config response is invalid")
        return result

    @staticmethod
    def _spec_uses_local_sources(spec: Mapping[str, Any]) -> bool:
        references = spec.get("references")
        if isinstance(references, Mapping):
            for key in ("images", "videos", "audio"):
                items = references.get(key)
                if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                    for item in items:
                        if not isinstance(item, Mapping):
                            continue
                        if any(
                            item.get(field)
                            for field in (
                                "source",
                                "path",
                                "soundtrack_source",
                                "soundtrack_path",
                            )
                        ):
                            return True
        shots = spec.get("shots")
        if isinstance(shots, Sequence) and not isinstance(shots, (str, bytes)):
            for shot in shots:
                if not isinstance(shot, Mapping):
                    continue
                scene = shot.get("scene_reference")
                if isinstance(scene, Mapping) and (
                    scene.get("source") or scene.get("path")
                ):
                    return True
        return False

    def _require_deep_health(self, operation: str) -> dict[str, Any]:
        health = self.health(deep=True)
        if (
            health.get("connected") is not True
            or health.get("ready") is not True
            or health.get("model_status") != "verified"
        ):
            message = str(health.get("message") or health.get("model_note") or "")
            detail = f": {message}" if message else ""
            raise SeriesClientError(
                f"deep runtime health is not ready for {operation}{detail}"
            )
        return health

    def preflight_series_spec(
        self,
        spec: Mapping[str, Any],
        *,
        require_runtime: bool | None = None,
    ) -> dict[str, Any]:
        """Verify the v1 server contract before any upload or costly start."""

        if not isinstance(spec, Mapping):
            raise SeriesClientError("series spec must be a JSON object")
        effective_spec = _with_world_travel_omission_defaults(spec)
        config = self.config()
        if config.get("series_api_version") != SERIES_API_VERSION:
            raise SeriesClientError(
                f"server series_api_version must be {SERIES_API_VERSION}; stopped before upload"
            )
        profiles = config.get("profiles")
        if not isinstance(profiles, list):
            raise SeriesClientError("server profile capability list is invalid")
        profile_index = {
            str(profile.get("id")): profile
            for profile in profiles
            if isinstance(profile, Mapping) and isinstance(profile.get("id"), str)
        }
        settings = effective_spec.get("settings") or {}
        if not isinstance(settings, Mapping):
            raise SeriesClientError("settings must be a JSON object")
        profile_id = str(settings.get("profile") or MAXIMUM_QUALITY_PROFILE)
        selected_profile = profile_index.get(profile_id)
        if selected_profile is None:
            raise SeriesClientError(
                f"selected profile {profile_id!r} is not advertised by this server"
            )
        series = config.get("series")
        if not isinstance(series, Mapping):
            raise SeriesClientError("server series capability contract is missing")
        template = str(effective_spec.get("template") or "lalachan")
        templates = series.get("templates")
        if not isinstance(templates, list) or template not in templates:
            raise SeriesClientError(
                f"template {template!r} is not advertised by this server"
            )
        uses_image_omissions = _validate_spec_image_omissions(effective_spec)
        if uses_image_omissions:
            policy = series.get("shot_reference_policy")
            if not isinstance(policy, Mapping) or any(
                (
                    policy.get("field") != "omit_shared_image_labels",
                    policy.get("logical_picture_tags_remapped") is not True,
                    policy.get("first_shot_must_keep_all") is not True,
                )
            ):
                raise SeriesClientError(
                    "server does not advertise safe per-shot shared-image omission"
                )

        if template == "world_travel":
            capabilities = series.get("capabilities")
            capability = (
                capabilities.get("world_travel")
                if isinstance(capabilities, Mapping)
                else None
            )
            if not isinstance(capability, Mapping):
                raise SeriesClientError("world_travel capability contract is missing")
            if (
                capability.get("template") != "world_travel"
                or str(capability.get("render_mode")).lower() != "r2v"
            ):
                raise SeriesClientError("world_travel must advertise the R2V contract")
            if capability.get("maximum_quality_profile") != MAXIMUM_QUALITY_PROFILE:
                raise SeriesClientError(
                    "world_travel maximum-quality profile contract has changed"
                )
            if profile_id != MAXIMUM_QUALITY_PROFILE:
                raise SeriesClientError(
                    "world_travel cross-project runs require quality_bf16_dual; refusing a silent quality downgrade"
                )
            maximum = profile_index.get(MAXIMUM_QUALITY_PROFILE)
            if not isinstance(maximum, Mapping) or any(
                (
                    maximum.get("precision") != "bf16",
                    maximum.get("dual_gpu") is not True,
                    maximum.get("turbo") is not False,
                    maximum.get("steps_ref") != 25,
                )
            ):
                raise SeriesClientError(
                    "quality_bf16_dual must be BF16, dual-GPU, non-Turbo, and 25-step R2V"
                )
            picture_slots = capability.get("picture_slots")
            if not isinstance(picture_slots, Mapping):
                raise SeriesClientError("world_travel picture-slot contract is missing")
            expected_shared = [
                {"slot": slot, "label": label}
                for slot, label in enumerate(WORLD_TRAVEL_REFERENCE_LABELS, start=1)
            ]
            if picture_slots.get("shared") != expected_shared:
                raise SeriesClientError(
                    "world_travel must advertise exact canonical P1-P7 labels and order"
                )
            scene = picture_slots.get("scene")
            if not isinstance(scene, Mapping) or any(
                (
                    scene.get("slot") != 8,
                    scene.get("kind") != "image",
                    scene.get("scope") != "shot",
                    scene.get("required") is not True,
                )
            ):
                raise SeriesClientError(
                    "world_travel must advertise required per-shot scene image P8"
                )
            final_frame = picture_slots.get("continuity_final_frame")
            if not isinstance(final_frame, Mapping) or any(
                (
                    final_frame.get("slot") != 9,
                    final_frame.get("kind") != "image",
                    final_frame.get("scope") != "successor_shot",
                    final_frame.get("when_continuity_enabled") is not True,
                    final_frame.get("sha256_required") is not True,
                )
            ):
                raise SeriesClientError(
                    "world_travel must advertise verified predecessor final frame P9"
                )
            references = effective_spec.get("references") or {}
            images = (
                references.get("images") if isinstance(references, Mapping) else None
            )
            if not isinstance(images, list) or [
                item.get("label") if isinstance(item, Mapping) else None
                for item in images
            ] != list(WORLD_TRAVEL_REFERENCE_LABELS):
                raise SeriesClientError(
                    "world_travel requires exactly seven shared pictures in canonical label order"
                )
            shots = effective_spec.get("shots")
            if not isinstance(shots, list) or any(
                not isinstance(shot, Mapping)
                or not isinstance(shot.get("scene_reference"), Mapping)
                for shot in shots
            ):
                raise SeriesClientError(
                    "every world-travel shot needs a scene_reference object for P8"
                )
            effective_picture_layouts = _world_travel_effective_picture_layouts(
                effective_spec
            )
        else:
            effective_picture_layouts = []

        needs_runtime = (
            self._spec_uses_local_sources(effective_spec)
            if require_runtime is None
            else require_runtime
        )
        health = (
            self._require_deep_health("source upload or series start")
            if needs_runtime
            else None
        )
        return {
            "series_api_version": SERIES_API_VERSION,
            "template": template,
            "profile": profile_id,
            "runtime": health,
            "effective_picture_layouts": effective_picture_layouts,
        }

    def upload(self, kind: str, path: str | Path) -> dict[str, Any]:
        """Preflight readiness, then stream one regular local reference."""

        self._require_deep_health("source upload")
        return self._upload_file(kind, path)

    def _upload_file(self, kind: str, path: str | Path) -> dict[str, Any]:
        """Stream one regular local file to the normalized upload endpoint."""

        if kind not in REFERENCE_KINDS:
            raise SeriesClientError("upload kind must be image, video, or audio")
        source = Path(path).expanduser()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise SeriesClientError(
                f"could not safely open reference: {source}"
            ) from exc
        connection: http.client.HTTPConnection | None = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise SeriesClientError("reference must be a regular file")
            name = _safe_upload_name(source)
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            boundary = f"LocalVideoGen{secrets.token_hex(18)}"
            preamble = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("ascii")
            suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
            length = len(preamble) + info.st_size + len(suffix)
            connection = self._connection(self.upload_timeout)
            target = self._target("/api/uploads", {"kind": kind})
            connection.putrequest("POST", target)
            connection.putheader("Accept", "application/json")
            connection.putheader("User-Agent", "LocalVideoGen-Series-Client/1")
            connection.putheader("Origin", self.base_url)
            connection.putheader(
                "Content-Type", f"multipart/form-data; boundary={boundary}"
            )
            connection.putheader("Content-Length", str(length))
            connection.endheaders()
            connection.send(preamble)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
            connection.send(suffix)
            after = os.fstat(descriptor)
            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            ):
                raise SeriesClientError(
                    "reference changed while it was being uploaded; use a stable source file"
                )
            response = connection.getresponse()
            data = self._read_response(response)
            if not 200 <= response.status < 300:
                raise self._response_error(response.status, data)
        except SeriesClientError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise SeriesTransportError(
                f"could not upload reference to {self.base_url}"
            ) from exc
        finally:
            os.close(descriptor)
            if connection is not None:
                connection.close()
        try:
            result = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SeriesClientError("upload response is invalid JSON") from exc
        if (
            not isinstance(result, dict)
            or result.get("kind") != kind
            or not isinstance(result.get("token"), str)
            or not result["token"]
        ):
            raise SeriesClientError(
                "upload response does not contain a valid opaque handle"
            )
        return result

    def validate_uploads(self, handles: Sequence[Mapping[str, str]]) -> list[str]:
        normalized: list[dict[str, str]] = []
        for handle in handles:
            token = handle.get("token")
            kind = handle.get("kind")
            if not isinstance(token, str) or not token or kind not in REFERENCE_KINDS:
                raise SeriesClientError(
                    "each upload handle needs a token and valid kind"
                )
            normalized.append({"token": token, "kind": kind})
        result = self._request_json(
            "POST", "/api/uploads/validate", {"uploads": normalized}
        )
        if not isinstance(result, dict) or not isinstance(result.get("valid"), list):
            raise SeriesClientError("upload-validation response is invalid")
        valid = [item for item in result["valid"] if isinstance(item, str)]
        if set(valid) != {item["token"] for item in normalized}:
            raise SeriesClientError(
                "one or more upload handles expired; upload the references again"
            )
        return valid

    def prepare_series_payload(
        self,
        spec: Mapping[str, Any],
        *,
        base_dir: str | Path = ".",
    ) -> dict[str, Any]:
        """Upload path-based references and return a token-only API document."""

        if not isinstance(spec, Mapping):
            raise SeriesClientError("series spec must be a JSON object")
        try:
            payload = json.loads(
                json.dumps(
                    dict(spec), ensure_ascii=False, allow_nan=False, sort_keys=True
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SeriesClientError(
                "series spec must contain finite JSON values"
            ) from exc
        settings = payload.setdefault("settings", {})
        if not isinstance(settings, dict):
            raise SeriesClientError("settings must be a JSON object")
        settings.setdefault("profile", "quality_bf16_dual")
        settings.setdefault("width", 1024)
        settings.setdefault("height", 768)
        settings.setdefault("ref_image_size", "max")
        settings.setdefault(
            "continuity_seconds",
            2 if payload.get("template") == "world_travel" else 3,
        )
        settings.setdefault("advance", True)

        payload = _with_world_travel_omission_defaults(payload)

        raw_references = payload.get("references") or {}
        if not isinstance(raw_references, Mapping):
            raise SeriesClientError("references must be a JSON object")
        if payload.get("template") == "world_travel":
            raw_images = raw_references.get("images") or []
            if (
                not isinstance(raw_images, list)
                or tuple(
                    item.get("label") if isinstance(item, Mapping) else None
                    for item in raw_images
                )
                != WORLD_TRAVEL_REFERENCE_LABELS
            ):
                raise SeriesClientError(
                    "world_travel requires exactly seven shared pictures in canonical label order"
                )
            raw_shots = payload.get("shots")
            if not isinstance(raw_shots, list) or any(
                not isinstance(shot, Mapping)
                or not isinstance(shot.get("scene_reference"), Mapping)
                for shot in raw_shots
            ):
                raise SeriesClientError(
                    "every world-travel shot needs a scene_reference object"
                )
        self.preflight_series_spec(payload)
        root = Path(base_dir).expanduser().resolve()
        upload_cache: dict[tuple[str, str], str] = {}
        handles: list[dict[str, str]] = []

        def token_for(
            item: Mapping[str, Any],
            kind: str,
            *,
            path_key: str = "path",
            token_key: str = "token",
        ) -> str:
            raw_path = item.get(path_key)
            source_key = (
                "source"
                if path_key == "path"
                else f"{path_key.removesuffix('_path')}_source"
            )
            raw_source = item.get(source_key)
            if raw_path and raw_source:
                raise SeriesClientError(
                    f"{kind} reference needs only {path_key} or {source_key}"
                )
            if raw_source:
                raw_path = raw_source
            raw_token = item.get(token_key)
            if bool(raw_path) == bool(raw_token):
                raise SeriesClientError(
                    f"each {kind} reference needs exactly one local source or {token_key}"
                )
            if raw_token:
                if not isinstance(raw_token, str):
                    raise SeriesClientError(f"{kind} reference token must be text")
                token = raw_token
            else:
                if not isinstance(raw_path, str):
                    raise SeriesClientError(f"{kind} reference path must be text")
                source = Path(raw_path).expanduser()
                if not source.is_absolute():
                    source = root / source
                cache_key = (kind, str(source.resolve()))
                token = upload_cache.get(cache_key, "")
                if not token:
                    uploaded = self._upload_file(kind, source)
                    token = str(uploaded["token"])
                    upload_cache[cache_key] = token
            handles.append({"token": token, "kind": kind})
            return token

        prepared: dict[str, list[dict[str, Any]]] = {
            "images": [],
            "videos": [],
            "audio": [],
        }
        for key, kind in (("images", "image"), ("videos", "video"), ("audio", "audio")):
            items = raw_references.get(key) or []
            if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
                raise SeriesClientError(f"references.{key} must be a list")
            for item in items:
                if not isinstance(item, Mapping):
                    raise SeriesClientError(
                        f"each references.{key} item must be an object"
                    )
                record: dict[str, Any] = {"token": token_for(item, kind)}
                if "label" in item:
                    record["label"] = item["label"]
                if kind == "video":
                    has_soundtrack_path = bool(item.get("soundtrack_path"))
                    has_soundtrack_source = bool(item.get("soundtrack_source"))
                    has_soundtrack_token = bool(item.get("soundtrack"))
                    soundtrack_inputs = sum(
                        (
                            has_soundtrack_path,
                            has_soundtrack_source,
                            has_soundtrack_token,
                        )
                    )
                    if soundtrack_inputs > 1:
                        raise SeriesClientError(
                            "video soundtrack needs only soundtrack_source, soundtrack_path, or soundtrack"
                        )
                    if soundtrack_inputs:
                        record["soundtrack"] = token_for(
                            item,
                            "audio",
                            path_key="soundtrack_path",
                            token_key="soundtrack",
                        )
                prepared[key].append(record)
        payload["references"] = prepared
        shots = payload.get("shots")
        if isinstance(shots, list):
            for index, shot in enumerate(shots):
                if not isinstance(shot, dict):
                    continue
                raw_scene = shot.get("scene_reference")
                if raw_scene is None:
                    if payload.get("template") == "world_travel":
                        raise SeriesClientError(
                            f"world-travel shot {index + 1} needs a scene_reference"
                        )
                    continue
                if not isinstance(raw_scene, Mapping):
                    raise SeriesClientError(
                        f"shot {index + 1} scene_reference must be an object"
                    )
                scene = {"token": token_for(raw_scene, "image")}
                if "label" in raw_scene:
                    scene["label"] = raw_scene["label"]
                shot["scene_reference"] = scene
        unique_handles = list(
            {(item["token"], item["kind"]): item for item in handles}.values()
        )
        if unique_handles:
            self.validate_uploads(unique_handles)
        return payload

    def create_series(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        effective_payload = _with_world_travel_omission_defaults(payload)
        self.preflight_series_spec(effective_payload, require_runtime=False)
        return self._create_series(effective_payload)

    def _create_series(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self._request_json("POST", "/api/series", payload)
        if not isinstance(result, dict):
            raise SeriesClientError("create-series response is invalid")
        return result

    def create_series_from_spec(
        self,
        spec: Mapping[str, Any],
        *,
        base_dir: str | Path = ".",
    ) -> dict[str, Any]:
        payload = self.prepare_series_payload(spec, base_dir=base_dir)
        return self._create_series(payload)

    def update_series_from_spec(
        self,
        series_id: str,
        spec: Mapping[str, Any],
        *,
        base_dir: str | Path = ".",
    ) -> dict[str, Any]:
        canonical = _canonical_uuid(series_id, "series id")
        payload = self.prepare_series_payload(spec, base_dir=base_dir)
        result = self._request_json("PUT", f"/api/series/{canonical}", payload)
        if not isinstance(result, dict):
            raise SeriesClientError("update-series response is invalid")
        return result

    def list_series(self, *, limit: int = 40) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise SeriesClientError("list limit must be between 1 and 100")
        result = self._request_json("GET", "/api/series", query={"limit": str(limit)})
        if not isinstance(result, dict):
            raise SeriesClientError("series-list response is invalid")
        return result

    def get_series(
        self, series_id: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        canonical = _canonical_uuid(series_id, "series id")
        result = self._request_json("GET", f"/api/series/{canonical}", timeout=timeout)
        if not isinstance(result, dict):
            raise SeriesClientError("series response is invalid")
        return result

    def recover_from_receipt(self, path: str | Path) -> dict[str, Any]:
        """Read one v1 receipt and inspect its exact durable series without writes."""

        receipt, receipt_path = load_series_receipt(path)
        if receipt["base_url"] != self.base_url:
            raise SeriesClientError(
                "receipt base_url differs from this client; pass its exact loopback --base-url"
            )
        series = self.get_series(receipt["series_id"])
        returned_id = _canonical_uuid(str(series.get("id") or ""), "returned series id")
        if returned_id != receipt["series_id"]:
            raise SeriesClientError(
                "server returned a different series than the receipt"
            )
        status = str(series.get("status") or "unknown")
        next_actions = {
            "ready": (
                "start",
                f"start {returned_id}",
                "The durable storyboard exists and has not been started.",
            ),
            "queued": (
                "wait",
                f"wait {returned_id} --until terminal-or-paused",
                "The series is queued; observe it without submitting again.",
            ),
            "waiting": (
                "wait",
                f"wait {returned_id} --until terminal-or-paused",
                "The series is waiting for the one shared engine slot.",
            ),
            "running": (
                "wait",
                f"wait {returned_id} --until terminal-or-paused",
                "A shot is already running; do not start a duplicate.",
            ),
            "pausing": (
                "wait",
                f"wait {returned_id} --until terminal-or-paused",
                "The active shot is being preserved before a pause.",
            ),
            "paused": (
                "review_then_resume",
                f"status {returned_id}  # review, then: resume {returned_id}",
                "Review the preserved attempt before explicitly resuming.",
            ),
            "cancelling": (
                "wait",
                f"wait {returned_id} --until terminal-or-paused",
                "Cancellation is still settling; do not submit another action.",
            ),
            "stitching": (
                "wait",
                f"wait {returned_id} --until terminal-or-paused",
                "Finalization is already running.",
            ),
            "completed": (
                "download_verified_artifacts",
                f"artifacts {returned_id}",
                "Inspect the allowlist, then download final and manifest.",
            ),
            "failed": (
                "inspect_then_retry",
                f"status {returned_id}",
                "Inspect the retained error and attempts before choosing retry.",
            ),
            "cancelled": (
                "inspect_then_retry",
                f"status {returned_id}",
                "Inspect retained attempts before choosing a shot retry.",
            ),
        }
        action, command, reason = next_actions.get(
            status,
            (
                "inspect_status",
                f"status {returned_id}",
                "The server returned a newer state; inspect it before any mutation.",
            ),
        )
        return {
            "receipt": {
                **receipt,
                "path": str(receipt_path),
            },
            "series": series,
            "recommended_next_action": {
                "action": action,
                "command": command,
                "reason": reason,
            },
        }

    def _action(
        self, series_id: str, action: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        canonical = _canonical_uuid(series_id, "series id")
        result = self._request_json(
            "POST",
            f"/api/series/{canonical}/{action}",
            {} if payload is None else payload,
        )
        if not isinstance(result, dict):
            raise SeriesClientError("series action response is invalid")
        return result

    def start_series(self, series_id: str) -> dict[str, Any]:
        series = self.get_series(series_id)
        self.preflight_series_spec(series, require_runtime=True)
        return self._action(series_id, "start")

    def pause_series(self, series_id: str) -> dict[str, Any]:
        return self._action(series_id, "pause")

    def resume_series(self, series_id: str) -> dict[str, Any]:
        return self._action(series_id, "resume")

    def cancel_active(self, series_id: str) -> dict[str, Any]:
        return self._action(series_id, "cancel-active")

    def set_shot_reference_policy(
        self,
        series_id: str,
        shot_index: int,
        *,
        omit_shared_image_labels: Sequence[str],
    ) -> dict[str, Any]:
        if (
            isinstance(shot_index, bool)
            or not isinstance(shot_index, int)
            or shot_index < 0
        ):
            raise SeriesClientError("shot index must be a non-negative integer")
        if isinstance(omit_shared_image_labels, (str, bytes)) or not isinstance(
            omit_shared_image_labels, Sequence
        ):
            raise SeriesClientError("omit_shared_image_labels must be a list")
        labels = list(omit_shared_image_labels)
        if any(not isinstance(label, str) or not label.strip() for label in labels):
            raise SeriesClientError(
                "omit_shared_image_labels must contain non-empty text labels"
            )
        labels = [label.strip() for label in labels]
        if len(set(labels)) != len(labels):
            raise SeriesClientError(
                "omit_shared_image_labels must not contain duplicates"
            )
        canonical = _canonical_uuid(series_id, "series id")
        result = self._request_json(
            "PUT",
            f"/api/series/{canonical}/shots/{shot_index}/reference-policy",
            {"omit_shared_image_labels": labels},
        )
        if not isinstance(result, dict):
            raise SeriesClientError("reference-policy response is invalid")
        return result

    def retry_shot(
        self,
        series_id: str,
        shot_index: int,
        *,
        regenerate_following: bool = False,
    ) -> dict[str, Any]:
        if (
            isinstance(shot_index, bool)
            or not isinstance(shot_index, int)
            or shot_index < 0
        ):
            raise SeriesClientError("shot index must be a non-negative integer")
        return self._action(
            series_id,
            f"shots/{shot_index}/retry",
            {"regenerate_following": regenerate_following},
        )

    def retry_finalization(self, series_id: str) -> dict[str, Any]:
        return self._action(series_id, "retry-finalization")

    def wait_for_series(
        self,
        series_id: str,
        *,
        interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        stop_statuses: Collection[str] = TERMINAL_SERIES_STATUSES,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        on_transport_error: Callable[[SeriesTransportError, float], None] | None = None,
    ) -> dict[str, Any]:
        if not 0.2 <= interval <= 300:
            raise SeriesClientError("poll interval must be between 0.2 and 300 seconds")
        if not 0 < timeout <= 7 * 24 * 60 * 60:
            raise SeriesClientError("wait timeout must be between 0 and 604800 seconds")
        stops = frozenset(stop_statuses)
        if not stops or not stops <= SERIES_STATUSES:
            raise SeriesClientError("wait stop statuses are invalid")
        canonical = _canonical_uuid(series_id, "series id")
        deadline = time.monotonic() + timeout
        transport_failures = 0
        last_status = "not observed"
        last_transport_error: str | None = None

        def timed_out() -> SeriesClientError:
            detail = f"last observed status={last_status}"
            if last_transport_error:
                detail += f"; last transport error={last_transport_error}"
            if last_status == "paused" and "paused" not in stops:
                detail += "; resume it or wait with terminal-or-paused"
            return SeriesClientError(
                f"timed out while waiting ({detail}); durable series {canonical} remains saved"
            )

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timed_out()
            try:
                series = self.get_series(
                    canonical, timeout=max(0.1, min(self.timeout, remaining))
                )
            except SeriesTransportError as exc:
                transport_failures += 1
                last_transport_error = str(exc)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise timed_out() from exc
                retry_delay = min(
                    max(0.5, interval) * (2 ** min(transport_failures - 1, 5)),
                    30.0,
                    remaining,
                )
                if on_transport_error is not None:
                    on_transport_error(exc, retry_delay)
                time.sleep(retry_delay)
                continue
            transport_failures = 0
            last_transport_error = None
            last_status = str(series.get("status") or "unknown")
            if on_update is not None:
                on_update(series)
            if last_status in stops:
                return series
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timed_out()
            time.sleep(min(interval, remaining))

    @staticmethod
    def _artifact_index(series: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}

        def add(value: Any) -> None:
            if isinstance(value, Mapping) and isinstance(value.get("id"), str):
                found[str(value["id"])] = dict(value)

        for artifact in series.get("artifacts") or []:
            add(artifact)
        add(series.get("final_artifact"))
        for shot in series.get("shots") or []:
            if not isinstance(shot, Mapping):
                continue
            for artifact in shot.get("continuity") or []:
                add(artifact)
            for attempt in shot.get("attempts") or []:
                if isinstance(attempt, Mapping):
                    for artifact in attempt.get("outputs") or []:
                        add(artifact)
        return found

    def list_artifacts(self, series_id: str) -> list[dict[str, Any]]:
        return list(self._artifact_index(self.get_series(series_id)).values())

    def _resolve_artifact(
        self, series: Mapping[str, Any], selector: str
    ) -> dict[str, Any]:
        index = self._artifact_index(series)
        if selector == "final":
            artifact = series.get("final_artifact")
            if isinstance(artifact, Mapping) and artifact.get("id") in index:
                return index[str(artifact["id"])]
            raise SeriesClientError("series has no active final artifact")
        if selector == "manifest":
            matches = [
                item
                for item in index.values()
                if item.get("kind") == "manifest" and item.get("superseded") is not True
            ]
            if matches:
                return matches[-1]
            raise SeriesClientError("series has no active manifest artifact")
        artifact_id = _canonical_uuid(selector, "artifact selector")
        if artifact_id not in index:
            raise SeriesClientError(
                "artifact is not exposed by this series' durable allowlist"
            )
        return index[artifact_id]

    @staticmethod
    def _artifact_integrity(artifact: Mapping[str, Any]) -> tuple[int, str]:
        metadata = artifact.get("metadata")
        if not isinstance(metadata, Mapping):
            raise SeriesClientError(
                "artifact does not publish verified size and SHA-256 metadata"
            )
        expected_size = metadata.get("bytes")
        expected_sha256 = str(metadata.get("sha256") or "").lower()
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise SeriesClientError(
                "artifact does not publish verified size and SHA-256 metadata"
            )
        return expected_size, expected_sha256

    def download_artifact(
        self,
        series_id: str,
        selector: str,
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        canonical = _canonical_uuid(series_id, "series id")
        artifact = self._resolve_artifact(self.get_series(canonical), selector)
        artifact_id = _canonical_uuid(str(artifact["id"]), "artifact id")
        expected_size, expected_sha256 = self._artifact_integrity(artifact)
        target = Path(destination).expanduser()
        if target.exists() and not overwrite:
            raise SeriesClientError(f"download target already exists: {target}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SeriesClientError(
                f"could not create download directory: {target.parent}"
            ) from exc

        connection = self._connection()
        temporary_name: str | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            connection.request(
                "GET",
                self._target(
                    f"/api/series/{canonical}/artifacts/{artifact_id}",
                    {"download": "1"},
                ),
                headers={
                    "Accept": "application/octet-stream",
                    "User-Agent": "LocalVideoGen-Series-Client/1",
                },
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                data = self._read_response(response)
                raise self._response_error(response.status, data)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.name}.",
                suffix=".part",
                dir=target.parent,
                delete=False,
            ) as output:
                temporary_name = output.name
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            actual_sha256 = digest.hexdigest()
            if size != expected_size or actual_sha256 != expected_sha256:
                raise SeriesClientError(
                    "artifact size or SHA-256 differs from its durable metadata; "
                    "the temporary download was discarded"
                )
            _install_temporary(temporary_name, target, overwrite=overwrite)
            temporary_name = None
        except SeriesClientError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise SeriesClientError(
                "artifact download failed; any partial file was discarded"
            ) from exc
        finally:
            connection.close()
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
        return {
            "artifact": artifact,
            "path": str(target.resolve()),
            "size": size,
            "sha256": digest.hexdigest(),
        }


def _print_json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _progress_printer() -> Callable[[dict[str, Any]], None]:
    previous = ""

    def report(series: dict[str, Any]) -> None:
        nonlocal previous
        progress = (
            series.get("progress")
            if isinstance(series.get("progress"), Mapping)
            else {}
        )
        render = (
            progress.get("render")
            if isinstance(progress.get("render"), Mapping)
            else {}
        )
        marker = "|".join(
            str(value)
            for value in (
                series.get("status"),
                series.get("active_shot"),
                progress.get("completed_shots"),
                progress.get("total_shots"),
                progress.get("overall_percent"),
                render.get("percent"),
            )
        )
        if marker != previous:
            previous = marker
            print(
                f"status={series.get('status')} shots={progress.get('completed_shots', 0)}/"
                f"{progress.get('total_shots', 0)} overall={progress.get('overall_percent', 0)}%"
                + (
                    f" render={render.get('percent')}%"
                    if render.get("percent") is not None
                    else ""
                ),
                file=sys.stderr,
                flush=True,
            )

    return report


def _transport_retry_printer(error: SeriesTransportError, retry_delay: float) -> None:
    print(
        f"poll transport warning: {error}; retrying safe GET in {retry_delay:.1f}s",
        file=sys.stderr,
        flush=True,
    )


def _receipt_document(
    client: LocalVideoGenClient, created: Mapping[str, Any]
) -> dict[str, Any]:
    series_id = _canonical_uuid(str(created.get("id") or ""), "created series id")
    return {
        "schema": SERIES_RECEIPT_SCHEMA,
        "series_id": series_id,
        "base_url": client.base_url,
        "title": str(created.get("title") or ""),
        "status": str(created.get("status") or "ready"),
        "revision": created.get("revision"),
        "created_ms": created.get("created_ms"),
        "receipt_written_ms": int(time.time() * 1000),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--upload-timeout", type=float, default=DEFAULT_UPLOAD_TIMEOUT)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("health", help="inspect webapp/model/engine readiness")
    commands.add_parser("config", help="read the public render contract")

    recover = commands.add_parser(
        "recover", help="read a v1 receipt and recommend a non-mutating next step"
    )
    recover.add_argument("receipt", type=Path)

    upload = commands.add_parser("upload", help="upload and normalize one reference")
    upload.add_argument("kind", choices=sorted(REFERENCE_KINDS))
    upload.add_argument("path", type=Path)

    validate = commands.add_parser(
        "validate", help="validate kind:token upload handles"
    )
    validate.add_argument("handles", nargs="+")

    create = commands.add_parser(
        "create", help="upload spec references and save a ready series"
    )
    create.add_argument("spec", type=Path)

    update = commands.add_parser(
        "update", help="replace a ready series from a JSON spec"
    )
    update.add_argument("series_id")
    update.add_argument("spec", type=Path)

    listing = commands.add_parser("list", help="list durable series summaries")
    listing.add_argument("--limit", type=int, default=40)

    for name, help_text in (
        ("status", "read full durable series state"),
        ("start", "start a ready series"),
        ("pause", "pause safely after the active shot"),
        ("resume", "resume a paused series"),
        ("cancel-active", "cancel only the active owned shot"),
        ("retry-finalization", "retry stitching without regenerating shots"),
        ("artifacts", "list every publicly allowlisted artifact"),
    ):
        action = commands.add_parser(name, help=help_text)
        action.add_argument("series_id")

    wait = commands.add_parser(
        "wait", help="poll a durable series to the selected terminal/review state"
    )
    wait.add_argument("series_id")
    wait.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT)
    wait.add_argument(
        "--until",
        choices=sorted(WAIT_MODES),
        default="terminal-or-paused",
        help="stop at a terminal state, or also return when review mode pauses",
    )

    retry = commands.add_parser("retry", help="regenerate one zero-based shot")
    retry.add_argument("series_id")
    retry.add_argument("shot_index", type=int)
    retry.add_argument("--regenerate-following", action="store_true")

    reference_policy = commands.add_parser(
        "set-reference-policy",
        help="set shared-image omissions for one stopped zero-based shot",
    )
    reference_policy.add_argument("series_id")
    reference_policy.add_argument("shot_index", type=int)
    reference_policy.add_argument(
        "--omit-shared-image-label",
        action="append",
        default=[],
        help="repeat once for each shared image to omit; no flags clears the policy",
    )

    download = commands.add_parser(
        "download", help="download final, manifest, or an allowlisted UUID"
    )
    download.add_argument("series_id")
    download.add_argument("selector")
    download.add_argument("output", type=Path)
    download.add_argument("--overwrite", action="store_true")

    run = commands.add_parser(
        "run",
        help="create, receipt, start, then pause for review or download the final bundle",
    )
    run.add_argument("spec", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    run.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT)
    run.add_argument(
        "--until",
        choices=sorted(WAIT_MODES),
        default="terminal-or-paused",
        help="defaults to returning safely when advance:false pauses for review",
    )
    run.add_argument(
        "--receipt",
        type=Path,
        help="atomic creation receipt path; defaults beside outputs using the series ID",
    )
    run.add_argument(
        "--overwrite-receipt",
        action="store_true",
        help="replace only the explicitly selected durable receipt path",
    )
    run.add_argument(
        "--overwrite-downloads",
        action="store_true",
        help="replace only the final/manifest destinations after verification",
    )
    return parser


def _parse_handles(values: Sequence[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        kind, separator, token = value.partition(":")
        if not separator or kind not in REFERENCE_KINDS or not token:
            raise SeriesClientError("validation handles must use kind:token")
        result.append({"kind": kind, "token": token})
    return result


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    durable_series_id: str | None = None
    durable_receipt: Path | None = None
    try:
        client = LocalVideoGenClient(
            options.base_url,
            timeout=options.http_timeout,
            upload_timeout=options.upload_timeout,
        )
        command = options.command
        if command == "health":
            result = client.health()
        elif command == "config":
            result = client.config()
        elif command == "recover":
            result = client.recover_from_receipt(options.receipt)
        elif command == "upload":
            result = client.upload(options.kind, options.path)
        elif command == "validate":
            result = {"valid": client.validate_uploads(_parse_handles(options.handles))}
        elif command in {"create", "update", "run"}:
            spec, base_dir = load_series_spec(options.spec)
            if (
                command == "run"
                and isinstance(spec.get("settings"), Mapping)
                and spec["settings"].get("advance") is False
            ):
                if options.until == "terminal":
                    print(
                        "review mode: advance=false; --until terminal requires another "
                        "operator to resume every paused shot",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        "review mode: advance=false; run will return after the next "
                        "preserved shot pauses",
                        file=sys.stderr,
                        flush=True,
                    )
            if command == "update":
                result = client.update_series_from_spec(
                    options.series_id, spec, base_dir=base_dir
                )
            else:
                if (
                    command == "run"
                    and options.receipt is not None
                    and os.path.lexists(options.receipt.expanduser())
                    and not options.overwrite_receipt
                ):
                    raise SeriesClientError(
                        f"receipt target already exists: {options.receipt.expanduser()}"
                    )
                created = client.create_series_from_spec(spec, base_dir=base_dir)
                if command == "create":
                    result = created
                else:
                    series_id = str(created["id"])
                    durable_series_id = series_id
                    output_dir = options.output_dir.expanduser()
                    receipt_target = (
                        options.receipt.expanduser()
                        if options.receipt is not None
                        else output_dir / f"{series_id}-receipt.json"
                    )
                    durable_receipt = atomic_write_json(
                        receipt_target,
                        _receipt_document(client, created),
                        overwrite=options.overwrite_receipt,
                    )
                    print(
                        f"created durable series {series_id}; receipt={durable_receipt}",
                        file=sys.stderr,
                        flush=True,
                    )
                    client.start_series(series_id)
                    finished = client.wait_for_series(
                        series_id,
                        interval=options.poll_interval,
                        timeout=options.timeout,
                        stop_statuses=WAIT_MODES[options.until],
                        on_update=_progress_printer(),
                        on_transport_error=_transport_retry_printer,
                    )
                    if finished.get("status") == "paused":
                        print(
                            f"series {series_id} paused for review; resume it explicitly when approved",
                            file=sys.stderr,
                            flush=True,
                        )
                        result = {
                            "series": finished,
                            "receipt": str(durable_receipt),
                            "downloads": {},
                        }
                    elif finished.get("status") != "completed":
                        raise SeriesClientError(
                            f"series ended as {finished.get('status')}; retained attempts were not deleted"
                        )
                    else:
                        final_id = str(
                            client._resolve_artifact(finished, "final")["id"]
                        )
                        manifest_id = str(
                            client._resolve_artifact(finished, "manifest")["id"]
                        )
                        downloads = {
                            "final": client.download_artifact(
                                series_id,
                                final_id,
                                output_dir / f"{series_id}-final.mp4",
                                overwrite=options.overwrite_downloads,
                            ),
                            "manifest": client.download_artifact(
                                series_id,
                                manifest_id,
                                output_dir / f"{series_id}-manifest.json",
                                overwrite=options.overwrite_downloads,
                            ),
                        }
                        result = {
                            "series": finished,
                            "receipt": str(durable_receipt),
                            "downloads": downloads,
                        }
        elif command == "list":
            result = client.list_series(limit=options.limit)
        elif command == "status":
            result = client.get_series(options.series_id)
        elif command == "start":
            result = client.start_series(options.series_id)
        elif command == "pause":
            result = client.pause_series(options.series_id)
        elif command == "resume":
            result = client.resume_series(options.series_id)
        elif command == "cancel-active":
            result = client.cancel_active(options.series_id)
        elif command == "retry-finalization":
            result = client.retry_finalization(options.series_id)
        elif command == "wait":
            result = client.wait_for_series(
                options.series_id,
                interval=options.poll_interval,
                timeout=options.timeout,
                stop_statuses=WAIT_MODES[options.until],
                on_update=_progress_printer(),
                on_transport_error=_transport_retry_printer,
            )
        elif command == "retry":
            result = client.retry_shot(
                options.series_id,
                options.shot_index,
                regenerate_following=options.regenerate_following,
            )
        elif command == "set-reference-policy":
            result = client.set_shot_reference_policy(
                options.series_id,
                options.shot_index,
                omit_shared_image_labels=options.omit_shared_image_label,
            )
        elif command == "artifacts":
            result = {"artifacts": client.list_artifacts(options.series_id)}
        elif command == "download":
            result = client.download_artifact(
                options.series_id,
                options.selector,
                options.output,
                overwrite=options.overwrite,
            )
        else:  # pragma: no cover - argparse makes this unreachable.
            raise SeriesClientError("unknown command")
        _print_json(result)
        return 0
    except KeyboardInterrupt:
        durable_note = (
            f" Durable series ID: {durable_series_id}; receipt: {durable_receipt}."
            if durable_series_id
            else ""
        )
        print(
            "Interrupted; durable series state and generated attempts were left intact."
            + durable_note,
            file=sys.stderr,
        )
        return 130
    except SeriesClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if durable_series_id:
            print(
                f"durable series remains saved: id={durable_series_id}; receipt={durable_receipt}",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
