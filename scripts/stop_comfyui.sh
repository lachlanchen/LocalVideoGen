#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
state_file="$project_root/runtime/comfyui-state.json"
lock_file="$project_root/runtime/lifecycle.lock"
force=0
expect_pid=""
expect_instance=""
while (( $# > 0 )); do
  case "$1" in
    --force)
      force=1
      shift
      ;;
    --expect-pid)
      [[ $# -ge 2 ]] || { echo "--expect-pid requires a value." >&2; exit 2; }
      expect_pid="$2"
      shift 2
      ;;
    --expect-instance)
      [[ $# -ge 2 ]] || { echo "--expect-instance requires a value." >&2; exit 2; }
      expect_instance="$2"
      shift 2
      ;;
    *)
      echo "Usage: $0 [--force] [--expect-pid PID --expect-instance UUID]" >&2
      exit 2
      ;;
  esac
done
if [[ -n "$expect_pid" || -n "$expect_instance" ]]; then
  if [[ ! "$expect_pid" =~ ^[0-9]+$ || -z "$expect_instance" ]]; then
    echo "--expect-pid and --expect-instance must be supplied together." >&2
    exit 2
  fi
fi

mkdir -p "$project_root/runtime"
exec 8>"$lock_file"
if ! flock -n 8; then
  echo "Another project runtime lifecycle operation is active." >&2
  exit 1
fi

if [[ ! -f "$state_file" ]]; then
  if [[ -n "$expect_pid" ]]; then
    echo "The expected project runtime state no longer exists; nothing was signalled." >&2
    exit 1
  fi
  echo "No project ComfyUI runtime state exists."
  exit 0
fi

state_fields=$(jq -er '
  select(type == "object") |
  [.pid, .instance, .port] |
  select((.[0] | type) == "number" and (.[1] | type) == "string" and (.[1] | length) > 0 and (.[2] | type) == "number") |
  @tsv
' "$state_file" 2>/dev/null || true)
IFS=$'\t' read -r pid instance port <<< "$state_fields"
if [[ -z "${pid:-}" || -z "${instance:-}" || -z "${port:-}" ]]; then
  echo "The project runtime state is malformed; it was preserved and no process was signalled." >&2
  exit 1
fi
if [[ -n "$expect_pid" && ( "$pid" != "$expect_pid" || "$instance" != "$expect_instance" ) ]]; then
  echo "The live state does not match the expected PID/instance; refusing to stop it." >&2
  exit 1
fi

if ! "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" verify; then
  set +e
  /usr/bin/python3 "$script_dir/runtime_identity.py" alive \
    --expect-pid "$pid" --expect-instance "$instance" >/dev/null 2>&1
  alive_rc=$?
  set -e
  if (( alive_rc == 0 )); then
    echo "The original process is alive but its full identity is unverifiable; state was preserved and nothing was signalled." >&2
    exit 1
  fi
  if (( alive_rc != 3 )); then
    echo "The runtime identity state cannot be safely classified; state was preserved and nothing was signalled." >&2
    exit 1
  fi
  echo "The recorded runtime has exited; no process was signalled."
  "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" clear
  exit 0
fi

listener=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
if [[ -n "$listener" && "$listener" != *"pid=${pid},"* ]]; then
  echo "Port $port is owned by a different process; refusing to signal anything." >&2
  exit 1
fi

queue=$(curl --silent --fail --max-time 3 "http://127.0.0.1:${port}/queue" 2>/dev/null || true)
if [[ -n "$queue" ]] && jq -e \
  'type == "object" and (.queue_running | type == "array") and (.queue_pending | type == "array")' \
  >/dev/null 2>&1 <<< "$queue"; then
  work_count=$(jq '(.queue_running | length) + (.queue_pending | length)' <<< "$queue")
  if (( work_count > 0 && force == 0 )); then
    echo "ComfyUI has $work_count running or queued job(s); refusing to stop without --force." >&2
    exit 1
  fi
elif (( force == 0 )); then
  echo "ComfyUI's queue state is unavailable or malformed; refusing to stop without --force." >&2
  exit 1
else
  echo "WARNING: queue state is unavailable or malformed; --force permits verified shutdown." >&2
fi

signal_runtime() {
  /usr/bin/python3 "$script_dir/runtime_identity.py" signal \
    --expect-pid "$pid" \
    --expect-instance "$instance" \
    --signal "$1" >/dev/null
}

runtime_is_live() {
  if "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" verify >/dev/null 2>&1; then
    return 0
  fi
  set +e
  /usr/bin/python3 "$script_dir/runtime_identity.py" alive \
    --expect-pid "$pid" --expect-instance "$instance" >/dev/null 2>&1
  alive_rc=$?
  set -e
  if (( alive_rc == 3 )); then
    return 1
  fi
  if (( alive_rc == 0 )); then
    echo "Runtime kernel identity remains alive but full verification was lost; aborting shutdown with state preserved." >&2
  else
    echo "Runtime exit state cannot be safely classified; aborting shutdown with state preserved." >&2
  fi
  exit 1
}

signal_runtime INT
for _ in $(seq 1 30); do
  if ! runtime_is_live; then
    break
  fi
  sleep 1
done

if runtime_is_live; then
  signal_runtime TERM
  for _ in $(seq 1 15); do
    runtime_is_live || break
    sleep 1
  done
fi

if runtime_is_live; then
  if (( force == 0 )); then
    echo "Verified PID $pid did not stop after SIGINT and SIGTERM; it was not force-killed." >&2
    exit 1
  fi
  signal_runtime KILL
  for _ in $(seq 1 10); do
    runtime_is_live || break
    sleep 1
  done
fi

if runtime_is_live; then
  echo "PID $pid remains alive; runtime state was preserved." >&2
  exit 1
fi

"$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" clear
remaining_listener=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
if [[ -n "$remaining_listener" ]]; then
  echo "Project PID stopped, but another process now listens on port $port: $remaining_listener" >&2
else
  echo "Stopped verified project ComfyUI PID $pid; port $port is free."
fi
