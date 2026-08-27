#!/usr/bin/env python3
"""Create and verify a private identity for one LocalVideoGen runtime."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
SERVICE_CONFIG = {
    "comfyui": ("comfyui-state.json", "LOCALVIDEOGEN_COMFY_INSTANCE"),
    "webapp": ("webapp-state.json", "LOCALVIDEOGEN_WEBAPP_INSTANCE"),
}
STATE_PATH = PROJECT_ROOT / "runtime" / SERVICE_CONFIG["comfyui"][0]
INSTANCE_ENV = SERVICE_CONFIG["comfyui"][1]


def configure_service(service: str) -> None:
    global STATE_PATH, INSTANCE_ENV
    state_name, marker_name = SERVICE_CONFIG[service]
    STATE_PATH = PROJECT_ROOT / "runtime" / state_name
    INSTANCE_ENV = marker_name


def start_ticks(pid: int) -> int:
    stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    remainder = stat_text[stat_text.rfind(")") + 2 :].split()
    return int(remainder[19])


def process_state(pid: int) -> str:
    stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    remainder = stat_text[stat_text.rfind(")") + 2 :].split()
    return remainder[0]


def process_argv(pid: int) -> list[str]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return [part.decode("utf-8") for part in raw.rstrip(b"\0").split(b"\0")]


def process_has_instance(pid: int, instance: str) -> bool:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    expected = INSTANCE_ENV.encode("ascii") + b"=" + instance.encode("utf-8")
    return expected in raw.split(b"\0")


def load_state() -> dict[str, Any]:
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def verify_state(state: dict[str, Any]) -> tuple[bool, str]:
    pid = int(state["pid"])
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return False, "recorded process no longer exists"
    try:
        if proc.stat().st_uid != int(state["uid"]):
            return False, "process UID differs"
        if BOOT_ID_PATH.read_text(encoding="utf-8").strip() != state["boot_id"]:
            return False, "boot ID differs"
        if start_ticks(pid) != int(state["start_ticks"]):
            return False, "process start time differs"
        if str(Path(f"/proc/{pid}/cwd").resolve()) != state["cwd"]:
            return False, "working directory differs"
        if process_argv(pid) != state["argv"]:
            return False, "process argv differs"
        if not process_has_instance(pid, str(state["instance"])):
            return False, "private instance marker differs"
    except (FileNotFoundError, ProcessLookupError):
        return False, "recorded process exited during verification"
    except (OSError, UnicodeError) as error:
        return False, f"process inspection failed: {error}"
    return True, "identity verified"


def create(args: argparse.Namespace, expected_argv: list[str]) -> int:
    pid = int(args.pid)
    state = {
        "pid": pid,
        "uid": os.getuid(),
        "start_ticks": start_ticks(pid),
        "boot_id": BOOT_ID_PATH.read_text(encoding="utf-8").strip(),
        "instance": args.instance,
        "cwd": str(Path(f"/proc/{pid}/cwd").resolve()),
        "argv": expected_argv,
        "log": str(Path(args.log).resolve()),
        "port": int(args.port),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".comfyui-state.", dir=STATE_PATH.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, STATE_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


def launch(args: argparse.Namespace, expected_argv: list[str]) -> int:
    args.pid = os.getpid()
    os.environ[INSTANCE_ENV] = args.instance
    create(args, expected_argv)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.execvpe(expected_argv[0], expected_argv, os.environ)
    raise AssertionError("exec returned unexpectedly")


def signal_process(args: argparse.Namespace) -> int:
    """Signal the verified state process through a pidfd, never a bare PID."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        print("this Python interpreter lacks pidfd signaling support", file=sys.stderr)
        return 5
    if not STATE_PATH.exists():
        print("runtime already exited (no state)")
        return 0
    try:
        state = load_state()
        pid = int(state["pid"])
        instance = str(state["instance"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, UnicodeError) as error:
        print(f"invalid runtime state: {error}", file=sys.stderr)
        return 4
    if pid != args.expect_pid or instance != args.expect_instance:
        print("runtime state does not match the expected owner", file=sys.stderr)
        return 4

    try:
        descriptor = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        print(f"runtime PID {pid} already exited")
        return 0
    try:
        valid, reason = verify_state(state)
        if not valid:
            try:
                zombie = process_state(pid) == "Z"
            except (FileNotFoundError, ProcessLookupError):
                zombie = True
            if not Path(f"/proc/{pid}").exists() or "exited" in reason or zombie:
                print(f"runtime PID {pid} already exited")
                return 0
            print(f"refusing signal: {reason}", file=sys.stderr)
            return 3
        signal_number = getattr(signal, f"SIG{args.signal_name}")
        try:
            signal.pidfd_send_signal(descriptor, signal_number, None, 0)
        except ProcessLookupError:
            print(f"runtime PID {pid} already exited")
            return 0
    finally:
        os.close(descriptor)
    print(f"sent SIG{args.signal_name} to verified runtime PID {pid}")
    return 0


def signal_marked_process(args: argparse.Namespace) -> int:
    """Signal a pre-state launcher after opening a pidfd and checking its marker."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        print("this Python interpreter lacks pidfd signaling support", file=sys.stderr)
        return 5
    try:
        descriptor = os.pidfd_open(args.pid, 0)
    except ProcessLookupError:
        print(f"launcher PID {args.pid} already exited")
        return 0
    try:
        try:
            marked = process_has_instance(args.pid, args.instance)
        except (FileNotFoundError, ProcessLookupError):
            print(f"launcher PID {args.pid} already exited")
            return 0
        except OSError as error:
            print(f"cannot inspect launcher marker: {error}", file=sys.stderr)
            return 4
        if not marked:
            print("launcher does not carry the expected private marker", file=sys.stderr)
            return 4
        try:
            signal_number = getattr(signal, f"SIG{args.signal_name}")
            signal.pidfd_send_signal(descriptor, signal_number, None, 0)
        except ProcessLookupError:
            print(f"launcher PID {args.pid} already exited")
            return 0
    finally:
        os.close(descriptor)
    print(f"sent SIG{args.signal_name} to marked launcher PID {args.pid}")
    return 0


def kernel_identity_alive(args: argparse.Namespace) -> int:
    """Report whether the state's original PID/start-time identity is still alive."""
    if not STATE_PATH.exists():
        print("original runtime is gone (no state)")
        return 3
    try:
        state = load_state()
        pid = int(state["pid"])
        instance = str(state["instance"])
        uid = int(state["uid"])
        ticks = int(state["start_ticks"])
        boot_id = str(state["boot_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, UnicodeError) as error:
        print(f"invalid runtime state: {error}", file=sys.stderr)
        return 4
    if pid != args.expect_pid or instance != args.expect_instance:
        print("runtime state does not match the expected owner", file=sys.stderr)
        return 4
    try:
        if BOOT_ID_PATH.read_text(encoding="utf-8").strip() != boot_id:
            print("original runtime is gone (boot changed)")
            return 3
        proc = Path(f"/proc/{pid}")
        if (
            not proc.exists()
            or proc.stat().st_uid != uid
            or start_ticks(pid) != ticks
            or process_state(pid) == "Z"
        ):
            print("original runtime is gone (PID identity changed)")
            return 3
    except (FileNotFoundError, ProcessLookupError):
        print("original runtime is gone")
        return 3
    except OSError as error:
        print(f"cannot inspect kernel identity: {error}", file=sys.stderr)
        return 4
    print(f"original runtime PID {pid} remains alive")
    return 0


def verify(_: argparse.Namespace) -> int:
    if not STATE_PATH.exists():
        print("no runtime state")
        return 3
    try:
        state = load_state()
        valid, reason = verify_state(state)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, UnicodeError) as error:
        print(f"invalid runtime state: {error}")
        return 4
    print(f"PID {state.get('pid')}: {reason}")
    return 0 if valid else 3


def show(_: argparse.Namespace) -> int:
    if not STATE_PATH.exists():
        print(json.dumps({"running": False, "reason": "no runtime state"}))
        return 3
    try:
        state = load_state()
        valid, reason = verify_state(state)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, UnicodeError) as error:
        print(json.dumps({"running": False, "reason": f"invalid runtime state: {error}"}))
        return 4
    state["running"] = valid
    state["reason"] = reason
    print(json.dumps(state, indent=2))
    return 0 if valid else 3


def clear(_: argparse.Namespace) -> int:
    if not STATE_PATH.exists():
        return 0
    try:
        state = load_state()
        valid, reason = verify_state(state)
        pid = int(state["pid"])
        same_kernel_identity = (
            BOOT_ID_PATH.read_text(encoding="utf-8").strip() == str(state["boot_id"])
            and Path(f"/proc/{pid}").exists()
            and Path(f"/proc/{pid}").stat().st_uid == int(state["uid"])
            and start_ticks(pid) == int(state["start_ticks"])
            and process_state(pid) != "Z"
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as error:
        print(f"refusing to clear malformed runtime state: {error}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ProcessLookupError):
        valid, reason, same_kernel_identity = False, "recorded process exited", False
    except OSError as error:
        print(f"refusing to clear unclassifiable runtime state: {error}", file=sys.stderr)
        return 1
    if valid:
        print("refusing to clear the identity of a live verified process", file=sys.stderr)
        return 1
    if same_kernel_identity:
        print(
            f"refusing to clear state: kernel identity is still alive but full verification failed ({reason})",
            file=sys.stderr,
        )
        return 1
    STATE_PATH.unlink()
    print(f"cleared inactive runtime state ({reason})")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    expected_argv: list[str] = []
    if "--" in argv:
        divider = argv.index("--")
        expected_argv = argv[divider + 1 :]
        argv = argv[:divider]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--service",
        choices=tuple(SERVICE_CONFIG),
        default="comfyui",
        help="runtime identity namespace (default: comfyui)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--pid", required=True, type=int)
    create_parser.add_argument("--instance", required=True)
    create_parser.add_argument("--log", required=True)
    create_parser.add_argument("--port", required=True, type=int)
    create_parser.set_defaults(function=lambda args: create(args, expected_argv))
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("--instance", required=True)
    launch_parser.add_argument("--log", required=True)
    launch_parser.add_argument("--port", required=True, type=int)
    launch_parser.set_defaults(function=lambda args: launch(args, expected_argv))
    signal_parser = subparsers.add_parser("signal")
    signal_parser.add_argument("--expect-pid", required=True, type=int)
    signal_parser.add_argument("--expect-instance", required=True)
    signal_parser.add_argument("--signal", dest="signal_name", choices=("INT", "TERM", "KILL"), required=True)
    signal_parser.set_defaults(function=signal_process)
    marker_parser = subparsers.add_parser("signal-marker")
    marker_parser.add_argument("--pid", required=True, type=int)
    marker_parser.add_argument("--instance", required=True)
    marker_parser.add_argument("--signal", dest="signal_name", choices=("INT", "TERM", "KILL"), required=True)
    marker_parser.set_defaults(function=signal_marked_process)
    alive_parser = subparsers.add_parser("alive")
    alive_parser.add_argument("--expect-pid", required=True, type=int)
    alive_parser.add_argument("--expect-instance", required=True)
    alive_parser.set_defaults(function=kernel_identity_alive)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.set_defaults(function=verify)
    show_parser = subparsers.add_parser("show")
    show_parser.set_defaults(function=show)
    clear_parser = subparsers.add_parser("clear")
    clear_parser.set_defaults(function=clear)
    args = parser.parse_args(argv)
    configure_service(args.service)
    if args.command in ("create", "launch") and not expected_argv:
        parser.error(f"{args.command} requires expected process argv after --")
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
