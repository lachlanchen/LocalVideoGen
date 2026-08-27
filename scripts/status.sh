#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"

"$script_dir/check_resources.sh" || true

echo
echo "H3 model files"
has_in_progress=false
while read -r _ path; do
  [[ -z "${path:-}" ]] && continue
  if [[ -f "$project_root/$path.aria2" ]]; then
    has_in_progress=true
    break
  fi
done < "$project_root/config/model-manifest.sha256"

receipt_valid=false
receipt="$project_root/runtime/models.verified"
if [[ "$has_in_progress" == false && -f "$receipt" ]]; then
  recorded_fingerprint=$(<"$receipt")
  current_fingerprint=$("$script_dir/model_fingerprint.sh" 2>/dev/null || true)
  if [[ "$recorded_fingerprint" =~ ^[0-9a-f]{64}$ && "$current_fingerprint" == "$recorded_fingerprint" ]]; then
    receipt_valid=true
  fi
fi

while read -r _ path; do
  [[ -z "${path:-}" ]] && continue
  if [[ -f "$project_root/$path.aria2" ]]; then
    size="-"
    [[ -f "$project_root/$path" ]] && size=$(du -h "$project_root/$path" | awk '{print $1}')
    state="downloading"
  elif [[ ! -f "$project_root/$path" ]]; then
    size="-"
    state="missing"
  else
    size=$(du -h "$project_root/$path" | awk '{print $1}')
    if [[ "$receipt_valid" == true ]]; then
      state="verified"
    elif "$project_root/.venv/bin/python" "$script_dir/inventory_models.py" --path "$path" >/dev/null 2>&1; then
      state="structurally-valid"
    else
      state="present/unverified"
    fi
  fi
  printf '%-20s %8s  %s\n' "$state" "$size" "$path"
done < "$project_root/config/model-manifest.sha256"

echo
echo "Project ComfyUI runtime"
"$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" show || true

echo
echo "Project H3 Studio runtime"
"$project_root/.venv/bin/python" "$script_dir/runtime_identity.py" --service webapp show || true
