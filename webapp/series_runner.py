"""Durable, strictly sequential orchestration for local H3 video series."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import mimetypes
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .comfy_client import ComfyClient, ComfyError, flatten_outputs
from .job_store import ACTIVE_STATUSES, JobStore
from .series_media import SeriesMedia, SeriesMediaError
from .series_store import (
    SeriesNotFoundError,
    SeriesStore,
    SeriesStoreValidationError,
)
from .workflows import (
    PROFILES,
    RequestError,
    UploadedAsset,
    aligned_frame_count,
    compile_prompt,
    parse_render_spec,
)


MAX_SHOTS = 12
MIN_SHOTS = 2
MAX_TOTAL_SECONDS = 180.0
LALACHAN_REFERENCE_LABELS = (
    "Words card",
    "Zhuangzi Robot",
    "LightMind glasses",
    "Patchwork notebook",
    "Rara Xia",
    "Aya Chan",
    "Sasa Kun",
)
WORLD_TRAVEL_PERSISTENT_IMAGE_LABELS = frozenset(
    {
        "Zhuangzi Robot",
        "Rara Xia",
        "Aya Chan",
        "Sasa Kun",
    }
)
WORLD_TRAVEL_OPENING_ONLY_IMAGE_LABELS = (
    "Words card",
    "LightMind glasses",
    "Patchwork notebook",
)
WORLD_TRAVEL_OPENING_PROP_PATTERNS = {
    "Words card": re.compile(r"\b(?:words?\s+card|glacier\s+card)\b|(?:单词卡|词卡)", re.IGNORECASE),
    "LightMind glasses": re.compile(r"\blightmind(?:\s+glasses)?\b|轻智眼镜", re.IGNORECASE),
    "Patchwork notebook": re.compile(r"\b(?:patchwork\s+notebook|notebook)\b|笔记本", re.IGNORECASE),
}
WORLD_TRAVEL_OPENING_PROP_LIST_PATTERNS = {
    **WORLD_TRAVEL_OPENING_PROP_PATTERNS,
    "Words card": re.compile(
        r"\b(?:words?\s+card|glacier\s+card|card)\b|(?:单词卡|词卡|卡片)",
        re.IGNORECASE,
    ),
}
REFERENCE_POLICY_STATES = frozenset(
    {"ready", "paused", "failed", "cancelled", "completed"}
)
LOGGER = logging.getLogger("h3-webapp.series")
REFERENCE_TAG_PATTERN = re.compile(r"<(Picture|Video|Audio)\s+([0-9]+)>", re.IGNORECASE)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMPLETED_OUTPUT_REFRESH_ATTEMPTS = 6
COMPLETED_OUTPUT_REFRESH_MAX_DELAY = 0.5


def _video_output(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    outputs = job.get("outputs")
    if isinstance(outputs, Sequence) and not isinstance(outputs, (str, bytes)):
        return next(
            (
                item
                for item in outputs
                if isinstance(item, Mapping) and item.get("media_type") == "video"
            ),
            None,
        )
    return None


def _text(value: Any, label: str, *, maximum: int, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise RequestError(f"{label} must be text")
    result = value.strip()
    if not result and not optional:
        raise RequestError(f"{label} is required")
    if len(result) > maximum:
        raise RequestError(f"{label} is longer than {maximum} characters")
    return result


def _plain_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RequestError(f"{label} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError(f"{label} must be an integer") from exc
    if isinstance(value, float) and value != result:
        raise RequestError(f"{label} must be an integer")
    return result


def _trusted_asset(asset: UploadedAsset, *, label: Any) -> dict[str, Any]:
    path = PurePosixPath(asset.path)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in asset.path
    ):
        raise RequestError("an uploaded reference has an unsafe local location")
    record = {
        "kind": asset.kind,
        "path": str(path),
        "name": _text(asset.original_name, "reference name", maximum=160),
        "label": _text(label, "reference label", maximum=80),
    }
    if asset.kind == "video":
        record["has_audio"] = bool(
            isinstance(asset.metadata, Mapping) and asset.metadata.get("has_audio")
        )
    if isinstance(asset.metadata, Mapping):
        digest = str(asset.metadata.get("sha256") or "").lower()
        if SHA256_PATTERN.fullmatch(digest):
            record["sha256"] = digest
    return record


def _continuity_for_successor(
    document: Mapping[str, Any], shot_index: int
) -> dict[str, str] | None:
    """Validate a recovered P9/final-tail pair before it enters a prompt graph."""

    settings = document.get("settings")
    continuity_seconds = (
        settings.get("continuity_seconds") if isinstance(settings, Mapping) else 0
    )
    if shot_index <= 0 or not continuity_seconds:
        return None
    shots = document.get("shots")
    prior = (
        shots[shot_index - 1]
        if (
            isinstance(shots, Sequence)
            and not isinstance(shots, (str, bytes))
            and shot_index < len(shots)
        )
        else None
    )
    continuity = prior.get("continuity_input") if isinstance(prior, Mapping) else None
    if not isinstance(continuity, Mapping):
        raise SeriesMediaError(
            "previous shot continuity handoff is incomplete or unverified; "
            "retry the previous shot before GPU submission"
        )

    validated: dict[str, str] = {}
    for kind in ("video", "image"):
        path_value = continuity.get(f"{kind}_path")
        name_value = continuity.get(f"{kind}_name")
        digest = str(continuity.get(f"{kind}_sha256") or "").lower()
        path = PurePosixPath(path_value) if isinstance(path_value, str) else None
        if (
            path is None
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in path_value
            or not isinstance(name_value, str)
            or not name_value.strip()
            or len(name_value) > 160
            or not SHA256_PATTERN.fullmatch(digest)
        ):
            raise SeriesMediaError(
                "previous shot continuity handoff is incomplete or unverified; "
                "retry the previous shot before GPU submission"
            )
        validated[f"{kind}_path"] = str(path)
        validated[f"{kind}_name"] = name_value.strip()
        validated[f"{kind}_sha256"] = digest
    return validated


def _validated_image_omissions(
    value: Any,
    references: Mapping[str, Any],
    *,
    template: str,
    shot_index: int,
) -> list[str]:
    """Return a canonical per-shot shared-image omission policy."""

    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RequestError("omit_shared_image_labels must be a list")
    if len(value) > len(references["images"]):
        raise RequestError("omit_shared_image_labels contains too many labels")
    requested = [
        _text(item, "omitted shared image label", maximum=80) for item in value
    ]
    if len(set(requested)) != len(requested):
        raise RequestError("omit_shared_image_labels must not contain duplicates")
    shared_labels = [str(item["label"]) for item in references["images"]]
    unknown = [label for label in requested if label not in shared_labels]
    if unknown:
        raise RequestError(
            "omit_shared_image_labels contains an unknown shared image: " + unknown[0]
        )
    if shot_index == 0 and requested:
        raise RequestError("Shot 1 must keep every shared image reference")
    if template == "world_travel":
        protected = WORLD_TRAVEL_PERSISTENT_IMAGE_LABELS.intersection(requested)
        if protected:
            raise RequestError(
                "world_travel cannot omit persistent cast reference: "
                + sorted(protected)[0]
            )
    requested_set = set(requested)
    return [label for label in shared_labels if label in requested_set]


def _selected_shared_images(
    references: Mapping[str, Any], omitted_labels: Sequence[str]
) -> list[Mapping[str, Any]]:
    omitted = set(omitted_labels)
    return [
        item for item in references["images"] if str(item["label"]) not in omitted
    ]


def _picture_reference_layout(
    references: Mapping[str, Any],
    *,
    omitted_labels: Sequence[str],
    scene_reference: Mapping[str, Any] | None,
    include_continuity: bool,
) -> tuple[list[str], dict[int, int]]:
    """Build physical picture labels and logical-to-physical tag mapping."""

    omitted = set(omitted_labels)
    labels: list[str] = []
    tag_map: dict[int, int] = {}
    physical_slot = 0
    shared_images = references["images"]
    for logical_slot, item in enumerate(shared_images, start=1):
        if str(item["label"]) in omitted:
            continue
        physical_slot += 1
        tag_map[logical_slot] = physical_slot
        labels.append(f"<Picture {physical_slot}> = {item['label']}")
    next_logical_slot = len(shared_images) + 1
    if scene_reference is not None:
        physical_slot += 1
        tag_map[next_logical_slot] = physical_slot
        labels.append(
            f"<Picture {physical_slot}> = {scene_reference['label']}"
        )
        next_logical_slot += 1
    if include_continuity:
        physical_slot += 1
        tag_map[next_logical_slot] = physical_slot
        labels.append(
            f"<Picture {physical_slot}> = previous shot's exact final frame"
        )
    return labels, tag_map


def _remap_picture_tags(value: str, tag_map: Mapping[int, int]) -> str:
    """Translate stable authored picture tags to compact physical H3 slots."""

    def replace(match: re.Match[str]) -> str:
        if match.group(1).lower() != "picture":
            return match.group(0)
        physical_slot = tag_map.get(int(match.group(2)))
        return (
            f"<Picture {physical_slot}>"
            if physical_slot is not None
            else match.group(0)
        )

    return REFERENCE_TAG_PATTERN.sub(replace, value)


def _series_reference_labels(
    references: Mapping[str, Any],
    *,
    omitted_image_labels: Sequence[str] = (),
    scene_reference: Mapping[str, Any] | None = None,
    continuity_seconds: int,
    include_continuity: bool,
) -> list[str]:
    labels, _ = _picture_reference_layout(
        references,
        omitted_labels=omitted_image_labels,
        scene_reference=scene_reference,
        include_continuity=include_continuity and bool(continuity_seconds),
    )
    video_labels: list[tuple[str, str | None]] = []
    for item in references["videos"]:
        soundtrack = item.get("soundtrack")
        if isinstance(soundtrack, Mapping):
            audio_label = str(soundtrack["label"])
        elif item.get("has_audio"):
            audio_label = f"original audio from {item['label']}"
        else:
            audio_label = None
        video_labels.append((str(item["label"]), audio_label))
    if include_continuity and continuity_seconds:
        video_labels.append(
            (
                f"previous shot's final {continuity_seconds} seconds",
                "stereo audio from the previous shot continuity tail",
            )
        )
    audio_index = 1
    for index, (video_label, audio_label) in enumerate(video_labels):
        if audio_label is not None:
            labels.append(f"<Audio {audio_index}> = {audio_label}")
            audio_index += 1
        labels.append(f"<Video {index + 1}> = {video_label}")
    for item in references["audio"]:
        labels.append(f"<Audio {audio_index}> = {item['label']}")
        audio_index += 1
    return labels


def _compose_series_prompt(
    document: Mapping[str, Any], shot_index: int, labels: Sequence[str]
) -> str:
    shot = document["shots"][shot_index]
    if document["template"] == "lalachan":
        guidance = (
            "LALACHAN series continuity: keep every named character's species, human face, costume, "
            "scale, voice and relationships exact across shots. Use natural Chinese dialogue and clear "
            "screen direction; do not add, merge, duplicate or replace cast members."
        )
    elif document["template"] == "world_travel":
        if shot.get("omit_shared_image_labels"):
            picture_guidance = (
                "Shared pictures present in the reference map are identity and series-style anchors "
                "only. The shot-specific location picture named in the reference map controls the "
                "location, architecture, terrain, light and atmosphere for this shot only; "
            )
        else:
            picture_guidance = (
                "Pictures 1-7 are shared identity and series-style anchors only. Picture 8 is the "
                "location, architecture, terrain, light and atmosphere anchor for this shot only; "
            )
        guidance = (
            "LALACHAN World Travel continuity: lock every named character's identity, species, human "
            "face, body scale, wardrobe, accessories, relationships and voice across the whole journey. "
            "Keep the stated travel route, geography, screen direction, time progression and carried "
            "props coherent. "
            f"{picture_guidance}"
            "do not carry its place-specific details into another destination. Any earlier-episode, "
            "video or audio reference may guide character appearance or voice timbre only: never copy "
            "its country, plot, story direction, actions, blocking, landmarks or visual composition. "
            "Use natural concise dialogue and let one motivated journey connect the history and sights "
            "instead of presenting an unrelated tourist checklist."
        )
    else:
        guidance = (
            "Movie continuity: preserve cast identity, wardrobe, props, geography, lighting direction, "
            "voices and the previous shot's final action."
        )
    continuity = ""
    if shot_index > 0 and document["settings"]["continuity_seconds"]:
        continuity = (
            " Continue directly and seamlessly from the previous-shot reference tail; "
            "do not replay its completed action."
        )
        if document["template"] == "world_travel":
            continuity += (
                " Complete the match into this shot's location within the first second; "
                "from one second onward, show only the current location and this shot's new action."
            )
    reference_map = "\nReference map:\n" + "\n".join(labels) if labels else ""
    _, picture_tag_map = _picture_reference_layout(
        document["references"],
        omitted_labels=shot.get("omit_shared_image_labels") or [],
        scene_reference=(
            shot.get("scene_reference")
            if isinstance(shot.get("scene_reference"), Mapping)
            else None
        ),
        include_continuity=(
            shot_index > 0 and bool(document["settings"]["continuity_seconds"])
        ),
    )
    authored_brief = str(document.get("brief") or "")
    authored_prompt = str(shot["prompt"])
    if document["template"] == "world_travel":
        omitted_labels = shot.get("omit_shared_image_labels") or []
        authored_brief = _without_omitted_opening_prop_mentions(
            authored_brief, omitted_labels
        )
        authored_prompt = _without_omitted_opening_prop_mentions(
            authored_prompt, omitted_labels
        )
    remapped_brief = _remap_picture_tags(authored_brief, picture_tag_map)
    remapped_prompt = _remap_picture_tags(authored_prompt, picture_tag_map)
    brief = (
        f"\nSilent series context, never speak or display: {remapped_brief}"
        if remapped_brief
        else ""
    )
    settings = document["settings"]
    target = (
        f"\nTarget output: {settings['width']}x{settings['height']}; "
        f"{float(shot['duration']):g} seconds."
    )
    return (
        "Production rule: never speak or display a series title, shot title, reference label, "
        "production note or instruction. Spoken content is limited to dialogue explicitly quoted "
        "in the authored shot direction.\n"
        f"{guidance}{continuity}{target}{brief}{reference_map}\n\n{remapped_prompt}"
    )


def _without_omitted_opening_prop_mentions(
    text: str, omitted_labels: Sequence[str]
) -> str:
    """Keep omitted opening props out of H3's positive-language conditioning.

    Negative instructions such as "no notebook" can still cause a generative
    model to reconstruct that object.  When an opening-only World Travel image
    is physically absent, remove matching prose clauses as well.  Comma-based
    ``No ...`` lists retain their unrelated safeguards.
    """

    strong_patterns = [
        WORLD_TRAVEL_OPENING_PROP_PATTERNS[label]
        for label in omitted_labels
        if label in WORLD_TRAVEL_OPENING_PROP_PATTERNS
    ]
    list_patterns = [
        WORLD_TRAVEL_OPENING_PROP_LIST_PATTERNS[label]
        for label in omitted_labels
        if label in WORLD_TRAVEL_OPENING_PROP_LIST_PATTERNS
    ]
    if not text or not strong_patterns:
        return text

    def mentions_prop(fragment: str, patterns: Sequence[re.Pattern[str]]) -> bool:
        return any(pattern.search(fragment) for pattern in patterns)

    exclusion_prefix = re.compile(
        r"^(?P<prefix>\s*(?:no|do\s+not\s+(?:show|include|display)|"
        r"never\s+(?:show|include|display))\s+)",
        re.IGNORECASE,
    )
    exclusion_state = re.compile(
        r"\b(?:off[ -]?camera|out\s+of\s+(?:frame|view)|absent|hidden|"
        r"never\s+returns?|do\s+not\s+returns?)\b",
        re.IGNORECASE,
    )
    opening_history = re.compile(
        r"\b(?:opening|shot\s*1|first\s+shot)\b", re.IGNORECASE
    )

    cleaned_sentences: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sentence:
            continue
        terminal = sentence[-1] if sentence[-1:] in {".", "!", "?"} else ""
        kept_clauses: list[str] = []
        for clause in re.split(r";\s*", sentence):
            prefix_match = exclusion_prefix.match(clause)
            if prefix_match:
                body = clause[prefix_match.end() :]
                retained = [
                    part.strip()
                    for part in body.split(",")
                    if not mentions_prop(part, list_patterns)
                ]
                if retained:
                    retained[0] = re.sub(
                        r"^(?:and|or)\s+", "", retained[0], flags=re.IGNORECASE
                    )
                    kept_clauses.append(
                        prefix_match.group("prefix").strip() + " " + ", ".join(retained)
                    )
                continue

            subclauses = re.split(r",\s+and\s+", clause)
            retained_subclauses: list[str] = []
            for subclause in subclauses:
                has_prop = mentions_prop(subclause, strong_patterns)
                is_prop_only_direction = has_prop and (
                    exclusion_state.search(subclause)
                    or opening_history.search(subclause)
                )
                if not is_prop_only_direction:
                    retained_subclauses.append(subclause.strip())
            if retained_subclauses:
                kept_clauses.append(", and ".join(retained_subclauses))
            elif not mentions_prop(clause, strong_patterns):
                kept_clauses.append(clause.strip())
        cleaned = "; ".join(part for part in kept_clauses if part).strip()
        if cleaned:
            if terminal and cleaned[-1:] not in {".", "!", "?"}:
                cleaned += terminal
            cleaned_sentences.append(cleaned)
    return " ".join(cleaned_sentences)


def build_series_document(
    payload: Mapping[str, Any],
    resolve_asset: Callable[[Any, str, bool], UploadedAsset | None],
) -> dict[str, Any]:
    """Validate an API payload and permanently resolve its opaque upload tokens."""

    if not isinstance(payload, Mapping):
        raise RequestError("request body must be a JSON object")
    title = _text(payload.get("title"), "title", maximum=120)
    brief = _text(payload.get("brief"), "brief", maximum=2_000, optional=True)
    template = str(payload.get("template") or "lalachan")
    if template not in {"lalachan", "movie", "world_travel"}:
        raise RequestError("template must be lalachan, movie, or world_travel")
    settings_raw = payload.get("settings") or {}
    if not isinstance(settings_raw, Mapping):
        raise RequestError("settings must be a JSON object")
    profile = str(settings_raw.get("profile") or "quality_bf16_dual")
    if profile not in PROFILES:
        raise RequestError("unknown quality profile")
    width = _plain_int(settings_raw.get("width", 1024), "width")
    height = _plain_int(settings_raw.get("height", 768), "height")
    ref_image_size = str(settings_raw.get("ref_image_size") or "max")
    if ref_image_size not in {"match", "max"}:
        raise RequestError("ref_image_size must be match or max")
    continuity_seconds = _plain_int(
        settings_raw.get("continuity_seconds", 3), "continuity_seconds"
    )
    if continuity_seconds not in {0, 2, 3, 4}:
        raise RequestError("continuity_seconds must be 0, 2, 3, or 4")
    advance = settings_raw.get("advance", True)
    if not isinstance(advance, bool):
        raise RequestError("advance must be true or false")

    references_raw = payload.get("references") or {}
    if not isinstance(references_raw, Mapping):
        raise RequestError("references must be a JSON object")

    def reference_list(key: str, kind: str, maximum: int) -> list[dict[str, Any]]:
        raw = references_raw.get(key) or []
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise RequestError(f"references.{key} must be a list")
        if len(raw) > maximum:
            raise RequestError(f"references.{key} accepts at most {maximum} items")
        result: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise RequestError(f"each references.{key} item must be an object")
            asset = resolve_asset(item.get("token"), kind, False)
            assert asset is not None
            label = item.get("label") or f"{kind.title()} {index + 1}"
            record = _trusted_asset(asset, label=label)
            if kind == "video":
                soundtrack = resolve_asset(item.get("soundtrack"), "audio", True)
                record["soundtrack"] = (
                    _trusted_asset(soundtrack, label=f"{label} soundtrack")
                    if soundtrack is not None
                    else None
                )
            result.append(record)
        return result

    references = {
        # Reserve one of H3's nine picture slots for the previous final frame.
        "images": reference_list("images", "image", 8),
        # Reserve one of H3's three video slots for the previous shot tail.
        "videos": reference_list("videos", "video", 2),
        "audio": reference_list("audio", "audio", 3),
    }
    if template in {"lalachan", "world_travel"}:
        image_labels = tuple(item["label"] for item in references["images"])
        valid_count = (
            len(image_labels) == 7
            if template == "world_travel"
            else len(image_labels) in {7, 8}
        )
        if not valid_count or image_labels[:7] != LALACHAN_REFERENCE_LABELS:
            count_note = (
                "exactly seven pictures"
                if template == "world_travel"
                else "its first seven pictures"
            )
            raise RequestError(
                f"{template} requires {count_note} in this exact order: "
                + ", ".join(LALACHAN_REFERENCE_LABELS)
            )

    shots_raw = payload.get("shots")
    if isinstance(shots_raw, (str, bytes)) or not isinstance(shots_raw, Sequence):
        raise RequestError("shots must be a list")
    if not MIN_SHOTS <= len(shots_raw) <= MAX_SHOTS:
        raise RequestError(f"series requires between {MIN_SHOTS} and {MAX_SHOTS} shots")
    shots: list[dict[str, Any]] = []
    total_seconds = 0.0
    for index, item in enumerate(shots_raw):
        if not isinstance(item, Mapping):
            raise RequestError("each shot must be a JSON object")
        shot_title = _text(item.get("title") or f"Shot {index + 1}", "shot title", maximum=120)
        prompt = _text(item.get("prompt"), "shot prompt", maximum=10_000)
        scene_reference: dict[str, Any] | None = None
        scene_raw = item.get("scene_reference")
        if template == "world_travel":
            if not isinstance(scene_raw, Mapping):
                raise RequestError(
                    f"Shot {index + 1} scene_reference must be an image upload object"
                )
            scene_asset = resolve_asset(scene_raw.get("token"), "image", False)
            assert scene_asset is not None
            scene_reference = _trusted_asset(
                scene_asset,
                label=scene_raw.get("label")
                or f"Shot {index + 1} location reference",
            )
        elif scene_raw is not None:
            raise RequestError("scene_reference is only available for world_travel")
        omitted_shared_images = _validated_image_omissions(
            item.get("omit_shared_image_labels"),
            references,
            template=template,
            shot_index=index,
        )
        try:
            duration = float(item.get("duration", 10))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RequestError("shot duration must be a number") from exc
        seed = _plain_int(item.get("seed", index + 1), "shot seed")
        # Reuse the established renderer's exact profile/canvas/duration/seed validation.
        parse_render_spec(
            {
                "mode": "t2v",
                "profile": profile,
                "prompt": prompt,
                "width": width,
                "height": height,
                "duration": duration,
                "seed": seed,
            },
            {},
        )
        total_seconds += duration
        shots.append(
            {
                "index": index,
                "title": shot_title,
                "prompt": prompt,
                "duration": duration,
                "actual_duration": aligned_frame_count(duration) / 24.0,
                "seed": seed,
                "scene_reference": scene_reference,
                "omit_shared_image_labels": omitted_shared_images,
                "status": "pending",
                "attempts": [],
                "accepted_attempt": None,
                "continuity_artifacts": [],
                "continuity_input": None,
                "error": None,
            }
        )
    if continuity_seconds:
        for index, shot in enumerate(shots[:-1]):
            if float(shot["actual_duration"]) + 0.05 < continuity_seconds:
                raise RequestError(
                    f"Shot {index + 1} is too short for its "
                    f"{continuity_seconds}-second continuity handoff"
                )
    if total_seconds > MAX_TOTAL_SECONDS:
        raise RequestError(f"series requested duration exceeds {MAX_TOTAL_SECONDS:g} seconds")
    document = {
        "title": title,
        "brief": brief,
        "template": template,
        "settings": {
            "profile": profile,
            "width": width,
            "height": height,
            "ref_image_size": ref_image_size,
            "continuity_seconds": continuity_seconds,
            "advance": advance,
        },
        "references": references,
        "shots": shots,
        "active_shot": None,
        "pause_requested": False,
        "cancel_requested": False,
        "error": None,
        "artifacts": [],
    }
    used_audio_semantics: dict[int, str] = {}
    for index in range(len(shots)):
        labels = _series_reference_labels(
            references,
            omitted_image_labels=(
                shots[index].get("omit_shared_image_labels") or []
            ),
            scene_reference=shots[index].get("scene_reference"),
            continuity_seconds=continuity_seconds,
            include_continuity=index > 0,
        )
        _, picture_tag_map = _picture_reference_layout(
            references,
            omitted_labels=shots[index].get("omit_shared_image_labels") or [],
            scene_reference=(
                shots[index].get("scene_reference")
                if isinstance(shots[index].get("scene_reference"), Mapping)
                else None
            ),
            include_continuity=index > 0 and bool(continuity_seconds),
        )
        available_tags = {
            (match.group(1).lower(), int(match.group(2)))
            for label in labels
            if (match := REFERENCE_TAG_PATTERN.search(label))
        }
        available_tags = {
            tag for tag in available_tags if tag[0] != "picture"
        }
        available_tags.update(("picture", slot) for slot in picture_tag_map)
        audio_semantics: dict[int, str] = {}
        for label in labels:
            match = REFERENCE_TAG_PATTERN.search(label)
            if match and match.group(1).lower() == "audio":
                audio_semantics[int(match.group(2))] = label.split("=", 1)[1].strip()
        for match in REFERENCE_TAG_PATTERN.finditer(f"{brief}\n{shots[index]['prompt']}"):
            tag = (match.group(1).lower(), int(match.group(2)))
            if tag not in available_tags:
                raise RequestError(
                    f"Shot {index + 1} uses {match.group(0)} without a matching reference"
                )
            if tag[0] == "audio":
                semantic = audio_semantics[tag[1]]
                previous = used_audio_semantics.setdefault(tag[1], semantic)
                if previous != semantic:
                    raise RequestError(
                        f"{match.group(0)} changes meaning between shots; use the named "
                        "reference or each shot's exact audio tag"
                    )
        if len(_compose_series_prompt(document, index, labels)) > 12_000:
            raise RequestError(
                f"composed prompt for Shot {index + 1} exceeds 12,000 characters"
            )
    return document


def _public_artifact(series_id: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    artifact_id = str(artifact["id"])
    result = {
        "id": artifact_id,
        "kind": str(artifact.get("kind") or "file"),
        "label": str(artifact.get("label") or "Artifact"),
        "mime": str(artifact.get("mime") or "application/octet-stream"),
        "url": f"/api/series/{series_id}/artifacts/{artifact_id}",
        "download_url": f"/api/series/{series_id}/artifacts/{artifact_id}?download=1",
        "metadata": copy.deepcopy(artifact.get("metadata") or {}),
    }
    if artifact.get("superseded") is True:
        result["superseded"] = True
    return result


def public_series(record: Mapping[str, Any], client: ComfyClient | None = None) -> dict[str, Any]:
    series_id = str(record["id"])
    document = record["document"]
    artifact_index = {
        str(item["id"]): item
        for item in document.get("artifacts") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    public_references: dict[str, list[dict[str, Any]]] = {"images": [], "videos": [], "audio": []}
    for key in public_references:
        for item in document.get("references", {}).get(key, []):
            public = {
                "kind": item.get("kind"),
                "name": item.get("name"),
                "label": item.get("label"),
            }
            if key == "videos":
                soundtrack = item.get("soundtrack")
                public["has_audio"] = bool(item.get("has_audio"))
                public["soundtrack"] = (
                    {"kind": "audio", "name": soundtrack.get("name"), "label": soundtrack.get("label")}
                    if isinstance(soundtrack, Mapping)
                    else None
                )
            public_references[key].append(public)
    shots: list[dict[str, Any]] = []
    completed = 0
    for shot in document.get("shots") or []:
        if shot.get("status") == "completed":
            completed += 1
        attempts: list[dict[str, Any]] = []
        for attempt in shot.get("attempts") or []:
            outputs = [
                _public_artifact(series_id, artifact_index[artifact_id])
                for artifact_id in attempt.get("artifact_ids") or []
                if artifact_id in artifact_index
            ]
            attempt_public = {
                "number": attempt.get("number"),
                "job_id": attempt.get("job_id"),
                "status": attempt.get("status"),
                "error": attempt.get("error"),
                "outputs": outputs,
                "reference_map": copy.deepcopy(attempt.get("reference_map") or []),
            }
            if attempt.get("superseded") is True:
                attempt_public["superseded"] = True
            attempts.append(attempt_public)
        continuity = [
            _public_artifact(series_id, artifact_index[artifact_id])
            for artifact_id in shot.get("continuity_artifacts") or []
            if artifact_id in artifact_index
        ]
        public_shot = {
            "index": shot.get("index"),
            "title": shot.get("title"),
            "prompt": shot.get("prompt"),
            "duration": shot.get("duration"),
            "actual_duration": shot.get("actual_duration"),
            "seed": shot.get("seed"),
            "omit_shared_image_labels": copy.deepcopy(
                shot.get("omit_shared_image_labels") or []
            ),
            "status": shot.get("status"),
            "attempts": attempts,
            "accepted_attempt": shot.get("accepted_attempt"),
            "continuity": continuity,
            "error": shot.get("error"),
        }
        scene_reference = shot.get("scene_reference")
        if isinstance(scene_reference, Mapping):
            public_shot["scene_reference"] = {
                "kind": scene_reference.get("kind"),
                "name": scene_reference.get("name"),
                "label": scene_reference.get("label"),
            }
        shots.append(public_shot)
    active_shot = document.get("active_shot")
    progress = None
    if client is not None and isinstance(active_shot, int) and 0 <= active_shot < len(shots):
        attempts = document["shots"][active_shot].get("attempts") or []
        if attempts and attempts[-1].get("job_id"):
            progress = client.job_progress(str(attempts[-1]["job_id"]))
    artifacts = [
        _public_artifact(series_id, item)
        for item in document.get("artifacts") or []
        if isinstance(item, Mapping) and item.get("kind") in {"final", "manifest"}
    ]
    artifacts.sort(key=lambda item: item.get("superseded") is True)
    active_final = next(
        (item for item in reversed(artifacts) if item["kind"] == "final" and not item.get("superseded")),
        None,
    )
    return {
        "id": series_id,
        "title": document.get("title"),
        "brief": document.get("brief", ""),
        "template": document.get("template"),
        "status": record.get("status"),
        "settings": copy.deepcopy(document.get("settings") or {}),
        "references": public_references,
        "shots": shots,
        "active_shot": active_shot,
        "progress": {
            "completed_shots": completed,
            "total_shots": len(shots),
            "percent": (
                progress.get("percent")
                if isinstance(progress, Mapping) and progress.get("percent") is not None
                else (round(100 * completed / len(shots), 1) if shots else 0)
            ),
            "overall_percent": round(100 * completed / len(shots), 1) if shots else 0,
            "render": progress,
        },
        "error": document.get("error"),
        "artifacts": artifacts,
        "final_artifact": active_final,
        "revision": record.get("revision"),
        "created_ms": record.get("created_ms"),
        "updated_ms": record.get("updated_ms"),
    }


def public_series_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Compact library row; detail-only prompts and artifact inventories stay out."""

    document = record["document"]
    shots = document.get("shots") or []
    completed = sum(shot.get("status") == "completed" for shot in shots)
    settings = document.get("settings") or {}
    return {
        "id": str(record["id"]),
        "title": document.get("title"),
        "template": document.get("template"),
        "status": record.get("status"),
        "settings": {
            key: settings.get(key)
            for key in ("profile", "width", "height", "continuity_seconds")
        },
        "shot_count": len(shots),
        "active_shot": document.get("active_shot"),
        "progress": {
            "completed_shots": completed,
            "total_shots": len(shots),
            "percent": round(100 * completed / len(shots), 1) if shots else 0,
        },
        "error": document.get("error"),
        "revision": record.get("revision"),
        "created_ms": record.get("created_ms"),
        "updated_ms": record.get("updated_ms"),
    }


def find_artifact(record: Mapping[str, Any], artifact_id: str) -> dict[str, Any] | None:
    for item in record.get("document", {}).get("artifacts") or []:
        if isinstance(item, Mapping) and item.get("id") == artifact_id:
            return dict(item)
    return None


class SeriesRunner:
    """One cooperative runner; it never submits alongside any unrelated job."""

    def __init__(
        self,
        store: SeriesStore,
        jobs: JobStore,
        client: ComfyClient,
        media: SeriesMedia,
        *,
        poll_interval: float = 3.0,
        submission_lock: asyncio.Lock | None = None,
        runtime_check: Callable[[], Awaitable[None]] | None = None,
        submission_check: Callable[[], Awaitable[None]] | None = None,
        input_root: str | Path | None = None,
    ) -> None:
        self.store = store
        self.jobs = jobs
        self.client = client
        self.media = media
        self.poll_interval = poll_interval
        self.submission_lock = submission_lock or asyncio.Lock()
        self.runtime_check = runtime_check
        self.submission_check = submission_check or runtime_check
        self.input_root = Path(input_root).resolve() if input_root is not None else None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="h3-series-runner")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def run_forever(self) -> None:
        while True:
            try:
                progressed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Per-series errors are normally captured in run_once.  Keep the
                # singleton alive after an unexpected registry/runtime problem.
                LOGGER.exception("Unexpected series runner failure")
                progressed = False
            if progressed:
                await asyncio.sleep(0)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> bool:
        candidates = self.store.list(limit=100, runnable=True)
        if not candidates:
            return False
        priority = {
            "cancelling": 0,
            "pausing": 0,
            "running": 0,
            "stitching": 0,
            "waiting": 1,
            "queued": 2,
        }
        candidates.sort(
            key=lambda item: (
                priority.get(str(item["status"]), 3),
                int(item["created_ms"]),
            )
        )
        record = candidates[0]
        try:
            return await self._advance(record)
        except asyncio.CancelledError:
            raise
        except ComfyError:
            # Engine identity/connectivity failures are transient.  Never turn
            # a costly active render into a failed series merely because the
            # local service is temporarily unreachable.
            return False
        except Exception as exc:
            series_id = str(record["id"])
            error_text = str(exc)[:2000]

            def fail(document: dict[str, Any], _: str):
                document["error"] = error_text
                active = document.get("active_shot")
                if isinstance(active, int) and 0 <= active < len(document.get("shots") or []):
                    shot = document["shots"][active]
                    shot["status"] = "failed"
                    shot["error"] = error_text
                    if shot.get("attempts"):
                        attempt = shot["attempts"][-1]
                        attempt["status"] = "failed"
                        attempt["error"] = error_text
                        artifact_ids = set(attempt.get("artifact_ids") or [])
                        for artifact in document.get("artifacts") or []:
                            if artifact.get("id") in artifact_ids and artifact.get("kind") == "shot":
                                metadata = artifact.get("metadata") or {}
                                if metadata.get("validation") == "passed":
                                    artifact["metadata"] = {
                                        **metadata,
                                        "finalization_error": error_text[:1000],
                                    }
                                else:
                                    artifact["metadata"] = {
                                        **metadata,
                                        "validation": "failed",
                                    "validation_error": error_text[:1000],
                                }
                else:
                    pending = next(
                        (
                            shot
                            for shot in document.get("shots") or []
                            if shot.get("status") == "pending"
                        ),
                        None,
                    )
                    if pending is not None:
                        pending["status"] = "failed"
                        pending["error"] = error_text
                document["active_shot"] = None
                return document, "failed"

            self.store.mutate(series_id, fail)
            return True

    def _sync_job(self, job_id: str, upstream: Mapping[str, Any]) -> dict[str, Any]:
        current = self.jobs.get(job_id)
        if current is None:
            raise SeriesMediaError("series render job is missing from the private registry")
        status = str(upstream.get("status") or current["status"]).lower()
        status = {
            "success": "completed",
            "error": "failed",
            "running": "in_progress",
            "queued": "pending",
        }.get(status, status)
        # A concurrent watcher may already have observed a terminal result.
        # Never let a stale engine response regress that durable result.
        if current["status"] not in ACTIVE_STATUSES:
            status = str(current["status"])
        if status not in ACTIVE_STATUSES | {"completed", "failed", "cancelled"}:
            status = str(current["status"])
        outputs = flatten_outputs(upstream) if isinstance(upstream.get("outputs"), Mapping) else None
        if outputs is not None and current["status"] not in ACTIVE_STATUSES:
            current_outputs = current.get("outputs") or []
            # A terminal row may be refreshed specifically because ComfyUI
            # published its output locator just after publishing completion.
            # Fill an empty row, but never let a stale response erase or
            # replace already-preserved terminal outputs.
            if current_outputs or not outputs:
                outputs = None
        error = upstream.get("execution_error")
        error_text: str | None = None
        if isinstance(error, Mapping):
            error_text = str(error.get("exception_message") or error.get("message") or "Render failed")[:8192]
        return self.jobs.update(
            job_id,
            status if status != current["status"] else None,
            outputs=outputs if outputs is not None else None,
            error=error_text if error is not None else None,
        )

    async def _refresh_job(self, job_id: str) -> dict[str, Any]:
        current = self.jobs.get(job_id)
        if current is None:
            raise SeriesMediaError("series render job is missing from the private registry")
        if current["status"] not in ACTIVE_STATUSES:
            return current
        if self.runtime_check is not None:
            await self.runtime_check()
        try:
            upstream = await self.client.get_job(job_id)
        except ComfyError as exc:
            if exc.status == 404:
                # Preserve restart grace semantics: the ordinary JobStore watcher
                # owns terminalization, so a recently missing job is not resubmitted.
                return current
            raise
        return self._sync_job(job_id, upstream)

    async def _refresh_completed_output(
        self, job: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Boundedly recover an output locator published just after completion."""

        if _video_output(job) is not None:
            return dict(job)
        job_id = str(job.get("id") or "")
        current = self.jobs.get(job_id)
        if current is None:
            raise SeriesMediaError("series render job is missing from the private registry")
        if _video_output(current) is not None:
            return current
        if self.runtime_check is not None:
            await self.runtime_check()
        delay = min(
            max(float(self.poll_interval), 0.01),
            COMPLETED_OUTPUT_REFRESH_MAX_DELAY,
        )
        for refresh_index in range(COMPLETED_OUTPUT_REFRESH_ATTEMPTS):
            if refresh_index:
                await asyncio.sleep(delay)
                current = self.jobs.get(job_id)
                if current is None:
                    raise SeriesMediaError(
                        "series render job is missing from the private registry"
                    )
                if _video_output(current) is not None:
                    return current
            try:
                upstream = await self.client.get_job(job_id)
            except ComfyError as exc:
                if exc.status != 404:
                    raise
            else:
                current = self._sync_job(job_id, upstream)
                if _video_output(current) is not None:
                    return current
        return current

    def _unrelated_job_active(self, series_id: str, own_job_id: str | None = None) -> bool:
        for job in self.jobs.active(limit=100):
            if own_job_id and job["id"] == own_job_id:
                continue
            metadata = job.get("metadata") or {}
            if metadata.get("series_id") != series_id:
                return True
            # A second active job for the same series is also a conflict.
            return True
        return False

    async def _advance(self, record: Mapping[str, Any]) -> bool:
        series_id = str(record["id"])
        document = record["document"]
        status = str(record["status"])
        active_index = document.get("active_shot")
        if status == "cancelling" or document.get("cancel_requested"):
            return await self._finish_cancel(series_id, document)
        if isinstance(active_index, int):
            shot = document["shots"][active_index]
            attempts = shot.get("attempts") or []
            if not attempts:
                raise SeriesMediaError("active series shot has no render attempt")
            attempt = attempts[-1]
            job_id = str(attempt["job_id"])
            job = await self._refresh_job(job_id)
            if job["status"] in ACTIVE_STATUSES:
                return False
            if job["status"] in {"failed", "cancelled"}:
                error = job.get("error") or f"Render {job['status']}"

                def terminal(document: dict[str, Any], _: str):
                    item = document["shots"][active_index]
                    item["status"] = str(job["status"])
                    item["error"] = error
                    item["attempts"][-1]["status"] = str(job["status"])
                    item["attempts"][-1]["error"] = error
                    document["active_shot"] = None
                    document["error"] = error
                    return document, "cancelled" if job["status"] == "cancelled" else "failed"

                self.store.mutate(series_id, terminal)
                return True
            if job["status"] == "completed":
                await self._accept_completed(series_id, active_index, job)
                return True
            return False
        if status == "pausing" or document.get("pause_requested"):

            def paused(document: dict[str, Any], _: str):
                document["pause_requested"] = False
                return document, "paused"

            self.store.mutate(series_id, paused)
            return True
        next_index = next(
            (index for index, shot in enumerate(document["shots"]) if shot["status"] == "pending"),
            None,
        )
        if next_index is None:
            if all(shot["status"] == "completed" for shot in document["shots"]):
                return await self._finalize_series(
                    series_id, document, expected_revision=int(record["revision"])
                )
            raise SeriesMediaError("series has no pending shot to continue")
        if self._unrelated_job_active(series_id):
            if status != "waiting":
                self.store.mutate(series_id, lambda document, _: (document, "waiting"))
                return True
            return False
        return await self._submit_shot(
            series_id,
            document,
            next_index,
            expected_revision=int(record["revision"]),
        )

    def _references_for_shot(
        self, document: Mapping[str, Any], shot_index: int
    ) -> tuple[dict[str, Any], str, list[str]]:
        refs = document["references"]
        shot = document["shots"][shot_index]
        omitted_image_labels = shot.get("omit_shared_image_labels") or []
        images = [
            UploadedAsset("image", item["path"], item["name"])
            for item in _selected_shared_images(refs, omitted_image_labels)
        ]
        scene_reference = shot.get("scene_reference")
        if isinstance(scene_reference, Mapping):
            images.append(
                UploadedAsset(
                    "image", scene_reference["path"], scene_reference["name"]
                )
            )
        videos = [
            UploadedAsset("video", item["path"], item["name"]) for item in refs["videos"]
        ]
        video_audio = [
            UploadedAsset("audio", item["soundtrack"]["path"], item["soundtrack"]["name"])
            if isinstance(item.get("soundtrack"), Mapping)
            else None
            for item in refs["videos"]
        ]
        audio = [
            UploadedAsset("audio", item["path"], item["name"]) for item in refs["audio"]
        ]
        continuity = _continuity_for_successor(document, shot_index)
        if continuity is not None:
            images.append(
                UploadedAsset(
                    "image", continuity["image_path"], continuity["image_name"]
                )
            )
            videos.append(
                UploadedAsset(
                    "video", continuity["video_path"], continuity["video_name"]
                )
            )
            video_audio.append(None)
        labels = _series_reference_labels(
            refs,
            omitted_image_labels=omitted_image_labels,
            scene_reference=(scene_reference if isinstance(scene_reference, Mapping) else None),
            continuity_seconds=int(document["settings"]["continuity_seconds"]),
            include_continuity=continuity is not None,
        )
        assets = {
            "first_frame": None,
            "last_frame": None,
            "ref_images": images,
            "ref_videos": videos,
            "ref_video_audios": video_audio,
            "ref_audios": audio,
        }
        mode = "r2v" if any((images, videos, audio)) else "t2v"
        return assets, mode, labels

    @staticmethod
    def _reference_records_for_shot(
        document: Mapping[str, Any], shot_index: int
    ) -> list[dict[str, str]]:
        """Return private reference provenance in exact per-shot media order."""

        refs = document["references"]
        shot = document["shots"][shot_index]
        omitted_image_labels = shot.get("omit_shared_image_labels") or []
        records: list[dict[str, str]] = []

        def append(
            item: Mapping[str, Any], *, kind: str, label: str | None = None
        ) -> None:
            digest = str(item.get("sha256") or "").lower()
            if not SHA256_PATTERN.fullmatch(digest):
                return
            records.append(
                {
                    "kind": kind,
                    "path": str(item["path"]),
                    "name": str(item["name"]),
                    "label": label or str(item["label"]),
                    "sha256": digest,
                }
            )

        for item in _selected_shared_images(refs, omitted_image_labels):
            append(item, kind="image")
        scene = shot.get("scene_reference")
        if isinstance(scene, Mapping):
            append(scene, kind="image")
        for item in refs["videos"]:
            append(item, kind="video")
            soundtrack = item.get("soundtrack")
            if isinstance(soundtrack, Mapping):
                append(soundtrack, kind="audio")
        for item in refs["audio"]:
            append(item, kind="audio")

        continuity = _continuity_for_successor(document, shot_index)
        if continuity is not None:
            for kind in ("image", "video"):
                records.append(
                    {
                        "kind": kind,
                        "path": continuity[f"{kind}_path"],
                        "name": continuity[f"{kind}_name"],
                        "label": f"previous shot continuity {kind}",
                        "sha256": continuity[f"{kind}_sha256"],
                    }
                )
        return records

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    async def _verify_reference_integrity(
        self, document: Mapping[str, Any], shot_index: int
    ) -> list[dict[str, str]]:
        """Reject changed normalized inputs before claiming a costly GPU attempt."""

        records = self._reference_records_for_shot(document, shot_index)
        if self.input_root is None:
            return records
        root = self.input_root

        async def verify(record: Mapping[str, str]) -> None:
            relative = PurePosixPath(record["path"])
            candidate = root.joinpath(*relative.parts)
            try:
                if candidate.is_symlink():
                    raise OSError("symbolic reference")
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
                if not resolved.is_file():
                    raise OSError("not a regular file")
                actual = await asyncio.to_thread(self._sha256, resolved)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SeriesMediaError(
                    f"reference '{record['label']}' is no longer available; upload it again"
                ) from exc
            if actual != record["sha256"]:
                raise SeriesMediaError(
                    f"reference '{record['label']}' changed after upload; refusing a GPU submission"
                )

        await asyncio.gather(*(verify(record) for record in records))
        return records

    def _series_prompt(
        self, document: Mapping[str, Any], shot_index: int, labels: Sequence[str]
    ) -> str:
        prompt = _compose_series_prompt(document, shot_index, labels)
        if len(prompt) > 12_000:
            raise SeriesMediaError("series guidance makes this shot prompt exceed 12000 characters")
        return prompt

    async def _submit_shot(
        self,
        series_id: str,
        document: Mapping[str, Any],
        shot_index: int,
        *,
        expected_revision: int,
    ) -> bool:
        assets, mode, labels = self._references_for_shot(document, shot_index)
        reference_fingerprints = self._reference_records_for_shot(
            document, shot_index
        )
        fingerprint_manifest = [
            {key: value for key, value in record.items() if key != "path"}
            for record in reference_fingerprints
        ]
        shot = document["shots"][shot_index]
        prompt_text = self._series_prompt(document, shot_index, labels)
        settings = document["settings"]
        payload = {
            "mode": mode,
            "profile": settings["profile"],
            "prompt": prompt_text,
            "width": settings["width"],
            "height": settings["height"],
            "duration": shot["duration"],
            "seed": shot["seed"],
            "ref_image_size": settings["ref_image_size"],
        }
        spec = parse_render_spec(payload, assets)
        if self.submission_check is not None:
            await self.submission_check()
        readiness = await self.client.health(inspect_nodes=True)
        if readiness.get("ready") is False:
            raise ComfyError("ComfyUI is missing required H3 nodes", status=409)
        devices = readiness.get("stats", {}).get("devices", [])
        if spec.profile.dual_gpu and (not isinstance(devices, list) or len(devices) < 2):
            raise ComfyError("The selected profile requires both RTX 4090 GPUs", status=409)
        graph = compile_prompt(spec)
        job_id = str(uuid.uuid4())
        attempt_number = len(shot.get("attempts") or []) + 1
        metadata = {
            "series_id": series_id,
            "series_title": document["title"],
            "shot_index": shot_index,
            "shot_title": shot["title"],
            "attempt": attempt_number,
            "mode": spec.mode,
            "profile": spec.profile.id,
            "prompt": spec.prompt,
            "width": spec.width,
            "height": spec.height,
            "duration": spec.duration,
            "length": spec.length,
            "seed": str(spec.seed),
            "ref_image_size": spec.ref_image_size,
            "reference_map": list(labels),
            "reference_fingerprints": copy.deepcopy(fingerprint_manifest),
        }

        def claim(document: dict[str, Any], _: str):
            item = document["shots"][shot_index]
            item["status"] = "submitting"
            item["error"] = None
            item["attempts"].append(
                {
                    "number": attempt_number,
                    "job_id": job_id,
                    "status": "submitting",
                    "error": None,
                    "artifact_ids": [],
                    "reference_map": list(labels),
                    "reference_fingerprints": copy.deepcopy(fingerprint_manifest),
                }
            )
            document["active_shot"] = shot_index
            document["error"] = None
            return document, "running"

        async with self.submission_lock:
            latest = self.store.get(series_id)
            if latest is None:
                raise SeriesNotFoundError("series not found")
            latest_document = latest["document"]
            first_pending = next(
                (
                    index
                    for index, candidate in enumerate(latest_document["shots"])
                    if candidate.get("status") == "pending"
                ),
                None,
            )
            if (
                int(latest["revision"]) != expected_revision
                or latest["status"] not in {"queued", "waiting", "running"}
                or latest_document.get("pause_requested")
                or latest_document.get("cancel_requested")
                or latest_document.get("active_shot") is not None
                or first_pending != shot_index
                or any(
                    prior.get("status") != "completed"
                    or prior.get("accepted_attempt") is None
                    for prior in latest_document["shots"][:shot_index]
                )
            ):
                return True
            if self._unrelated_job_active(series_id):
                self.store.mutate(
                    series_id, lambda current, _: (current, "waiting")
                )
                return True
            # Recheck normalized bytes while holding the shared admission lock,
            # immediately before the durable claim and upstream submission.
            verified = await self._verify_reference_integrity(
                latest_document, shot_index
            )
            verified_manifest = [
                {key: value for key, value in record.items() if key != "path"}
                for record in verified
            ]
            if verified_manifest != fingerprint_manifest:
                raise SeriesMediaError("series references changed before GPU submission")
            self.store.mutate(series_id, claim)
            self.jobs.register(job_id, metadata, status="submitting")
            try:
                await self.client.submit(graph, metadata, job_id)
            except Exception as exc:
                self.jobs.update(job_id, "failed", error=str(exc)[:8192])
                raise
            self.jobs.update(job_id, "pending", error=None)

        def submitted(document: dict[str, Any], status: str):
            item = document["shots"][shot_index]
            if item["attempts"] and item["attempts"][-1]["job_id"] == job_id:
                item["status"] = "rendering"
                item["attempts"][-1]["status"] = "rendering"
            return document, status

        self.store.mutate(series_id, submitted)
        return True

    async def _accept_completed(
        self, series_id: str, shot_index: int, job: Mapping[str, Any]
    ) -> None:
        record = self.store.get(series_id)
        if record is None:
            raise SeriesNotFoundError("series not found")
        document = record["document"]
        shot = document["shots"][shot_index]
        attempt = shot["attempts"][-1]
        job = await self._refresh_completed_output(job)
        video_locator = _video_output(job)
        if not isinstance(video_locator, Mapping):
            raise SeriesMediaError("completed H3 job has no saved video output")
        attempt_number = int(attempt["number"])
        artifact_index = {
            str(item["id"]): item
            for item in document.get("artifacts") or []
            if isinstance(item, Mapping) and item.get("id")
        }
        output_artifact = next(
            (
                artifact_index[artifact_id]
                for artifact_id in attempt.get("artifact_ids") or []
                if artifact_id in artifact_index
                and artifact_index[artifact_id].get("kind") == "shot"
            ),
            None,
        )
        if not isinstance(output_artifact, Mapping):
            output_artifact = {
                "id": str(uuid.uuid4()),
                "kind": "shot",
                "label": f"Shot {shot_index + 1}, attempt {attempt_number}",
                "storage": "output",
                "locator": dict(video_locator),
                "mime": mimetypes.guess_type(str(video_locator["filename"]))[0]
                or "video/mp4",
                "download_name": str(video_locator["filename"]),
                "metadata": {"validation": "pending"},
            }

            def attach_output(document: dict[str, Any], status: str):
                item = document["shots"][shot_index]
                current = item["attempts"][-1]
                if current["job_id"] != job["id"]:
                    raise SeriesStoreValidationError(
                        "active series attempt changed before validation"
                    )
                document["artifacts"].append(output_artifact)
                current["artifact_ids"].append(output_artifact["id"])
                return document, status

            self.store.mutate(series_id, attach_output)
        output_artifact_id = str(output_artifact["id"])
        source = self.media.output_path(video_locator)
        expected_frames = aligned_frame_count(float(shot["duration"]))

        def validating(document: dict[str, Any], status: str):
            item = document["shots"][shot_index]
            item["status"] = "validating"
            item["attempts"][-1]["status"] = "validating"
            return document, status

        self.store.mutate(series_id, validating)
        metadata = await self.media.validate_video(
            source,
            width=int(document["settings"]["width"]),
            height=int(document["settings"]["height"]),
            expected_frames=expected_frames,
        )
        metadata = {**metadata, "validation": "passed"}

        def record_validation(document: dict[str, Any], status: str):
            artifact = next(
                (
                    artifact
                    for artifact in document["artifacts"]
                    if artifact.get("id") == output_artifact_id
                ),
                None,
            )
            if artifact is None:
                raise SeriesStoreValidationError("preserved shot output disappeared")
            artifact["metadata"] = metadata
            return document, status

        self.store.mutate(series_id, record_validation)
        derived: list[dict[str, Any]] = []
        continuity_input: dict[str, Any] | None = None
        seconds = int(document["settings"]["continuity_seconds"])
        if seconds and shot_index + 1 < len(document["shots"]):
            tail, frame = await self.media.make_continuity(
                series_id,
                shot_index=shot_index,
                attempt_number=attempt_number,
                source=source,
                source_duration=float(metadata["duration"]),
                source_frames=int(metadata["frames"]),
                seconds=seconds,
                width=int(document["settings"]["width"]),
                height=int(document["settings"]["height"]),
            )
            derived.extend([tail, frame])
            tail_path = self.media.artifact_path(series_id, str(tail["relative"]))
            with tail_path.open("rb") as handle:
                uploaded = await self.client.upload(
                    fileobj=handle,
                    filename=str(tail["download_name"]),
                    content_type="video/mp4",
                    subfolder=f"h3-webapp/series/{series_id}",
                )
            frame_path = self.media.artifact_path(series_id, str(frame["relative"]))
            with frame_path.open("rb") as handle:
                uploaded_frame = await self.client.upload(
                    fileobj=handle,
                    filename=str(frame["download_name"]),
                    content_type="image/png",
                    subfolder=f"h3-webapp/series/{series_id}",
                )
            continuity_input = {
                "video_path": str(uploaded["path"]),
                "video_name": str(tail["download_name"]),
                "video_sha256": str(
                    (tail.get("metadata") or {}).get("sha256") or ""
                ),
                "image_path": str(uploaded_frame["path"]),
                "image_name": str(frame["download_name"]),
                "image_sha256": str(
                    (frame.get("metadata") or {}).get("sha256") or ""
                ),
            }

        def accepted(document: dict[str, Any], status: str):
            item = document["shots"][shot_index]
            current = item["attempts"][-1]
            if current["job_id"] != job["id"]:
                raise SeriesStoreValidationError("active series attempt changed during validation")
            artifact = next(
                (
                    artifact
                    for artifact in document["artifacts"]
                    if artifact.get("id") == output_artifact_id
                ),
                None,
            )
            if artifact is None:
                raise SeriesStoreValidationError("preserved shot output disappeared")
            artifact["metadata"] = metadata
            document["artifacts"].extend(derived)
            current["status"] = "completed"
            current["error"] = None
            item["continuity_artifacts"] = [artifact["id"] for artifact in derived]
            item["continuity_input"] = continuity_input
            item["status"] = "completed"
            item["accepted_attempt"] = attempt_number
            item["error"] = None
            document["active_shot"] = None
            document["error"] = None
            if document.get("cancel_requested"):
                document["cancel_requested"] = False
                document["pause_requested"] = False
                document["error"] = "Cancelled after preserving the completed shot"
                return document, "cancelled"
            if document.get("pause_requested") or (
                not document["settings"].get("advance", True)
                and shot_index + 1 < len(document["shots"])
            ):
                document["pause_requested"] = False
                return document, "paused"
            return document, "running"

        self.store.mutate(series_id, accepted)

    async def _finalize_series(
        self,
        series_id: str,
        document: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> bool:
        async with self.submission_lock:
            current = self.store.get(series_id)
            if current is None:
                raise SeriesNotFoundError("series not found")
            if (
                int(current["revision"]) != expected_revision
                or current["status"] not in {"running", "queued", "stitching"}
                or current["document"].get("active_shot") is not None
                or not all(
                    shot.get("status") == "completed"
                    and shot.get("accepted_attempt") is not None
                    for shot in current["document"].get("shots") or []
                )
            ):
                return True
            stitching_record = self.store.mutate(
                series_id, lambda current, _: (current, "stitching")
            )
        document = stitching_record["document"]
        stitching_revision = int(stitching_record["revision"])
        accepted: list[dict[str, Any]] = []
        artifact_index = {
            str(item["id"]): item for item in document["artifacts"] if isinstance(item, Mapping)
        }
        for shot in document["shots"]:
            accepted_number = shot.get("accepted_attempt")
            attempt = next(
                (item for item in shot["attempts"] if item["number"] == accepted_number), None
            )
            if not isinstance(attempt, Mapping):
                raise SeriesMediaError("series has a shot without an accepted attempt")
            artifact = next(
                (
                    artifact_index[artifact_id]
                    for artifact_id in attempt.get("artifact_ids") or []
                    if artifact_id in artifact_index and artifact_index[artifact_id].get("kind") == "shot"
                ),
                None,
            )
            if not isinstance(artifact, Mapping):
                raise SeriesMediaError("accepted shot output is missing")
            accepted.append(
                {
                    "shot_index": shot["index"],
                    "attempt": attempt["number"],
                    "job_id": attempt["job_id"],
                    "locator": artifact["locator"],
                    "metadata": artifact["metadata"],
                }
            )
        final, manifest = await self.media.stitch(
            series_id,
            title=str(document["title"]),
            shots=accepted,
            width=int(document["settings"]["width"]),
            height=int(document["settings"]["height"]),
        )

        async with self.submission_lock:
            current = self.store.get(series_id)
            if current is None:
                raise SeriesNotFoundError("series not found")
            if (
                int(current["revision"]) != stitching_revision
                or current["status"] != "stitching"
                or current["document"].get("active_shot") is not None
                or not all(
                    shot.get("status") == "completed"
                    and shot.get("accepted_attempt") is not None
                    for shot in current["document"].get("shots") or []
                )
            ):
                return True

            def complete(document: dict[str, Any], _: str):
                document["artifacts"].extend([final, manifest])
                document["error"] = None
                return document, "completed"

            self.store.mutate(series_id, complete)
        return True

    async def _finish_cancel(
        self, series_id: str, document: Mapping[str, Any]
    ) -> bool:
        completed: tuple[int, dict[str, Any]] | None = None

        def mark_cancelled_if_active(job_id: str, *, error: str | None = None):
            latest = self.jobs.get(job_id)
            if latest is None or latest["status"] not in ACTIVE_STATUSES:
                return latest
            return self.jobs.update(job_id, "cancelled", error=error)

        async with self.submission_lock:
            current = self.store.get(series_id)
            if current is None:
                raise SeriesNotFoundError("series not found")
            document = current["document"]
            active = document.get("active_shot")
            if not isinstance(active, int):
                return False
            attempts = document["shots"][active].get("attempts") or []
            if not attempts:
                return False
            job_id = str(attempts[-1]["job_id"])
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job["status"] in ACTIVE_STATUSES:
                if self.runtime_check is not None:
                    await self.runtime_check()
                try:
                    result = await self.client.cancel(job_id)
                except ComfyError as exc:
                    if exc.status != 404:
                        return False
                    job = mark_cancelled_if_active(
                        job_id,
                        error="The local engine no longer had this job.",
                    )
                else:
                    if result.get("cancelled") is True:
                        job = mark_cancelled_if_active(job_id)
                    else:
                        try:
                            upstream = await self.client.get_job(job_id)
                        except ComfyError as exc:
                            if exc.status != 404:
                                return False
                            job = mark_cancelled_if_active(
                                job_id,
                                error="The local engine no longer had this job.",
                            )
                        else:
                            job = self._sync_job(job_id, upstream)
                            if job["status"] in ACTIVE_STATUSES:
                                return False
            if job is None or job["status"] in ACTIVE_STATUSES:
                return False

            # A cancel request can lose the race with a render that has just
            # completed.  Preserve and validate that expensive output before
            # terminalizing; _accept_completed observes cancel_requested and
            # ends the series without advancing to another shot.
            if job["status"] == "completed":
                completed = (active, job)
            else:

                def cancelled(document: dict[str, Any], _: str):
                    active = document.get("active_shot")
                    if isinstance(active, int):
                        item = document["shots"][active]
                        item["status"] = "cancelled"
                        item["error"] = "Cancelled by user"
                        if item.get("attempts"):
                            item["attempts"][-1]["status"] = "cancelled"
                            item["attempts"][-1]["error"] = "Cancelled by user"
                    document["active_shot"] = None
                    document["cancel_requested"] = False
                    document["pause_requested"] = False
                    document["error"] = "Cancelled by user"
                    return document, "cancelled"

                self.store.mutate(series_id, cancelled)
        if completed is not None:
            await self._accept_completed(series_id, completed[0], completed[1])
        return True

    async def cancel(self, series_id: str) -> dict[str, Any]:
        def request_cancel(document: dict[str, Any], status: str):
            if status not in {"running", "pausing", "cancelling"} or not isinstance(
                document.get("active_shot"), int
            ):
                raise RequestError("series has no active shot to cancel")
            document["cancel_requested"] = True
            document["pause_requested"] = False
            return document, "cancelling"

        async with self.submission_lock:
            record = self.store.mutate(series_id, request_cancel)
        self.wake()
        return record

    async def pause(self, series_id: str) -> dict[str, Any]:
        def request_pause(document: dict[str, Any], status: str):
            if status not in {"queued", "waiting", "running", "pausing"}:
                raise RequestError("series is not running")
            active = isinstance(document.get("active_shot"), int)
            document["pause_requested"] = active
            return document, "pausing" if active else "paused"

        async with self.submission_lock:
            record = self.store.mutate(series_id, request_pause)
        self.wake()
        return record

    def resume(self, series_id: str) -> dict[str, Any]:
        def resume_series(document: dict[str, Any], status: str):
            if status != "paused":
                raise RequestError("only a paused series can resume")
            document["pause_requested"] = False
            document["cancel_requested"] = False
            document["error"] = None
            return document, "queued"

        record = self.store.mutate(series_id, resume_series)
        self.wake()
        return record

    async def set_shot_reference_policy(
        self,
        series_id: str,
        shot_index: int,
        *,
        omit_shared_image_labels: Any,
    ) -> dict[str, Any]:
        """Change only the references used by a future attempt of one stopped shot."""

        if isinstance(shot_index, bool) or not isinstance(shot_index, int):
            raise RequestError("shot_index must be an integer")

        def set_policy(document: dict[str, Any], status: str):
            if status not in REFERENCE_POLICY_STATES:
                raise RequestError(
                    "series must be ready, paused, or stopped before reference policy can change"
                )
            shots = document["shots"]
            if not 0 <= shot_index < len(shots):
                raise RequestError("shot_index is out of range")
            if isinstance(document.get("active_shot"), int):
                raise RequestError("cannot change reference policy while a render is active")
            omissions = _validated_image_omissions(
                omit_shared_image_labels,
                document["references"],
                template=str(document["template"]),
                shot_index=shot_index,
            )
            shot = shots[shot_index]
            _, picture_tag_map = _picture_reference_layout(
                document["references"],
                omitted_labels=omissions,
                scene_reference=(
                    shot.get("scene_reference")
                    if isinstance(shot.get("scene_reference"), Mapping)
                    else None
                ),
                include_continuity=(
                    shot_index > 0
                    and bool(document["settings"]["continuity_seconds"])
                ),
            )
            authored = f"{document.get('brief') or ''}\n{shot['prompt']}"
            for match in REFERENCE_TAG_PATTERN.finditer(authored):
                if (
                    match.group(1).lower() == "picture"
                    and int(match.group(2)) not in picture_tag_map
                ):
                    raise RequestError(
                        f"Shot {shot_index + 1} uses {match.group(0)} but its reference policy omits it"
                    )
            shot["omit_shared_image_labels"] = omissions
            return document, status

        async with self.submission_lock:
            return self.store.mutate(series_id, set_policy)

    async def retry(
        self,
        series_id: str,
        shot_index: int,
        *,
        regenerate_following: bool = False,
    ) -> dict[str, Any]:
        if isinstance(shot_index, bool) or not isinstance(shot_index, int):
            raise RequestError("shot_index must be an integer")
        if not isinstance(regenerate_following, bool):
            raise RequestError("regenerate_following must be true or false")

        def retry_shot(document: dict[str, Any], status: str):
            if status not in {"paused", "failed", "cancelled", "completed"}:
                raise RequestError("series must be paused or stopped before a shot can retry")
            shots = document["shots"]
            if not 0 <= shot_index < len(shots):
                raise RequestError("shot_index is out of range")
            if isinstance(document.get("active_shot"), int):
                raise RequestError("cannot retry while a render is active")
            target = shots[shot_index]
            if target["status"] == "failed" and not regenerate_following and target.get("attempts"):
                latest = target["attempts"][-1]
                latest_artifacts = set(latest.get("artifact_ids") or [])
                attached_shots = [
                    artifact
                    for artifact in document.get("artifacts") or []
                    if artifact.get("id") in latest_artifacts
                    and artifact.get("kind") == "shot"
                ]
                reusable_artifact = any(
                    (artifact.get("metadata") or {}).get("validation") == "passed"
                    for artifact in attached_shots
                )
                latest_job = self.jobs.get(str(latest["job_id"]))
                recovered_unattached_output = (
                    not attached_shots
                    and isinstance(latest_job, Mapping)
                    and _video_output(latest_job) is not None
                )
                if (
                    isinstance(latest_job, Mapping)
                    and latest_job.get("status") == "completed"
                    and (reusable_artifact or recovered_unattached_output)
                ):
                    target["status"] = "validating"
                    target["error"] = None
                    latest["status"] = "validating"
                    latest["error"] = None
                    document["active_shot"] = shot_index
                    document["pause_requested"] = False
                    document["cancel_requested"] = False
                    document["error"] = None
                    return document, "running"
            accepted_later = any(
                item.get("accepted_attempt") is not None for item in shots[shot_index + 1 :]
            )
            if target["status"] == "completed" and accepted_later and not regenerate_following:
                raise RequestError("regenerate_following is required because later shots depend on this shot")
            if target["status"] == "completed" and not regenerate_following and shot_index + 1 < len(shots):
                raise RequestError("regenerate_following is required for an accepted non-final shot")
            if target["status"] not in {"completed", "failed", "cancelled"}:
                raise RequestError("shot is not ready to retry")
            end = len(shots) if regenerate_following else shot_index + 1
            affected_job_ids: set[str] = set()
            for item in shots[shot_index:end]:
                if item.get("accepted_attempt") is not None:
                    for attempt in item["attempts"]:
                        if attempt["number"] == item["accepted_attempt"]:
                            attempt["superseded"] = True
                            affected_job_ids.add(str(attempt["job_id"]))
                item["status"] = "pending"
                item["accepted_attempt"] = None
                item["continuity_input"] = None
                item["continuity_artifacts"] = []
                item["error"] = None
            for artifact in document["artifacts"]:
                if artifact.get("kind") in {"final", "manifest"}:
                    artifact["superseded"] = True
                if artifact.get("kind") == "shot":
                    locator_job = next(
                        (
                            attempt["job_id"]
                            for item in shots
                            for attempt in item["attempts"]
                            if artifact.get("id") in attempt.get("artifact_ids", [])
                        ),
                        None,
                    )
                    if locator_job in affected_job_ids:
                        artifact["superseded"] = True
            document["active_shot"] = None
            document["pause_requested"] = False
            document["cancel_requested"] = False
            document["error"] = None
            return document, "queued"

        async with self.submission_lock:
            record = self.store.mutate(series_id, retry_shot)
        self.wake()
        return record

    async def retry_finalization(self, series_id: str) -> dict[str, Any]:
        """Retry only lossless stitching/manifest work from accepted MP4s."""

        def retry_finalization(document: dict[str, Any], status: str):
            if status != "failed" or isinstance(document.get("active_shot"), int):
                raise RequestError("series finalization is not ready to retry")
            shots = document.get("shots") or []
            if not shots or not all(
                shot.get("status") == "completed"
                and shot.get("accepted_attempt") is not None
                for shot in shots
            ):
                raise RequestError("every shot must be accepted before finalization can retry")
            for artifact in document.get("artifacts") or []:
                if artifact.get("kind") in {"final", "manifest"}:
                    artifact["superseded"] = True
            document["error"] = None
            document["pause_requested"] = False
            document["cancel_requested"] = False
            return document, "stitching"

        async with self.submission_lock:
            record = self.store.mutate(series_id, retry_finalization)
        self.wake()
        return record
