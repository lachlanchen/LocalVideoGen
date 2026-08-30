# Subtitle-free reference guard

H3 can reproduce burned captions from a reference video even when the prompt says “no subtitles.” A negative prompt is therefore not a source-cleaning step. Any video used for visual conditioning must be cleaned **before** it is uploaded; use an audio-only reference when only voice or music continuity is needed.

## Required preflight for visual video references

Cut the exact 2–15 second segment that H3 will receive, remove or inpaint every burned subtitle in that segment, and then run:

```bash
./scripts/preflight_reference_subtitles.py \
  --declare-subtitle-free \
  --pretty \
  /absolute/path/to/exact-reference-segment.mp4 \
  > reference-subtitle-preflight.json
```

Only exit status `0` is a pass. The command is intentionally fail-closed:

- `0`: the operator declared this exact source subtitle-free, all required tools and OCR languages were available, the source stayed unchanged, every sample decoded, and no caption-like text was found in overlapping bands spanning the lower 45% of the frame.
- `1`: caption-like text was found. Pre-clean the source and inspect the new file; do not upload the flagged version.
- `2`: the inspection was inconclusive. This includes a missing declaration, tool or language failure, invalid media, a source longer than the upload window, decode/OCR failure, or a source that changed during inspection. Do not upload it.

The JSON report records the inspected source SHA-256, media dimensions and duration, scan settings, approximate finding times, and OCR rectangles. Keep it with the private production manifest. If the source hash changes, the pass no longer applies.

The guard is conservative but still sampling-based. It enlarges likely caption bands 3×, raises local contrast, and tries sparse-line plus single/multiline OCR separately for each installed language; the hardened path was verified against the actual captioned Madeira reference that exposed the simpler scanner’s false pass. Its pass means “no caption-like text was detected under this documented scan,” not mathematical proof that every pixel is text-free. Review the exact cleaned segment visually as well. Increase `--sample-fps` up to 12 or set `--bottom-fraction 1` when captions may flash briefly or appear outside the usual lower region. A bounded pixel/invocation budget rejects combinations that would consume excessive shared-workstation disk or CPU; shorten or downscale the exact reference segment if an exhaustive setting is inconclusive.

## Reusable source policy

For every future series:

1. Prefer still scene plates plus a clean continuity tail. They carry less unwanted story, text, and composition than a whole prior episode.
2. If a prior episode is needed only for voices, demux and upload its audio. Never upload its captioned frames for visual conditioning.
3. If a video must guide motion or scenery, cut the exact upload segment, pre-clean subtitles, run the guard, record its SHA-256 report, and only then upload it.
4. Keep “no subtitles, no captions, no titles, no text overlays” in the generation prompt as a secondary constraint. It never replaces source cleaning and preflight.
5. OCR the generated shots before assembly. A failed output check is a delivery issue, not permission to silently spend GPU time on a rerun.
