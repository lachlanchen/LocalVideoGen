"""Loopback-only aiohttp service for the local MiniMax H3 studio."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .comfy_client import ComfyClient, ComfyError, flatten_outputs
from .job_store import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    JobStore,
    JobStoreError,
    JobStoreValidationError,
)
from .media import (
    DEFAULT_LIMITS,
    MediaValidationError,
    prepare_upload,
    validate_video_probe,
)
from .series_media import SeriesMedia, SeriesMediaError
from .series_runner import (
    LALACHAN_REFERENCE_LABELS,
    REFERENCE_POLICY_STATES,
    WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS,
    WORLD_TRAVEL_PERSISTENT_IMAGE_LABELS,
    SeriesRunner,
    build_series_document,
    find_artifact,
    public_series,
    public_series_summary,
)
from .series_store import (
    SeriesStore,
    SeriesStoreError,
    SeriesStoreValidationError,
    canonical_series_id,
)
from .settings_store import (
    MAX_SYSTEM_PROMPT_CHARS,
    SettingsStore,
    SettingsStoreError,
    SettingsStoreValidationError,
)
from .workflows import (
    PROFILES,
    RequestError,
    UploadedAsset,
    apply_system_prompt,
    compile_prompt,
    parse_render_spec,
    profile_requires_two_devices,
    public_config,
)


LOGGER = logging.getLogger("h3-webapp")
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
STATIC_ROOT = PACKAGE_ROOT / "static"
DEFAULT_JOB_DB = PROJECT_ROOT / "runtime" / "private" / "webapp-jobs.sqlite3"
DEFAULT_SERIES_DB = PROJECT_ROOT / "runtime" / "private" / "webapp-series.sqlite3"
DEFAULT_SETTINGS_FILE = PROJECT_ROOT / "runtime" / "private" / "h3-studio-settings.json"
DEFAULT_SERIES_ARTIFACT_ROOT = PROJECT_ROOT / "runtime" / "private" / "series-artifacts"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "ComfyUI" / "output"
START_ENGINE_COMMAND = f"cd {PROJECT_ROOT} && H3_CUDA_DEVICES=0,1 ./scripts/start_comfyui.sh"
COMFY_KEY = web.AppKey("h3.comfy")
ASSETS_KEY = web.AppKey("h3.assets")
JOBS_KEY = web.AppKey("h3.jobs")
SETTINGS_KEY = web.AppKey("h3.settings")
OUTPUT_ROOT_KEY = web.AppKey("h3.output_root")
INPUT_ROOT_KEY = web.AppKey("h3.input_root")
JOB_WATCHER_KEY = web.AppKey("h3.job_watcher")
SERIES_KEY = web.AppKey("h3.series")
SERIES_MEDIA_KEY = web.AppKey("h3.series_media")
SERIES_RUNNER_KEY = web.AppKey("h3.series_runner")
SUBMISSION_LOCK_KEY = web.AppKey("h3.submission_lock")
MISSING_JOB_GRACE_MS = 5 * 60 * 1000
SERIES_API_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

UPLOAD_RULES: dict[str, dict[str, Any]] = {
    "image": {
        "extensions": {".png", ".jpg", ".jpeg", ".webp", ".bmp"},
        "mime_prefixes": ("image/",),
        "max_bytes": 30 * 1024 * 1024,
    },
    "video": {
        "extensions": {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"},
        "mime_prefixes": ("video/", "application/octet-stream"),
        "max_bytes": DEFAULT_LIMITS.max_video_source_bytes,
    },
    "audio": {
        "extensions": {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".opus"},
        "mime_prefixes": ("audio/", "application/ogg", "application/octet-stream"),
        "max_bytes": 100 * 1024 * 1024,
    },
}


@dataclass(frozen=True)
class AssetRecord:
    token: str
    asset: UploadedAsset
    size: int
    created: float


class AssetRegistry:
    """Short-lived opaque handles; clients never supply filesystem paths."""

    def __init__(self, *, ttl: float = 24 * 3600, maximum: int = 256) -> None:
        self.ttl = ttl
        self.maximum = maximum
        self._items: OrderedDict[str, AssetRecord] = OrderedDict()

    def register(self, asset: UploadedAsset, size: int) -> AssetRecord:
        self.purge()
        token = secrets.token_urlsafe(24)
        record = AssetRecord(token=token, asset=asset, size=size, created=time.time())
        self._items[token] = record
        while len(self._items) > self.maximum:
            self._items.popitem(last=False)
        return record

    def resolve(self, token: Any, kind: str, *, optional: bool = False) -> UploadedAsset | None:
        self.purge()
        if (token is None or token == "") and optional:
            return None
        if not isinstance(token, str):
            raise RequestError(f"expected a valid {kind} upload token")
        record = self._items.get(token)
        if record is None or record.asset.kind != kind:
            raise RequestError(f"the {kind} upload expired or is invalid; upload it again")
        self._items.move_to_end(token)
        return record.asset

    def valid(self, token: Any, kind: Any) -> bool:
        self.purge()
        if not isinstance(token, str) or not isinstance(kind, str):
            return False
        record = self._items.get(token)
        return record is not None and record.asset.kind == kind

    def purge(self) -> None:
        cutoff = time.time() - self.ttl
        expired = [token for token, record in self._items.items() if record.created < cutoff]
        for token in expired:
            self._items.pop(token, None)


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise web.HTTPBadRequest(text="invalid job id") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise web.HTTPBadRequest(text="invalid job id")
    return canonical


def _clean_original_name(value: str) -> str:
    name = PurePosixPath(value.replace("\\", "/")).name.strip()
    if not name:
        raise RequestError("upload filename is missing")
    return name[:160]


def _mime_allowed(content_type: str, prefixes: tuple[str, ...]) -> bool:
    content_type = content_type.lower().split(";", 1)[0].strip()
    return any(content_type == prefix or content_type.startswith(prefix) for prefix in prefixes)


def _request_is_loopback(request: web.Request) -> bool:
    """Require both the TCP peer and Host header to identify loopback."""

    peer = request.transport.get_extra_info("peername") if request.transport else None
    try:
        if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
            return False
    except (ValueError, TypeError, IndexError):
        return False
    try:
        host = urlsplit(f"//{request.host}").hostname
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _apply_security_headers(response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    )


async def _local_model_status() -> tuple[str, str]:
    model_root = PROJECT_ROOT / "ComfyUI" / "models"
    if any(model_root.rglob("*.aria2")):
        return "downloading", "Model files are still downloading"
    receipt = PROJECT_ROOT / "runtime" / "models.verified"
    if not receipt.is_file():
        return "unverified", "The aligned bundle has no verification receipt"

    def current_fingerprint() -> str | None:
        try:
            result = subprocess.run(
                [str(PROJECT_ROOT / "scripts" / "model_fingerprint.sh")],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    fingerprint = await asyncio.to_thread(current_fingerprint)
    try:
        recorded = await asyncio.to_thread(receipt.read_text)
    except OSError:
        recorded = ""
    if fingerprint and recorded.strip() == fingerprint:
        return "verified", "Aligned nine-file bundle"
    return "invalid", "Model files changed after verification"


async def _project_comfy_status(client: ComfyClient) -> tuple[bool, str]:
    """Verify that the configured backend is this project's captured runtime."""

    def inspect() -> tuple[bool, str]:
        state_path = PROJECT_ROOT / "runtime" / "comfyui-state.json"
        try:
            if not state_path.is_file():
                return False, "No verified LocalVideoGen ComfyUI runtime is active"
            result = subprocess.run(
                [
                    str(PROJECT_ROOT / ".venv" / "bin" / "python"),
                    str(PROJECT_ROOT / "scripts" / "runtime_identity.py"),
                    "verify",
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                return False, "No verified LocalVideoGen ComfyUI runtime is active"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            configured = urlsplit(client.base_url)
            configured_port = configured.port or (443 if configured.scheme == "https" else 80)
            if (
                configured.scheme != "http"
                or configured.hostname not in {"127.0.0.1", "localhost", "::1"}
                or int(state.get("port", -1)) != configured_port
            ):
                return False, "Configured ComfyUI URL does not match the verified project runtime"
        except (AttributeError, json.JSONDecodeError, OSError, subprocess.SubprocessError, TypeError, ValueError):
            return False, "The LocalVideoGen ComfyUI runtime identity could not be verified"
        return True, "Verified LocalVideoGen ComfyUI runtime"

    return await asyncio.to_thread(inspect)


async def _require_project_comfy(request: web.Request) -> None:
    verified, note = await _project_comfy_status(request.app[COMFY_KEY])
    if not verified:
        raise ComfyError(note, status=409)


def _resolve_asset_payload(payload: Mapping[str, Any], registry: AssetRegistry) -> dict[str, Any]:
    def resolve_list(key: str, kind: str, maximum: int) -> list[UploadedAsset]:
        raw = payload.get(key) or []
        if isinstance(raw, (str, bytes)) or not isinstance(raw, list):
            raise RequestError(f"{key} must be a list")
        if len(raw) > maximum:
            raise RequestError(f"{key} accepts at most {maximum} uploads")
        return [registry.resolve(token, kind) for token in raw]  # type: ignore[list-item]

    ref_videos = resolve_list("ref_videos", "video", 3)
    raw_overrides = payload.get("ref_video_audios") or []
    if not isinstance(raw_overrides, list) or len(raw_overrides) > len(ref_videos):
        raise RequestError("ref_video_audios must align with reference videos")
    ref_video_audios = [registry.resolve(token, "audio", optional=True) for token in raw_overrides]
    ref_video_audios.extend([None] * (len(ref_videos) - len(ref_video_audios)))
    return {
        "first_frame": registry.resolve(payload.get("first_frame"), "image", optional=True),
        "last_frame": registry.resolve(payload.get("last_frame"), "image", optional=True),
        "ref_images": resolve_list("ref_images", "image", 9),
        "ref_videos": ref_videos,
        "ref_video_audios": ref_video_audios,
        "ref_audios": resolve_list("ref_audios", "audio", 3),
    }


def _reference_assets(assets: Mapping[str, Any]) -> list[UploadedAsset]:
    """Flatten resolved Single Clip inputs in their actual graph order."""

    result: list[UploadedAsset] = []
    for key in ("first_frame", "last_frame"):
        item = assets.get(key)
        if isinstance(item, UploadedAsset):
            result.append(item)
    for key in ("ref_images", "ref_videos", "ref_video_audios", "ref_audios"):
        items = assets.get(key) or []
        if isinstance(items, (str, bytes)) or not isinstance(items, list):
            continue
        result.extend(item for item in items if isinstance(item, UploadedAsset))
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _verify_single_reference_integrity(
    assets: Mapping[str, Any],
    *,
    input_root: Path | None,
    media: SeriesMedia,
) -> None:
    """Recheck every resolved input immediately before upstream submission."""

    references = _reference_assets(assets)
    if not references:
        return
    if input_root is None:
        raise SeriesMediaError(
            "the trusted ComfyUI input directory is unavailable; restart this "
            "project's H3 Studio before submitting references"
        )
    root = input_root.resolve()
    for asset in references:
        metadata = asset.metadata
        digest = (
            str(metadata.get("sha256") or "").lower()
            if isinstance(metadata, Mapping)
            else ""
        )
        if not SHA256_PATTERN.fullmatch(digest):
            raise SeriesMediaError(
                f"reference '{asset.original_name or asset.kind}' has no trusted "
                "upload fingerprint; remove it and upload it again"
            )
        path_value = str(asset.path or "")
        relative = PurePosixPath(path_value)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in path_value
        ):
            raise SeriesMediaError("a Single Clip reference has an unsafe input path")
        candidate = root.joinpath(*relative.parts)
        try:
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise OSError("symbolic reference")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise OSError("not a regular file")
            actual_digest = await asyncio.to_thread(_sha256_file, resolved)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SeriesMediaError(
                f"reference '{asset.original_name or asset.kind}' is no longer "
                "an owned regular input; remove it and upload it again"
            ) from exc
        if actual_digest != digest:
            raise SeriesMediaError(
                f"reference '{asset.original_name or asset.kind}' changed after "
                "upload; refusing the GPU submission"
            )
        if asset.kind != "video":
            continue
        try:
            probed = validate_video_probe(
                await media.probe(resolved), normalized=True
            )
            stored_width = metadata.get("width")
            stored_height = metadata.get("height")
            stored_audio = metadata.get("has_audio")
            if (
                isinstance(stored_width, bool)
                or not isinstance(stored_width, int)
                or isinstance(stored_height, bool)
                or not isinstance(stored_height, int)
                or not isinstance(stored_audio, bool)
                or int(probed["width"]) != stored_width
                or int(probed["height"]) != stored_height
                or bool(probed["has_audio"]) != stored_audio
                or int(probed.get("rotation") or 0) != 0
            ):
                raise MediaValidationError(
                    "normalized video metadata no longer matches its upload record"
                )
        except (MediaValidationError, KeyError, TypeError, ValueError) as exc:
            raise SeriesMediaError(
                f"reference video '{asset.original_name or 'video'}' no longer "
                "matches the normalized H3 input contract; remove it and upload "
                "it again"
            ) from exc


def _job_summary(job: Mapping[str, Any], client: ComfyClient) -> dict[str, Any]:
    job_id = str(job.get("id") or "")
    result = {
        "id": job_id,
        "status": str(job.get("status") or "unknown"),
        "create_time": job.get("create_time"),
        "execution_start_time": job.get("execution_start_time"),
        "execution_end_time": job.get("execution_end_time"),
        "outputs_count": int(job.get("outputs_count") or 0),
        "previewable_outputs_count": int(job.get("previewable_outputs_count") or 0),
        "progress": client.job_progress(job_id),
    }
    error = job.get("execution_error")
    if isinstance(error, Mapping):
        result["error"] = str(error.get("exception_message") or error.get("message") or "Render failed")[:1000]
    return result


def _normalized_job_status(value: Any, fallback: str = "pending") -> str:
    status = str(value or fallback).lower()
    return {
        "success": "completed",
        "error": "failed",
        "running": "in_progress",
        "queued": "pending",
    }.get(
        status,
        status
        if status in {"submitting", "pending", "in_progress", "cancelling", "completed", "failed", "cancelled"}
        else fallback,
    )


def _job_error(job: Mapping[str, Any]) -> str | None:
    error = job.get("execution_error")
    if isinstance(error, Mapping):
        return str(error.get("exception_message") or error.get("message") or "Render failed")[:8192]
    if isinstance(error, str):
        return error[:8192]
    return None


def _sync_stored_job(store: JobStore, job_id: str, job: Mapping[str, Any]) -> dict[str, Any]:
    current = store.get(job_id)
    if current is None:
        raise web.HTTPNotFound(text="job not found")
    status = _normalized_job_status(job.get("status"), str(current["status"]))
    current_status = str(current["status"])
    current_terminal = current_status in TERMINAL_STATUSES
    # Terminal observations are monotonic.  A completed artifact has highest
    # precedence; stale active/failed/cancelled responses must never hide it.
    if current_status == "completed" or (
        current_terminal and status != "completed"
    ):
        status = current_status
    updates: dict[str, Any] = {}
    if isinstance(job.get("outputs"), Mapping):
        outputs = flatten_outputs(job)
        current_outputs = current.get("outputs") or []
        if outputs != current_outputs and (
            not current_terminal or (outputs and not current_outputs)
        ):
            updates["outputs"] = outputs
    if not current_terminal and "execution_error" in job:
        error = _job_error(job)
        if error != current.get("error"):
            updates["error"] = error
    elif status == "completed" and current.get("error") is not None:
        updates["error"] = None
    status_update = status if status != current.get("status") else None
    if status_update is None and not updates:
        return current
    return store.update(job_id, status_update, **updates)


def _cancel_stored_job_if_active(
    store: JobStore, job_id: str, *, error: str | None = None
) -> dict[str, Any]:
    """Synchronously terminalize cancellation unless a watcher already won."""

    latest = store.get(job_id)
    if latest is None:
        raise web.HTTPNotFound(text="job not found")
    if latest["status"] not in ACTIVE_STATUSES:
        return latest
    return store.update(job_id, "cancelled", error=error)


def _cancel_response(record: Mapping[str, Any], *, already_missing: bool = False):
    if record.get("status") == "completed":
        return web.json_response({"cancelled": False, "already_completed": True})
    body = {"cancelled": record.get("status") == "cancelled"}
    if already_missing:
        body["already_missing"] = True
    return web.json_response(body)


def _terminalize_missing_job(store: JobStore, record: Mapping[str, Any], error: ComfyError) -> dict[str, Any]:
    """Close a stale active row only when a reachable engine explicitly says 404."""

    if error.status != 404 or record.get("status") in {"completed", "failed", "cancelled"}:
        return dict(record)
    try:
        age_ms = int(time.time() * 1000) - int(record["updated_ms"])
    except (KeyError, TypeError, ValueError):
        return dict(record)
    if age_ms < MISSING_JOB_GRACE_MS:
        return dict(record)
    return store.update(
        str(record["id"]),
        "failed",
        error="The local engine no longer has this job; it may have restarted.",
    )


def _stored_job_summary(record: Mapping[str, Any], client: ComfyClient) -> dict[str, Any]:
    job_id = str(record["id"])
    outputs = record.get("outputs") if isinstance(record.get("outputs"), list) else []
    result: dict[str, Any] = {
        "id": job_id,
        "status": _normalized_job_status(record.get("status")),
        "create_time": record.get("created_ms"),
        "execution_start_time": None,
        "execution_end_time": record.get("updated_ms") if record.get("status") in {"completed", "failed", "cancelled"} else None,
        "outputs_count": len(outputs),
        "previewable_outputs_count": len(outputs),
        "progress": client.job_progress(job_id),
    }
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        prompt = " ".join(str(metadata.get("prompt") or "").split())
        if len(prompt) > 88:
            prompt = prompt[:87].rstrip() + "…"
        result["session"] = {
            "title": prompt or "Untitled H3 session",
            "mode": metadata.get("mode"),
            "profile": metadata.get("profile"),
            "width": metadata.get("width"),
            "height": metadata.get("height"),
            "duration": metadata.get("duration"),
            "deletable": (
                record.get("status") in TERMINAL_STATUSES
                and not bool(metadata.get("series_id"))
            ),
            "managed_by_series": bool(metadata.get("series_id")),
        }
    if record.get("error"):
        result["error"] = str(record["error"])[:1000]
    return result


def _stored_job_detail(record: Mapping[str, Any], client: ComfyClient) -> dict[str, Any]:
    result = _stored_job_summary(record, client)
    outputs: list[dict[str, Any]] = []
    for raw in record.get("outputs") or []:
        item = dict(raw)
        item["url"] = f"/api/jobs/{result['id']}/outputs/{item['id']}"
        item["download_url"] = item["url"] + "?download=1"
        item.pop("filename", None)
        item.pop("subfolder", None)
        item.pop("type", None)
        outputs.append(item)
    result["outputs"] = outputs
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        result["render"] = dict(metadata)
    return result


def _local_output_path(output_root: Path, item: Mapping[str, Any]) -> Path | None:
    try:
        root = output_root.resolve(strict=True)
        relative = PurePosixPath(str(item.get("subfolder") or "")) / str(item["filename"])
        candidate = (root / Path(*relative.parts)).resolve(strict=True)
        candidate.relative_to(root)
    except (KeyError, OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _deletable_output_path(output_root: Path, item: Mapping[str, Any]) -> Path | None:
    """Resolve one stored output without following any symbolic links."""

    try:
        root = output_root.resolve(strict=True)
        relative = PurePosixPath(str(item.get("subfolder") or "")) / str(item["filename"])
        candidate = root / Path(*relative.parts)
        candidate.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ComfyError(
                    "This session output is linked to another location and was not deleted.",
                    status=409,
                )
        if not candidate.exists():
            return None
        if not candidate.is_file():
            raise ComfyError("This session output is not a regular file.", status=409)
        candidate.resolve(strict=True).relative_to(root)
        return candidate
    except ComfyError:
        raise
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise ComfyError("This session output has an unsafe local location.", status=409) from exc


def _job_detail(job: Mapping[str, Any], client: ComfyClient) -> dict[str, Any]:
    result = _job_summary(job, client)
    outputs = flatten_outputs(job)
    job_id = result["id"]
    for item in outputs:
        item["url"] = f"/api/jobs/{job_id}/outputs/{item['id']}"
        item["download_url"] = item["url"] + "?download=1"
        item.pop("filename", None)
        item.pop("subfolder", None)
        item.pop("type", None)
    result["outputs"] = outputs
    workflow = job.get("workflow")
    if isinstance(workflow, Mapping):
        extra = workflow.get("extra_data")
        if isinstance(extra, Mapping):
            pnginfo = extra.get("extra_pnginfo")
            if isinstance(pnginfo, Mapping) and isinstance(pnginfo.get("h3_webapp"), Mapping):
                metadata = pnginfo["h3_webapp"]
                result["render"] = {
                    key: metadata.get(key)
                    for key in ("mode", "profile", "width", "height", "duration", "length", "seed")
                }
    return result


@web.middleware
async def security_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        if not _request_is_loopback(request):
            raise web.HTTPMisdirectedRequest(text="this service accepts loopback requests only")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                raise web.HTTPForbidden(text="cross-site requests are not allowed")
            origin = request.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                expected = request.host.lower()
                if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != expected:
                    raise web.HTTPForbidden(text="origin does not match this local service")
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    _apply_security_headers(response)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    if isinstance(response, web.HTTPException):
        raise response
    return response


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (
        RequestError,
        JobStoreValidationError,
        SettingsStoreValidationError,
        SeriesStoreValidationError,
        ValueError,
    ) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except ComfyError as exc:
        LOGGER.warning("ComfyUI request failed: %s", exc)
        body: dict[str, Any] = {"error": str(exc)}
        if exc.details is not None:
            body["details"] = exc.details
        return web.json_response(body, status=exc.status)
    except JobStoreError:
        LOGGER.exception("Durable job registry failure")
        return web.json_response({"error": "the private job registry is unavailable"}, status=503)
    except SettingsStoreError:
        LOGGER.exception("Durable studio settings failure")
        return web.json_response({"error": "the private studio settings are unavailable"}, status=503)
    except SeriesStoreError:
        LOGGER.exception("Durable series registry failure")
        return web.json_response({"error": "the private series registry is unavailable"}, status=503)
    except SeriesMediaError as exc:
        LOGGER.warning("Series media request failed: %s", exc)
        return web.json_response({"error": str(exc)}, status=409)
    except Exception:
        LOGGER.exception("Unhandled webapp request error")
        return web.json_response({"error": "internal webapp error"}, status=500)


def create_app(
    client: ComfyClient | None = None,
    *,
    job_store: JobStore | None = None,
    settings_store: SettingsStore | None = None,
    output_root: Path | None = None,
    series_store: SeriesStore | None = None,
    series_media: SeriesMedia | None = None,
    series_runner: SeriesRunner | None = None,
    input_root: Path | None = None,
) -> web.Application:
    app = web.Application(
        client_max_size=620 * 1024 * 1024,
        middlewares=[security_middleware, error_middleware],
    )
    app[COMFY_KEY] = client or ComfyClient("http://127.0.0.1:8188")
    app[ASSETS_KEY] = AssetRegistry()
    app[JOBS_KEY] = job_store or JobStore(DEFAULT_JOB_DB)
    settings_path = (
        app[JOBS_KEY].path.with_name("h3-studio-settings.json")
        if job_store is not None
        else DEFAULT_SETTINGS_FILE
    )
    app[SETTINGS_KEY] = settings_store or SettingsStore(settings_path)
    app[SUBMISSION_LOCK_KEY] = asyncio.Lock()
    app[OUTPUT_ROOT_KEY] = (output_root or DEFAULT_OUTPUT_ROOT).resolve()
    app[INPUT_ROOT_KEY] = (
        input_root.resolve()
        if input_root is not None
        else (
            (PROJECT_ROOT / "ComfyUI" / "input").resolve()
            if isinstance(app[COMFY_KEY], ComfyClient)
            else None
        )
    )
    if series_store is None:
        series_db = (
            app[JOBS_KEY].path.with_name("webapp-series.sqlite3")
            if job_store is not None
            else DEFAULT_SERIES_DB
        )
        series_store = SeriesStore(series_db)
    app[SERIES_KEY] = series_store
    if series_media is None:
        artifact_root = (
            app[JOBS_KEY].path.parent / "series-artifacts"
            if job_store is not None or output_root is not None
            else DEFAULT_SERIES_ARTIFACT_ROOT
        )
        series_media = SeriesMedia(app[OUTPUT_ROOT_KEY], artifact_root)
    app[SERIES_MEDIA_KEY] = series_media

    async def verify_series_runtime() -> None:
        verified, note = await _project_comfy_status(app[COMFY_KEY])
        if not verified:
            raise ComfyError(note, status=409)

    async def verify_series_submission() -> None:
        await verify_series_runtime()
        model_status, model_note = await _local_model_status()
        if model_status != "verified":
            raise ComfyError(model_note, status=409)

    app[SERIES_RUNNER_KEY] = series_runner or SeriesRunner(
        app[SERIES_KEY],
        app[JOBS_KEY],
        app[COMFY_KEY],
        app[SERIES_MEDIA_KEY],
        submission_lock=app[SUBMISSION_LOCK_KEY],
        runtime_check=verify_series_runtime,
        submission_check=verify_series_submission,
        input_root=app[INPUT_ROOT_KEY],
        system_prompt_provider=lambda: str(
            app[SETTINGS_KEY].get()["system_prompt"]
        ),
    )

    async def watch_jobs(application: web.Application) -> None:
        while True:
            try:
                verified, _ = await _project_comfy_status(application[COMFY_KEY])
                if not verified:
                    await application[COMFY_KEY].close()
                    await asyncio.sleep(3)
                    continue
                active = application[JOBS_KEY].active(limit=100)
                for stored in active:
                    job_id = str(stored["id"])
                    try:
                        record = await application[COMFY_KEY].get_job(job_id)
                    except ComfyError as exc:
                        _terminalize_missing_job(application[JOBS_KEY], stored, exc)
                        continue
                    if isinstance(record, Mapping):
                        _sync_stored_job(application[JOBS_KEY], job_id, record)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Background job reconciliation failed")
            await asyncio.sleep(3)

    async def on_startup(application: web.Application) -> None:
        verified, _ = await _project_comfy_status(application[COMFY_KEY])
        if verified:
            await application[COMFY_KEY].open()
        application[JOB_WATCHER_KEY] = asyncio.create_task(watch_jobs(application), name="h3-job-registry-sync")
        application[SERIES_RUNNER_KEY].start()

    async def on_cleanup(application: web.Application) -> None:
        await application[SERIES_RUNNER_KEY].close()
        task = application.get(JOB_WATCHER_KEY)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await application[COMFY_KEY].close()

    async def on_response_prepare(_: web.Request, response: web.StreamResponse) -> None:
        # Stream proxies prepare their response inside the route, so middleware
        # alone is too late to put these headers on the wire.
        _apply_security_headers(response)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.on_response_prepare.append(on_response_prepare)

    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_: web.Request) -> web.StreamResponse:
        response = web.FileResponse(STATIC_ROOT / "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @routes.get("/favicon.ico")
    async def favicon(_: web.Request) -> web.Response:
        return web.Response(status=204, headers={"Cache-Control": "public, max-age=86400"})

    @routes.get("/api/config")
    async def config(_: web.Request) -> web.Response:
        data = public_config()
        data["version"] = __version__
        data["series_api_version"] = SERIES_API_VERSION
        data["engine_start_command"] = START_ENGINE_COMMAND
        data["limits"]["system_prompt_chars"] = MAX_SYSTEM_PROMPT_CHARS
        data["uploads"] = {
            "multipart_field": "file",
            "kinds": ["image", "video", "audio"],
            "normalized_max_bytes": DEFAULT_LIMITS.max_comfy_file_bytes,
            "image": {
                "extensions": sorted(UPLOAD_RULES["image"]["extensions"]),
                "source_max_bytes": DEFAULT_LIMITS.max_image_source_bytes,
                "source_max_edge": DEFAULT_LIMITS.max_image_edge,
                "source_max_pixels": DEFAULT_LIMITS.max_image_pixels,
                "decoded_formats": ["PNG", "JPEG", "WEBP", "BMP"],
                "single_frame": True,
                "normalized": {
                    "format": "PNG",
                    "content_type": "image/png",
                },
            },
            "video": {
                "extensions": sorted(UPLOAD_RULES["video"]["extensions"]),
                "source_max_bytes": DEFAULT_LIMITS.max_video_source_bytes,
                "source_max_edge": DEFAULT_LIMITS.max_source_video_edge,
                "source_max_pixels": DEFAULT_LIMITS.max_source_video_pixels,
                "source_fps": {
                    "min": DEFAULT_LIMITS.min_source_video_fps,
                    "max": DEFAULT_LIMITS.max_source_video_fps,
                },
                "source_audio_sample_rate_max": (
                    DEFAULT_LIMITS.max_source_audio_sample_rate
                ),
                "source_audio_channels_max": DEFAULT_LIMITS.max_audio_channels,
                "source_streams_max": DEFAULT_LIMITS.max_streams,
                "duration_seconds": {
                    "min": DEFAULT_LIMITS.video_min_seconds,
                    "max": DEFAULT_LIMITS.video_max_seconds,
                },
                "normalized": {
                    "container": "MP4",
                    "video_codec": "H.264",
                    "pixel_format": "yuv420p",
                    "fps": DEFAULT_LIMITS.video_fps,
                    "max_edge": DEFAULT_LIMITS.normalized_video_edge,
                    "max_pixels": DEFAULT_LIMITS.normalized_video_pixels,
                    "audio_optional": True,
                    "audio_codec": "AAC",
                    "audio_sample_rate": DEFAULT_LIMITS.audio_sample_rate,
                    "audio_channels": 2,
                },
            },
            "audio": {
                "extensions": sorted(UPLOAD_RULES["audio"]["extensions"]),
                "source_max_bytes": DEFAULT_LIMITS.max_audio_source_bytes,
                "source_sample_rate_max": (
                    DEFAULT_LIMITS.max_source_audio_sample_rate
                ),
                "source_channels_max": DEFAULT_LIMITS.max_audio_channels,
                "source_streams_max": DEFAULT_LIMITS.max_streams,
                "duration_seconds": {
                    "min": DEFAULT_LIMITS.audio_min_seconds,
                    "max": DEFAULT_LIMITS.audio_max_seconds,
                },
                "normalized": {
                    "format": "WAV",
                    "codec": "pcm_s16le",
                    "sample_rate": DEFAULT_LIMITS.audio_sample_rate,
                    "channels": 2,
                },
            },
        }
        data["series"] = {
            "templates": ["lalachan", "movie", "world_travel"],
            "shot_min": 2,
            "shot_max": 12,
            "total_seconds_max": 180,
            "shared_images_max": 8,
            "shared_videos_max": 2,
            "shared_audio_max": 3,
            "continuity_seconds": [0, 2, 3, 4],
            "default_settings": {
                "profile": data["long_reference"]["profile"],
                "width": data["long_reference"]["landscape"]["width"],
                "height": data["long_reference"]["landscape"]["height"],
                "ref_image_size": data["long_reference"]["ref_image_size"],
            },
            "lalachan_picture_labels": list(LALACHAN_REFERENCE_LABELS),
            "world_travel_scene_reference_per_shot": 1,
            "sequential_only": True,
            "shot_reference_policy": {
                "field": "omit_shared_image_labels",
                "logical_picture_tags_remapped": True,
                "first_shot_must_keep_all": True,
                "recommended_omissions_after_first": list(
                    WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS
                ),
                "editable_states": sorted(REFERENCE_POLICY_STATES),
                "endpoint": "/api/series/{series_id}/shots/{shot_index}/reference-policy",
            },
            "capabilities": {
                "world_travel": {
                    "template": "world_travel",
                    "render_mode": "r2v",
                    "maximum_quality_profile": "quality_bf16_dual",
                    "long_reference_safe_profile": "quality_int8_offload",
                    "recommended_continuity_seconds": 2,
                    "persistent_shared_image_labels": [
                        label
                        for label in LALACHAN_REFERENCE_LABELS
                        if label in WORLD_TRAVEL_PERSISTENT_IMAGE_LABELS
                    ],
                    "picture_slots": {
                        "shared": [
                            {"slot": index, "label": label}
                            for index, label in enumerate(
                                LALACHAN_REFERENCE_LABELS, start=1
                            )
                        ],
                        "scene": {
                            "slot": 8,
                            "kind": "image",
                            "scope": "shot",
                            "required": True,
                        },
                        "continuity_final_frame": {
                            "slot": 9,
                            "kind": "image",
                            "scope": "successor_shot",
                            "when_continuity_enabled": True,
                            "sha256_required": True,
                        },
                    },
                    "continuity_tail": {
                        "kind": "video",
                        "placement": "after_shared_videos",
                        "maximum_slot": 3,
                        "sha256_required": True,
                    },
                    "continuity_recovery_requires": [
                        "video_path",
                        "video_sha256",
                        "image_path",
                        "image_sha256",
                    ],
                }
            },
        }
        return web.json_response(data)

    @routes.get("/api/settings")
    async def get_settings(request: web.Request) -> web.Response:
        return web.json_response(request.app[SETTINGS_KEY].get())

    @routes.put("/api/settings")
    async def update_settings(request: web.Request) -> web.Response:
        payload = await request.json(loads=json.loads)
        if not isinstance(payload, Mapping):
            raise RequestError("request body must be a JSON object")
        allowed = {"system_prompt", "preferred_duration"}
        unknown = set(payload) - allowed
        if unknown:
            raise RequestError("unknown studio setting: " + sorted(unknown)[0])
        if not payload:
            raise RequestError("provide a studio setting to save")
        updates = {key: payload[key] for key in allowed if key in payload}
        return web.json_response(request.app[SETTINGS_KEY].update(**updates))

    @routes.get("/api/health")
    async def health(request: web.Request) -> web.Response:
        deep = request.query.get("deep") == "1"
        model_status, model_note = await _local_model_status()
        runtime_verified, runtime_note = await _project_comfy_status(request.app[COMFY_KEY])
        if not runtime_verified:
            return web.json_response(
                {
                    "webapp": True,
                    "connected": False,
                    "ready": False,
                    "message": runtime_note,
                    "model_status": model_status,
                    "model_note": model_note,
                    "engine_start_command": START_ENGINE_COMMAND,
                    "note": "Start the verified project runtime once; this webapp never attaches to another project's backend.",
                }
            )
        try:
            result = await request.app[COMFY_KEY].health(inspect_nodes=deep)
            node_ready = result.get("ready", True)
            result.update(
                {
                    "webapp": True,
                    "ready": bool(node_ready and model_status == "verified"),
                    "model_status": model_status,
                    "model_note": model_note,
                    "engine_start_command": START_ENGINE_COMMAND,
                }
            )
            if model_status != "verified":
                result["message"] = model_note
            return web.json_response(result)
        except ComfyError as exc:
            return web.json_response(
                {
                    "webapp": True,
                    "connected": False,
                    "ready": False,
                    "message": str(exc),
                    "model_status": model_status,
                    "model_note": model_note,
                    "engine_start_command": START_ENGINE_COMMAND,
                    "note": "Start the verified project runtime once; this webapp never launches a duplicate.",
                }
            )

    @routes.post("/api/uploads")
    async def upload(request: web.Request) -> web.Response:
        kind = request.query.get("kind", "")
        rules = UPLOAD_RULES.get(kind)
        if rules is None:
            raise RequestError("upload kind must be image, video, or audio")
        await _require_project_comfy(request)
        if not request.content_type.lower().startswith("multipart/"):
            raise RequestError("upload must use multipart/form-data")
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file" or not part.filename:
            raise RequestError("multipart field 'file' is required")
        original = _clean_original_name(part.filename)
        extension = Path(original).suffix.lower()
        if extension not in rules["extensions"]:
            raise RequestError(f"unsupported {kind} file extension")
        content_type = part.headers.get("Content-Type") or mimetypes.guess_type(original)[0] or "application/octet-stream"
        if not _mime_allowed(content_type, rules["mime_prefixes"]):
            raise RequestError(f"file content type is not valid for a {kind} upload")

        size = 0
        subfolder = f"h3-webapp/{kind}"
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b") as temporary:
            while True:
                chunk = await part.read_chunk(size=1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > rules["max_bytes"]:
                    raise RequestError(f"{kind} upload exceeds the {rules['max_bytes'] // (1024 * 1024)} MiB limit")
                temporary.write(chunk)
            if size == 0:
                raise RequestError("upload is empty")
            if await reader.next() is not None:
                raise RequestError("only one file may be uploaded per request")
            temporary.seek(0)
            prepared = await prepare_upload(
                temporary,
                kind=kind,
                original_name=original,
            )
            try:
                with prepared.open() as upload_file:
                    result = await request.app[COMFY_KEY].upload(
                        fileobj=upload_file,
                        filename=prepared.filename,
                        content_type=prepared.content_type,
                        subfolder=subfolder,
                    )
                normalized_size = prepared.size
                metadata = prepared.metadata
            finally:
                prepared.cleanup()

        asset = UploadedAsset(
            kind=kind,
            path=result["path"],
            original_name=original,
            metadata=metadata,
        )
        record = request.app[ASSETS_KEY].register(asset, normalized_size)
        return web.json_response(
            {
                "token": record.token,
                "kind": kind,
                "name": original,
                "size": normalized_size,
                "source_size": size,
                "metadata": metadata,
            },
            status=201,
        )

    @routes.post("/api/uploads/validate")
    async def validate_uploads(request: web.Request) -> web.Response:
        payload = await request.json(loads=__import__("json").loads)
        if not isinstance(payload, Mapping):
            raise RequestError("request body must be a JSON object")
        uploads = payload.get("uploads")
        if isinstance(uploads, (str, bytes)) or not isinstance(uploads, list):
            raise RequestError("uploads must be a list")
        if len(uploads) > 32:
            raise RequestError("at most 32 upload handles can be validated at once")
        registry = request.app[ASSETS_KEY]
        valid: list[str] = []
        for item in uploads:
            if not isinstance(item, Mapping):
                raise RequestError("each upload handle must be an object")
            token = item.get("token")
            kind = item.get("kind")
            if kind not in UPLOAD_RULES:
                raise RequestError("upload kind must be image, video, or audio")
            if not isinstance(token, str) or not token or len(token) > 256:
                raise RequestError("upload token must be non-empty text")
            if registry.valid(token, kind):
                valid.append(token)
        return web.json_response({"valid": valid})

    @routes.post("/api/renders")
    async def render(request: web.Request) -> web.Response:
        payload = await request.json(loads=__import__("json").loads)
        if not isinstance(payload, Mapping):
            raise RequestError("request body must be a JSON object")
        await _require_project_comfy(request)
        remembered = str(request.app[SETTINGS_KEY].get()["system_prompt"])
        authored_prompt, effective_prompt = apply_system_prompt(
            payload.get("prompt"), remembered
        )
        effective_payload = dict(payload)
        effective_payload["prompt"] = effective_prompt
        resolved = _resolve_asset_payload(effective_payload, request.app[ASSETS_KEY])
        spec = parse_render_spec(effective_payload, resolved)
        model_status, model_note = await _local_model_status()
        if model_status != "verified":
            raise ComfyError(model_note, status=409)
        readiness = await request.app[COMFY_KEY].health(inspect_nodes=True)
        if readiness.get("ready") is False:
            missing = ", ".join(readiness.get("missing_nodes") or [])
            raise ComfyError(
                f"ComfyUI is missing required H3 nodes{': ' + missing if missing else ''}",
                status=409,
            )
        devices = readiness.get("stats", {}).get("devices", [])
        if profile_requires_two_devices(spec.profile) and (
            not isinstance(devices, list) or len(devices) < 2
        ):
            raise ComfyError("The selected profile requires both RTX 4090 GPUs", status=409)
        prompt = compile_prompt(spec)
        prompt_id = str(uuid.uuid4())
        metadata = {
            "mode": spec.mode,
            "profile": spec.profile.id,
            "prompt": authored_prompt,
            "system_prompt": remembered,
            "width": spec.width,
            "height": spec.height,
            "duration": spec.duration,
            "length": spec.length,
            "seed": str(spec.seed),
            "ref_image_size": spec.ref_image_size,
            "references": {
                "first_frame": spec.first_frame.original_name if spec.first_frame else None,
                "last_frame": spec.last_frame.original_name if spec.last_frame else None,
                "images": [asset.original_name for asset in spec.ref_images],
                "videos": [asset.original_name for asset in spec.ref_videos],
                "video_soundtracks": [asset.original_name if asset else None for asset in spec.ref_video_audios],
                "audio": [asset.original_name for asset in spec.ref_audios],
            },
        }
        async with request.app[SUBMISSION_LOCK_KEY]:
            if request.app[JOBS_KEY].active(limit=1):
                raise ComfyError(
                    "Another local H3 render is active; wait for it to finish before starting a Single Clip.",
                    status=409,
                )
            await _verify_single_reference_integrity(
                resolved,
                input_root=request.app[INPUT_ROOT_KEY],
                media=request.app[SERIES_MEDIA_KEY],
            )
            request.app[JOBS_KEY].register(prompt_id, metadata, status="submitting")
            try:
                result = await request.app[COMFY_KEY].submit(prompt, metadata, prompt_id)
            except Exception as exc:
                request.app[JOBS_KEY].update(prompt_id, "failed", error=str(exc)[:8192])
                raise
            request.app[JOBS_KEY].update(prompt_id, "pending", error=None)
        return web.json_response({"id": result["prompt_id"], "number": result.get("number"), "render": metadata}, status=202)

    def series_record(request: web.Request) -> tuple[str, dict[str, Any]]:
        series_id = canonical_series_id(request.match_info["series_id"])
        record = request.app[SERIES_KEY].get(series_id)
        if record is None:
            raise web.HTTPNotFound(text="series not found")
        return series_id, record

    async def series_payload(request: web.Request) -> Mapping[str, Any]:
        payload = await request.json(loads=json.loads)
        if not isinstance(payload, Mapping):
            raise RequestError("request body must be a JSON object")
        return payload

    def resolved_series_document(request: web.Request, payload: Mapping[str, Any]) -> dict[str, Any]:
        registry = request.app[ASSETS_KEY]

        def resolve(token: Any, kind: str, optional: bool) -> UploadedAsset | None:
            return registry.resolve(token, kind, optional=optional)

        return build_series_document(payload, resolve)

    @routes.post("/api/series")
    async def create_series(request: web.Request) -> web.Response:
        payload = await series_payload(request)
        document = resolved_series_document(request, payload)
        record = request.app[SERIES_KEY].create(str(uuid.uuid4()), document, status="ready")
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=201)

    @routes.get("/api/series")
    async def list_series(request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "40"))
        except ValueError as exc:
            raise RequestError("limit must be an integer") from exc
        records = request.app[SERIES_KEY].list(limit=limit)
        # The store's scheduler order is oldest-first; the library is newest-first.
        records.sort(key=lambda item: (item["updated_ms"], item["created_ms"]), reverse=True)
        series = [public_series_summary(record) for record in records]
        return web.json_response(
            {"series": series, "pagination": {"count": len(series), "limit": limit}}
        )

    @routes.get("/api/series/{series_id}")
    async def get_series(request: web.Request) -> web.Response:
        _, record = series_record(request)
        return web.json_response(public_series(record, request.app[COMFY_KEY]))

    @routes.put("/api/series/{series_id}")
    async def update_series(request: web.Request) -> web.Response:
        series_id, existing = series_record(request)
        if existing["status"] != "ready":
            raise RequestError("only a ready series can be edited")
        payload = await series_payload(request)
        replacement = resolved_series_document(request, payload)

        def replace(_: dict[str, Any], status: str):
            if status != "ready":
                raise RequestError("only a ready series can be edited")
            return replacement, "ready"

        record = request.app[SERIES_KEY].mutate(series_id, replace)
        return web.json_response(public_series(record, request.app[COMFY_KEY]))

    @routes.post("/api/series/{series_id}/start")
    async def start_series(request: web.Request) -> web.Response:
        series_id, existing = series_record(request)
        if existing["status"] != "ready":
            raise RequestError("only a ready series can start")
        existing = await request.app[SERIES_RUNNER_KEY].preflight_series(series_id)
        if not getattr(request.app[SERIES_MEDIA_KEY], "available", True):
            raise ComfyError(
                "ffmpeg and ffprobe are required for series validation", status=409
            )
        await _require_project_comfy(request)
        model_status, model_note = await _local_model_status()
        if model_status != "verified":
            raise ComfyError(model_note, status=409)
        readiness = await request.app[COMFY_KEY].health(inspect_nodes=True)
        if readiness.get("ready") is False:
            missing = ", ".join(readiness.get("missing_nodes") or [])
            raise ComfyError(
                f"ComfyUI is missing required H3 nodes{': ' + missing if missing else ''}",
                status=409,
            )
        profile_id = str(existing["document"]["settings"]["profile"])
        devices = readiness.get("stats", {}).get("devices", [])
        if profile_requires_two_devices(PROFILES[profile_id]) and (
            not isinstance(devices, list) or len(devices) < 2
        ):
            raise ComfyError("The selected profile requires both RTX 4090 GPUs", status=409)

        def queue(document: dict[str, Any], status: str):
            if status != "ready":
                raise RequestError("only a ready series can start")
            document["error"] = None
            document["pause_requested"] = False
            document["cancel_requested"] = False
            return document, "queued"

        record = request.app[SERIES_KEY].mutate(series_id, queue)
        request.app[SERIES_RUNNER_KEY].wake()
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=202)

    @routes.post("/api/series/{series_id}/pause")
    async def pause_series(request: web.Request) -> web.Response:
        series_id, _ = series_record(request)
        record = await request.app[SERIES_RUNNER_KEY].pause(series_id)
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=202)

    @routes.post("/api/series/{series_id}/resume")
    async def resume_series(request: web.Request) -> web.Response:
        series_id, _ = series_record(request)
        await request.app[SERIES_RUNNER_KEY].preflight_series(series_id)
        record = request.app[SERIES_RUNNER_KEY].resume(series_id)
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=202)

    @routes.post("/api/series/{series_id}/cancel-active")
    async def cancel_active_series(request: web.Request) -> web.Response:
        series_id, _ = series_record(request)
        await _require_project_comfy(request)
        record = await request.app[SERIES_RUNNER_KEY].cancel(series_id)
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=202)

    @routes.put(
        "/api/series/{series_id}/shots/{shot_index}/reference-policy"
    )
    async def set_shot_reference_policy(request: web.Request) -> web.Response:
        series_id, _ = series_record(request)
        try:
            shot_index = int(request.match_info["shot_index"])
        except ValueError as exc:
            raise RequestError("shot_index must be an integer") from exc
        payload = await series_payload(request)
        if set(payload) != {"omit_shared_image_labels"}:
            raise RequestError(
                "reference policy requires only omit_shared_image_labels"
            )
        record = await request.app[SERIES_RUNNER_KEY].set_shot_reference_policy(
            series_id,
            shot_index,
            omit_shared_image_labels=payload["omit_shared_image_labels"],
        )
        return web.json_response(public_series(record, request.app[COMFY_KEY]))

    async def retry_series_shot(
        request: web.Request, series_id: str, shot_index: int
    ) -> web.Response:
        payload: Mapping[str, Any] = {}
        if request.can_read_body:
            payload = await series_payload(request)
        regenerate_following = payload.get("regenerate_following", False)
        record = await request.app[SERIES_RUNNER_KEY].retry(
            series_id,
            shot_index,
            regenerate_following=regenerate_following,
        )
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=202)

    @routes.post("/api/series/{series_id}/shots/{shot_index}/retry")
    async def retry_shot(request: web.Request) -> web.Response:
        series_id, _ = series_record(request)
        try:
            shot_index = int(request.match_info["shot_index"])
        except ValueError as exc:
            raise RequestError("shot_index must be an integer") from exc
        return await retry_series_shot(request, series_id, shot_index)

    @routes.post("/api/series/{series_id}/retry")
    async def retry_shot_alias(request: web.Request) -> web.Response:
        series_id, _ = series_record(request)
        payload = await series_payload(request)
        try:
            shot_index = int(payload.get("shot_index"))
        except (TypeError, ValueError) as exc:
            raise RequestError("shot_index must be an integer") from exc
        regenerate_following = payload.get("regenerate_following", False)
        record = await request.app[SERIES_RUNNER_KEY].retry(
            series_id,
            shot_index,
            regenerate_following=regenerate_following,
        )
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=202)

    @routes.post("/api/series/{series_id}/retry-finalization")
    async def retry_series_finalization(request: web.Request) -> web.Response:
        series_id, _ = series_record(request)
        record = await request.app[SERIES_RUNNER_KEY].retry_finalization(series_id)
        return web.json_response(public_series(record, request.app[COMFY_KEY]), status=202)

    @routes.get("/api/series/{series_id}/artifacts/{artifact_id}")
    async def series_artifact(request: web.Request) -> web.StreamResponse:
        series_id, record = series_record(request)
        artifact_id = canonical_series_id(request.match_info["artifact_id"])
        artifact = find_artifact(record, artifact_id)
        if artifact is None:
            raise web.HTTPNotFound(text="artifact not found")
        try:
            if artifact.get("storage") == "output" and isinstance(artifact.get("locator"), Mapping):
                path = request.app[SERIES_MEDIA_KEY].output_path(artifact["locator"])
            elif artifact.get("storage") == "series" and isinstance(artifact.get("relative"), str):
                path = request.app[SERIES_MEDIA_KEY].artifact_path(series_id, artifact["relative"])
            else:
                raise SeriesMediaError("series artifact storage is invalid")
        except SeriesMediaError as exc:
            raise web.HTTPNotFound(text="artifact not found") from exc
        response = web.FileResponse(path)
        response.content_type = str(artifact.get("mime") or "application/octet-stream")
        if request.query.get("download") == "1":
            raw_name = PurePosixPath(str(artifact.get("download_name") or "series-artifact")).name
            safe_name = raw_name.replace('"', "_").replace("\\", "_")[:180]
            response.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        return response

    @routes.get("/api/jobs")
    async def jobs(request: web.Request) -> web.Response:
        scope = request.query.get("scope", "all")
        try:
            limit = int(request.query.get("limit", "40"))
        except ValueError as exc:
            raise RequestError("limit must be an integer") from exc
        if scope not in {"all", "active", "history"}:
            raise RequestError("scope must be all, active, or history")
        if not 1 <= limit <= 100:
            raise RequestError("limit must be between 1 and 100")
        engine_connected, _ = await _project_comfy_status(request.app[COMFY_KEY])
        if engine_connected:
            try:
                body = await request.app[COMFY_KEY].list_jobs(scope=scope, limit=limit)
                raw_jobs = body.get("jobs") if isinstance(body, Mapping) else []
                for raw in raw_jobs:
                    if not isinstance(raw, Mapping):
                        continue
                    job_id = str(raw.get("id") or "")
                    if request.app[JOBS_KEY].owns(job_id):
                        _sync_stored_job(request.app[JOBS_KEY], job_id, raw)
            except ComfyError:
                engine_connected = False
        stored = request.app[JOBS_KEY].list(scope=scope, limit=limit)
        safe_jobs = [_stored_job_summary(record, request.app[COMFY_KEY]) for record in stored]
        return web.json_response(
            {
                "jobs": safe_jobs,
                "pagination": {"count": len(safe_jobs), "limit": limit},
                "engine_connected": engine_connected,
            }
        )

    @routes.get("/api/jobs/{job_id}")
    async def job(request: web.Request) -> web.Response:
        job_id = _canonical_uuid(request.match_info["job_id"])
        stored = request.app[JOBS_KEY].get(job_id)
        if stored is None:
            raise web.HTTPNotFound(text="job not found")
        runtime_verified, _ = await _project_comfy_status(request.app[COMFY_KEY])
        if runtime_verified:
            try:
                record = await request.app[COMFY_KEY].get_job(job_id)
                if isinstance(record, Mapping):
                    stored = _sync_stored_job(request.app[JOBS_KEY], job_id, record)
            except ComfyError as exc:
                stored = _terminalize_missing_job(request.app[JOBS_KEY], stored, exc)
        return web.json_response(_stored_job_detail(stored, request.app[COMFY_KEY]))

    @routes.post("/api/jobs/{job_id}/cancel")
    async def cancel(request: web.Request) -> web.Response:
        job_id = _canonical_uuid(request.match_info["job_id"])
        stored = request.app[JOBS_KEY].get(job_id)
        if stored is None:
            raise web.HTTPNotFound(text="job not found")
        await _require_project_comfy(request)
        try:
            result = await request.app[COMFY_KEY].cancel(job_id)
        except ComfyError as exc:
            if exc.status != 404:
                raise
            stored = _cancel_stored_job_if_active(
                request.app[JOBS_KEY],
                job_id,
                error="The local engine no longer had this job.",
            )
            return _cancel_response(stored, already_missing=True)
        if result.get("cancelled") is True:
            stored = _cancel_stored_job_if_active(request.app[JOBS_KEY], job_id)
            if stored["status"] == "completed":
                return _cancel_response(stored)
        else:
            try:
                record = await request.app[COMFY_KEY].get_job(job_id)
                if isinstance(record, Mapping):
                    stored = _sync_stored_job(request.app[JOBS_KEY], job_id, record)
            except ComfyError as exc:
                if exc.status == 404:
                    stored = _cancel_stored_job_if_active(
                        request.app[JOBS_KEY],
                        job_id,
                        error="The local engine no longer had this job.",
                    )
                    return _cancel_response(stored, already_missing=True)
                raise
            return _cancel_response(stored)
        return web.json_response(result)

    @routes.delete("/api/jobs/{job_id}")
    async def delete_job(request: web.Request) -> web.Response:
        job_id = _canonical_uuid(request.match_info["job_id"])
        stored = request.app[JOBS_KEY].get(job_id)
        if stored is None:
            raise web.HTTPNotFound(text="session not found")
        metadata = stored.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("series_id"):
            raise ComfyError(
                "This render belongs to a saved video series and must stay with that series.",
                status=409,
            )
        if stored.get("status") in ACTIVE_STATUSES:
            raise ComfyError(
                "Wait for this session to finish, or cancel it before deleting it.",
                status=409,
            )
        paths: list[Path] = []
        missing = 0
        for item in stored.get("outputs") or []:
            path = _deletable_output_path(request.app[OUTPUT_ROOT_KEY], item)
            if path is None:
                missing += 1
            else:
                paths.append(path)
        deleted_files = 0
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:
                raise ComfyError(
                    "The video could not be removed, so the session was kept.", status=409
                ) from exc
            deleted_files += 1
        deleted = request.app[JOBS_KEY].delete(job_id)
        if deleted is None:
            raise web.HTTPNotFound(text="session not found")
        return web.json_response(
            {
                "deleted": True,
                "id": job_id,
                "files_deleted": deleted_files,
                "files_already_missing": missing,
            }
        )

    @routes.get("/api/jobs/{job_id}/outputs/{output_id}")
    async def output(request: web.Request) -> web.StreamResponse:
        job_id = _canonical_uuid(request.match_info["job_id"])
        try:
            output_id = int(request.match_info["output_id"])
        except ValueError as exc:
            raise web.HTTPNotFound(text="output not found") from exc
        stored = request.app[JOBS_KEY].get(job_id)
        if stored is None:
            raise web.HTTPNotFound(text="output not found")
        runtime_verified, runtime_note = await _project_comfy_status(request.app[COMFY_KEY])
        if runtime_verified:
            try:
                record = await request.app[COMFY_KEY].get_job(job_id)
                if isinstance(record, Mapping):
                    stored = _sync_stored_job(request.app[JOBS_KEY], job_id, record)
            except ComfyError as exc:
                stored = _terminalize_missing_job(request.app[JOBS_KEY], stored, exc)
        outputs = stored.get("outputs") or []
        if output_id < 0 or output_id >= len(outputs):
            raise web.HTTPNotFound(text="output not found")
        local_path = _local_output_path(request.app[OUTPUT_ROOT_KEY], outputs[output_id])
        if local_path is not None:
            response = web.FileResponse(local_path)
            if request.query.get("download") == "1":
                response.headers["Content-Disposition"] = "attachment; filename=MiniMax_H3_output.mp4"
            return response
        if not runtime_verified:
            raise ComfyError(runtime_note, status=409)
        upstream = await request.app[COMFY_KEY].output_response(
            outputs[output_id],
            request.headers,
            head=request.method == "HEAD",
        )
        response = web.StreamResponse(status=upstream.status)
        for header in (
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "ETag",
            "Last-Modified",
            "Cache-Control",
        ):
            if header in upstream.headers:
                response.headers[header] = upstream.headers[header]
        response.headers["Content-Type"] = mimetypes.guess_type(str(outputs[output_id]["filename"]))[0] or "application/octet-stream"
        if request.query.get("download") == "1":
            response.headers["Content-Disposition"] = "attachment; filename=MiniMax_H3_output.mp4"
        await response.prepare(request)
        if request.method == "HEAD" or upstream.status in {304, 416}:
            upstream.release()
            await response.write_eof()
            return response
        try:
            async for chunk in upstream.content.iter_chunked(1024 * 1024):
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            upstream.close()
            raise
        finally:
            upstream.release()
        await response.write_eof()
        return response

    app.add_routes(routes)
    app.router.add_static("/static", STATIC_ROOT, show_index=False, follow_symlinks=False)
    return app


def _loopback_host(value: str) -> str:
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise argparse.ArgumentTypeError("the webapp may only bind to a loopback address")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local MiniMax H3 web studio")
    parser.add_argument("--host", type=_loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("H3_WEBAPP_PORT", "8190")))
    parser.add_argument("--comfy-url", default=os.environ.get("H3_COMFY_URL", "http://127.0.0.1:8188"))
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        client = ComfyClient(args.comfy_url)
    except ValueError as exc:
        parser.error(str(exc))
    LOGGER.info("Serving MiniMax H3 studio at http://%s:%s", args.host, args.port)
    LOGGER.info("Connecting to the existing ComfyUI service at %s", client.base_url)
    web.run_app(create_app(client), host=args.host, port=args.port, print=None, handle_signals=True)


if __name__ == "__main__":
    main()
