#!/usr/bin/env python3
"""Release NVIDIA compute processes without touching protected project trees.

The default mode is a dry run.  ``--apply`` sends SIGINT and then SIGTERM to
the exact kernel process identities returned by NVIDIA.  ``--force`` permits a
final SIGKILL if a process ignores both graceful signals.  Linux pidfds bind
signals and waits to the original process, so PID reuse cannot target a
successor accidentally.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTECTED_ROOTS = (
    Path("/home/lachlan/ProjectsLFS/LocalLLM"),
    Path("/home/lachlan/ProjectsLFS/AgenticApp"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="signal eligible processes")
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow SIGKILL after SIGINT and SIGTERM time out",
    )
    parser.add_argument("--interrupt-timeout", type=float, default=20.0)
    parser.add_argument("--terminate-timeout", type=float, default=10.0)
    parser.add_argument(
        "--protected-root",
        type=Path,
        action="append",
        default=[],
        help="additional project root whose process ancestry must be preserved",
    )
    args = parser.parse_args()
    if args.force and not args.apply:
        parser.error("--force requires --apply")
    if args.interrupt_timeout < 0 or args.terminate_timeout < 0:
        parser.error("timeouts must be non-negative")
    return args


def query_compute_processes() -> list[dict[str, Any]]:
    command = [
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_gpu_memory",
        "--format=csv,noheader,nounits",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    by_pid: dict[int, dict[str, Any]] = {}
    for raw_line in result.stdout.splitlines():
        if not raw_line.strip():
            continue
        fields = [field.strip() for field in raw_line.split(",", 2)]
        if len(fields) != 3:
            raise RuntimeError(f"unexpected nvidia-smi row: {raw_line!r}")
        pid = int(fields[0])
        try:
            used_mib: int | None = int(fields[2])
        except ValueError:
            used_mib = None
        record = by_pid.setdefault(
            pid,
            {"pid": pid, "process_name": fields[1], "used_gpu_memory_mib": 0},
        )
        if used_mib is None:
            record["used_gpu_memory_mib"] = None
        elif record["used_gpu_memory_mib"] is not None:
            record["used_gpu_memory_mib"] += used_mib
    return sorted(by_pid.values(), key=lambda item: item["pid"])


def read_cmdline(pid: int) -> str:
    data = Path(f"/proc/{pid}/cmdline").read_bytes()
    return " ".join(part.decode("utf-8", "replace") for part in data.split(b"\0") if part)


def read_cwd(pid: int) -> str:
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve(strict=True))
    except (FileNotFoundError, PermissionError, RuntimeError):
        return ""


def read_parent(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    suffix = stat[stat.rfind(")") + 2 :].split()
    return int(suffix[1])


def read_uid(pid: int) -> int:
    return Path(f"/proc/{pid}").stat().st_uid


def read_comm(pid: int) -> str:
    return Path(f"/proc/{pid}/comm").read_text().strip()


def ancestry(pid: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = pid
    while current > 0 and current not in seen:
        seen.add(current)
        try:
            comm = read_comm(current)
            # A single tmux server supervises unrelated project sessions on this
            # workstation.  Its cwd/argv must not make every pane inherit one
            # pane's project ownership.  systemd is likewise only a supervisor.
            if comm in {"tmux: server", "systemd"}:
                break
            item = {
                "pid": current,
                "comm": comm,
                "cwd": read_cwd(current),
                "cmdline": read_cmdline(current),
            }
            result.append(item)
            parent = read_parent(current)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            break
        if parent == current:
            break
        current = parent
    return result


def under_root(path: str, root: Path) -> bool:
    if not path:
        return False
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def protected_reason(chain: list[dict[str, Any]], roots: tuple[Path, ...]) -> str | None:
    for member in chain:
        cwd = member["cwd"]
        cmdline = member["cmdline"]
        for root in roots:
            root_text = str(root)
            if under_root(cwd, root) or root_text in cmdline:
                return f"ancestry touches protected root {root_text} at PID {member['pid']}"
    return None


def wait_pidfd(pidfd: int, timeout: float) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(round(timeout * 1000)))


def signal_exact(pidfd: int, signum: signal.Signals) -> None:
    signal.pidfd_send_signal(pidfd, signum)


def terminate(pidfd: int, args: argparse.Namespace) -> str:
    try:
        signal_exact(pidfd, signal.SIGINT)
    except ProcessLookupError:
        return "already-exited"
    if wait_pidfd(pidfd, args.interrupt_timeout):
        return "exited-after-SIGINT"
    try:
        signal_exact(pidfd, signal.SIGTERM)
    except ProcessLookupError:
        return "exited-after-SIGINT"
    if wait_pidfd(pidfd, args.terminate_timeout):
        return "exited-after-SIGTERM"
    if not args.force:
        return "still-running-force-not-authorized"
    try:
        signal_exact(pidfd, signal.SIGKILL)
    except ProcessLookupError:
        return "exited-after-SIGTERM"
    if wait_pidfd(pidfd, 10.0):
        return "exited-after-SIGKILL"
    return "still-running-after-SIGKILL"


def write_audit(payload: dict[str, Any]) -> Path:
    runtime = PROJECT_ROOT / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = runtime / f"gpu-release-{stamp}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(destination)
    return destination


def main() -> int:
    args = parse_args()
    roots = tuple(root.resolve() for root in (*DEFAULT_PROTECTED_ROOTS, *args.protected_root))
    current_uid = os.getuid()
    records: list[dict[str, Any]] = []
    for gpu_record in query_compute_processes():
        pid = gpu_record["pid"]
        record = dict(gpu_record)
        try:
            # Open the kernel identity before inspecting /proc.  Every later
            # signal and exit wait uses this descriptor, never a bare PID.
            pidfd = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            record.update(action="skip", reason="process exited before identity capture")
            records.append(record)
            continue
        try:
            record["uid"] = read_uid(pid)
            record["cwd"] = read_cwd(pid)
            record["cmdline"] = read_cmdline(pid)
            chain = ancestry(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError) as exc:
            record.update(action="skip", reason=f"identity unavailable: {exc}")
            records.append(record)
            os.close(pidfd)
            continue
        reason = protected_reason(chain, roots)
        if record["uid"] != current_uid:
            reason = f"owned by UID {record['uid']}, not caller UID {current_uid}"
        if reason:
            record.update(action="preserve", reason=reason)
        elif not args.apply:
            record.update(action="eligible", reason="dry-run")
        else:
            record.update(action="signal", reason=terminate(pidfd, args))
        records.append(record)
        os.close(pidfd)

    payload = {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "force": args.force,
        "protected_roots": [str(root) for root in roots],
        "processes": records,
    }
    audit_path = write_audit(payload)
    for record in records:
        memory = record["used_gpu_memory_mib"]
        memory_text = "unknown MiB" if memory is None else f"{memory} MiB"
        print(
            f"PID {record['pid']}: {record['action']} ({record['reason']}), "
            f"{memory_text}, cwd={record.get('cwd', '')}"
        )
    if not records:
        print("No NVIDIA compute processes found.")
    print(f"Audit: {audit_path}")
    unsafe = any(
        record["action"] == "signal" and record["reason"].startswith("still-running")
        for record in records
    )
    return 1 if unsafe else 0


if __name__ == "__main__":
    sys.exit(main())
