# Maximum-quality Series API example

`maximum-quality-world-travel.json` is an editable client-side spec. It deliberately points to media files that are not committed. Create its `references/` and `references/scenes/` files, or replace every `source` with an absolute path to your own local reference.

The example mirrors the production route: Rome Colosseum → Roman Forum → Florence Duomo → Uffizi/Ponte Vecchio → Venice San Marco → a Venice bacaro at blue hour. It fixes `quality_bf16_dual` (BF16, dual GPU, non-Turbo, 25 R2V steps), 1024×768 4:3 composition, maximum reference fidelity, three-second continuity, six ten-second shots, seven canonical LALACHAN identity references, and one authoritative location image per shot. The profile is the maximum-fidelity choice; 1024×768 is a deliberate story composition, not a claim that it is the largest canvas. Use 1344×768 for a wider native landscape while retaining the same profile and 25-step fidelity.

Its per-shot `omit_shared_image_labels` values demonstrate the opening-prop guard: later shots drop the words card, LightMind glasses, and patchwork notebook unless that exact shot uses one. An explicit empty list is the keep-all override. Prompts continue to use logical P8/P9; preflight shows and validates the compact physical H3 slots before any upload or render.

Its only Iran reference is an audio-only dialogue guide of no more than 15 seconds. Excluding Iran’s video frames, ambience, and music is the strongest way to prevent that episode from steering Italy’s plot, architecture, geography, palette, or composition. Clip and AAC-encode the portable example’s expected file without overwriting an existing guide:

```bash
mkdir -p examples/series-api/references/scenes
ffmpeg -i /absolute/path/to/iran-episode.mp4 -map 0:a:0 -t 15 -vn \
  -c:a aac -b:a 128k examples/series-api/references/iran-voices-first-15s.m4a
```

Saving the storyboard uploads and validates its sources but does not render:

```bash
./scripts/localvideogen_series.py create \
  examples/series-api/maximum-quality-world-travel.json
```

The checked-in spec uses `"advance": false`, the highest-assurance workflow. The explicit command creates a durable series, immediately writes an atomic receipt containing its ID, starts one shot, then returns when that preserved shot pauses for review:

```bash
./scripts/localvideogen_series.py run \
  examples/series-api/maximum-quality-world-travel.json \
  --output-dir /absolute/path/to/episode-output \
  --receipt /absolute/path/to/episode-output/italy-series-receipt.json
```

The receipt survives a start error, polling outage, timeout, or Ctrl-C. Recover the exact ID, inspect the accepted attempt, then resume it only after approval:

```bash
./scripts/localvideogen_series.py recover \
  /absolute/path/to/episode-output/italy-series-receipt.json
./scripts/localvideogen_series.py status SERIES_ID
./scripts/localvideogen_series.py resume SERIES_ID
./scripts/localvideogen_series.py wait SERIES_ID --until terminal-or-paused
```

Repeat review/resume at each pause. If a shot is wrong, retry its zero-based index; add `--regenerate-following` when a changed accepted shot invalidates later continuity. For an unattended run, explicitly change only `settings.advance` to `true`; validation, sequential rendering, continuity, provenance, and the maximum-quality profile remain active, but there is no human gate between shots.

`run` never overwrites anything by default. `--overwrite-receipt` applies only to the selected receipt path, while `--overwrite-downloads` applies only to verified final/manifest destinations. They are intentionally independent.

See [`../../docs/local-series-api.md`](../../docs/local-series-api.md) before integrating it into another project or Codex session.
