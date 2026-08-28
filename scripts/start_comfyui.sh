#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
runtime_dir="$project_root/runtime"
state_file="$runtime_dir/comfyui-state.json"
lock_file="$runtime_dir/lifecycle.lock"
listen_address="127.0.0.1"
port="${H3_PORT:-8188}"
cuda_devices="${H3_CUDA_DEVICES:-0,1}"

if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$port < 1024 || 10#$port > 65535 )); then
  echo "H3_PORT must be an integer from 1024 through 65535." >&2
  exit 2
fi
port=$((10#$port))
if [[ ! "$cuda_devices" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  echo "H3_CUDA_DEVICES must be a comma-separated list of unique GPU indices." >&2
  exit 2
fi
mapfile -t installed_devices < <(nvidia-smi --query-gpu=index --format=csv,noheader,nounits)
declare -A installed=()
for device in "${installed_devices[@]}"; do
  installed["${device// /}"]=1
done
IFS=',' read -r -a requested_devices <<< "$cuda_devices"
declare -A requested=()
for device in "${requested_devices[@]}"; do
  if [[ -n "${requested[$device]:-}" || -z "${installed[$device]:-}" ]]; then
    echo "H3_CUDA_DEVICES contains a duplicate or unavailable GPU index: $device" >&2
    exit 2
  fi
  requested["$device"]=1
done
expected_device_count=${#requested_devices[@]}
startup_wait_seconds="${H3_STARTUP_WAIT_SECONDS:-}"
if [[ -z "$startup_wait_seconds" ]]; then
  if [[ -n "${H3_PARTIAL_PROFILE:-}" ]]; then
    startup_wait_seconds=300
  else
    startup_wait_seconds=90
  fi
fi
if [[ ! "$startup_wait_seconds" =~ ^[1-9][0-9]{0,3}$ ]]; then
  echo "H3_STARTUP_WAIT_SECONDS must be a positive integer up to 9999." >&2
  exit 2
fi
startup_wait_seconds=$((10#$startup_wait_seconds))

mkdir -p "$runtime_dir"
exec 8>"$lock_file"
if ! flock -n 8; then
  echo "Another project runtime lifecycle operation is active." >&2
  exit 1
fi

if [[ -f "$state_file" ]]; then
  existing_fields=$(jq -er '
    select(type == "object") |
    [.pid, .instance] |
    select((.[0] | type) == "number" and (.[1] | type) == "string" and (.[1] | length) > 0) |
    @tsv
  ' "$state_file" 2>/dev/null || true)
  IFS=$'\t' read -r existing_pid existing_instance <<< "$existing_fields"
  if [[ -z "${existing_pid:-}" || -z "${existing_instance:-}" ]]; then
    echo "Existing runtime state is malformed; preserving it and refusing a second launch." >&2
    exit 1
  fi
  if "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" verify >/dev/null; then
    echo "ComfyUI is already running as verified PID $existing_pid." >&2
    exit 1
  fi
  set +e
  /usr/bin/python3 "$script_dir/runtime_identity.py" alive \
    --expect-pid "$existing_pid" --expect-instance "$existing_instance" >/dev/null 2>&1
  existing_alive_rc=$?
  set -e
  if (( existing_alive_rc == 0 )); then
    echo "The prior runtime kernel identity is alive but full verification was lost; preserving state and refusing a second launch." >&2
    exit 1
  fi
  if (( existing_alive_rc != 3 )); then
    echo "The prior runtime state cannot be safely classified; preserving it and refusing a second launch." >&2
    exit 1
  fi
  "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" clear
fi

instance="${H3_RUNTIME_INSTANCE:-$(< /proc/sys/kernel/random/uuid)}"
if [[ ! "$instance" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  echo "H3_RUNTIME_INSTANCE must be a lowercase UUID." >&2
  exit 2
fi
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
log_dir="$runtime_dir/logs"
log_file="$log_dir/comfyui-${timestamp}-${instance}.log"
mkdir -p "$log_dir"
install -m 600 /dev/null "$log_file"

export LOCALVIDEOGEN_COMFY_INSTANCE="$instance"
export H3_LOG_FILE="$log_file"
launcher_pid=""

state_belongs_to_launcher() {
  [[ -n "$launcher_pid" ]] &&
  "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" verify >/dev/null 2>&1 &&
    jq -e --argjson pid "$launcher_pid" --arg instance "$instance" --argjson port "$port" \
      '.pid == $pid and .instance == $instance and .port == $port' "$state_file" >/dev/null 2>&1
}

launcher_has_private_marker() {
  [[ -n "$launcher_pid" && -r "/proc/$launcher_pid/environ" ]] &&
    grep -zFqx "LOCALVIDEOGEN_COMFY_INSTANCE=$instance" "/proc/$launcher_pid/environ" 2>/dev/null
}

launcher_exited() {
  local stat_text remainder process_state
  [[ -n "$launcher_pid" ]] || return 0
  if [[ ! -r "/proc/$launcher_pid/stat" ]]; then
    return 0
  fi
  stat_text=$(< "/proc/$launcher_pid/stat") || return 0
  remainder="${stat_text##*) }"
  process_state="${remainder%% *}"
  [[ "$process_state" == "Z" ]]
}

signal_launcher() {
  local signal_name="$1"
  if state_belongs_to_launcher; then
    /usr/bin/python3 "$script_dir/runtime_identity.py" signal \
      --expect-pid "$launcher_pid" \
      --expect-instance "$instance" \
      --signal "$signal_name" >/dev/null 2>&1 || true
  else
    /usr/bin/python3 "$script_dir/runtime_identity.py" signal-marker \
      --pid "$launcher_pid" \
      --instance "$instance" \
      --signal "$signal_name" >/dev/null 2>&1 || true
  fi
}

cleanup_failed_start() {
  trap - EXIT INT TERM
  [[ -n "$launcher_pid" ]] || return 0
  if state_belongs_to_launcher; then
    signal_launcher INT
    for _ in $(seq 1 15); do
      launcher_exited && break
      sleep 1
    done
  fi
  if ! launcher_exited && launcher_has_private_marker; then
    signal_launcher TERM
  fi
  for _ in $(seq 1 15); do
    launcher_exited && break
    sleep 1
  done
  if ! launcher_exited && launcher_has_private_marker; then
    signal_launcher KILL
    for _ in $(seq 1 10); do
      launcher_exited && break
      sleep 1
    done
  fi
  if launcher_exited; then
    wait "$launcher_pid" 2>/dev/null || true
    if [[ -f "$state_file" ]] && jq -e --argjson pid "$launcher_pid" --arg instance "$instance" \
      '.pid == $pid and .instance == $instance' "$state_file" >/dev/null 2>&1; then
      "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" clear >/dev/null 2>&1 || true
    fi
  else
    echo "Failed-start cleanup could not terminate verified launcher PID $launcher_pid; state/log were preserved." >&2
  fi
}
trap cleanup_failed_start EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The lifecycle lock belongs only to this short start operation. Closing FD 8
# in the child prevents the long-lived Comfy process from retaining its flock.
# Detach the heavy runtime from this short-lived launcher's process group.
# Some non-interactive runners terminate their command process group after the
# launcher exits even though nohup already ignores SIGHUP.  The webapp
# lifecycle uses the same setsid boundary, and ComfyUI needs it as well so a
# successfully verified engine remains alive for long series renders.
nohup setsid "$script_dir/run_comfyui.sh" 8>&- >>"$log_file" 2>&1 &
launcher_pid=$!

for _ in $(seq 1 "$startup_wait_seconds"); do
  if ! kill -0 "$launcher_pid" 2>/dev/null; then
    echo "ComfyUI failed to start. Recent log:" >&2
    tail -n 80 "$log_file" >&2
    exit 1
  fi
  stats=$(curl --silent --fail --max-time 2 "http://${listen_address}:${port}/system_stats" 2>/dev/null || true)
  listener=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
  if \
    [[ -n "$stats" ]] && \
    state_belongs_to_launcher && \
    jq -e --argjson count "$expected_device_count" \
      '.devices | length == $count and all(.[]; (.name // "") | test("RTX 4090"))' \
      >/dev/null <<< "$stats" && \
    [[ "$listener" == *"127.0.0.1:${port}"* ]] && \
    [[ "$listener" == *"pid=${launcher_pid},"* ]] && \
    grep -q 'DynamicVRAM support detected and enabled' "$log_file" && \
    grep -q 'Using async weight offloading with 2 streams' "$log_file"; then
    trap - EXIT INT TERM
    echo "ComfyUI is ready at http://${listen_address}:${port} (verified PID $launcher_pid)."
    echo "Log: $log_file"
    exit 0
  fi
  sleep 1
done

echo "ComfyUI failed its ${startup_wait_seconds}-second identity/API/memory-mode validation. Recent log:" >&2
tail -n 100 "$log_file" >&2
exit 1
