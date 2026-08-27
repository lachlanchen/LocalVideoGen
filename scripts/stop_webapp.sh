#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
state_file="$project_root/runtime/webapp-state.json"
lock_file="$project_root/runtime/webapp-lifecycle.lock"
identity=("$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" --service webapp)
force=0
if (( $# > 1 )) || [[ ${1:-} != "" && ${1:-} != "--force" ]]; then
  echo "Usage: $0 [--force]" >&2
  exit 2
fi
[[ ${1:-} == "--force" ]] && force=1

mkdir -p "$project_root/runtime"
exec 8>"$lock_file"
if ! flock -n 8; then
  echo "Another webapp lifecycle operation is active." >&2
  exit 1
fi

if [[ ! -f "$state_file" ]]; then
  echo "No H3 Studio runtime state exists."
  exit 0
fi
fields=$(jq -er '
  select(type == "object") |
  [.pid, .instance, .port] |
  select((.[0] | type) == "number" and (.[1] | type) == "string" and (.[2] | type) == "number") |
  @tsv
' "$state_file" 2>/dev/null || true)
IFS=$'\t' read -r pid instance port <<< "$fields"
if [[ -z "${pid:-}" || -z "${instance:-}" || -z "${port:-}" ]]; then
  echo "Webapp state is malformed; it was preserved and no process was signalled." >&2
  exit 1
fi

if ! "${identity[@]}" verify >/dev/null 2>&1; then
  set +e
  /usr/bin/python3 "$script_dir/runtime_identity.py" --service webapp alive \
    --expect-pid "$pid" --expect-instance "$instance" >/dev/null 2>&1
  alive_rc=$?
  set -e
  if (( alive_rc == 3 )); then
    "${identity[@]}" clear
    echo "The recorded H3 Studio process had already exited."
    exit 0
  fi
  echo "The original webapp identity is alive or unclassifiable but not fully verified; preserving state." >&2
  exit 1
fi

listener=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
if [[ -n "$listener" && "$listener" != *"pid=${pid},"* ]]; then
  echo "Port $port is now owned by another process; refusing to signal anything." >&2
  exit 1
fi

signal_runtime() {
  /usr/bin/python3 "$script_dir/runtime_identity.py" --service webapp signal \
    --expect-pid "$pid" --expect-instance "$instance" --signal "$1" >/dev/null
}

runtime_live() {
  "${identity[@]}" verify >/dev/null 2>&1
}

signal_runtime INT
for _ in $(seq 1 20); do
  runtime_live || break
  sleep 0.5
done
if runtime_live; then
  signal_runtime TERM
  for _ in $(seq 1 20); do
    runtime_live || break
    sleep 0.5
  done
fi
if runtime_live && (( force == 1 )); then
  signal_runtime KILL
  for _ in $(seq 1 10); do
    runtime_live || break
    sleep 0.5
  done
fi
if runtime_live; then
  echo "Verified H3 Studio PID $pid did not stop; rerun with --force to permit SIGKILL." >&2
  exit 1
fi

"${identity[@]}" clear >/dev/null
remaining=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
if [[ -n "$remaining" ]]; then
  echo "H3 Studio stopped, but another process now listens on port $port: $remaining" >&2
else
  echo "Stopped verified H3 Studio PID $pid; port $port is free. ComfyUI jobs continue independently."
fi
