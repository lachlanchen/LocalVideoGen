#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"

cd "$project_root"
{
  sha256sum config/model-manifest.sha256
  while read -r _ path; do
    [[ -n "$path" ]] || continue
    stat --format='%n %s %Y' "$path"
  done < config/model-manifest.sha256
} | sha256sum | awk '{print $1}'
