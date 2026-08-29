#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
runtime_dir="$project_root/runtime"
state_file="$runtime_dir/webapp-state.json"
lock_file="$runtime_dir/webapp-lifecycle.lock"
port="${H3_WEBAPP_PORT:-8190}"
comfy_url="${H3_COMFY_URL:-http://127.0.0.1:8188}"
# This workstation is shared: keep GPU 1 available for LocalLLM unless a
# deliberate launch opts back into dual-device conditioning.
aux_device="${H3_AUX_DEVICE:-gpu:0}"
identity=("$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" --service webapp)

if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$port < 1024 || 10#$port > 65535 )); then
  echo "H3_WEBAPP_PORT must be an integer from 1024 through 65535." >&2
  exit 2
fi
port=$((10#$port))
if [[ "$aux_device" != "gpu:0" && "$aux_device" != "gpu:1" ]]; then
  echo "H3_AUX_DEVICE must be gpu:0 or gpu:1." >&2
  exit 2
fi
export H3_AUX_DEVICE="$aux_device"

mkdir -p "$runtime_dir"
install -d -m 700 "$runtime_dir/private"
exec 8>"$lock_file"
if ! flock -n 8; then
  echo "Another webapp lifecycle operation is active." >&2
  exit 1
fi

if [[ -f "$state_file" ]]; then
  fields=$(jq -er '
    select(type == "object") |
    [.pid, .instance, .port] |
    select((.[0] | type) == "number" and (.[1] | type) == "string" and (.[2] | type) == "number") |
    @tsv
  ' "$state_file" 2>/dev/null || true)
  IFS=$'\t' read -r old_pid old_instance old_port <<< "$fields"
  if [[ -z "${old_pid:-}" || -z "${old_instance:-}" || -z "${old_port:-}" ]]; then
    echo "Existing webapp state is malformed; preserving it and refusing a second launch." >&2
    exit 1
  fi
  if "${identity[@]}" verify >/dev/null 2>&1; then
    echo "H3 Studio is already running at http://127.0.0.1:${old_port} (verified PID ${old_pid})."
    exit 0
  fi
  set +e
  /usr/bin/python3 "$script_dir/runtime_identity.py" --service webapp alive \
    --expect-pid "$old_pid" --expect-instance "$old_instance" >/dev/null 2>&1
  alive_rc=$?
  set -e
  if (( alive_rc == 0 )); then
    echo "The prior webapp kernel identity is alive but full verification was lost; preserving state." >&2
    exit 1
  fi
  if (( alive_rc != 3 )); then
    echo "The prior webapp state cannot be classified safely; preserving it." >&2
    exit 1
  fi
  "${identity[@]}" clear >/dev/null
fi

listener=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
if [[ -n "$listener" ]]; then
  echo "Port $port already has a listener; refusing to start a duplicate: $listener" >&2
  exit 1
fi

instance="${H3_WEBAPP_INSTANCE:-$(< /proc/sys/kernel/random/uuid)}"
if [[ ! "$instance" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  echo "H3_WEBAPP_INSTANCE must be a lowercase UUID." >&2
  exit 2
fi
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
log_dir="$runtime_dir/logs"
log_file="$log_dir/webapp-${timestamp}-${instance}.log"
mkdir -p "$log_dir"
install -m 600 /dev/null "$log_file"

argv=(
  "$project_root/.venv/bin/python"
  -m webapp.server
  --host 127.0.0.1
  --port "$port"
  --comfy-url "$comfy_url"
)
launcher_pid=""

cleanup_failed_start() {
  trap - EXIT INT TERM
  [[ -n "$launcher_pid" ]] || return 0
  if "${identity[@]}" verify >/dev/null 2>&1 &&
    jq -e --argjson pid "$launcher_pid" --arg instance "$instance" \
      '.pid == $pid and .instance == $instance' "$state_file" >/dev/null 2>&1; then
    /usr/bin/python3 "$script_dir/runtime_identity.py" --service webapp signal \
      --expect-pid "$launcher_pid" --expect-instance "$instance" --signal INT >/dev/null 2>&1 || true
  else
    /usr/bin/python3 "$script_dir/runtime_identity.py" --service webapp signal-marker \
      --pid "$launcher_pid" --instance "$instance" --signal TERM >/dev/null 2>&1 || true
  fi
  for _ in $(seq 1 15); do
    kill -0 "$launcher_pid" 2>/dev/null || break
    sleep 1
  done
  wait "$launcher_pid" 2>/dev/null || true
  "${identity[@]}" clear >/dev/null 2>&1 || true
}
trap cleanup_failed_start EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

nohup setsid "${identity[@]}" launch \
  --instance "$instance" --log "$log_file" --port "$port" \
  -- "${argv[@]}" 8>&- >>"$log_file" 2>&1 &
launcher_pid=$!

for _ in $(seq 1 40); do
  if ! kill -0 "$launcher_pid" 2>/dev/null; then
    echo "H3 Studio failed to start. Recent log:" >&2
    tail -n 80 "$log_file" >&2
    exit 1
  fi
  config=$(curl --silent --fail --max-time 2 "http://127.0.0.1:${port}/api/config" 2>/dev/null || true)
  listener=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
  if [[ -n "$config" ]] &&
    "${identity[@]}" verify >/dev/null 2>&1 &&
    jq -e --argjson pid "$launcher_pid" --arg instance "$instance" --argjson port "$port" \
      '.pid == $pid and .instance == $instance and .port == $port' "$state_file" >/dev/null 2>&1 &&
    [[ "$listener" == *"127.0.0.1:${port}"* ]] &&
    [[ "$listener" == *"pid=${launcher_pid},"* ]]; then
    trap - EXIT INT TERM
    echo "H3 Studio is ready at http://127.0.0.1:${port} (verified PID $launcher_pid; auxiliary stages on $aux_device)."
    echo "Log: $log_file"
    exit 0
  fi
  sleep 0.5
done

echo "H3 Studio failed its identity, listener, or API readiness check. Recent log:" >&2
tail -n 100 "$log_file" >&2
exit 1
