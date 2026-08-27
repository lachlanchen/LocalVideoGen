#!/usr/bin/env python3
"""Submit one tracked MiniMax H3 text-to-video test to the owned ComfyUI.

This command deliberately does not start or stop any service.  It accepts an
explicit prompt, compiles the same allowlisted native graph as H3 Studio, and
will only submit after the project's runtime identity has been verified.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Protocol


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.comfy_client import ComfyClient, ComfyError, flatten_outputs  # noqa: E402
from webapp.job_store import JobStore, JobStoreError, canonical_job_id  # noqa: E402
from webapp.workflows import FPS, PROFILES, RequestError, RenderSpec, compile_prompt, parse_render_spec  # noqa: E402


DEFAULT_PROFILE = "preview_int8_turbo_dual"
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 352
DEFAULT_DURATION = 2.0
DEFAULT_SEED = 1
DEFAULT_TIMEOUT = 4 * 60 * 60.0
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_PROBE_TIMEOUT = 30.0
RUNTIME_STATE_LIMIT = 64 * 1024
MISSING_JOB_GRACE = 30.0

class SubmitRenderError(RuntimeError):
    """A safe failure from the direct render workflow."""


class TerminalRenderError(SubmitRenderError):
    """ComfyUI explicitly reported a terminal non-success status."""


class ClientProtocol(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self, *, inspect_nodes: bool = False) -> dict[str, Any]: ...

    async def submit(
        self,
        prompt: Mapping[str, Any],
        metadata: Mapping[str, Any],
        prompt_id: str,
    ) -> dict[str, Any]: ...

    async def get_job(self, job_id: str) -> dict[str, Any]: ...

    async def cancel(self, job_id: str) -> dict[str, Any]: ...


class StoreProtocol(Protocol):
    def register(
        self,
        job_id: str,
        metadata: Mapping[str, Any],
        *,
        status: str = "submitting",
    ) -> dict[str, Any]: ...

    def update(
        self,
        job_id: str,
        status: str | None = None,
        *,
        metadata: Mapping[str, Any] | None = None,
        outputs: Sequence[Mapping[str, Any]] | None = None,
        error: str | None | object = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RuntimeTarget:
    """The verified, project-owned ComfyUI process and its loopback endpoint."""

    base_url: str
    pid: int
    instance: str
    port: int
    start_ticks: int
    boot_id: str
    cwd: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class RenderOptions:
    prompt: str
    profile: str = DEFAULT_PROFILE
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    duration: float = DEFAULT_DURATION
    seed: int = DEFAULT_SEED
    timeout: float = DEFAULT_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    http_timeout: float = DEFAULT_HTTP_TIMEOUT
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT


RuntimeVerifier = Callable[[Path], RuntimeTarget]
ClientFactory = Callable[..., ClientProtocol]
ProbeFunction = Callable[..., Awaitable[dict[str, Any]]]
ProgressFunction = Callable[[str], None]


def _read_private_runtime_state(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SubmitRenderError("the project ComfyUI runtime state is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise SubmitRenderError("the project ComfyUI runtime state is not private")
        data = os.read(descriptor, RUNTIME_STATE_LIMIT + 1)
        if len(data) > RUNTIME_STATE_LIMIT:
            raise SubmitRenderError("the project ComfyUI runtime state is unexpectedly large")
        return data
    finally:
        os.close(descriptor)


def _positive_plain_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SubmitRenderError(f"runtime {label} is invalid")
    return value


def _decode_runtime_target(data: bytes, project_root: Path) -> RuntimeTarget:
    try:
        state = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubmitRenderError("the project ComfyUI runtime state is invalid") from exc
    if not isinstance(state, Mapping):
        raise SubmitRenderError("the project ComfyUI runtime state is invalid")

    pid = _positive_plain_int(state.get("pid"), "PID")
    port = _positive_plain_int(state.get("port"), "port")
    start_ticks = _positive_plain_int(state.get("start_ticks"), "start time")
    if not 1024 <= port <= 65535:
        raise SubmitRenderError("runtime port is outside the allowed range")
    if state.get("uid") != os.getuid():
        raise SubmitRenderError("the project ComfyUI runtime belongs to another user")

    instance = state.get("instance")
    try:
        if not isinstance(instance, str) or str(uuid.UUID(instance)) != instance:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise SubmitRenderError("runtime instance marker is not a canonical UUID") from exc

    boot_id = state.get("boot_id")
    cwd = state.get("cwd")
    argv = state.get("argv")
    if not isinstance(boot_id, str) or not boot_id or any(character.isspace() for character in boot_id):
        raise SubmitRenderError("runtime boot identity is invalid")
    if not isinstance(cwd, str) or Path(cwd) != (project_root / "ComfyUI").resolve():
        raise SubmitRenderError("runtime working directory is not this project's ComfyUI")
    if (
        not isinstance(argv, list)
        or len(argv) < 3
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise SubmitRenderError("runtime command is invalid")
    try:
        main_script = Path(argv[2]).resolve()
    except OSError as exc:
        raise SubmitRenderError("runtime command cannot be resolved") from exc
    if argv[1] != "-u" or main_script != (project_root / "ComfyUI" / "main.py").resolve():
        raise SubmitRenderError("runtime command is not this project's ComfyUI launcher")

    def option_value(name: str) -> str | None:
        try:
            index = argv.index(name)
        except ValueError:
            return None
        return argv[index + 1] if index + 1 < len(argv) else None

    if option_value("--listen") != "127.0.0.1" or option_value("--port") != str(port):
        raise SubmitRenderError("runtime is not bound to the expected loopback endpoint")
    return RuntimeTarget(
        base_url=f"http://127.0.0.1:{port}",
        pid=pid,
        instance=instance,
        port=port,
        start_ticks=start_ticks,
        boot_id=boot_id,
        cwd=cwd,
        argv=tuple(argv),
    )


def verify_project_runtime(
    project_root: Path = PROJECT_ROOT,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RuntimeTarget:
    """Verify a stable private state using the project's runtime_identity tool."""

    root = project_root.resolve()
    state_path = root / "runtime" / "comfyui-state.json"
    before = _read_private_runtime_state(state_path)
    command = [sys.executable, str(root / "scripts" / "runtime_identity.py"), "verify"]
    try:
        result = runner(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SubmitRenderError("runtime_identity verification could not run") from exc
    if result.returncode != 0:
        note = (result.stdout or result.stderr or "identity mismatch").strip().splitlines()[0][:300]
        raise SubmitRenderError(f"project ComfyUI runtime is not verified: {note}")
    after = _read_private_runtime_state(state_path)
    if after != before:
        raise SubmitRenderError("project ComfyUI runtime changed during identity verification")
    return _decode_runtime_target(after, root)


def _render_spec(options: RenderOptions) -> RenderSpec:
    if not 0 < options.timeout <= 86_400:
        raise SubmitRenderError("timeout must be greater than zero and no more than 86400 seconds")
    if not 0 < options.poll_interval <= 30:
        raise SubmitRenderError("poll interval must be greater than zero and no more than 30 seconds")
    if not 0 < options.http_timeout <= 300:
        raise SubmitRenderError("HTTP timeout must be greater than zero and no more than 300 seconds")
    if not 0 < options.probe_timeout <= 300:
        raise SubmitRenderError("ffprobe timeout must be greater than zero and no more than 300 seconds")
    return parse_render_spec(
        {
            "mode": "t2v",
            "profile": options.profile,
            "prompt": options.prompt,
            "width": options.width,
            "height": options.height,
            "duration": options.duration,
            "seed": options.seed,
        },
        {},
    )


def _render_metadata(spec: RenderSpec) -> dict[str, Any]:
    return {
        "mode": "t2v",
        "profile": spec.profile.id,
        "prompt": spec.prompt,
        "width": spec.width,
        "height": spec.height,
        "duration": spec.duration,
        "length": spec.length,
        "seed": str(spec.seed),
        "ref_image_size": spec.ref_image_size,
        "references": {
            "first_frame": None,
            "last_frame": None,
            "images": [],
            "videos": [],
            "video_soundtracks": [],
            "audio": [],
        },
    }


def _normalized_status(value: Any) -> str:
    raw = str(value or "pending").lower()
    normalized = {
        "success": "completed",
        "error": "failed",
        "running": "in_progress",
        "queued": "pending",
    }.get(raw, raw)
    if normalized not in {"pending", "in_progress", "completed", "failed", "cancelled"}:
        raise SubmitRenderError("ComfyUI returned an unknown job status")
    return normalized


def _job_error(job: Mapping[str, Any]) -> str:
    raw = job.get("execution_error")
    if isinstance(raw, Mapping):
        return str(raw.get("exception_message") or raw.get("message") or "H3 render failed")[:8192]
    if isinstance(raw, str) and raw:
        return raw[:8192]
    return "H3 render did not complete successfully"


def _validate_job_identity(job: Mapping[str, Any], job_id: str) -> None:
    returned = job.get("id", job.get("prompt_id"))
    if returned is not None:
        try:
            returned_id = canonical_job_id(str(returned))
        except ValueError as exc:
            raise SubmitRenderError("ComfyUI returned an invalid job identifier") from exc
        if returned_id != job_id:
            raise SubmitRenderError("ComfyUI returned a different job record")
    workflow_id = job.get("workflow_id")
    if workflow_id != "local-video-gen-minimax-h3-webapp":
        raise SubmitRenderError("ComfyUI returned a job owned by a different workflow")


async def _poll_job(
    client: ClientProtocol,
    store: StoreProtocol,
    job_id: str,
    options: RenderOptions,
    progress: ProgressFunction,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    missing_since: float | None = None
    last_status: str | None = None
    try:
        async with asyncio.timeout(options.timeout):
            while True:
                try:
                    job = await client.get_job(job_id)
                    missing_since = None
                except ComfyError as exc:
                    if exc.status == 404:
                        missing_since = missing_since or time.monotonic()
                        if time.monotonic() - missing_since > MISSING_JOB_GRACE:
                            raise SubmitRenderError("the accepted render disappeared from ComfyUI") from exc
                    else:
                        progress("ComfyUI is temporarily unreachable; continuing to poll")
                    await sleep(options.poll_interval)
                    continue
                if not isinstance(job, Mapping):
                    raise SubmitRenderError("ComfyUI returned an invalid job record")
                _validate_job_identity(job, job_id)
                status = _normalized_status(job.get("status"))
                if status != last_status:
                    store.update(job_id, status, error=None)
                    progress(f"job {job_id}: {status}")
                    last_status = status
                if status == "completed":
                    return dict(job)
                if status in {"failed", "cancelled"}:
                    message = _job_error(job) if status == "failed" else "H3 render was cancelled"
                    store.update(job_id, status, error=message)
                    raise TerminalRenderError(message)
                await sleep(options.poll_interval)
    except TimeoutError as exc:
        raise SubmitRenderError(f"H3 render exceeded the {options.timeout:g}-second timeout") from exc


async def _cancel_owned_job(
    client: ClientProtocol,
    store: StoreProtocol,
    job_id: str,
    reason: str,
) -> None:
    try:
        store.update(job_id, "cancelling", error=reason[:8192])
    except Exception:
        pass
    try:
        await asyncio.wait_for(client.cancel(job_id), timeout=15)
    except Exception as exc:
        try:
            store.update(job_id, "cancelling", error=f"{reason}; cancellation could not be confirmed: {exc}"[:8192])
        except Exception:
            pass
        return
    try:
        store.update(job_id, "cancelled", error=reason[:8192])
    except Exception:
        pass


def resolve_output_path(project_root: Path, item: Mapping[str, Any]) -> Path:
    """Resolve one normalized Comfy locator beneath this project's output root."""

    try:
        filename = item["filename"]
        subfolder = item.get("subfolder") or ""
        if not isinstance(filename, str) or PurePosixPath(filename).name != filename or "\\" in filename:
            raise ValueError
        if not isinstance(subfolder, str) or "\\" in subfolder:
            raise ValueError
        folder = PurePosixPath(subfolder)
        if folder.is_absolute() or any(part in {"", ".", ".."} for part in folder.parts):
            if subfolder:
                raise ValueError
        root = (project_root / "ComfyUI" / "output").resolve(strict=True)
        untrusted = root / Path(*folder.parts) / filename
        if untrusted.is_symlink():
            raise ValueError
        artifact = untrusted.resolve(strict=True)
        artifact.relative_to(root)
        info = artifact.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size <= 0:
            raise ValueError
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise SubmitRenderError("ComfyUI returned an unsafe or unavailable output path") from exc
    return artifact


async def probe_media(path: Path, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> dict[str, Any]:
    """Require a decodable generated container with both video and audio."""

    command = [
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "format=format_name,duration,size:stream=index,codec_type,codec_name,width,height,sample_rate,channels,duration,r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames",
        "-of",
        "json",
        str(path.resolve()),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise SubmitRenderError("ffprobe is unavailable") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise SubmitRenderError("ffprobe timed out while validating the generated video") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        note = detail[0][:500] if detail else "unknown decoder error"
        raise SubmitRenderError(f"ffprobe rejected the generated video: {note}")
    try:
        report = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubmitRenderError("ffprobe returned invalid JSON") from exc
    if not isinstance(report, Mapping) or not isinstance(report.get("streams"), list):
        raise SubmitRenderError("ffprobe returned an invalid media report")
    streams = [stream for stream in report["streams"] if isinstance(stream, Mapping)]
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None:
        raise SubmitRenderError("generated artifact has no decodable video stream")
    if audio is None:
        raise SubmitRenderError("generated artifact has no decodable audio stream")
    try:
        if int(video.get("width") or 0) <= 0 or int(video.get("height") or 0) <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise SubmitRenderError("generated artifact has invalid video dimensions") from exc
    return {
        "format": dict(report.get("format")) if isinstance(report.get("format"), Mapping) else {},
        "video": dict(video),
        "audio": dict(audio),
    }


def _validate_probe_report(report: Mapping[str, Any], spec: RenderSpec) -> None:
    video = report.get("video")
    audio = report.get("audio")
    container = report.get("format")
    if not isinstance(video, Mapping) or not isinstance(audio, Mapping) or not isinstance(container, Mapping):
        raise SubmitRenderError("media validation did not return video, audio, and container details")
    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise SubmitRenderError("generated artifact has invalid video dimensions") from exc
    if (width, height) != (spec.width, spec.height):
        raise SubmitRenderError(
            f"generated artifact is {width}x{height}, expected {spec.width}x{spec.height}"
        )

    raw_rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
    try:
        rate = float(Fraction(str(raw_rate)))
    except (ValueError, ZeroDivisionError) as exc:
        raise SubmitRenderError("generated artifact has an invalid frame rate") from exc
    if abs(rate - FPS) > 0.01:
        raise SubmitRenderError(f"generated artifact is {rate:g} fps, expected {FPS} fps")

    raw_frames = video.get("nb_read_frames") or video.get("nb_frames")
    try:
        frames = int(raw_frames)
    except (TypeError, ValueError) as exc:
        raise SubmitRenderError("generated artifact frame count is unavailable") from exc
    if frames != spec.length:
        raise SubmitRenderError(
            f"generated artifact has {frames} frames, expected the aligned {spec.length} frames"
        )

    try:
        duration = float(container.get("duration") or video.get("duration"))
    except (TypeError, ValueError) as exc:
        raise SubmitRenderError("generated artifact duration is unavailable") from exc
    expected_duration = spec.length / FPS
    if duration <= 0 or abs(duration - expected_duration) > 0.25:
        raise SubmitRenderError(
            f"generated artifact is {duration:g}s, expected about {expected_duration:.3f}s"
        )


async def _call_probe(probe: ProbeFunction, path: Path, timeout: float) -> dict[str, Any]:
    result = probe(path, timeout=timeout)
    if not inspect.isawaitable(result):
        raise SubmitRenderError("media probe did not return an awaitable result")
    return await result


async def submit_test_render(
    options: RenderOptions,
    *,
    project_root: Path = PROJECT_ROOT,
    runtime_verifier: RuntimeVerifier = verify_project_runtime,
    client_factory: ClientFactory = ComfyClient,
    store: StoreProtocol | None = None,
    probe: ProbeFunction = probe_media,
    job_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    progress: ProgressFunction = lambda message: print(message, file=sys.stderr, flush=True),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Compile, submit, track, validate, and return one direct T2V test."""

    root = project_root.resolve()
    spec = _render_spec(options)
    prompt = compile_prompt(spec)
    metadata = _render_metadata(spec)
    initial_target = runtime_verifier(root)
    client = client_factory(initial_target.base_url, timeout=options.http_timeout)
    registry = store or JobStore(root / "runtime" / "private" / "webapp-jobs.sqlite3")
    job_id = canonical_job_id(job_id_factory())
    registered = False
    submitted = False
    await client.open()
    try:
        readiness = await client.health(inspect_nodes=True)
        if readiness.get("ready") is not True:
            missing = readiness.get("missing_nodes")
            note = ", ".join(str(item) for item in missing) if isinstance(missing, list) else "unknown"
            raise SubmitRenderError(f"ComfyUI is missing required H3 nodes: {note}")
        devices = readiness.get("stats", {}).get("devices", [])
        if spec.profile.dual_gpu:
            if not isinstance(devices, list) or len(devices) < 2:
                raise SubmitRenderError("the selected test profile requires both RTX 4090 GPUs")
            names = [str(item.get("name") or "") for item in devices[:2] if isinstance(item, Mapping)]
            if len(names) != 2 or any("RTX 4090" not in name for name in names):
                raise SubmitRenderError("the verified ComfyUI does not expose both expected RTX 4090 GPUs")

        registry.register(job_id, metadata, status="submitting")
        registered = True
        current_target = runtime_verifier(root)
        if current_target != initial_target:
            raise SubmitRenderError("project ComfyUI changed after readiness validation; render was not submitted")
        try:
            accepted = await client.submit(prompt, metadata, job_id)
        except Exception as exc:
            registry.update(job_id, "failed", error=str(exc)[:8192])
            raise
        returned_id = canonical_job_id(str(accepted.get("prompt_id") or ""))
        if returned_id != job_id:
            raise SubmitRenderError("ComfyUI accepted a different render identifier")
        submitted = True
        registry.update(job_id, "pending", error=None)
        progress(f"submitted H3 test job {job_id}")

        try:
            terminal = await _poll_job(client, registry, job_id, options, progress, sleep)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                _cancel_owned_job(client, registry, job_id, "direct H3 test interrupted")
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise
        except TerminalRenderError:
            raise
        except SubmitRenderError as exc:
            await _cancel_owned_job(client, registry, job_id, str(exc))
            raise

        try:
            finished_target = runtime_verifier(root)
            if finished_target != initial_target:
                raise SubmitRenderError(
                    "project ComfyUI changed before output validation; refusing to trust the artifact"
                )
        except Exception as exc:
            registry.update(job_id, "failed", error=str(exc)[:8192])
            raise

        outputs = flatten_outputs(terminal)
        video_outputs = [item for item in outputs if item.get("media_type") == "video"]
        if not video_outputs:
            message = "completed H3 job did not expose a safe video output"
            registry.update(job_id, "failed", outputs=outputs, error=message)
            raise SubmitRenderError(message)
        artifact = resolve_output_path(root, video_outputs[0])
        try:
            probe_report = await _call_probe(probe, artifact, options.probe_timeout)
            _validate_probe_report(probe_report, spec)
        except Exception as exc:
            registry.update(job_id, "failed", outputs=outputs, error=str(exc)[:8192])
            raise
        registry.update(job_id, "completed", outputs=outputs, error=None)
        return {
            "job_id": job_id,
            "status": "completed",
            "artifact": str(artifact),
            "render": metadata,
            "probe": probe_report,
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if registered and not submitted:
            try:
                registry.update(job_id, "failed", error=str(exc)[:8192])
            except Exception:
                pass
        raise
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit one tracked MiniMax H3 text-to-video test to the verified local runtime."
    )
    parser.add_argument("--prompt", required=True, help="exact H3 prompt; it is not expanded or rewritten")
    parser.add_argument("--profile", choices=tuple(PROFILES), default=DEFAULT_PROFILE)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="overall render timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--probe-timeout", type=float, default=DEFAULT_PROBE_TIMEOUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = RenderOptions(
        prompt=args.prompt,
        profile=args.profile,
        width=args.width,
        height=args.height,
        duration=args.duration,
        seed=args.seed,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        http_timeout=args.http_timeout,
        probe_timeout=args.probe_timeout,
    )
    try:
        result = asyncio.run(submit_test_render(options))
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "error": "interrupted"}), file=sys.stderr)
        return 130
    except (SubmitRenderError, RequestError, ComfyError, JobStoreError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
