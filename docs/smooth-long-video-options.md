# Smoother H3 long-video options

This page records the quality decision for long MiniMax H3 series on this workstation. It was evaluated on 2026-08-28 against the linked upstream project sources. The trusted production path remains the pinned, official ComfyUI H3 implementation with the BF16 dual-GPU quality profile and LocalVideoGen's validated shot handoff. New continuity or interpolation projects are useful research candidates, but none should silently alter an accepted native render.

## Decision

For the Italy World Travel episode, use the existing native 24 fps H3 path:

1. Render every shot with `quality_bf16_dual`, 25 full-model steps, one job at a time.
2. Keep the seven canonical character/prop references stable. Give each World Travel shot its own scene plate as `<Picture 8>` and give shots after the first the preceding shot's exact final frame as `<Picture 9>`.
3. Carry the preceding shot's validated three-second video/audio tail as continuity context. Preserve the authored prompt, seed, source hashes, exact final frame, tail, accepted MP4, and validation manifest.
4. Review and accept the native 24 fps master before trying any optional smoother derivative.

This is the least speculative route because it uses the pinned runtime, the official core H3 nodes, and continuity behavior already exercised by LocalVideoGen. The [official ComfyUI H3 implementation](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py) contains the native H3 conditioning and first/last-image support; it also enforces H3's frame-grid constraints. LocalVideoGen adds durable attempts, exact handoff artifacts, sequential GPU admission, full-media validation, and lossless final concatenation around that core.

## Options at a glance

| Option | What it can improve | Main quality or integration risk | Decision for the Italy master |
| --- | --- | --- | --- |
| Official ComfyUI H3 nodes + LocalVideoGen P8/P9 handoff | Native motion/audio generation, deterministic reference order, strong boundary identity and spatial cues, validated media and durable handoffs | A single H3 shot can still drift; reference conditioning cannot by itself guarantee matching pixels, velocity, waveform, or dialogue cadence | **Trusted baseline** |
| [LeonSooLab `comfyui-h3-motion-context`](https://github.com/LeonSooLab/comfyui-h3-motion-context) | Carries decoded frames and audio/latent context between H3 segments; its project guidance includes a 22-frame context and head anchors | New custom-node/process-patch surface, workflow and cache compatibility, possible repeated or smeared boundary motion, and a separate audio-timeline behavior to validate | **Experimental after Italy** |
| [akatz-ai `h3-relay`](https://github.com/akatz-ai/h3-relay) | Staged H3 continuation with overlap, caching, review gates, and optional LTX 2.5/RIFE enhancement | It is an additional orchestration and model stack, not a transparent switch; overlap blending, enhancement, voices, licenses, VRAM, and reproducibility all need independent validation | **Experimental after Italy** |
| [Fannovel16 `ComfyUI-Frame-Interpolation`](https://github.com/Fannovel16/ComfyUI-Frame-Interpolation) using RIFE | Creates a higher-frame-rate derivative after generation; useful when native motion is good but 24 fps cadence looks coarse | Invented intermediate detail can warp hands, faces, text, thin structures, occlusions, fast pans, or mouth shapes | **Optional A/B derivative only** |
| [ECCV2022-RIFE](https://github.com/hzwer/ECCV2022-RIFE) directly | A narrower, scriptable interpolation experiment independent of the generation graph | Separate environment/checkpoint and codec management; the same interpolation artifacts and lip-sync concerns remain | **Optional A/B derivative only** |

The [official ComfyUI FILM interpolation template](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/utility-frame_interpolation-film.json) is useful evidence for how ComfyUI composes an interpolation stage, but it is a utility workflow rather than proof that interpolation improves a particular H3 scene. Its custom-node dependencies must be treated separately from the core H3 graph.

## Why custom nodes are not in the trusted runtime

LocalVideoGen intentionally starts the pinned ComfyUI process with `--disable-all-custom-nodes`. That boundary is part of the reproducible and resource-safe baseline: a third-party node cannot patch sampling, load extra models, change dependency versions, or affect an expensive render unexpectedly.

Consequently:

- `comfyui-h3-motion-context` cannot be evaluated by dropping it into the current production graph. It needs a named experimental runtime profile with a pinned commit and a reviewed dependency lock.
- The ComfyUI frame-interpolation plugin likewise needs a separate, explicitly enabled experiment profile. Direct upstream RIFE can instead run as a separate post-process, but it still needs its own pinned environment and model digest.
- `h3-relay` should be evaluated as a separate orchestrator. Do not let it write over LocalVideoGen's accepted shots, continuity artifacts, or final movie.
- Official core H3 nodes remain available because they ship inside the pinned ComfyUI tree; disabling custom nodes does not disable those core extras.

Do not change this launch policy during the Italy run. A new node, model, sampler patch, or post-process should never enter the quality baseline merely because it starts successfully.

## Native continuity contract

The World Travel handoff deliberately separates stable identity from changing geography:

| Input | Contract |
| --- | --- |
| `<Picture 1>`–`<Picture 7>` | Same ordered words-card, robot, glasses, notebook, and three-character identity references for every shot |
| `<Picture 8>` | The current shot's location-specific scene plate; changes per shot, is hashed before submission |
| `<Picture 9>` | The previous accepted shot's exact final frame; absent only for shot 1 |
| Continuity video/audio | The preceding accepted shot's accurately trimmed three-second tail, with its native soundtrack |
| Authored prompt | States what must persist and what must change; prior-episode references may transfer identity/voice only, never plot, destination, palette, or camera direction |

The exact prior frame gives H3 the strongest available boundary-appearance reference in this production graph; the tail supplies recent movement and sound context. They are complementary, but Ref2VA conditioning does not force pixel-exact frames or sample-exact waveform continuation. Neither input should be replaced by a loosely selected screenshot or a re-encoded social-media copy.

The motion-context project's own comparison says stock reference audio can be regenerated as a similar-sounding clip rather than continued as the same waveform. That is an important baseline limitation, not a reason to change the Italy runtime mid-production: listen to every accepted join, keep dialogue away from a boundary where practical, and make a clean ambience or musical transition part of the authored shot design.

If a shot is retried, preserve the old attempt and its handoffs. Regenerate every dependent successor before assembling a new final, because its P9 frame and continuity tail were derived from the superseded attempt.

## Optional interpolation experiment

Interpolation is a finishing test, not a repair for bad native motion. First reject or rerender native shots with severe identity drift, broken geometry, scene cuts, frozen motion, malformed speech, or a discontinuous handoff. Only an accepted native master is eligible for an interpolation A/B.

For an initial RIFE trial:

1. Pin the chosen upstream repository commit, model/checkpoint digest, Python/PyTorch/CUDA versions, command, and complete arguments in an experiment manifest.
2. Read the accepted native 24 fps MP4 without resizing, retiming, denoising, frame blending, or replacing its audio.
3. Produce a separately named 48 fps candidate. Never overwrite the 24 fps shot or final master.
4. Mux the original accepted audio stream without time stretching. Confirm equal presentation duration and inspect audio start/end alignment after muxing.
5. Fully decode the derivative; record codec, dimensions, frame count, reported average fps, duration, audio format, and SHA-256.
6. Compare native and interpolated versions side by side at normal speed and frame by frame at every shot boundary. Publish the derivative only if it is clearly better.

The expected 48 fps frame count depends on the interpolator's endpoint convention, so do not approve it from `2 × native_frames` alone. Verify duration, timestamps, full decode, and the first/last source frames as well.

## Quality review gate

Review the native master and every experimental derivative against the same checklist:

- **Boundary continuity:** no duplicated pause, backward jump, dissolve, exposure pulse, or object teleport around P9/tail handoffs.
- **Character identity:** face shape, age, clothing, body scale, prop ownership, and screen direction remain stable.
- **Scene geometry:** arches, railings, canals, hands, glasses, notebook edges, food, signs, and background people do not split or melt.
- **Motion:** camera and subject velocity feel continuous; interpolation does not add rubbery acceleration, halos, or double contours.
- **Speech and voice:** every line is intelligible, belongs to the intended character, and keeps the accepted voice identity and cadence.
- **Lip sync:** inspect plosives, mouth closure, turns away from camera, and fast dialogue. Interpolation must not create a visible mouth phase that leads or trails the unchanged audio.
- **Audio:** no repeated syllable or ambience at joins, clipped word, echo, resampling pitch change, channel loss, or end drift.
- **Artifacts specific to interpolation:** inspect occlusions, fast pans, crossing limbs, water reflections, fine architecture, and any readable card text at both normal and slow playback.

Keep review evidence next to the derivative: contact sheet, short boundary extracts, media probe, validation log, comparison decision, and hashes. “Smoother” is not sufficient if character identity, articulation, or architecture becomes less credible.

## Safe adoption roadmap

### Stage 0 — Italy production

- Keep the official pinned H3 graph, BF16 dual-GPU quality profile, P8/P9 references, and three-second tail.
- Preserve all expensive attempts and accepted native masters.
- Do not enable custom nodes or change the sampling/runtime environment mid-episode.

### Stage 1 — Post-acceptance RIFE A/B

- Select one motion-heavy, one dialogue-heavy, and one fine-architecture shot from the accepted episode.
- Run a 24→48 fps derivative in an isolated, pinned post-process.
- Apply the quality gate above. Keep both outcomes and document rejection as carefully as acceptance.

### Stage 2 — Motion-context micro-pilot

- Review and pin `comfyui-h3-motion-context` and all transitive dependencies in a separate experimental profile.
- Recreate a short two-shot boundary from retained source artifacts; do not spend six-shot production GPU time first.
- Compare against LocalVideoGen's exact-frame plus three-second-tail baseline with identical prompts, references, seeds where supported, and resolution.
- Measure waveform continuity as well as listening and lip-sync. The upstream README discloses cumulative high-frequency loss down long audio chains, an approximately constant 10 ms audio offset in its tests, and narrow validation on one machine/configuration; reproduce those measurements locally rather than assuming they transfer.
- Reject the integration if it changes voice timing, increases identity drift, makes recovery nondeterministic, or requires untracked process patches.

### Stage 3 — H3 Relay micro-pilot

- Treat H3 Relay, overlap continuation, LTX enhancement, and RIFE as separate variables. Enable only one new stage per comparison.
- Pin every repository/model revision and keep its cache outside accepted LocalVideoGen artifact directories.
- Require review gates, resumability, resource ownership, and immutable native-source handling before considering application integration.

### Stage 4 — Production eligibility

Promote an experiment only after repeated wins across dialogue, travel movement, architecture, and water/occlusion scenes. A production candidate must also pass restart recovery, exact artifact provenance, full decode, audio alignment, bounded GPU/RAM behavior, and a rollback test. Until then it remains opt-in and may never replace the trusted baseline.

## Preservation and reproducibility record

Every native or experimental result should identify:

- LocalVideoGen commit and workflow/profile name;
- ComfyUI commit and H3 model/config hashes;
- third-party repository commit, dependency lock, model/checkpoint hashes, and license notes when applicable;
- authored prompt, composed prompt, ordered reference hashes, seed, canvas, frames, fps, and continuity inputs;
- source artifact hashes and output hash;
- FFmpeg/interpolator command and media probe;
- review result: accepted native master, accepted derivative, or rejected experiment, with a short reason.

Store derivatives beside, not over, their source. Name them by operation (for example, `movie.native-24fps.mp4` and `movie.rife-48fps.experimental.mp4`) and keep the native master as the provenance root even when a smoother derivative is selected for delivery.
