"""Small, allowlisted client for an already-running local ComfyUI instance."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp

from .workflows import WORKFLOW_ID


LOGGER = logging.getLogger("h3-webapp.comfy")
REQUIRED_NODES = (
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "SelectModelDevice",
    "SelectCLIPDevice",
    "SelectVAEDevice",
    "LoraLoaderModelOnly",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "LoadImage",
    "LoadVideo",
    "LoadAudio",
    "GetVideoComponents",
    "RandomNoise",
    "KSamplerSelect",
    "BasicScheduler",
    "BasicGuider",
    "SamplerCustomAdvanced",
    "VAEDecode",
    "VAEDecodeAudio",
    "CreateVideo",
    "SaveVideo",
)


class ComfyError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


def validate_loopback_url(value: str) -> str:
    """Reject remote targets and URL features that could turn the app into a proxy."""

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("ComfyUI URL must use http or https")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("ComfyUI URL must point to this machine's loopback interface")
    if parsed.username or parsed.password:
        raise ValueError("ComfyUI URL cannot contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("ComfyUI URL cannot contain a path, query, or fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid ComfyUI port")
    clean_host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    netloc = f"{clean_host}:{parsed.port}" if parsed.port else clean_host
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def safe_input_path(name: str, subfolder: str, file_type: str) -> str:
    """Turn a trusted Comfy upload response into a loader combo value."""

    if file_type != "input" or not isinstance(name, str) or not name:
        raise ComfyError("ComfyUI returned an invalid upload location")
    if PurePosixPath(name).name != name or "\\" in name:
        raise ComfyError("ComfyUI returned an unsafe upload filename")
    subfolder = subfolder or ""
    path = PurePosixPath(subfolder)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        if subfolder:
            raise ComfyError("ComfyUI returned an unsafe upload folder")
    return str(path / name) if subfolder else name


def safe_output_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Accept only ordinary files under ComfyUI's output root."""

    filename = item.get("filename")
    subfolder = item.get("subfolder") or ""
    if item.get("type") != "output" or not isinstance(filename, str) or not filename:
        return None
    if PurePosixPath(filename).name != filename or "\\" in filename:
        return None
    if not isinstance(subfolder, str):
        return None
    folder = PurePosixPath(subfolder)
    if folder.is_absolute() or any(part in {"", ".", ".."} for part in folder.parts):
        if subfolder:
            return None
    extension = PurePosixPath(filename).suffix.lower()
    if extension in {".mp4", ".webm", ".mov", ".mkv", ".m4v"}:
        media_type = "video"
    elif extension in {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}:
        media_type = "audio"
    else:
        media_type = str(item.get("mediaType") or "images")
    return {
        "filename": filename,
        "subfolder": subfolder,
        "type": "output",
        "media_type": media_type,
        "node_id": str(item.get("nodeId") or ""),
    }


def flatten_outputs(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    outputs = job.get("outputs")
    if not isinstance(outputs, Mapping):
        return result
    for node_id, node_outputs in outputs.items():
        if not isinstance(node_outputs, Mapping):
            continue
        for media_type, items in node_outputs.items():
            if media_type == "animated" or not isinstance(items, list):
                continue
            for raw in items:
                if not isinstance(raw, Mapping):
                    continue
                normalized = safe_output_item({**raw, "nodeId": node_id, "mediaType": media_type})
                if normalized:
                    normalized["id"] = len(result)
                    result.append(normalized)
    return result


@dataclass
class ProgressState:
    phase: str = "queued"
    node: str | None = None
    value: int | None = None
    maximum: int | None = None
    updated: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        percent = None
        if self.value is not None and self.maximum:
            percent = round(max(0.0, min(1.0, self.value / self.maximum)) * 100, 1)
        return {
            "phase": self.phase,
            "node": self.node,
            "value": self.value,
            "max": self.maximum,
            "percent": percent,
            "updated": self.updated,
        }


class ComfyClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = validate_loopback_url(base_url)
        self.client_id = str(uuid.uuid4())
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None
        self.ws_task: asyncio.Task[None] | None = None
        self.progress: dict[str, ProgressState] = {}
        self._closing = False

    async def open(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        if self.ws_task is None or self.ws_task.done():
            self._closing = False
            self.ws_task = asyncio.create_task(self._ws_loop(), name="h3-comfy-events")

    async def close(self) -> None:
        self._closing = True
        if self.ws_task is not None:
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass
            self.ws_task = None
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            await self.open()
        assert self.session is not None
        return self.session

    def _url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("internal ComfyUI path must be absolute")
        return self.base_url + path

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        session = await self._ensure_session()
        try:
            async with session.request(method, self._url(path), **kwargs) as response:
                try:
                    body = await response.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError):
                    body = {"error": (await response.text())[:1000]}
                if response.status >= 400:
                    message = "ComfyUI rejected the request"
                    if isinstance(body, Mapping):
                        message = str(body.get("error") or body.get("message") or message)
                    status = response.status if 400 <= response.status < 500 else 502
                    raise ComfyError(message, status=status, details=body)
                return body
        except ComfyError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ComfyError("Cannot reach the existing ComfyUI service on loopback") from exc

    async def health(self, *, inspect_nodes: bool = False) -> dict[str, Any]:
        stats = await self._json("GET", "/system_stats")
        result: dict[str, Any] = {"connected": True, "stats": stats}
        if inspect_nodes:
            missing: list[str] = []
            for node in REQUIRED_NODES:
                info = await self._json("GET", f"/object_info/{node}")
                if node not in info:
                    missing.append(node)
            result["missing_nodes"] = missing
            result["ready"] = not missing
        return result

    async def upload(self, *, fileobj: BinaryIO, filename: str, content_type: str, subfolder: str) -> dict[str, Any]:
        session = await self._ensure_session()
        form = aiohttp.FormData()
        form.add_field("image", fileobj, filename=filename, content_type=content_type)
        form.add_field("type", "input")
        form.add_field("subfolder", subfolder)
        try:
            async with session.post(self._url("/upload/image"), data=form) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise ComfyError("ComfyUI rejected the upload", details=body)
        except ComfyError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise ComfyError("Upload to ComfyUI failed") from exc
        if not isinstance(body, Mapping):
            raise ComfyError("ComfyUI returned an invalid upload response")
        path = safe_input_path(str(body.get("name") or ""), str(body.get("subfolder") or ""), str(body.get("type") or ""))
        return {"path": path}

    async def submit(self, prompt: Mapping[str, Any], metadata: Mapping[str, Any], prompt_id: str) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "prompt_id": prompt_id,
            "client_id": self.client_id,
            "extra_data": {
                "extra_pnginfo": {
                    "workflow": {"id": WORKFLOW_ID, "name": "Local MiniMax H3 Webapp"},
                    "h3_webapp": dict(metadata),
                }
            },
        }
        body = await self._json("POST", "/prompt", json=payload)
        if not isinstance(body, Mapping) or body.get("error"):
            raise ComfyError("ComfyUI did not accept the render", details=body)
        returned_id = str(body.get("prompt_id") or prompt_id)
        if returned_id != prompt_id:
            raise ComfyError("ComfyUI returned a different job identifier", details=body)
        self.progress.setdefault(returned_id, ProgressState(phase="queued", updated=time.time()))
        return {"prompt_id": returned_id, "number": body.get("number")}

    async def list_jobs(self, *, scope: str = "all", limit: int = 40) -> dict[str, Any]:
        params: dict[str, str] = {
            "workflow_id": WORKFLOW_ID,
            "sort_order": "desc",
            "limit": str(max(1, min(limit, 100))),
        }
        if scope == "active":
            params["status"] = "pending,in_progress"
        elif scope == "history":
            params["status"] = "completed,failed,cancelled"
        elif scope != "all":
            raise ValueError("invalid job scope")
        body = await self._json("GET", "/api/jobs?" + urlencode(params))
        return body if isinstance(body, dict) else {"jobs": [], "pagination": {}}

    async def get_job(self, job_id: str) -> dict[str, Any]:
        body = await self._json("GET", f"/api/jobs/{job_id}")
        if not isinstance(body, dict):
            raise ComfyError("ComfyUI returned an invalid job record")
        return body

    async def cancel(self, job_id: str) -> dict[str, Any]:
        body = await self._json("POST", f"/api/jobs/{job_id}/cancel")
        return body if isinstance(body, dict) else {"cancelled": False}

    async def output_response(
        self,
        item: Mapping[str, Any],
        request_headers: Mapping[str, str] | None = None,
        *,
        head: bool = False,
    ) -> aiohttp.ClientResponse:
        session = await self._ensure_session()
        query = urlencode({"filename": item["filename"], "subfolder": item["subfolder"], "type": "output"})
        headers = {
            name: value
            for name, value in (request_headers or {}).items()
            if name.lower() in {"range", "if-range", "if-none-match", "if-modified-since"}
        }
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=180)
        try:
            response = await session.request(
                "HEAD" if head else "GET",
                self._url("/view?" + query),
                headers=headers,
                timeout=timeout,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ComfyError("Could not retrieve the ComfyUI output") from exc
        if response.status >= 400 and response.status != 416:
            response.release()
            status = response.status if response.status in {404, 410} else 502
            raise ComfyError("ComfyUI output is unavailable", status=status)
        return response

    def job_progress(self, job_id: str) -> dict[str, Any] | None:
        state = self.progress.get(job_id)
        return state.as_dict() if state else None

    async def _ws_loop(self) -> None:
        delay = 1.0
        while not self._closing:
            try:
                session = await self._ensure_session()
                parsed = urlsplit(self.base_url)
                scheme = "wss" if parsed.scheme == "https" else "ws"
                ws_url = urlunsplit((scheme, parsed.netloc, "/ws", f"clientId={self.client_id}", ""))
                async with session.ws_connect(ws_url, heartbeat=20, receive_timeout=60) as websocket:
                    delay = 1.0
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                event = json.loads(message.data)
                            except json.JSONDecodeError:
                                continue
                            self._record_event(event)
                        elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # ComfyUI may simply not be running yet.
                LOGGER.debug("ComfyUI event stream unavailable: %s", exc)
            if not self._closing:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    def _record_event(self, event: Mapping[str, Any]) -> None:
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, Mapping):
            return
        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str):
            return
        state = self.progress.setdefault(prompt_id, ProgressState())
        state.updated = time.time()
        if event_type == "execution_start":
            state.phase = "loading"
        elif event_type == "executing":
            state.node = str(data.get("node")) if data.get("node") is not None else None
            state.phase = "finalizing" if state.node is None else "executing"
        elif event_type == "progress":
            state.phase = "sampling"
            state.node = str(data.get("node")) if data.get("node") is not None else state.node
            try:
                state.value = int(data.get("value"))
                state.maximum = int(data.get("max"))
            except (TypeError, ValueError):
                state.value = state.maximum = None
        elif event_type == "execution_success":
            state.phase = "complete"
            if state.maximum:
                state.value = state.maximum
        elif event_type == "execution_error":
            state.phase = "failed"
        elif event_type == "execution_interrupted":
            state.phase = "cancelled"
