#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
base_url="${H3_BASE_URL:-http://127.0.0.1:8188}"
timeout_seconds="${H3_SMOKE_TIMEOUT:-7200}"
payload="$project_root/config/h3-smoke-api.json"
model_name="${H3_SMOKE_MODEL:-minimax_h3_fl2va_pruned_int8_convrot.safetensors}"
prompt_id=""
status=""

if [[ ! "$timeout_seconds" =~ ^[1-9][0-9]{0,7}$ ]]; then
  echo "H3_SMOKE_TIMEOUT must be a positive canonical decimal integer." >&2
  exit 2
fi
timeout_seconds=$((10#$timeout_seconds))

case "$model_name" in
  minimax_h3_fl2va_pruned_int8_convrot.safetensors)
    output_prefix="video/MiniMax_H3_smoke_int8"
    ;;
  minimax_h3_fl2va_pruned_bf16.safetensors)
    output_prefix="video/MiniMax_H3_smoke_bf16"
    ;;
  *)
    echo "Unsupported H3_SMOKE_MODEL: $model_name" >&2
    exit 2
    ;;
esac

cancel_unfinished() {
  if [[ -n "$prompt_id" && "$status" != "completed" ]]; then
    curl --silent --fail --max-time 5 -X POST "$base_url/api/jobs/$prompt_id/cancel" >/dev/null 2>&1 || true
  fi
}
trap cancel_unfinished EXIT INT TERM

if find "$project_root/ComfyUI/models" -type f -name '*.aria2' -print -quit | grep -q .; then
  echo "Model downloads are still active; smoke generation is blocked." >&2
  exit 1
fi

receipt="$project_root/runtime/models.verified"
if [[ ! -f "$receipt" ]] || [[ "$(<"$receipt")" != "$("$script_dir/model_fingerprint.sh")" ]]; then
  echo "Models do not match a verification receipt. Run scripts/verify_models.sh first." >&2
  exit 1
fi

curl --silent --fail --max-time 5 "$base_url/system_stats" >/dev/null

required_nodes=(
  UNETLoader CLIPLoader VAELoader MiniMaxH3ImageToVideo RandomNoise
  KSamplerSelect BasicScheduler BasicGuider SamplerCustomAdvanced
  VAEDecode VAEDecodeAudio CreateVideo SaveVideo
)
for node in "${required_nodes[@]}"; do
  curl --silent --fail --max-time 10 "$base_url/object_info/$node" |
    jq -e --arg node "$node" 'has($node)' >/dev/null
done

curl --silent --fail --max-time 10 "$base_url/object_info/UNETLoader" |
  jq -e --arg model "$model_name" '.UNETLoader.input.required.unet_name[0] | index($model) != null' >/dev/null
curl --silent --fail --max-time 10 "$base_url/object_info/CLIPLoader" |
  jq -e '.CLIPLoader.input.required.clip_name[0] | index("qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors") != null' >/dev/null

payload_json=$(jq --arg model "$model_name" --arg prefix "$output_prefix" \
  '.prompt["1"].inputs.unet_name = $model | .prompt["14"].inputs.filename_prefix = $prefix' \
  "$payload")
response=$(curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  --data-binary "$payload_json" \
  "$base_url/prompt")
prompt_id=$(jq -er '.prompt_id' <<< "$response")
echo "Submitted MiniMax H3 smoke job $prompt_id with $model_name"

deadline=$((SECONDS + timeout_seconds))
job=""
while (( SECONDS < deadline )); do
  job=$(curl --silent --fail --max-time 10 "$base_url/api/jobs/$prompt_id" 2>/dev/null || true)
  if [[ -n "$job" ]]; then
    status=$(jq -r '.status // "unknown"' <<< "$job")
    case "$status" in
      completed)
        break
        ;;
      failed|cancelled)
        jq '.execution_error // .execution_status // .' <<< "$job" >&2
        exit 1
        ;;
    esac
  fi
  sleep 5
done

if [[ "$status" != "completed" ]]; then
  echo "Smoke job did not complete within ${timeout_seconds}s." >&2
  exit 124
fi

output_relative=$(jq -er '.outputs["14"].images[0] | ((if .subfolder == "" then "" else .subfolder + "/" end) + .filename)' <<< "$job")
output_file=$(realpath -m "$project_root/ComfyUI/output/$output_relative")
output_root=$(realpath "$project_root/ComfyUI/output")
if [[ "$output_file" != "$output_root/"* ]] || [[ ! -s "$output_file" ]]; then
  echo "Smoke output path is invalid or empty: $output_file" >&2
  exit 1
fi

probe=$(ffprobe -v error \
  -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels \
  -of json "$output_file")
jq -e '
  any(.streams[]; .codec_type == "video" and .width == 256 and .height == 256) and
  any(.streams[]; .codec_type == "audio")
' >/dev/null <<< "$probe"

trap - EXIT INT TERM
echo "$probe" | jq .
echo "PASS: $output_file"
