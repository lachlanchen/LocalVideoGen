#!/usr/bin/env bash
set -euo pipefail
set -o noclobber

usage() {
  printf 'Usage: %s RECEIPT SHOT_INDEX REVIEW_ROOT\n' "${0##*/}" >&2
  printf 'Review one accepted zero-based series shot without overwriting evidence.\n' >&2
}

if (( $# != 3 )); then
  usage
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
client="$script_dir/localvideogen_series.py"
receipt="$1"
shot_index_text="$2"
review_root="$3"

if [[ ! "$shot_index_text" =~ ^(0|[1-9]|1[01])$ ]]; then
  echo "SHOT_INDEX must be a zero-based integer from 0 through 11." >&2
  exit 2
fi
if [[ -z "$review_root" ]]; then
  echo "REVIEW_ROOT must not be empty." >&2
  exit 2
fi
shot_index=$((10#$shot_index_text))

for command_name in jq ffmpeg ffprobe awk sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is unavailable: $command_name" >&2
    exit 1
  fi
done
if [[ ! -x "$client" ]]; then
  echo "Series client is unavailable or not executable: $client" >&2
  exit 1
fi

# recover validates the bounded, non-symlink receipt and performs one read-only
# lookup of its exact durable series.  This helper never starts or resumes it.
recovery="$("$client" recover "$receipt")"
series_state="$(jq -c '.series' <<< "$recovery")"
series_id="$(jq -er '.id' <<< "$series_state")"
series_status="$(jq -er '.status' <<< "$series_state")"

case "$series_status" in
  paused|completed)
    ;;
  *)
    echo "Series must be paused or completed before review; current status is $series_status." >&2
    exit 1
    ;;
esac

shot_count="$(jq -er '.shots | length' <<< "$series_state")"
if (( shot_index >= shot_count )); then
  echo "SHOT_INDEX $shot_index is outside this series' $shot_count shots." >&2
  exit 2
fi
if ! jq -e --argjson shot "$shot_index" '
  .shots[$shot].status == "completed"
  and ((.shots[$shot].accepted_attempt | type) == "number")
' >/dev/null <<< "$series_state"; then
  echo "Selected shot must be completed with an accepted attempt." >&2
  exit 1
fi

accepted_attempt="$(jq -er --argjson shot "$shot_index" \
  '.shots[$shot].accepted_attempt' <<< "$series_state")"
shot_artifact_id="$(jq -er \
  --argjson shot "$shot_index" \
  --argjson attempt "$accepted_attempt" '
    [
      .shots[$shot].attempts[]
      | select(.number == $attempt)
      | .outputs[]
      | select(.kind == "shot" and .superseded != true)
      | .id
    ]
    | if length == 1 then .[0]
      else error("accepted attempt does not expose exactly one current shot artifact")
      end
  ' <<< "$series_state")"

is_final_shot=false
if (( shot_index + 1 == shot_count )); then
  is_final_shot=true
fi

tail_artifact_id=""
frame_artifact_id=""
if [[ "$is_final_shot" == false ]]; then
  tail_artifact_id="$(jq -er --argjson shot "$shot_index" '
    [
      .shots[$shot].continuity[]?
      | select(.kind == "continuity_video" and .superseded != true)
      | .id
    ]
    | if length == 1 then .[0]
      else error("shot does not expose exactly one current continuity tail")
      end
  ' <<< "$series_state")"
  frame_artifact_id="$(jq -er --argjson shot "$shot_index" '
    [
      .shots[$shot].continuity[]?
      | select(.kind == "final_frame" and .superseded != true)
      | .id
    ]
    | if length == 1 then .[0]
      else error("shot does not expose exactly one current final frame")
      end
  ' <<< "$series_state")"
fi

shot_number="$(printf '%02d' "$((shot_index + 1))")"
attempt_number="$(printf '%02d' "$accepted_attempt")"
stem="shot-${shot_number}-attempt-${attempt_number}"
series_review_root="$review_root/$series_id"
review_dir="$series_review_root/$stem"

umask 077
mkdir -p -- "$series_review_root"
if ! mkdir -m 700 -- "$review_dir"; then
  echo "Review directory already exists or cannot be created; nothing was overwritten: $review_dir" >&2
  exit 1
fi

shot_file="$review_dir/$stem.mp4"
shot_probe="$review_dir/$stem-probe.json"
contact_sheet="$review_dir/$stem-contact-sheet.png"
boundary_strip="$review_dir/$stem-outgoing-boundary-strip.png"

"$client" download "$series_id" "$shot_artifact_id" "$shot_file" >/dev/null

probe_media() {
  local input_file="$1"
  local output_file="$2"
  ffprobe -v error -count_frames -show_streams -show_format -of json \
    "$input_file" > "$output_file"
}

decode_video_audio() {
  local input_file="$1"
  ffmpeg -hide_banner -nostdin -v error -xerror \
    -i "$input_file" -map 0:v:0 -map 0:a:0 -f null -
}

probe_media "$shot_file" "$shot_probe"
decode_video_audio "$shot_file"

shot_duration="$(jq -er '
  [.streams[] | select(.codec_type == "video") | .duration]
  | if length == 1 then .[0] else error("shot needs exactly one timed video stream") end
' "$shot_probe")"
contact_fps="$(awk -v duration="$shot_duration" 'BEGIN {
  if (duration <= 0) exit 1
  printf "%.12f", 12 / duration
}')"

ffmpeg -hide_banner -nostdin -v error -xerror -n \
  -i "$shot_file" \
  -vf "fps=${contact_fps},scale=448:-2:flags=lanczos,tile=4x3:padding=6:margin=6" \
  -frames:v 1 "$contact_sheet"

evidence_names=(
  "${stem}.mp4"
  "${stem}-probe.json"
  "${stem}-contact-sheet.png"
)

if [[ "$is_final_shot" == false ]]; then
  tail_file="$review_dir/$stem-continuity-tail.mp4"
  frame_file="$review_dir/$stem-final-frame.png"
  tail_probe="$review_dir/$stem-continuity-tail-probe.json"
  frame_probe="$review_dir/$stem-final-frame-probe.json"

  "$client" download "$series_id" "$tail_artifact_id" "$tail_file" >/dev/null
  "$client" download "$series_id" "$frame_artifact_id" "$frame_file" >/dev/null
  probe_media "$tail_file" "$tail_probe"
  probe_media "$frame_file" "$frame_probe"
  decode_video_audio "$tail_file"
  ffmpeg -hide_banner -nostdin -v error -xerror \
    -i "$frame_file" -map 0:v:0 -frames:v 1 -f null -

  boundary_duration="$(jq -er '
    [.streams[] | select(.codec_type == "video") | .duration]
    | if length == 1 then .[0] else error("tail needs exactly one timed video stream") end
  ' "$tail_probe")"
  boundary_fps="$(awk -v duration="$boundary_duration" 'BEGIN {
    if (duration <= 0) exit 1
    printf "%.12f", 6 / duration
  }')"
  ffmpeg -hide_banner -nostdin -v error -xerror -n \
    -i "$tail_file" \
    -vf "fps=${boundary_fps},scale=336:-2:flags=lanczos,tile=6x1:padding=4:margin=4" \
    -frames:v 1 "$boundary_strip"

  evidence_names+=(
    "${stem}-continuity-tail.mp4"
    "${stem}-final-frame.png"
    "${stem}-continuity-tail-probe.json"
    "${stem}-final-frame-probe.json"
    "${stem}-outgoing-boundary-strip.png"
  )
else
  ending_seconds="$(awk -v duration="$shot_duration" 'BEGIN {
    if (duration <= 0) exit 1
    printf "%.12f", (duration < 3 ? duration : 3)
  }')"
  ending_fps="$(awk -v duration="$ending_seconds" 'BEGIN {
    if (duration <= 0) exit 1
    printf "%.12f", 6 / duration
  }')"
  ffmpeg -hide_banner -nostdin -v error -xerror -n \
    -sseof "-${ending_seconds}" -i "$shot_file" \
    -vf "fps=${ending_fps},scale=336:-2:flags=lanczos,tile=6x1:padding=4:margin=4" \
    -frames:v 1 "$boundary_strip"
  evidence_names+=("${stem}-outgoing-boundary-strip.png")
fi

(
  cd -- "$review_dir"
  sha256sum -- "${evidence_names[@]}"
) > "$review_dir/SHA256SUMS"

echo "PASS: accepted shot review bundle created without overwriting files."
echo "Series: $series_id ($series_status)"
echo "Shot: $shot_index; accepted attempt: $accepted_attempt"
echo "Review directory: $review_dir"

whisper_bin="/home/lachlan/miniconda3/envs/whisper/bin/whisper"
whisper_dir="$review_dir/whisper-large-v2"
echo
echo "Optional Whisper large-v2 QA is never run automatically."
echo "Run it manually only while the series remains paused/completed and GPU 0 is idle:"
printf 'nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu --format=csv,noheader\n'
printf 'mkdir -m 700 %q && CUDA_VISIBLE_DEVICES=0 %q %q --model large-v2 --language Chinese --task transcribe --output_dir %q --output_format json --verbose False\n' \
  "$whisper_dir" "$whisper_bin" "$shot_file" "$whisper_dir"
