"""Loopback-only aiohttp service for the local MiniMax H3 studio."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import mimetypes
import os
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
from .job_store import JobStore, JobStoreError, JobStoreValidationError
from .media import DEFAULT_LIMITS, prepare_upload
from .workflows import RequestError, UploadedAsset, compile_prompt, parse_render_spec, public_config


LOGGER = logging.getLogger("h3-webapp")
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
STATIC_ROOT = PACKAGE_ROOT / "static"
DEFAULT_JOB_DB = PROJECT_ROOT / "runtime" / "private" / "webapp-jobs.sqlite3"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "ComfyUI" / "output"
START_ENGINE_COMMAND = f"cd {PROJECT_ROOT} && H3_CUDA_DEVICES=0,1 ./scripts/start_comfyui.sh"
COMFY_KEY = web.AppKey("h3.comfy")
ASSETS_KEY = web.AppKey("h3.assets")
JOBS_KEY = web.AppKey("h3.jobs")
OUTPUT_ROOT_KEY = web.AppKey("h3.output_root")
JOB_WATCHER_KEY = web.AppKey("h3.job_watcher")
MISSING_JOB_GRACE_MS = 5 * 60 * 1000

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
    terminal = {"completed", "failed", "cancelled"}
    if current.get("status") in terminal and status not in terminal:
        status = str(current["status"])
    updates: dict[str, Any] = {}
    if isinstance(job.get("outputs"), Mapping):
        outputs = flatten_outputs(job)
        if outputs != current.get("outputs"):
            updates["outputs"] = outputs
    if "execution_error" in job:
        error = _job_error(job)
        if error != current.get("error"):
            updates["error"] = error
    elif status == "completed" and current.get("error") is not None:
        updates["error"] = None
    status_update = status if status != current.get("status") else None
    if status_update is None and not updates:
        return current
    return store.update(job_id, status_update, **updates)


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
    if isinstance(response, web.HTTPException):
        raise response
    return response


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (RequestError, JobStoreValidationError, ValueError) as exc:
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
    except Exception:
        LOGGER.exception("Unhandled webapp request error")
        return web.json_response({"error": "internal webapp error"}, status=500)


def create_app(
    client: ComfyClient | None = None,
    *,
    job_store: JobStore | None = None,
    output_root: Path | None = None,
) -> web.Application:
    app = web.Application(
        client_max_size=620 * 1024 * 1024,
        middlewares=[security_middleware, error_middleware],
    )
    app[COMFY_KEY] = client or ComfyClient("http://127.0.0.1:8188")
    app[ASSETS_KEY] = AssetRegistry()
    app[JOBS_KEY] = job_store or JobStore(DEFAULT_JOB_DB)
    app[OUTPUT_ROOT_KEY] = (output_root or DEFAULT_OUTPUT_ROOT).resolve()

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

    async def on_cleanup(application: web.Application) -> None:
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

    @routes.get("/api/config")
    async def config(_: web.Request) -> web.Response:
        data = public_config()
        data["version"] = __version__
        data["engine_start_command"] = START_ENGINE_COMMAND
        return web.json_response(data)

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

        asset = UploadedAsset(kind=kind, path=result["path"], original_name=original)
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

    @routes.post("/api/renders")
    async def render(request: web.Request) -> web.Response:
        payload = await request.json(loads=__import__("json").loads)
        if not isinstance(payload, Mapping):
            raise RequestError("request body must be a JSON object")
        await _require_project_comfy(request)
        resolved = _resolve_asset_payload(payload, request.app[ASSETS_KEY])
        spec = parse_render_spec(payload, resolved)
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
        if spec.profile.dual_gpu and (not isinstance(devices, list) or len(devices) < 2):
            raise ComfyError("The selected profile requires both RTX 4090 GPUs", status=409)
        prompt = compile_prompt(spec)
        prompt_id = str(uuid.uuid4())
        metadata = {
            "mode": spec.mode,
            "profile": spec.profile.id,
            "prompt": spec.prompt,
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
        request.app[JOBS_KEY].register(prompt_id, metadata, status="submitting")
        try:
            result = await request.app[COMFY_KEY].submit(prompt, metadata, prompt_id)
        except Exception as exc:
            request.app[JOBS_KEY].update(prompt_id, "failed", error=str(exc)[:8192])
            raise
        request.app[JOBS_KEY].update(prompt_id, "pending", error=None)
        return web.json_response({"id": result["prompt_id"], "number": result.get("number"), "render": metadata}, status=202)

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
        if not request.app[JOBS_KEY].owns(job_id):
            raise web.HTTPNotFound(text="job not found")
        await _require_project_comfy(request)
        try:
            result = await request.app[COMFY_KEY].cancel(job_id)
        except ComfyError as exc:
            if exc.status != 404:
                raise
            request.app[JOBS_KEY].update(
                job_id,
                "cancelled",
                error="The local engine no longer had this job.",
            )
            return web.json_response({"cancelled": True, "already_missing": True})
        if result.get("cancelled") is True:
            request.app[JOBS_KEY].update(job_id, "cancelled")
        else:
            try:
                record = await request.app[COMFY_KEY].get_job(job_id)
                if isinstance(record, Mapping):
                    _sync_stored_job(request.app[JOBS_KEY], job_id, record)
            except ComfyError as exc:
                if exc.status == 404:
                    request.app[JOBS_KEY].update(
                        job_id,
                        "cancelled",
                        error="The local engine no longer had this job.",
                    )
                    return web.json_response({"cancelled": True, "already_missing": True})
                raise
        return web.json_response(result)

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
