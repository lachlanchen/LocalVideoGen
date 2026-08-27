#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
min_ram_gib="${H3_MIN_RAM_GIB:-48}"
min_gpu_free_mib="${H3_MIN_GPU_FREE_MIB:-20000}"
cuda_devices="${H3_CUDA_DEVICES:-0,1}"
port="${H3_PORT:-8188}"

if [[ ! "$min_ram_gib" =~ ^[1-9][0-9]{0,2}$ ]]; then
  echo "ERROR: H3_MIN_RAM_GIB must be a positive integer." >&2
  exit 2
fi
if [[ ! "$min_gpu_free_mib" =~ ^(0|[1-9][0-9]{0,5})$ ]]; then
  echo "ERROR: H3_MIN_GPU_FREE_MIB must be a non-negative integer." >&2
  exit 2
fi
if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$port < 1024 || 10#$port > 65535 )); then
  echo "ERROR: H3_PORT must be an integer from 1024 through 65535." >&2
  exit 2
fi
min_ram_gib=$((10#$min_ram_gib))
min_gpu_free_mib=$((10#$min_gpu_free_mib))
port=$((10#$port))
if [[ ! "$cuda_devices" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  echo "ERROR: H3_CUDA_DEVICES must be a comma-separated list of unique GPU indices." >&2
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
  if [[ -n "${requested[$device]:-}" ]]; then
    echo "ERROR: GPU index $device is duplicated in H3_CUDA_DEVICES." >&2
    exit 2
  fi
  if [[ -z "${installed[$device]:-}" ]]; then
    echo "ERROR: GPU index $device does not exist on this workstation." >&2
    exit 2
  fi
  requested["$device"]=1
done

mem_available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
swap_total_kib=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)
swap_free_kib=$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)
min_ram_kib=$((min_ram_gib * 1024 * 1024))

echo "System memory"
free -h
echo
echo "NVIDIA GPUs"
nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu \
  --format=csv,noheader
echo
echo "GPU compute processes (ownership is informational; nothing is stopped)"
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name --format=csv,noheader || true
echo
echo "Project-owned processes"
project_processes=$(ps -eo pid=,args= | awk -v root="$project_root" '
  index($0, root) && index($0, "check_resources.sh") == 0 && index($0, "status.sh") == 0 {print}
')
if [[ -n "$project_processes" ]]; then
  echo "$project_processes"
else
  echo "none"
fi
echo
echo "ComfyUI port"
ss -H -ltnp "sport = :$port" || true
echo
echo "tmux sessions"
tmux list-sessions 2>/dev/null || echo "none"

unsafe=0
if (( mem_available_kib < min_ram_kib )); then
  echo "ERROR: ${min_ram_gib} GiB available RAM is required; less is currently available." >&2
  unsafe=1
fi

if (( swap_total_kib > 0 )); then
  swap_used_kib=$((swap_total_kib - swap_free_kib))
  if (( swap_used_kib * 100 > swap_total_kib * 75 )); then
    echo "WARNING: swap use is above 75%; no foreign process will be stopped." >&2
    echo "ERROR: shared-workstation policy blocks a new heavy runtime until swap falls to 75% or below." >&2
    unsafe=1
  fi
fi

for device in "${requested_devices[@]}"; do
  free_mib=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i "$device" | tr -d ' ')
  if (( free_mib < min_gpu_free_mib )); then
    echo "ERROR: GPU $device has ${free_mib} MiB free; ${min_gpu_free_mib} MiB is required." >&2
    unsafe=1
  fi
done

if (( unsafe != 0 )); then
  exit 1
fi

echo
echo "Resource gate passed for CUDA device(s) $cuda_devices."
