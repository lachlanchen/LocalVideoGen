#!/usr/bin/env python3
"""Create ready-to-run MiniMax H3 workflows from ComfyUI's pinned templates."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = PROJECT_ROOT / "workflow_templates" / "templates"

TASKS = {
    "t2v": {
        "template": "video_minimax_h3_t2v.json",
        "family": "fl2va",
        "aspect": "16:9 (Widescreen)",
        "megapixels": 0.98,
        "scheduler": "simple",
    },
    "i2v": {
        "template": "video_minimax_h3_i2v.json",
        "family": "fl2va",
        "aspect": "1:1 (Square)",
        # ResolutionSelector defines 1 MP as 1024**2 pixels. 0.5625 is
        # therefore the exact 768x768 native H3 square canvas.
        "megapixels": 0.5625,
        "scheduler": "simple",
    },
    "r2v": {
        "template": "video_minimax_h3_r2v.json",
        "family": "ref2va",
        "aspect": "16:9 (Widescreen)",
        "megapixels": 0.98,
        "scheduler": "beta",
    },
}

MODEL_NAMES = {
    ("fl2va", "bf16"): "minimax_h3_fl2va_pruned_bf16.safetensors",
    ("fl2va", "int8"): "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    ("ref2va", "bf16"): "minimax_h3_ref2va_pruned_bf16.safetensors",
    ("ref2va", "int8"): "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
}

UPSTREAM_OUTPUT_CLAIM = "Output is up to 2K resolution, 24fps, and up to about 15 seconds."
LOCAL_OUTPUT_NOTE = (
    "Local open-weight output uses a 768-pixel short edge "
    "(up to 1344×768 at 16:9), 24fps, and up to about 15 seconds. "
    "MiniMax's separate 2K regeneration stage is API-only."
)


def objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def set_widget(node: dict[str, Any], index: int, value: Any, named: str | None = None) -> None:
    values = node.get("widgets_values")
    if isinstance(values, list) and len(values) > index:
        values[index] = value
    named_values = node.get("widgets_values_named")
    if named and isinstance(named_values, dict) and named in named_values:
        named_values[named] = value


def set_named_widget(node: dict[str, Any], name: str, value: Any) -> None:
    named_values = node.get("widgets_values_named")
    values = node.get("widgets_values")
    if not isinstance(named_values, dict) or name not in named_values:
        return
    keys = list(named_values)
    named_values[name] = value
    if isinstance(values, list):
        values[keys.index(name)] = value


def replace_strings(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, dict):
        return {key: replace_strings(child, old, new) for key, child in value.items()}
    if isinstance(value, list):
        return [replace_strings(child, old, new) for child in value]
    return value


def configure(
    source: dict[str, Any],
    *,
    family: str,
    precision: str,
    aspect: str,
    megapixels: float,
    steps: int,
    scheduler: str,
    turbo: bool,
    turbo_steps: int,
    max_reference_identity: bool = False,
) -> dict[str, Any]:
    workflow = copy.deepcopy(source)
    model_name = MODEL_NAMES[(family, precision)]
    workflow = replace_strings(workflow, MODEL_NAMES[(family, "int8")], model_name)

    for node in objects(workflow):
        node_type = node.get("type")
        if node_type == "UNETLoader":
            set_widget(node, 0, model_name, "unet_name")
        elif node_type == "ResolutionSelector":
            set_widget(node, 0, aspect, "aspect_ratio")
            set_widget(node, 1, megapixels, "megapixels")
        elif node_type == "BasicScheduler":
            set_widget(node, 0, scheduler, "scheduler")
        elif node_type == "PrimitiveInt":
            values = node.get("widgets_values", [])
            title = node.get("title", "")
            if values and (values[0] == 20 or title == "Int (Full)"):
                set_widget(node, 0, steps, "value")
            elif turbo and values and (values[0] in (4, 6, 8) or "Lightning" in title):
                set_widget(node, 0, turbo_steps, "value")
        elif node_type == "PrimitiveBoolean" and "Enable Lightning LoRA" in node.get("title", ""):
            set_widget(node, 0, turbo, "value")
        elif node_type == "MiniMaxH3ReferenceToVideo" and max_reference_identity:
            set_widget(node, 4, "max", "ref_image_size")

        if node_type == "PrimitiveStringMultiline":
            values = node.get("widgets_values", [])
            if values and isinstance(values[0], str):
                cleaned = values[0].replace(" and <Audio 1> exactly as it is", "")
                set_widget(node, 0, cleaned, "value")
        elif node_type == "MarkdownNote":
            values = node.get("widgets_values", [])
            if values and isinstance(values[0], str):
                corrected = values[0].replace(UPSTREAM_OUTPUT_CLAIM, LOCAL_OUTPUT_NOTE)
                set_widget(node, 0, corrected, "text")

        named_values = node.get("widgets_values_named")
        if isinstance(named_values, dict) and {"unet_name", "clip_name", "vae_name"} <= named_values.keys():
            set_named_widget(node, "unet_name", model_name)
            set_named_widget(node, "value", turbo)
            set_named_widget(node, "value_2", turbo_steps)

    return workflow


def add_dual_gpu_stage_routing(source: dict[str, Any]) -> dict[str, Any]:
    """Keep DiT on GPU 0 and move text/VAE stages to GPU 1."""
    workflow = copy.deepcopy(source)
    definitions = workflow.get("definitions") or {}
    subgraphs = definitions.get("subgraphs", [])
    candidates = [
        graph
        for graph in subgraphs
        if any(node.get("type") == "UNETLoader" for node in graph.get("nodes", []))
    ]
    if len(candidates) == 1:
        graph = candidates[0]
        dictionary_links = True
    elif not candidates and any(node.get("type") == "UNETLoader" for node in workflow.get("nodes", [])):
        graph = workflow
        dictionary_links = False
    else:
        raise ValueError("could not identify one H3 execution graph")
    nodes = graph["nodes"]
    links = graph["links"]

    graph_nodes = [*workflow.get("nodes", []), *nodes]
    next_node_id = max(node.get("id", 0) for node in graph_nodes if isinstance(node.get("id"), int)) + 1
    link_ids = [link[0] for link in workflow.get("links", []) if isinstance(link, list)]
    for subgraph in subgraphs:
        link_ids.extend(link["id"] for link in subgraph.get("links", []))
    next_link_id = max(link_ids) + 1

    def link_value(link: dict[str, Any] | list[Any], key: str) -> Any:
        if isinstance(link, dict):
            return link[key]
        indexes = {
            "id": 0,
            "origin_id": 1,
            "origin_slot": 2,
            "target_id": 3,
            "target_slot": 4,
            "type": 5,
        }
        return link[indexes[key]]

    def new_link(
        link_id: int,
        origin_id: int,
        origin_slot: int,
        target_id: int,
        target_slot: int,
        value_type: str,
    ) -> dict[str, Any] | list[Any]:
        if dictionary_links:
            return {
                "id": link_id,
                "origin_id": origin_id,
                "origin_slot": origin_slot,
                "target_id": target_id,
                "target_slot": target_slot,
                "type": value_type,
            }
        return [link_id, origin_id, origin_slot, target_id, target_slot, value_type]

    def node_by_id(node_id: int) -> dict[str, Any]:
        return next(node for node in nodes if node.get("id") == node_id)

    def route(source_node: dict[str, Any], selector_type: str, value_type: str, device: str) -> None:
        nonlocal next_node_id, next_link_id
        source_id = source_node["id"]
        old_links = [
            link
            for link in links
            if link_value(link, "origin_id") == source_id and link_value(link, "origin_slot") == 0
        ]
        if not old_links:
            raise ValueError(f"{source_node['type']} has no downstream link")

        selector_id = next_node_id
        next_node_id += 1
        input_link_id = next_link_id
        next_link_id += 1
        output_link_ids = list(range(next_link_id, next_link_id + len(old_links)))
        next_link_id += len(old_links)

        source_node["outputs"][0]["links"] = [input_link_id]
        old_ids = {link_value(link, "id") for link in old_links}
        links[:] = [link for link in links if link_value(link, "id") not in old_ids]
        links.append(new_link(input_link_id, source_id, 0, selector_id, 0, value_type))

        for old_link, output_link_id in zip(old_links, output_link_ids, strict=True):
            target_id = link_value(old_link, "target_id")
            target_slot = link_value(old_link, "target_slot")
            target = node_by_id(target_id)
            target["inputs"][target_slot]["link"] = output_link_id
            links.append(new_link(output_link_id, selector_id, 0, target_id, target_slot, value_type))

        x, y = source_node.get("pos", [0, 0])
        selector_label = selector_type.removeprefix("Select").removesuffix("Device")
        nodes.append(
            {
                "id": selector_id,
                "type": selector_type,
                "pos": [x + 680, y],
                "size": [260, 90],
                "flags": {},
                "order": max(node.get("order", 0) for node in nodes) + 1,
                "mode": 0,
                "inputs": [
                    {
                        "localized_name": selector_label.lower(),
                        "name": selector_label.lower(),
                        "type": value_type,
                        "link": input_link_id,
                    },
                    {
                        "localized_name": "device",
                        "name": "device",
                        "type": "COMBO",
                        "widget": {"name": "device"},
                    },
                ],
                "outputs": [
                    {
                        "localized_name": value_type,
                        "name": value_type,
                        "type": value_type,
                        "links": output_link_ids,
                    }
                ],
                "properties": {
                    "cnr_id": "comfy-core",
                    "ver": "0.34.0",
                    "Node name for S&R": selector_type,
                },
                "widgets_values": [device],
                "widgets_values_named": {"device": device},
            }
        )

    unet_loader = next(node for node in nodes if node.get("type") == "UNETLoader")
    clip_loader = next(node for node in nodes if node.get("type") == "CLIPLoader")
    vae_loaders = [node for node in nodes if node.get("type") == "VAELoader"]
    if len(vae_loaders) != 2:
        raise ValueError("expected video and audio VAE loaders")

    route(unet_loader, "SelectModelDevice", "MODEL", "gpu:0")
    route(clip_loader, "SelectCLIPDevice", "CLIP", "gpu:1")
    for vae_loader in vae_loaders:
        route(vae_loader, "SelectVAEDevice", "VAE", "gpu:1")

    workflow["last_node_id"] = max(workflow.get("last_node_id", 0), next_node_id - 1)
    workflow["last_link_id"] = max(workflow.get("last_link_id", 0), next_link_id - 1)
    return workflow


def write_workflow(path: Path, workflow: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    for task, task_config in TASKS.items():
        source_path = TEMPLATE_ROOT / task_config["template"]
        source = json.loads(source_path.read_text(encoding="utf-8"))

        for precision in ("bf16", "int8"):
            quality = configure(
                source,
                family=task_config["family"],
                precision=precision,
                aspect=task_config["aspect"],
                megapixels=task_config["megapixels"],
                steps=25,
                scheduler=task_config["scheduler"],
                turbo=False,
                turbo_steps=8 if task != "r2v" else 4,
            )
            write_workflow(
                PROJECT_ROOT / "workflows" / "quality" / f"h3_{task}_{precision}_25step.json",
                quality,
            )
            write_workflow(
                PROJECT_ROOT / "workflows" / "dual_gpu" / f"h3_{task}_{precision}_25step_dual_stage.json",
                add_dual_gpu_stage_routing(quality),
            )

        preview = configure(
            source,
            family=task_config["family"],
            precision="int8",
            aspect=task_config["aspect"],
            megapixels=0.4,
            steps=20,
            scheduler="simple",
            turbo=True,
            turbo_steps=8 if task != "r2v" else 4,
        )
        write_workflow(
            PROJECT_ROOT / "workflows" / "preview" / f"h3_{task}_int8_turbo.json",
            preview,
        )

    r2v_source = json.loads(
        (TEMPLATE_ROOT / TASKS["r2v"]["template"]).read_text(encoding="utf-8")
    )
    max_identity = configure(
        r2v_source,
        family="ref2va",
        precision="bf16",
        aspect="16:9 (Widescreen)",
        megapixels=0.98,
        steps=25,
        scheduler="beta",
        turbo=False,
        turbo_steps=4,
        max_reference_identity=True,
    )
    write_workflow(
        PROJECT_ROOT / "workflows" / "quality" / "h3_r2v_bf16_25step_max_identity.json",
        max_identity,
    )
    write_workflow(
        PROJECT_ROOT / "workflows" / "dual_gpu" / "h3_r2v_bf16_25step_max_identity_dual_stage.json",
        add_dual_gpu_stage_routing(max_identity),
    )


if __name__ == "__main__":
    main()
