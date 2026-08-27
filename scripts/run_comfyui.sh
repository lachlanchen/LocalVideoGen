#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
runtime_dir="$project_root/runtime"
lock_file="$runtime_dir/comfyui.lock"
download_lock_file="$runtime_dir/model-download.lock"
cuda_devices="${H3_CUDA_DEVICES:-0,1}"
listen_address="127.0.0.1"
port="${H3_PORT:-8188}"
instance="${LOCALVIDEOGEN_COMFY_INSTANCE:?missing private runtime instance marker}"
log_file="${H3_LOG_FILE:?missing runtime log path}"
partial_profile="${H3_PARTIAL_PROFILE:-}"

mkdir -p "$runtime_dir"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "This project's ComfyUI runtime is already locked." >&2
  exit 1
fi

# Hold the model-download lock for the runtime's full lifetime. This closes the
# check/start race and prevents a resumed downloader from mutating weights that
# an active render may mmap or stream from disk.
exec 7>"$download_lock_file"
if ! flock -n 7; then
  echo "This project's model downloader is active; refusing to start ComfyUI." >&2
  exit 1
fi

if ss -ltn | awk '{print $4}' | grep -Eq ":${port}$"; then
  echo "Port $port is already listening; refusing to start a second runtime." >&2
  exit 1
fi

if [[ -n "$partial_profile" ]]; then
  if [[ "$partial_profile" != "fl-int8-turbo" ]]; then
    echo "Unsupported H3_PARTIAL_PROFILE: $partial_profile" >&2
    exit 2
  fi
  "$project_root/.venv/bin/python" "$script_dir/verify_partial_smoke.py" --profile "$partial_profile"
else
  if find "$project_root/ComfyUI/models" -type f -name '*.aria2' -print -quit | grep -q .; then
    echo "Model downloads are still active; refusing to start a heavy H3 runtime." >&2
    exit 1
  fi

  receipt="$runtime_dir/models.verified"
  if [[ ! -f "$receipt" ]] || [[ "$(<"$receipt")" != "$("$script_dir/model_fingerprint.sh")" ]]; then
    echo "Models do not match a completed verification receipt. Run scripts/verify_models.sh." >&2
    exit 1
  fi
fi

"$script_dir/check_resources.sh"

cd "$project_root/ComfyUI"

comfy_argv=(
  "$project_root/.venv/bin/python" -u "$project_root/ComfyUI/main.py"
  --listen "$listen_address" \
  --port "$port" \
  --cuda-device "$cuda_devices" \
  --preview-method none \
  --cache-none \
  --reserve-vram 1 \
  --disable-auto-launch \
  --disable-all-custom-nodes \
  --log-stdout
)
exec "$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" launch \
  --instance "$instance" \
  --log "$log_file" \
  --port "$port" \
  -- "${comfy_argv[@]}"
