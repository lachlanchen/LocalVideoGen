#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"

cd "$project_root"
in_progress=""
if [[ -d ComfyUI/models ]]; then
  in_progress=$(find ComfyUI/models -type f -name '*.aria2' -print -quit)
fi
if [[ -n "$in_progress" ]]; then
  echo "Refusing verification while aria2 control file exists: $in_progress" >&2
  exit 1
fi

"$project_root/.venv/bin/python" "$script_dir/inventory_models.py"
sha256sum --check config/model-manifest.sha256

mkdir -p runtime
umask 077
receipt="runtime/models.verified"
temporary="runtime/.models.verified.$$"
"$script_dir/model_fingerprint.sh" > "$temporary"
mv -f -- "$temporary" "$receipt"
echo "Recorded model verification receipt: $receipt"
