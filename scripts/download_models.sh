#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
download_list="$project_root/runtime/model-download-queue.txt"
download_lock="$project_root/runtime/model-download.lock"
download_concurrency="${H3_DOWNLOAD_CONCURRENCY:-5}"

if [[ ! "$download_concurrency" =~ ^[1-9]$ ]]; then
  echo "H3_DOWNLOAD_CONCURRENCY must be a canonical integer from 1 through 9." >&2
  exit 2
fi

cd "$project_root"

mapfile -t aria2_pids < <(pgrep -x aria2c || true)
for aria2_pid in "${aria2_pids[@]}"; do
  [[ -r "/proc/$aria2_pid/cmdline" ]] || continue
  aria2_cmdline=$(tr '\0' ' ' < "/proc/$aria2_pid/cmdline" 2>/dev/null || true)
  aria2_cwd=$(readlink -f "/proc/$aria2_pid/cwd" 2>/dev/null || true)
  if [[ "$aria2_cwd" == "$project_root" && "$aria2_cmdline" == *"--input-file=$download_list"* ]]; then
    echo "Project model download is already active as PID $aria2_pid; refusing a second queue." >&2
    exit 1
  fi
done

mkdir -p runtime
exec 9>"$download_lock"
if ! flock -n 9; then
  echo "Another project model-download launcher holds $download_lock; refusing a second queue." >&2
  exit 1
fi

mkdir -p \
  ComfyUI/models/diffusion_models \
  ComfyUI/models/text_encoders \
  ComfyUI/models/vae \
  ComfyUI/models/loras

"$project_root/.venv/bin/python" "$script_dir/build_download_queue.py"
if [[ ! -s "$download_list" ]]; then
  echo "All model files are already complete and structurally valid."
  exit 0
fi

headroom_gib=${H3_DOWNLOAD_HEADROOM_GIB:-32}
if [[ ! "$headroom_gib" =~ ^(0|[1-9][0-9]{0,3})$ ]]; then
  echo "H3_DOWNLOAD_HEADROOM_GIB must be a non-negative integer." >&2
  exit 1
fi
headroom_gib=$((10#$headroom_gib))
remaining_bytes=$("$project_root/.venv/bin/python" "$script_dir/build_download_queue.py" --remaining-bytes)
if [[ ! "$remaining_bytes" =~ ^[0-9]+$ ]]; then
  echo "Could not determine the remaining model payload size." >&2
  exit 1
fi
gib=$((1024 * 1024 * 1024))
required_bytes=$((remaining_bytes + headroom_gib * gib))
available_bytes=$(df -B1 --output=avail . | tail -1 | tr -d ' ')
if (( available_bytes < required_bytes )); then
  remaining_gib=$(((remaining_bytes + gib - 1) / gib))
  required_gib=$(((required_bytes + gib - 1) / gib))
  echo "Need about $required_gib GiB free: $remaining_gib GiB estimated remaining payload plus $headroom_gib GiB headroom." >&2
  exit 1
fi

exec aria2c \
  --input-file="$download_list" \
  --continue=true \
  --max-concurrent-downloads="$download_concurrency" \
  --max-connection-per-server=16 \
  --split=16 \
  --min-split-size=1M \
  --file-allocation=none \
  --check-integrity=true \
  --max-tries=0 \
  --retry-wait=2 \
  --connect-timeout=30 \
  --timeout=30 \
  --auto-file-renaming=false \
  --allow-overwrite=false \
  --summary-interval=60 \
  --console-log-level=warn \
  --show-console-readout=false
