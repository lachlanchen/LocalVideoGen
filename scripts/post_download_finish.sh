#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[0-9]+$ ]]; then
  echo "Usage: $0 ARIA2_PID" >&2
  exit 2
fi

download_pid="$1"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
expected_queue="$project_root/runtime/model-download-queue.txt"
state_file="$project_root/runtime/comfyui-state.json"
gpu_wait_seconds="${H3_GPU_WAIT_SECONDS:-21600}"
port="${H3_PORT:-8188}"
umask 077
mkdir -p "$project_root/runtime"
exec 7>"$project_root/runtime/post-download.lock"
if ! flock -n 7; then
  echo "Another post-download monitor or validator is already active." >&2
  exit 1
fi
if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$port < 1024 || 10#$port > 65535 )); then
  echo "H3_PORT must be an integer from 1024 through 65535." >&2
  exit 2
fi
if [[ ! "$gpu_wait_seconds" =~ ^[1-9][0-9]{0,7}$ ]]; then
  echo "H3_GPU_WAIT_SECONDS must be a positive integer." >&2
  exit 2
fi
port=$((10#$port))
gpu_wait_seconds=$((10#$gpu_wait_seconds))

process_start_ticks() {
  local stat_text remainder
  local -a fields
  stat_text=$(< "/proc/$1/stat") || return 1
  remainder="${stat_text##*) }"
  read -r -a fields <<< "$remainder"
  [[ ${#fields[@]} -gt 19 ]] || return 1
  printf '%s\n' "${fields[19]}"
}

download_cwd=$(readlink -f "/proc/$download_pid/cwd" 2>/dev/null || true)
download_cmdline=$(tr '\0' ' ' < "/proc/$download_pid/cmdline" 2>/dev/null || true)
if [[ "$download_cwd" != "$project_root" || "$download_cmdline" != *"aria2c"* || "$download_cmdline" != *"$expected_queue"* ]]; then
  echo "PID $download_pid is not this project's active aria2 queue; refusing to monitor it." >&2
  exit 1
fi
download_start_ticks=$(process_start_ticks "$download_pid")

echo "Monitoring verified project download PID $download_pid."
next_report=$((SECONDS + 600))
while [[ -r "/proc/$download_pid/stat" ]]; do
  current_start_ticks=$(process_start_ticks "$download_pid" 2>/dev/null || true)
  if [[ "$current_start_ticks" != "$download_start_ticks" ]]; then
    echo "The monitored PID changed identity; treating the original aria2 process as exited." >&2
    break
  fi
  if (( SECONDS >= next_report )); then
    allocated=$(du -sh "$project_root/ComfyUI/models" | awk '{print $1}')
    echo "$(date -Is): download still active; model directory uses $allocated."
    next_report=$((SECONDS + 600))
  fi
  sleep 30
done

if find "$project_root/ComfyUI/models" -type f -name '*.aria2' -print -quit | grep -q .; then
  echo "aria2 exited with incomplete control files; restart scripts/download_models.sh." >&2
  exit 1
fi

echo "Download complete; starting independent validation."
cd "$project_root"
./scripts/verify_models.sh
"$project_root/.venv/bin/python" ./scripts/inventory_models.py
./scripts/validate_workflows.py

quick_test_root="$project_root/runtime/quick-test"
install -d -m 700 \
  "$quick_test_root/input" \
  "$quick_test_root/output" \
  "$quick_test_root/temp" \
  "$quick_test_root/user"
(
  cd ComfyUI
  ../.venv/bin/python -u main.py \
    --quick-test-for-ci \
    --cpu \
    --disable-all-custom-nodes \
    --disable-auto-launch \
    --preview-method none \
    --cache-none \
    --input-directory "$quick_test_root/input" \
    --output-directory "$quick_test_root/output" \
    --temp-directory "$quick_test_root/temp" \
    --user-directory "$quick_test_root/user" \
    --database-url sqlite:///:memory: \
    --log-stdout
)

echo "Validation complete; waiting for safe GPU 0 headroom for the native AV smoke test."
deadline=$((SECONDS + gpu_wait_seconds))
next_report=$SECONDS
ready=0
while (( SECONDS < deadline )); do
  mem_available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  swap_total_kib=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)
  swap_free_kib=$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)
  swap_safe=1
  if (( swap_total_kib > 0 && (swap_total_kib - swap_free_kib) * 100 > swap_total_kib * 75 )); then
    swap_safe=0
  fi
  gpu_free_mib=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i 0 | tr -d ' ')
  listener=$(ss -H -ltn "sport = :$port" 2>/dev/null || true)
  if (( mem_available_kib >= 48 * 1024 * 1024 && gpu_free_mib >= 20000 && swap_safe == 1 )) && [[ -z "$listener" ]]; then
    ready=1
    break
  fi
  if (( SECONDS >= next_report )); then
    echo "$(date -Is): waiting; RAM available $((mem_available_kib / 1024 / 1024)) GiB, GPU 0 free ${gpu_free_mib} MiB, swap-safe ${swap_safe}."
    next_report=$((SECONDS + 600))
  fi
  sleep 30
done

if (( ready == 0 )); then
  echo "Models are fully validated, but RAM/GPU/swap did not gain safe headroom within ${gpu_wait_seconds}s." >&2
  exit 2
fi

owned_instance=$(< /proc/sys/kernel/random/uuid)
H3_PORT="$port" H3_RUNTIME_INSTANCE="$owned_instance" H3_CUDA_DEVICES=0 ./scripts/start_comfyui.sh 7>&-

if ! "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" verify >/dev/null; then
  echo "The runtime started but its private identity could not be captured; refusing broad cleanup." >&2
  exit 1
fi
owned_pid=$(jq -er '.pid | numbers' "$state_file")
captured_instance=$(jq -er '.instance | strings | select(length > 0)' "$state_file")
captured_port=$(jq -er '.port | numbers' "$state_file")
if [[ "$captured_instance" != "$owned_instance" || "$captured_port" != "$port" ]]; then
  echo "The runtime identity changed immediately after startup; refusing to stop an unknown process." >&2
  exit 1
fi

cleanup_runtime() {
  H3_PORT="$port" H3_CUDA_DEVICES=0 ./scripts/stop_comfyui.sh \
    --force --expect-pid "$owned_pid" --expect-instance "$owned_instance" \
    >/dev/null 2>&1 || true
}
trap cleanup_runtime EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

H3_BASE_URL="http://127.0.0.1:$port" \
  H3_SMOKE_MODEL=minimax_h3_fl2va_pruned_int8_convrot.safetensors \
  ./scripts/smoke_test.sh
mem_available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
if (( mem_available_kib >= 72 * 1024 * 1024 )); then
  H3_BASE_URL="http://127.0.0.1:$port" H3_SMOKE_MODEL=minimax_h3_fl2va_pruned_bf16.safetensors ./scripts/smoke_test.sh
else
  echo "Skipping the BF16 execution smoke because less than 72 GiB RAM is available; hashes and headers are still validated."
fi
H3_PORT="$port" ./scripts/stop_comfyui.sh \
  --expect-pid "$owned_pid" --expect-instance "$owned_instance"
trap - EXIT INT TERM

echo "Post-download validation and native MiniMax H3 AV smoke test passed."
nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu --format=csv,noheader
