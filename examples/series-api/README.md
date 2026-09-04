# Quality-first Series API example

`maximum-quality-world-travel.json` is an editable client-side spec. It deliberately points to media files that are not committed. Create its `references/` and `references/scenes/` files, or replace every `source` with an absolute path to your own local reference.

The example mirrors the production route: Rome Colosseum → Roman Forum → Florence Duomo → Uffizi/Ponte Vecchio → Venice San Marco → a Venice bacaro at blue hour. It explicitly fixes the **Long reference · 24 GiB safe** profile (`quality_int8_offload`, non-Turbo, 25 R2V steps), 1024×768 4:3 composition, matched reference fidelity, the two-second World Travel continuity default, six ten-second shots, seven canonical LALACHAN identity references, and one authoritative location image per shot. The legacy filename is retained so existing commands keep working. The safe profile is required from Shot 2 because its continuity tail is a visual video reference. For these 243-frame shots, the shorter handoff keeps motion context while reducing repeated endings; its embedded audio is used only when no other audio conditioning is present.

Its per-shot `omit_shared_image_labels` values demonstrate the opening-prop guard: later shots drop the words card, LightMind glasses, and patchwork notebook unless that exact shot uses one. An explicit empty list is the keep-all override. Prompts continue to use logical P8/P9; preflight shows and validates the compact physical H3 slots before any upload or render.

Its only Iran reference is an audio-only dialogue guide of no more than 15 seconds. It owns the audio-conditioning slot, so Shots 2–6 use their continuity tails for visuals only rather than also feeding the tails' embedded sound. Excluding Iran’s video frames, ambience, and music is the strongest way to prevent that episode from steering Italy’s plot, architecture, geography, palette, or composition. Clip and AAC-encode the portable example’s expected file without overwriting an existing guide:

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

Repeat review/resume at each pause. If a shot is wrong, retry its zero-based index; add `--regenerate-following` when a changed accepted shot invalidates later continuity. For an unattended run, explicitly change only `settings.advance` to `true`; validation, sequential rendering, continuity, provenance, and the safe 25-step profile remain active, but there is no human gate between shots.

`run` never overwrites anything by default. `--overwrite-receipt` applies only to the selected receipt path, while `--overwrite-downloads` applies only to verified final/manifest destinations. They are intentionally independent.

See [`../../docs/local-series-api.md`](../../docs/local-series-api.md) before integrating it into another project or Codex session.
