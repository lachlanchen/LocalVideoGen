# LocalVideoGen Series API and cross-project client

This contract lets another local project, Python program, shell script, or Codex session create and supervise a maximum-quality H3 video series without controlling the browser. It uses H3 Studio’s existing durable Series API at `http://127.0.0.1:8190`; it does not bypass validation, start services, expose local paths, delete attempts, or allow parallel H3 renders.

The supported stdlib client is [`scripts/localvideogen_series.py`](../scripts/localvideogen_series.py). It has no package dependency beyond Python itself and may also be imported as `scripts.localvideogen_series`.

## Safety and trust boundary

- H3 Studio and ComfyUI remain bound to loopback. The server checks both the TCP peer and `Host`, rejects cross-site writes, and requires a matching `Origin` when one is supplied.
- The client accepts only `http://127.0.0.1`, `http://localhost`, or `http://[::1]`, with an optional port. It rejects credentials, URL paths, fragments, remote hosts, HTTPS indirection, and arbitrary artifact URLs.
- There is intentionally no bearer password on this single-user loopback API. An upload token is an expiring opaque handle, not authentication. Do not publish tokens, browser profiles, private runtime state, or specs containing private source locations.
- Reference files are streamed rather than loaded wholly into RAM. The client opens a regular file without following a final symlink, checks its device/inode/size/mtime again after streaming, and refuses a source that changed mid-upload.
- Uploads are normalized and bounded by media type. The server records the normalized asset’s SHA-256 provenance, permanently resolves opaque handles when the series is created, and never returns its input path or token in public Series responses.
- Artifact downloads are possible only after the artifact ID appears in that series’ public durable allowlist. The server resolves it beneath approved output roots; the client rebuilds the endpoint from canonical series/artifact UUIDs, checks the received byte count and SHA-256 against public durable metadata, `fsync`s a sibling temporary, and installs it atomically. No-overwrite mode uses an atomic hard-link claim, so another process cannot win the check/install gap and be clobbered.
- `run`, `start`, and retry operations use the same shared submission lock and dual-GPU readiness gates as the web UI. The client never starts or stops ComfyUI or H3 Studio. Start each verified service once with the project lifecycle scripts.

This API is for callers on the same workstation. Do not make port 8190 publicly reachable or put an unauthenticated reverse proxy in front of it.

## Capability discovery before uploads

Call `GET /api/config` before uploading large references. The response now includes the stable integer `series_api_version`. Version `1` defines the durable Series payload and lifecycle documented here; a caller that supports a different major version should stop before uploading. New optional capability fields may be added without changing that major version, so clients should ignore unknown keys.

The existing `profiles`, `series.templates`, limits, and defaults remain present. `series.capabilities.world_travel` adds a machine-readable preflight contract:

```json
{
  "series_api_version": 1,
  "series": {
    "capabilities": {
      "world_travel": {
        "template": "world_travel",
        "render_mode": "r2v",
        "maximum_quality_profile": "quality_bf16_dual",
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

Before uploading, a maximum-quality World Travel caller should verify all of the following:

1. `series_api_version` is `1`.
2. `world_travel` is in `series.templates` and its capability uses R2V.
3. The shared slots and labels are exactly P1–P7, the required per-shot scene is P8, and the continuity final frame is P9.
4. `maximum_quality_profile` identifies a returned profile with `precision: "bf16"`, `dual_gpu: true`, `turbo: false`, and `steps_ref: 25`.

This discovery request does not upload media or start the engine.

The stdlib client performs this preflight automatically before `create`, `update`, or `run` can upload anything. It requires API version 1, the selected profile to exist, and—on World Travel—the exact R2V P1–P9 contract above plus `quality_bf16_dual` as BF16, dual-GPU, non-Turbo, 25-step R2V. It refuses a different contract or World Travel profile instead of silently downgrading. A direct `upload`, any source-backed create/update/run, and `start` also require a successful deep health result (`connected: true`, `ready: true`, `model_status: "verified"`). Starting an existing series first fetches and preflights that same durable series ID.

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
| video | `.avi`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.webm`; decodable media in that container | 600 MiB (629,145,600 bytes) | 2–15 s; at most 8192 px on either edge and 40,000,000 pixels; source 1–240 fps; at most 32 streams; source audio at most 384 kHz and 32 channels | MP4, H.264, yuv420p, 24 fps, at most 2048 px on either edge; optional AAC stereo at 32 kHz |
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
    "profile": "quality_bf16_dual",
    "width": 1024,
    "height": 768,
    "ref_image_size": "max",
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

If a client spec omits setting values, the helper defaults to the production-quality contract above: `quality_bf16_dual`, 1024×768, `ref_image_size: "max"`, three-second continuity, and automatic advance. It never downgrades an explicitly selected profile. For World Travel, this cross-project client rejects any profile other than `quality_bf16_dual`.

BF16, dual-GPU placement, non-Turbo sampling, and 25 R2V steps define maximum *generation fidelity*. Canvas dimensions are a separate composition choice. The Italy example uses 1024×768 for a stable 4:3 ensemble composition. Choose the advertised 1344×768 preset for a wider native landscape with more horizontal scene area; it uses the same maximum-quality profile and 25 steps, while requiring more pixels and therefore more compute/memory.

Server limits remain authoritative:

| Field | Contract |
| --- | --- |
| `template` | `lalachan`, `world_travel`, or `movie` |
| `shots` | 2–12 ordered shots; total requested duration at most 180 seconds |
| `duration` | Validated and aligned by the same 24 fps render-spec compiler as Single Clip |
| `profile` | A profile returned by `GET /api/config`; maximum quality is `quality_bf16_dual` |
| shared pictures | At most 8 generally; World Travel uses exactly the canonical seven |
| shared videos | At most 2, reserving H3’s third video slot for the preceding continuity tail |
| shared audio | At most 3 |
| continuity | 0, 2, 3, or 4 seconds; 3 is the quality default |
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

For that shot, the location image becomes `<Picture 8>`. With continuity enabled, the exact final frame from the previous accepted shot becomes `<Picture 9>`, and its accurate 2–4 second tail occupies the reserved video slot. The scene image controls only that shot’s architecture, terrain, light, atmosphere, and geography. It is not carried into the next destination.

On restart or retry, a successor shot advertises and submits P9 and the continuity video only when the durable handoff contains both media paths and valid recorded SHA-256 values for both files. A missing final frame, missing tail, absent digest, malformed digest, or unsafe relative location fails the series before a new GPU attempt is claimed. The accepted prior render remains preserved; retry or regenerate that prior shot to rebuild its verified handoff.

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

Useful methods also include `health`, `config`, `preflight_series_spec`, `upload`, `validate_uploads`, `update_series_from_spec`, `recover_from_receipt`, `list_series`, `get_series`, `pause_series`, `resume_series`, `cancel_active`, `retry_shot`, `retry_finalization`, and `list_artifacts`.

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
- each World Travel shot’s sanitized `scene_reference` (`kind`, `name`, `label`, never path/token);
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
