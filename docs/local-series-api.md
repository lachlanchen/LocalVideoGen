# LocalVideoGen Series API and cross-project client

This contract lets another local project, Python program, shell script, or Codex session create and supervise a quality-first H3 video series without controlling the browser. It uses H3 Studio’s existing durable Series API at `http://127.0.0.1:8190`; it does not bypass validation, start services, expose local paths, delete attempts, or allow parallel H3 renders.

The supported stdlib client is [`scripts/localvideogen_series.py`](../scripts/localvideogen_series.py). It has no package dependency beyond Python itself and may also be imported as `scripts.localvideogen_series`.

## Safety and trust boundary

- H3 Studio and ComfyUI remain bound to loopback. The server checks both the TCP peer and `Host`, rejects cross-site writes, and requires a matching `Origin` when one is supplied.
- The client accepts only `http://127.0.0.1`, `http://localhost`, or `http://[::1]`, with an optional port. It rejects credentials, URL paths, fragments, remote hosts, HTTPS indirection, and arbitrary artifact URLs.
- There is intentionally no bearer password on this single-user loopback API. An upload token is an expiring opaque handle, not authentication. Do not publish tokens, browser profiles, private runtime state, or specs containing private source locations.
- Reference files are streamed rather than loaded wholly into RAM. The client opens a regular file without following a final symlink, checks its device/inode/size/mtime again after streaming, and refuses a source that changed mid-upload.
- A visual video reference must be pre-cleaned and inspected before upload. Run [`preflight_reference_subtitles.py`](../scripts/preflight_reference_subtitles.py) on the exact 2–15 second segment, keep its SHA-256 report with the private production manifest, and proceed only on exit status `0`. Prompt-only “no subtitles” cannot remove caption pixels already present in a reference. Use audio-only input when only voice or music continuity is required.
- Uploads are normalized and bounded by media type. Every visual video reference requires trusted dimensions at every duration and is fitted to H3-native 32-pixel axes within 1024 px and 589,824 pixels (576×1024 for a 9:16 source). The server records those effective dimensions and SHA-256 provenance, permanently resolves opaque handles when the series is created, and never returns its input path or token in public Series responses.
- Every video-reference R2V request fails before GPU submission when its base `aligned_frames × (output_pixels + sum(video_reference_pixels))` exceeds 510,000,000. For `ref_image_size: "match"`, the admission proxy then adds `max(0, matched_still_count − 1) × output_pixels`: one output canvas once for each still beyond the calibrated first, including a continuity final frame. From 243 aligned frames (about 9.5 requested seconds), a visual video reference also requires `quality_int8_offload` and match mode. The proxy sums every visual video, including a series continuity tail, so shorter requests cannot bypass it by adding videos. The measured 510M/24 GiB guarantee applies to this match-mode route; an explicit shorter BF16/max mix remains available but is outside the calibrated guarantee.
- Artifact downloads are possible only after the artifact ID appears in that series’ public durable allowlist. The server resolves it beneath approved output roots; the client rebuilds the endpoint from canonical series/artifact UUIDs, checks the received byte count and SHA-256 against public durable metadata, `fsync`s a sibling temporary, and installs it atomically. No-overwrite mode uses an atomic hard-link claim, so another process cannot win the check/install gap and be clobbered.
- `run`, `start`, and retry operations use the same shared submission lock and effective profile-aware GPU readiness gate as the web UI. The client never starts or stops ComfyUI or H3 Studio. Start each verified service once with the project lifecycle scripts.

This API is for callers on the same workstation. Do not make port 8190 publicly reachable or put an unauthenticated reverse proxy in front of it.

## Capability discovery before uploads

Call `GET /api/config` before uploading large references. The response now includes the stable integer `series_api_version`. Version `1` defines the durable Series payload and lifecycle documented here; a caller that supports a different major version should stop before uploading. New optional capability fields may be added without changing that major version, so clients should ignore unknown keys.

The existing `profiles`, `series.templates`, limits, and defaults remain present. `series.capabilities.world_travel` adds a machine-readable preflight contract:

```json
{
  "series_api_version": 1,
  "series": {
    "default_settings": {
      "profile": "quality_int8_offload",
      "width": 1248,
      "height": 704,
      "ref_image_size": "match"
    },
    "shot_reference_policy": {
      "field": "omit_shared_image_labels",
      "logical_picture_tags_remapped": true,
      "first_shot_must_keep_all": true,
      "recommended_omissions_after_first": [
        "Words card",
        "LightMind glasses",
        "Patchwork notebook"
      ]
    },
    "capabilities": {
      "world_travel": {
        "template": "world_travel",
        "render_mode": "r2v",
        "maximum_quality_profile": "quality_bf16_dual",
        "long_reference_safe_profile": "quality_int8_offload",
        "picture_slots": {
          "shared": [
            {"slot": 1, "label": "Words card"},
            {"slot": 2, "label": "Zhuangzi Robot"},
            {"slot": 3, "label": "LightMind glasses"},
            {"slot": 4, "label": "Patchwork notebook"},
            {"slot": 5, "label": "Rara Xia"},
            {"slot": 6, "label": "Aya Chan"},
            {"slot": 7, "label": "Sasa Kun"}
          ],
          "scene": {"slot": 8, "kind": "image", "scope": "shot", "required": true},
          "continuity_final_frame": {
            "slot": 9,
            "kind": "image",
            "scope": "successor_shot",
            "when_continuity_enabled": true,
            "sha256_required": true
          }
        },
        "continuity_tail": {
          "kind": "video",
          "placement": "after_shared_videos",
          "maximum_slot": 3,
          "sha256_required": true
        },
        "continuity_recovery_requires": [
          "video_path",
          "video_sha256",
          "image_path",
          "image_sha256"
        ]
      }
    }
  }
}
```

`continuity_recovery_requires` describes server-managed durable handoff state; it does not authorize client-supplied filesystem paths in create or update payloads.

Before uploading, a World Travel caller should verify all of the following:

1. `series_api_version` is `1`.
2. `world_travel` is in `series.templates` and its capability uses R2V.
3. The shared slots and labels are exactly P1–P7, the required per-shot scene is P8, and the continuity final frame is P9.
4. `maximum_quality_profile` identifies the explicit BF16 maximum-fidelity route, while `long_reference_safe_profile` identifies `quality_int8_offload`: INT8, one-GPU/offload, non-Turbo, and 25 R2V steps. The separate `requires_two_gpus` field reports whether the current effective route actually needs two visible devices.

This discovery request does not upload media or start the engine.

The stdlib client performs this preflight automatically before `create`, `update`, or `run` can upload anything. It requires API version 1, the selected profile to exist, and—on World Travel—the exact R2V P1–P9 contract above plus both advertised 25-step quality profiles. Omitted settings choose `quality_int8_offload`; an explicit `quality_bf16_dual` remains available for a short visual-video run, or for an image-only maximum-fidelity Series with continuity turned off. Other World Travel profiles are rejected. The client also computes every shot's effective compact picture list, verifies that Robot plus all three travelers remain, and rejects an authored logical picture tag that the shot omits. A direct `upload`, any source-backed `create`/`update`/`run`, and `start` also require a successful deep health result (`connected: true`, `ready: true`, `model_status: "verified"`). Starting an existing series first fetches and preflights that same durable series ID before the costly start request is sent.

Before upload, the stdlib client can count an explicit soundtrack and a persisted video token's advertised `has_audio` metadata. It does not claim to infer an arbitrary local source video's embedded streams by filename alone. After normalization, the server uses trusted probe metadata for the authoritative audio-cardinality check and still rejects an unsafe mix before any GPU render. To make a source-backed long spec fail earlier and unambiguously, demux the intended track as standalone audio and use a silent visual spine.

## Runtime preparation

From LocalVideoGen:

```bash
./scripts/status.sh
./scripts/start_comfyui.sh
./scripts/start_webapp.sh
./scripts/localvideogen_series.py health
```

`health` must report a verified aligned model bundle, a connected owned ComfyUI runtime, and `ready: true` before `start` or `run`. Uploads also require that verified runtime because normalized assets are placed in its private input namespace. Reading a previously saved series and downloading retained artifacts can continue when the render engine is stopped.

Ordinary API requests default to a 120-second socket timeout. Media upload uses a separate 600-second timeout (configurable with global `--upload-timeout`, minimum 300 seconds), so a large normalized video is not governed by the short control-plane timeout.

### Discoverable upload contract

`GET /api/config` publishes these exact values under `uploads`; clients should discover them instead of hard-coding a larger allowance:

| Kind | Accepted source extensions and decoded form | Source limit | Dimensions / rate / duration | Trusted normalized form |
| --- | --- | --- | --- | --- |
| image | `.bmp`, `.jpeg`, `.jpg`, `.png`, `.webp`; decoded PNG/JPEG/WebP/BMP, one frame | 30 MiB (31,457,280 bytes) | at most 8192 px on either edge and 40,000,000 pixels | PNG, `image/png` |
| video | `.avi`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.webm`; decodable media in that container | 600 MiB (629,145,600 bytes) | 2–15 s; at most 8192 px on either edge and 40,000,000 pixels; source 1–240 fps; at most 32 streams; source audio at most 384 kHz and 32 channels | MP4, H.264, yuv420p, 24 fps, at most 1024 px on either edge and 589,824 pixels; optional AAC stereo at 32 kHz |
| audio | `.aac`, `.flac`, `.m4a`, `.mp3`, `.ogg`, `.opus`, `.wav`; decodable audio | 100 MiB (104,857,600 bytes) | 0.10–15 s; source at most 384 kHz, 32 channels, and 32 streams | WAV, PCM s16le, stereo, 32 kHz |

Every normalized file must also remain below the 99 MiB (103,809,024-byte) ComfyUI file ceiling. The multipart field is exactly `file`, and `kind` is exactly `image`, `video`, or `audio`. Extension, MIME family, actual decoding, stream structure, and all limits are validated; renaming unsupported bytes does not make them valid.

## Two JSON layers

The helper accepts an ergonomic **client spec**. A reference can contain:

- `source` or `path`: a local file, resolved relative to the spec file; or
- `token`: an already uploaded opaque handle.

Use exactly one. A video may also use `soundtrack_source`/`soundtrack_path` or an existing `soundtrack` audio token. Before `POST /api/series`, the client uploads every local source, calls `/api/uploads/validate`, removes source locations, and sends only tokens and labels.

Upload handles expire after 24 hours and are held in the running webapp process, so a webapp restart also invalidates handles that have not yet been resolved by `POST /api/series`. Once creation succeeds, the durable series no longer depends on those client-visible handles.

The raw **API payload** never accepts a filesystem path:

```json
{
  "title": "One connected journey",
  "brief": "Series-wide direction and exclusions.",
  "template": "movie",
  "settings": {
    "profile": "quality_int8_offload",
    "width": 1248,
    "height": 704,
    "ref_image_size": "match",
    "continuity_seconds": 3,
    "advance": true
  },
  "references": {
    "images": [{"token": "opaque-image-handle", "label": "Lead character"}],
    "videos": [],
    "audio": []
  },
  "shots": [
    {"title": "Arrival", "prompt": "...", "duration": 10, "seed": 101},
    {"title": "Discovery", "prompt": "...", "duration": 10, "seed": 102}
  ]
}
```

If a client spec omits setting values, the helper defaults to **Long reference · 24 GiB safe**: `quality_int8_offload`, 1248×704, `ref_image_size: "match"`, automatic advance, and template-aware continuity. World Travel uses two seconds to reduce repeated shot handles; LALACHAN Series and My Movie use three. It never replaces an explicit 2-, 3-, or 4-second choice or an explicitly selected profile. World Travel accepts the advertised safe profile and the explicit BF16 maximum-quality profile.

This Series default is distinct from Single Clip duration behavior: entering **Use references** in the browser selects the 14-second safe preset, while a direct `POST /api/renders` R2V request that omits `duration` retains the five-second compatibility default. For direct R2V, the presence of a visual video makes omitted profile/canvas values resolve to `quality_int8_offload` and 1248×704; image/audio-only R2V retains the earlier `quality_bf16_dual` and 1344×768 defaults.

BF16, non-Turbo sampling, and 25 R2V steps still define maximum *generation fidelity*, but BF16/max is not the omitted default for long visual-video conditioning on a 24 GiB GPU. The proven 14-second portrait mapping is 704×1248 output, one normalized visual reference no larger than 576×1024, 345 aligned frames, `quality_int8_offload`, and `ref_image_size: "match"`: 506,603,520 combined frame-pixels. The old 736×1312 output plus 736×1312 reference is 666,286,080 and is rejected. One safe spine nearly fills the 510M budget; a second visual or continuity video may require a shorter duration/smaller canvas or audio-only guidance.

Server limits remain authoritative:

| Field | Contract |
| --- | --- |
| `template` | `lalachan`, `world_travel`, or `movie` |
| `shots` | 2–12 ordered shots; total requested duration at most 180 seconds |
| `duration` | Validated and aligned by the same 24 fps render-spec compiler as Single Clip |
| `profile` | A profile returned by `GET /api/config`; omitted series use `quality_int8_offload`, while explicit `quality_bf16_dual` maximum fidelity is for a short visual-video shot or image-only Series with continuity off |
| shared pictures | At most 8 generally; World Travel uses exactly the canonical seven |
| shared videos | At most 2, reserving H3’s third video slot for the preceding continuity tail |
| shared audio | At most 3 |
| long visual-video audio | From 243 aligned frames, at most one effective H3 audio conditioning: each standalone audio and each video soundtrack actually wired counts. Shared-video embedded/override audio or standalone audio takes precedence; only when none exists does the continuity tail's embedded audio count as one. |
| continuity | 0, 2, 3, or 4 seconds; World Travel defaults to 2, other templates to 3, and explicit values are preserved |
| per-shot shared pictures | optional `omit_shared_image_labels`; Shot 1 keeps all shared pictures, and World Travel always keeps Robot plus all three cast pictures; the web UI and stdlib helper default the three opening-only labels off after Shot 1 |
| prompt | Authored shot prompt at most 10,000 characters; composed prompt at most 12,000 |

## World Travel reference contract

`template: "world_travel"` is designed for a coherent LALACHAN journey rather than six unrelated attraction cards. Its shared pictures must be exactly these seven labels and this order:

| H3 picture | Stable purpose |
| --- | --- |
| `<Picture 1>` | Words card |
| `<Picture 2>` | Zhuangzi Robot |
| `<Picture 3>` | LightMind glasses |
| `<Picture 4>` | Patchwork notebook |
| `<Picture 5>` | Rara Xia |
| `<Picture 6>` | Aya Chan |
| `<Picture 7>` | Sasa Kun |

Every shot additionally requires one image:

```json
{
  "title": "Rome",
  "prompt": "Continue the journey through <Picture 8> ...",
  "duration": 10,
  "seed": 731001,
  "scene_reference": {
    "token": "opaque-scene-image-handle",
    "label": "Rome, Colosseum and Roman Forum"
  }
}
```

For that shot, the location image becomes `<Picture 8>`. With continuity enabled, the exact final frame from the previous accepted shot becomes `<Picture 9>`, and its accurate 2–4 second tail occupies the reserved video slot. From 243 aligned frames, a shared video's embedded/override soundtrack or standalone audio wins and the continuity tail contributes embedded audio only when none exists; this enforces the one-audio long-job cap. Short shots retain the legacy tail-audio-plus-guide ordering and tags. Thus the checked ten-second Italy example's Iran audio guide makes every successor tail visual-only. The scene image controls only that shot’s architecture, terrain, light, atmosphere, and geography. It is not carried into the next destination.

Later shots can keep identity references while removing opening props that otherwise tend to reappear. The web UI checks these three omissions by default, shows the resulting physical H3 map inside each shot card, and lets you uncheck a label when that shot deliberately needs the prop. The stdlib helper applies the same default when a later World Travel shot omits the field. Add or edit the field explicitly in a client spec:

```json
"omit_shared_image_labels": [
  "Words card",
  "LightMind glasses",
  "Patchwork notebook"
]
```

Shot 1 must use all seven canonical pictures. World Travel never permits omitting `Zhuangzi Robot`, `Rara Xia`, `Aya Chan`, or `Sasa Kun`. The policy removes the selected files from that shot's H3 graph and reference provenance; it is not merely a negative-prompt hint. The prompt composer also removes prose clauses naming an omitted opening prop, including negative mentions that can still pull the model toward that object. Authored picture tags retain their canonical logical meaning: for the example above, authored `<Picture 8>` is automatically remapped to physical `<Picture 5>` for the scene plate, and logical `<Picture 9>` becomes physical `<Picture 6>` for continuity. An authored tag for an omitted picture is rejected before source upload or GPU submission. An explicit empty list keeps all seven pictures for a later shot that intentionally uses the card, glasses, or notebook.

For raw HTTP compatibility, the server still interprets a missing field as an empty omission list. Callers that bypass the supported web UI or stdlib helper should therefore send the three-label policy explicitly. Existing durable series are not rewritten; their saved per-shot lists remain authoritative when `start`, `resume`, or `retry` preflights them.

A ready, paused, failed, cancelled, or completed series can set the policy for a future attempt without replacing the series or altering any saved attempt:

```bash
./scripts/localvideogen_series.py set-reference-policy SERIES_ID 4 \
  --omit-shared-image-label 'Words card' \
  --omit-shared-image-label 'LightMind glasses' \
  --omit-shared-image-label 'Patchwork notebook'
```

The shot index is zero-based. For a pending shot, set the policy while the series is paused and then `resume`. For an already accepted shot, setting the policy changes only a future attempt; explicitly call `retry` afterward if regeneration is desired. Omitting all flags clears the policy. Existing attempt records, output artifacts, hashes, and their historical reference maps remain unchanged.

On restart or retry, a successor shot advertises and submits P9 and the continuity video only when the durable handoff contains both media paths and valid recorded SHA-256 values for both files. Legacy tails without safe normalized metadata are verified against their owned path and recorded hash, then mechanically normalized to the current 32-pixel contract before any GPU job; an identity/runtime check occurs immediately before that recovery upload. If the artifact or hash cannot be trusted, the series fails closed before a new attempt is claimed. The accepted prior render and all attempts remain preserved.

An older `ready` series rejected under the new envelope can be updated with a safe spec (`quality_int8_offload`, `match`, and an admitted canvas). A paused, failed, cancelled, or completed series is never silently changed: keep its ID and artifacts, copy the original client spec, select the safe settings and only the remaining shots, then run `create` to make a separate clone. This migration may re-upload original source references but never requires re-rendering or deleting the preserved attempts merely to make the clone.

Earlier episodes—such as an Iran episode—may be supplied as a shared video or soundtrack for character appearance, motion, and voice timbre. If only voice continuity is needed, demux and upload its audio rather than its video; removing the old episode’s frames is the strongest protection against visual or geographic leakage. World Travel’s server-composed guidance explicitly forbids copying that reference’s country, plot, story direction, action, blocking, landmark, or composition. The current destination’s authored story and per-shot scene reference stay authoritative.

The full editable Italy example is [`examples/series-api/maximum-quality-world-travel.json`](../examples/series-api/maximum-quality-world-travel.json). It follows the production route—Rome Colosseum, Roman Forum, Florence Duomo, Uffizi/Ponte Vecchio, Venice San Marco, and a Venice bacaro—and uses an audio-only Iran voice guide capped at 15 seconds. Its referenced media is intentionally not committed; replace those portable relative `source` fields or create the described files beside the spec.

## Client commands

All successful commands write JSON to stdout. Long-running progress goes to stderr, so another program can safely parse stdout.

Global transport options precede the subcommand: `--base-url` defaults to the loopback studio, `--http-timeout` defaults to 120 seconds, and `--upload-timeout` defaults to 600 seconds and cannot be set below 300.

```text
localvideogen_series.py health
localvideogen_series.py config
localvideogen_series.py recover RECEIPT.json
localvideogen_series.py upload {image,video,audio} FILE
localvideogen_series.py validate kind:token [kind:token ...]
localvideogen_series.py create SPEC.json
localvideogen_series.py update SERIES_ID SPEC.json
localvideogen_series.py list [--limit 40]
localvideogen_series.py status SERIES_ID
localvideogen_series.py start SERIES_ID
localvideogen_series.py wait SERIES_ID [--until terminal-or-paused|terminal] [--poll-interval 5] [--timeout 86400]
localvideogen_series.py pause SERIES_ID
localvideogen_series.py resume SERIES_ID
localvideogen_series.py cancel-active SERIES_ID
localvideogen_series.py set-reference-policy SERIES_ID ZERO_BASED_SHOT [--omit-shared-image-label LABEL ...]
localvideogen_series.py retry SERIES_ID ZERO_BASED_SHOT [--regenerate-following]
localvideogen_series.py retry-finalization SERIES_ID
localvideogen_series.py artifacts SERIES_ID
localvideogen_series.py download SERIES_ID {final|manifest|ARTIFACT_UUID} OUTPUT
localvideogen_series.py run SPEC.json --output-dir OUTPUT_DIR [--receipt RECEIPT.json] [--until terminal-or-paused|terminal] [--overwrite-receipt] [--overwrite-downloads]
```

`create` uploads and validates references and saves a durable `ready` storyboard, but intentionally does not render. `run` is the explicit end-to-end operation. Immediately after creation—and before `start`—it atomically writes a `localvideogen.series-receipt.v1` JSON record containing the durable ID, origin, title, revision, and creation time. `--receipt` selects its path; otherwise it is `OUTPUT_DIR/{series_id}-receipt.json`. The same ID and receipt path are flushed to stderr. They therefore survive a rejected start, later API failure, timeout, or Ctrl-C even though final stdout was never reached.

Both `run` and `wait` default to `--until terminal-or-paused`. For the highest-assurance workflow, set `advance: false`: each costly shot is preserved, `run`/`wait` returns at `paused`, and a human can inspect the attempt, retry its zero-based shot (with `--regenerate-following` when continuity successors exist), or explicitly `resume`. For unattended sequential production, set `advance: true`; all validation and the one-engine gate remain active, but there is no human checkpoint between shots. Use `--until terminal` only when another operator will resume pauses while this waiter remains attached. Timeout errors always include the durable ID and last observed status, so a paused review cannot look like silent data loss.

Polling retries only safe `GET /api/series/{id}` transport failures with bounded exponential backoff (at most 30 seconds) until the caller’s overall deadline. A valid HTTP error, malformed response, or any state-changing request fails immediately. The client never automatically repeats upload, create, update, start, pause, resume, cancel, or retry writes because their server-side outcome may already be durable.

Downloads require valid `metadata.bytes` and `metadata.sha256` in the public artifact record and verify both before installation. A mismatch or incomplete transfer discards only the temporary file. Downloads refuse to overwrite by default, including a destination created during the transfer. Standalone `download --overwrite` replaces only its named destination. On `run`, `--overwrite-receipt` and `--overwrite-downloads` are separate: authorizing one never authorizes the other. The former legacy-style ambiguous `run --overwrite` spelling is intentionally rejected.

### Receipt recovery (read-only)

`recover RECEIPT.json` safely reads only a regular, non-symlink receipt of at most 64 KiB, requires the exact `localvideogen.series-receipt.v1` schema, a canonical UUID, and the exact loopback origin used by the client, then performs one `GET` for that same durable ID. It never starts, resumes, retries, uploads, or downloads. Its JSON includes current server state and a recommended next action:

| Current state | Recommendation | Mutation performed by `recover` |
| --- | --- | --- |
| `ready` | Review, then `start ID` if authorized | none |
| `queued`, `waiting`, `running`, `pausing`, `cancelling`, `stitching` | `wait ID --until terminal-or-paused`; never submit a duplicate | none |
| `paused` | Review the preserved attempt, then `resume ID` or retry | none |
| `failed`, `cancelled` | Inspect `status ID` and retained attempts before choosing a retry | none |
| `completed` | Inspect `artifacts ID`, then download verified final/manifest | none |

If `run` fails or is interrupted after create, use the receipt instead of creating a replacement:

```bash
./scripts/localvideogen_series.py recover \
  /absolute/path/to/italy-output/series-receipt.json
```

### Shell and Codex handoff

From any project on this workstation, use absolute paths so the other session does not need to change repositories:

```bash
LVG=/home/lachlan/ProjectsLFS/LocalVideoGen

"$LVG/scripts/localvideogen_series.py" health
"$LVG/scripts/localvideogen_series.py" create /absolute/path/to/italy-series.json \
  > /absolute/path/to/created-series.json
```

Read the `id` from the saved result, review it, then make the costly action explicit:

```bash
SERIES_ID=$(python -c 'import json; print(json.load(open("/absolute/path/to/created-series.json"))["id"])')
"$LVG/scripts/localvideogen_series.py" start "$SERIES_ID"
"$LVG/scripts/localvideogen_series.py" wait "$SERIES_ID" --timeout 86400 \
  > /absolute/path/to/final-series-state.json
"$LVG/scripts/localvideogen_series.py" download "$SERIES_ID" final \
  /absolute/path/to/italy-final.mp4
"$LVG/scripts/localvideogen_series.py" download "$SERIES_ID" manifest \
  /absolute/path/to/italy-manifest.json
```

A Codex handoff should give the next session only the required local paths, lifecycle authorization, and safety rules. This template is copy-paste ready:

```text
Continue this LocalVideoGen Series job.
LocalVideoGen root: /home/lachlan/ProjectsLFS/LocalVideoGen
Spec: /absolute/path/to/italy-series.json
Receipt: /absolute/path/to/italy-output/series-receipt.json
Output directory: /absolute/path/to/italy-output
Authorization: [inspect only | may start | may resume/retry after review]

First run exactly:
/home/lachlan/ProjectsLFS/LocalVideoGen/scripts/localvideogen_series.py recover /absolute/path/to/italy-output/series-receipt.json

Use the durable ID returned by recover; never create a replacement when a receipt exists.
Keep one LocalVideoGen engine/render at a time. Reuse the owned running services; do not start a second engine and do not stop another project's process.
Do not delete, replace, or supersede generated attempts/artifacts unless I explicitly authorize the exact action.
For advance:false, review each preserved shot before resume; retry the zero-based shot and regenerate following shots when continuity requires it.
Do not use overwrite flags unless I explicitly authorize receipt and/or downloads separately.
```

For a one-command handoff, always name the recovery receipt explicitly:

```bash
"$LVG/scripts/localvideogen_series.py" run /absolute/path/to/italy-series.json \
  --output-dir /absolute/path/to/italy-output \
  --receipt /absolute/path/to/italy-output/series-receipt.json \
  --until terminal-or-paused
```

If the command stops early, call the exact `recover` command above; do not parse an arbitrary JSON file, guess an ID, or create a replacement series.

### Python

```python
import json
import sys
from pathlib import Path

LOCAL_VIDEO_GEN = Path("/home/lachlan/ProjectsLFS/LocalVideoGen")
sys.path.insert(0, str(LOCAL_VIDEO_GEN))

from scripts.localvideogen_series import (
    LocalVideoGenClient,
    SERIES_RECEIPT_SCHEMA,
    WAIT_MODES,
    atomic_write_json,
    load_series_spec,
)

client = LocalVideoGenClient(
    "http://127.0.0.1:8190", timeout=120, upload_timeout=600
)
spec, base_dir = load_series_spec("/absolute/path/to/italy-series.json")

# Safe preparation: upload references and save durable state; no render yet.
created = client.create_series_from_spec(spec, base_dir=base_dir)
series_id = created["id"]
atomic_write_json(
    "/absolute/path/to/italy-series-receipt.json",
    {
        "schema": SERIES_RECEIPT_SCHEMA,
        "series_id": series_id,
        "base_url": client.base_url,
    },
)
print(json.dumps(created, ensure_ascii=False, indent=2))

# Costly action is deliberately separate.
client.start_series(series_id)
finished = client.wait_for_series(
    series_id,
    interval=5,
    timeout=86400,
    stop_statuses=WAIT_MODES["terminal-or-paused"],
)
if finished["status"] == "paused":
    raise SystemExit("Review the preserved shot, then resume this same series ID")
if finished["status"] != "completed":
    raise RuntimeError(f"series retained with status {finished['status']}")

final_receipt = client.download_artifact(
    series_id, "final", "/absolute/path/to/italy-final.mp4"
)
manifest_receipt = client.download_artifact(
    series_id, "manifest", "/absolute/path/to/italy-manifest.json"
)
print(final_receipt["sha256"], manifest_receipt["sha256"])
```

Useful methods also include `health`, `config`, `preflight_series_spec`, `upload`, `validate_uploads`, `update_series_from_spec`, `recover_from_receipt`, `list_series`, `get_series`, `pause_series`, `resume_series`, `cancel_active`, `set_shot_reference_policy`, `retry_shot`, `retry_finalization`, and `list_artifacts`.

## Raw HTTP and curl contract

Set the exact loopback origin:

```bash
BASE=http://127.0.0.1:8190
curl --fail-with-body "$BASE/api/health?deep=1"
```

Upload one reference. The multipart field must be named `file`, and `kind` must match the media:

```bash
curl --fail-with-body \
  -H "Origin: $BASE" \
  -F 'file=@/absolute/path/to/rara-xia.png;type=image/png' \
  "$BASE/api/uploads?kind=image" \
  > rara-upload.json
```

The 201 response contains `token`, `kind`, sanitized `name`, normalized `size`, source size, and safe media metadata. Validate one or more live handles before creating:

```bash
TOKEN=$(python -c 'import json; print(json.load(open("rara-upload.json"))["token"])')
curl --fail-with-body \
  -H "Origin: $BASE" -H 'Content-Type: application/json' \
  --data "{\"uploads\":[{\"token\":\"$TOKEN\",\"kind\":\"image\"}]}" \
  "$BASE/api/uploads/validate"
```

Build a token-only raw payload and create it:

```bash
curl --fail-with-body \
  -H "Origin: $BASE" -H 'Content-Type: application/json' \
  --data-binary @token-only-series-payload.json \
  "$BASE/api/series" \
  > created-series.json
```

The Series endpoints are:

| Method | Endpoint | Success | Meaning |
| --- | --- | --- | --- |
| `POST` | `/api/uploads?kind=image|video|audio` | 201 | Normalize one multipart `file` and issue an opaque handle |
| `POST` | `/api/uploads/validate` | 200 | Return the subset of still-valid token/kind handles |
| `POST` | `/api/series` | 201 | Resolve tokens and save a durable `ready` series |
| `GET` | `/api/series?limit=40` | 200 | Newest-first compact library rows |
| `GET` | `/api/series/{id}` | 200 | Full sanitized state, progress, attempts, and artifacts |
| `PUT` | `/api/series/{id}` | 200 | Replace only a `ready` series using fresh/live handles |
| `POST` | `/api/series/{id}/start` | 202 | Pass readiness gates and queue a `ready` series |
| `POST` | `/api/series/{id}/pause` | 202 | Preserve the active shot, then pause before the next |
| `POST` | `/api/series/{id}/resume` | 202 | Requeue a paused series |
| `POST` | `/api/series/{id}/cancel-active` | 202 | Cancel only this series’ owned active job |
| `PUT` | `/api/series/{id}/shots/{index}/reference-policy` | 200 | Set one stopped shot's `omit_shared_image_labels` without changing attempt history |
| `POST` | `/api/series/{id}/shots/{index}/retry` | 202 | Retry a zero-based shot; JSON may set `regenerate_following` |
| `POST` | `/api/series/{id}/retry-finalization` | 202 | Revalidate and restitch accepted shots without H3 generation |
| `GET` | `/api/series/{id}/artifacts/{artifact_id}` | 200/206 | Stream one durable allowlisted artifact; `?download=1` adds disposition |

State-changing curl requests should send `Origin: http://127.0.0.1:8190` and JSON requests should send `Content-Type: application/json`. IDs are lowercase canonical UUIDs. Retry indices are zero-based.

## Durable states and progress

The top-level `status` is one of:

```text
ready → queued ↔ waiting → running → stitching → completed
                         ↘ pausing → paused → queued
                         ↘ cancelling → cancelled
                         ↘ failed
```

`waiting` means another locally owned H3 job holds the shared gate; it does not mean another renderer will be launched. A detail response includes:

- `settings`, sanitized shared `references`, and ordered `shots`;
- each World Travel shot’s sanitized `scene_reference` (`kind`, `name`, `label`, never path/token) and `omit_shared_image_labels` policy;
- `active_shot` and `progress.completed_shots`, `total_shots`, `overall_percent`, and live render progress when available;
- all preserved attempts, their statuses, reference maps, validation outputs, and superseded markers;
- top-level active `final_artifact`, final/manifest artifact list, revision, timestamps, and a bounded error string.

Poll at roughly five seconds; sub-second loops add no quality. The client resets transport backoff after each successful observation and keeps the overall timeout authoritative. A pause is “after this shot”: an expensive valid generation is retained before the runner stops. Retrying with `regenerate_following: true` preserves old attempts but supersedes the chosen accepted shot and dependent successors. Retrying finalization never regenerates a shot.

## Error behavior

Errors normally use JSON `{"error":"..."}` with these status families:

- `400`: malformed JSON, bad UUID, invalid spec, missing/expired token, reference order/tag error, prompt/duration/profile limit, or invalid lifecycle action;
- `403`/`421`: cross-site, foreign peer, or non-loopback `Host` rejection;
- `404`: unknown durable series or artifact not in its allowlist;
- `409`: verified engine/model/node/GPU/FFmpeg prerequisite failed, media conflict, or resource gate unavailable;
- `503`: private durable registry unavailable.

Treat a client timeout as an observation failure, not a render failure. The timeout message reports the durable ID, last observed state, and latest transport error when applicable. Query that ID again before retrying anything. Never create a replacement merely because polling was interrupted. The generated attempts and accepted artifacts are designed to survive browser, client, and webapp restarts.
