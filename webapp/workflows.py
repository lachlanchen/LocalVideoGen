"""Validation and ComfyUI API-prompt compilation for MiniMax H3.

The UI workflows in ``workflows/`` are ideal for interactive editing.  This
module emits the equivalent small API graph directly, avoiding frontend-only
subgraphs, selector widgets, and graph-to-prompt conversion at request time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


FPS = 24
MAX_PIXELS = 768 * 1344
MAX_SEED = (1 << 64) - 1
WORKFLOW_ID = "local-video-gen-minimax-h3-webapp"

TEXT_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
AUX_DEVICE_ENV = "H3_AUX_DEVICE"
DEFAULT_AUX_DEVICE = "gpu:1"
SUPPORTED_AUX_DEVICES = frozenset({"gpu:0", "gpu:1"})


class RequestError(ValueError):
    """A safe, user-facing render request error."""


def auxiliary_device() -> str:
    """Return the configured device for Qwen and reference conditioning.

    GPU 1 remains the normal two-GPU layout. Shared workstations can set
    ``H3_AUX_DEVICE=gpu:0`` so a protected workload may retain GPU 1 while H3
    uses the same BF16 weights, resolution, sampler, and step count through
    standard model offload on GPU 0.
    """

    value = os.environ.get(AUX_DEVICE_ENV, DEFAULT_AUX_DEVICE).strip().lower()
    if value not in SUPPORTED_AUX_DEVICES:
        choices = ", ".join(sorted(SUPPORTED_AUX_DEVICES))
        raise RuntimeError(f"{AUX_DEVICE_ENV} must be one of: {choices}")
    return value


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    description: str
    precision: str
    dual_gpu: bool
    turbo: bool
    steps_fl: int
    steps_ref: int


PROFILES: dict[str, Profile] = {
    "quality_bf16_dual": Profile(
        id="quality_bf16_dual",
        label="Maximum quality · BF16 · 2 GPUs",
        description="Full 25-step sampling with BF16 diffusion weights. Best fidelity; slowest and heaviest.",
        precision="bf16",
        dual_gpu=True,
        turbo=False,
        steps_fl=25,
        steps_ref=25,
    ),
    "quality_int8_dual": Profile(
        id="quality_int8_dual",
        label="Balanced quality · INT8 · 2 GPUs",
        description="Full 25-step sampling with lower-RAM INT8 diffusion weights.",
        precision="int8",
        dual_gpu=True,
        turbo=False,
        steps_fl=25,
        steps_ref=25,
    ),
    "preview_int8_turbo_dual": Profile(
        id="preview_int8_turbo_dual",
        label="Fast preview · INT8 Turbo · 2 GPUs",
        description="LightX2V Turbo LoRA: 8 steps for T2V/I2V and 4 steps for R2V. Use for drafts.",
        precision="int8",
        dual_gpu=True,
        turbo=True,
        steps_fl=8,
        steps_ref=4,
    ),
    "quality_bf16_offload": Profile(
        id="quality_bf16_offload",
        label="Maximum quality · BF16 · standard offload",
        description="Full 25-step sampling without explicit GPU stage placement.",
        precision="bf16",
        dual_gpu=False,
        turbo=False,
        steps_fl=25,
        steps_ref=25,
    ),
    "quality_int8_offload": Profile(
        id="quality_int8_offload",
        label="Balanced quality · INT8 · standard offload",
        description="Lower-RAM full sampling without explicit GPU stage placement.",
        precision="int8",
        dual_gpu=False,
        turbo=False,
        steps_fl=25,
        steps_ref=25,
    ),
}


RESOLUTION_PRESETS = (
    {"id": "native_landscape", "label": "Native landscape · 1344×768", "width": 1344, "height": 768},
    {"id": "native_portrait", "label": "Native portrait · 768×1344", "width": 768, "height": 1344},
    {"id": "native_square", "label": "Native square · 768×768", "width": 768, "height": 768},
    {"id": "classic_landscape", "label": "Classic landscape · 1024×768", "width": 1024, "height": 768},
    {"id": "classic_portrait", "label": "Classic portrait · 768×1024", "width": 768, "height": 1024},
    {"id": "preview_landscape", "label": "Preview landscape · 864×480", "width": 864, "height": 480},
    {"id": "preview_portrait", "label": "Preview portrait · 480×864", "width": 480, "height": 864},
    {"id": "preview_square", "label": "Preview square · 640×640", "width": 640, "height": 640},
)


@dataclass(frozen=True)
class UploadedAsset:
    """An already-uploaded ComfyUI input, resolved from an opaque app token."""

    kind: str
    path: str
    original_name: str = ""
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RenderSpec:
    mode: str
    profile: Profile
    prompt: str
    width: int
    height: int
    duration: float
    length: int
    seed: int
    first_frame: UploadedAsset | None = None
    last_frame: UploadedAsset | None = None
    ref_images: tuple[UploadedAsset, ...] = ()
    ref_videos: tuple[UploadedAsset, ...] = ()
    ref_video_audios: tuple[UploadedAsset | None, ...] = ()
    ref_audios: tuple[UploadedAsset, ...] = ()
    ref_image_size: str = "match"

    @property
    def steps(self) -> int:
        return self.profile.steps_ref if self.mode == "r2v" else self.profile.steps_fl


def aligned_frame_count(duration: float) -> int:
    """Match the official workflow's ``round(seconds*24)`` and 17k+5 snap."""

    frames = max(5, round(duration * FPS))
    return frames + (5 - frames % 17) % 17


def _plain_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise RequestError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError(f"{field} must be an integer") from exc
    if isinstance(value, float) and value != parsed:
        raise RequestError(f"{field} must be an integer")
    return parsed


def _asset(value: Any, field: str, kind: str, *, optional: bool = False) -> UploadedAsset | None:
    if value is None and optional:
        return None
    if not isinstance(value, UploadedAsset) or value.kind != kind:
        suffix = " or omitted" if optional else ""
        raise RequestError(f"{field} must be a valid {kind} upload{suffix}")
    return value


def _asset_list(value: Any, field: str, kind: str, maximum: int) -> tuple[UploadedAsset, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RequestError(f"{field} must be a list")
    if len(value) > maximum:
        raise RequestError(f"{field} accepts at most {maximum} uploads")
    return tuple(_asset(item, field, kind) for item in value)  # type: ignore[arg-type]


def parse_render_spec(payload: Mapping[str, Any], assets: Mapping[str, Any]) -> RenderSpec:
    if not isinstance(payload, Mapping):
        raise RequestError("request body must be a JSON object")

    mode = str(payload.get("mode", "t2v")).lower()
    if mode not in {"t2v", "i2v", "r2v"}:
        raise RequestError("mode must be t2v, i2v, or r2v")

    profile_id = str(payload.get("profile", "quality_bf16_dual"))
    profile = PROFILES.get(profile_id)
    if profile is None:
        raise RequestError("unknown quality profile")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RequestError("prompt is required")
    prompt = prompt.strip()
    if len(prompt) > 12_000:
        raise RequestError("prompt is longer than 12,000 characters")

    width = _plain_int(payload.get("width", 1344), "width")
    height = _plain_int(payload.get("height", 768), "height")
    if width < 256 or height < 256 or width > 1344 or height > 1344:
        raise RequestError("width and height must each be between 256 and 1344")
    if width % 32 or height % 32:
        raise RequestError("width and height must be multiples of 32")
    if width * height > MAX_PIXELS:
        raise RequestError(f"resolution exceeds H3's {MAX_PIXELS:,}-pixel local canvas")

    try:
        duration = float(payload.get("duration", 5))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError("duration must be a number") from exc
    minimum_duration = 2 if profile.turbo else 5
    if not minimum_duration <= duration <= 15:
        raise RequestError(
            f"duration must be between {minimum_duration} and 15 seconds for this profile"
        )

    seed = _plain_int(payload.get("seed", 1), "seed")
    if not 0 <= seed <= MAX_SEED:
        raise RequestError(f"seed must be between 0 and {MAX_SEED}")

    first_frame = _asset(assets.get("first_frame"), "first_frame", "image", optional=True)
    last_frame = _asset(assets.get("last_frame"), "last_frame", "image", optional=True)
    ref_images = _asset_list(assets.get("ref_images"), "ref_images", "image", 9)
    ref_videos = _asset_list(assets.get("ref_videos"), "ref_videos", "video", 3)
    ref_audios = _asset_list(assets.get("ref_audios"), "ref_audios", "audio", 3)

    raw_video_audios = assets.get("ref_video_audios") or []
    if isinstance(raw_video_audios, (str, bytes)) or not isinstance(raw_video_audios, Sequence):
        raise RequestError("ref_video_audios must be a list")
    if len(raw_video_audios) > len(ref_videos):
        raise RequestError("ref_video_audios cannot outnumber reference videos")
    video_audios: list[UploadedAsset | None] = []
    for item in raw_video_audios:
        video_audios.append(_asset(item, "ref_video_audios", "audio", optional=True))
    video_audios.extend([None] * (len(ref_videos) - len(video_audios)))

    ref_image_size = str(payload.get("ref_image_size", "match"))
    if ref_image_size not in {"match", "max"}:
        raise RequestError("ref_image_size must be match or max")

    if mode == "t2v":
        if any((first_frame, last_frame, ref_images, ref_videos, ref_audios)):
            raise RequestError("T2V does not accept reference media")
    elif mode == "i2v":
        if first_frame is None:
            raise RequestError("I2V requires a first-frame image")
        if any((ref_images, ref_videos, ref_audios)):
            raise RequestError("I2V accepts first and optional last frame only")
    else:
        if first_frame or last_frame:
            raise RequestError("R2V uses reference media instead of keyframes")
        if not any((ref_images, ref_videos, ref_audios)):
            raise RequestError("R2V requires at least one image, video, or audio reference")

    return RenderSpec(
        mode=mode,
        profile=profile,
        prompt=prompt,
        width=width,
        height=height,
        duration=duration,
        length=aligned_frame_count(duration),
        seed=seed,
        first_frame=first_frame,
        last_frame=last_frame,
        ref_images=ref_images,
        ref_videos=ref_videos,
        ref_video_audios=tuple(video_audios),
        ref_audios=ref_audios,
        ref_image_size=ref_image_size,
    )


def _diffusion_model(spec: RenderSpec) -> str:
    task = "ref2va" if spec.mode == "r2v" else "fl2va"
    suffix = "bf16" if spec.profile.precision == "bf16" else "int8_convrot"
    return f"minimax_h3_{task}_pruned_{suffix}.safetensors"


def _turbo_lora(spec: RenderSpec) -> str:
    if spec.mode == "r2v":
        return "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
    return "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"


class _Graph:
    def __init__(self) -> None:
        self.prompt: dict[str, dict[str, Any]] = {}
        self._next = 1

    def add(self, class_type: str, **inputs: Any) -> str:
        node_id = str(self._next)
        self._next += 1
        self.prompt[node_id] = {"class_type": class_type, "inputs": inputs}
        return node_id

    @staticmethod
    def link(node_id: str, output: int = 0) -> list[Any]:
        return [node_id, output]


def compile_prompt(spec: RenderSpec) -> dict[str, dict[str, Any]]:
    """Compile a validated request into a native ComfyUI API prompt."""

    graph = _Graph()
    model = graph.add("UNETLoader", unet_name=_diffusion_model(spec), weight_dtype="default")
    clip = graph.add("CLIPLoader", clip_name=TEXT_ENCODER, type="minimax", device="default")
    decode_video_vae = graph.add("VAELoader", vae_name=VIDEO_VAE)
    decode_audio_vae = graph.add("VAELoader", vae_name=AUDIO_VAE)
    conditioning_video_vae = decode_video_vae
    conditioning_audio_vae = decode_audio_vae

    if spec.profile.dual_gpu:
        aux_device = auxiliary_device()
        model = graph.add("SelectModelDevice", model=graph.link(model), device="gpu:0")
        clip = graph.add("SelectCLIPDevice", clip=graph.link(clip), device=aux_device)
        conditioning_video_vae = graph.add(
            "SelectVAEDevice", vae=graph.link(decode_video_vae), device=aux_device
        )
        conditioning_audio_vae = graph.add(
            "SelectVAEDevice", vae=graph.link(decode_audio_vae), device=aux_device
        )

    if spec.profile.turbo:
        model = graph.add(
            "LoraLoaderModelOnly",
            model=graph.link(model),
            lora_name=_turbo_lora(spec),
            strength_model=1.0,
        )

    if spec.mode in {"t2v", "i2v"}:
        conditioning_inputs: dict[str, Any] = {
            "clip": graph.link(clip),
            "vae": graph.link(conditioning_video_vae),
            "prompt": spec.prompt,
            "width": spec.width,
            "height": spec.height,
            "length": spec.length,
        }
        if spec.first_frame:
            loader = graph.add("LoadImage", image=spec.first_frame.path)
            conditioning_inputs["first_frame"] = graph.link(loader)
        if spec.last_frame:
            loader = graph.add("LoadImage", image=spec.last_frame.path)
            conditioning_inputs["last_frame"] = graph.link(loader)
        conditioning = graph.add("MiniMaxH3ImageToVideo", **conditioning_inputs)
    else:
        conditioning_inputs = {
            "clip": graph.link(clip),
            "vae": graph.link(conditioning_video_vae),
            "audio_vae": graph.link(conditioning_audio_vae),
            "prompt": spec.prompt,
            "width": spec.width,
            "height": spec.height,
            "length": spec.length,
            "ref_image_size": spec.ref_image_size,
        }
        for index, asset in enumerate(spec.ref_images):
            loader = graph.add("LoadImage", image=asset.path)
            conditioning_inputs[f"ref_images.ref_image_{index}"] = graph.link(loader)
        for index, asset in enumerate(spec.ref_videos):
            loader = graph.add("LoadVideo", file=asset.path)
            components = graph.add("GetVideoComponents", video=graph.link(loader))
            conditioning_inputs[f"ref_videos.ref_video_{index}"] = graph.link(components, 0)
            audio_override = spec.ref_video_audios[index]
            if audio_override is not None:
                audio_loader = graph.add("LoadAudio", audio=audio_override.path)
                conditioning_inputs[f"ref_video_audios.ref_video_audio_{index}"] = graph.link(audio_loader)
            else:
                conditioning_inputs[f"ref_video_audios.ref_video_audio_{index}"] = graph.link(components, 1)
        for index, asset in enumerate(spec.ref_audios):
            loader = graph.add("LoadAudio", audio=asset.path)
            conditioning_inputs[f"ref_audios.ref_audio_{index}"] = graph.link(loader)
        conditioning = graph.add("MiniMaxH3ReferenceToVideo", **conditioning_inputs)

    noise = graph.add("RandomNoise", noise_seed=spec.seed)
    sampler = graph.add("KSamplerSelect", sampler_name="res_multistep")
    scheduler_name = "beta" if spec.mode == "r2v" and not spec.profile.turbo else "simple"
    sigmas = graph.add(
        "BasicScheduler",
        model=graph.link(model),
        scheduler=scheduler_name,
        steps=spec.steps,
        denoise=1.0,
    )
    guider = graph.add(
        "BasicGuider",
        model=graph.link(model),
        conditioning=graph.link(conditioning, 0),
    )
    sampled = graph.add(
        "SamplerCustomAdvanced",
        noise=graph.link(noise),
        guider=graph.link(guider),
        sampler=graph.link(sampler),
        sigmas=graph.link(sigmas),
        latent_image=graph.link(conditioning, 1),
    )
    # Decode from the loader's default GPU-0 wrapper, not the auxiliary clone.
    # This keeps a late protected GPU-1 workload from invalidating a completed
    # multi-hour sample during final video/audio VAE work.
    frames = graph.add(
        "VAEDecode", samples=graph.link(sampled), vae=graph.link(decode_video_vae)
    )
    audio = graph.add(
        "VAEDecodeAudio", samples=graph.link(sampled), vae=graph.link(decode_audio_vae)
    )
    video = graph.add(
        "CreateVideo",
        images=graph.link(frames),
        fps=FPS,
        audio=graph.link(audio),
        bit_depth=8,
        color_space="sRGB",
    )
    graph.add(
        "SaveVideo",
        video=graph.link(video),
        filename_prefix=f"video/H3_Web_{spec.mode.upper()}",
        format="auto",
    )
    return graph.prompt


def public_config() -> dict[str, Any]:
    aux_device = auxiliary_device()
    profiles = []
    for profile in PROFILES.values():
        published = dict(profile.__dict__)
        if profile.dual_gpu:
            published["device_layout"] = {
                "model": "gpu:0",
                "conditioning": aux_device,
                "final_decode": "gpu:0",
            }
            published["effective_dual_gpu"] = aux_device != "gpu:0"
        profiles.append(published)
    return {
        "modes": [
            {"id": "t2v", "label": "Text to video", "short": "T2V", "description": "Create picture and native stereo audio from a prompt."},
            {"id": "i2v", "label": "Image to video", "short": "I2V", "description": "Animate a first frame, with an optional final frame."},
            {"id": "r2v", "label": "Reference to video", "short": "R2V", "description": "Guide identity, motion, style, or voice with mixed media."},
        ],
        "profiles": profiles,
        "resolutions": list(RESOLUTION_PRESETS),
        "defaults": {
            "mode": "t2v",
            "profile": "quality_bf16_dual",
            "width": 1344,
            "height": 768,
            "duration": 5,
        },
        "limits": {
            "duration_min": 5,
            "turbo_duration_min": 2,
            "duration_max": 15,
            "prompt_chars": 12_000,
            "fps": FPS,
        },
        "model": {"text_encoder": TEXT_ENCODER, "local_canvas": "768px short edge, up to 1344×768 area"},
    }
