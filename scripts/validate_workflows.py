#!/usr/bin/env python3
"""Check generated MiniMax H3 workflow graph integrity and preset semantics."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_ROOT = PROJECT_ROOT / "workflows"

MODEL_NAMES = {
    ("fl2va", "bf16"): "minimax_h3_fl2va_pruned_bf16.safetensors",
    ("fl2va", "int8"): "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    ("ref2va", "bf16"): "minimax_h3_ref2va_pruned_bf16.safetensors",
    ("ref2va", "int8"): "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
}

TURBO_LORAS = {
    "fl2va": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    "ref2va": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
}

ASPECT_RATIOS = {
    "1:1 (Square)": (1, 1),
    "16:9 (Widescreen)": (16, 9),
}

EXPECTED_PATHS = {
    *(f"quality/h3_{task}_{precision}_25step.json" for task in ("t2v", "i2v", "r2v") for precision in ("bf16", "int8")),
    *(f"dual_gpu/h3_{task}_{precision}_25step_dual_stage.json" for task in ("t2v", "i2v", "r2v") for precision in ("bf16", "int8")),
    *(f"preview/h3_{task}_int8_turbo.json" for task in ("t2v", "i2v", "r2v")),
    "quality/h3_r2v_bf16_25step_max_identity.json",
    "dual_gpu/h3_r2v_bf16_25step_max_identity_dual_stage.json",
}


class WorkflowValidationError(RuntimeError):
    pass


def require(condition: bool, path: Path, message: str) -> None:
    if not condition:
        raise WorkflowValidationError(f"{path}: {message}")


def graph_scopes(workflow: dict[str, Any]):
    yield "root", workflow
    for graph in (workflow.get("definitions") or {}).get("subgraphs", []):
        yield f"subgraph {graph.get('id', 'unknown')}", graph


def link_field(link: dict[str, Any] | list[Any], field: str) -> Any:
    if isinstance(link, dict):
        return link[field]
    indexes = {
        "id": 0,
        "origin_id": 1,
        "origin_slot": 2,
        "target_id": 3,
        "target_slot": 4,
        "type": 5,
    }
    return link[indexes[field]]


def node_map(graph: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {node["id"]: node for node in graph["nodes"]}


def link_map(graph: dict[str, Any]) -> dict[int, dict[str, Any] | list[Any]]:
    return {link_field(link, "id"): link for link in graph["links"]}


def socket_accepts(socket_type: str, link_type: str) -> bool:
    return link_type in {part.strip() for part in socket_type.split(",")}


def validate_links(path: Path, scope: str, graph: dict[str, Any]) -> list[int]:
    nodes = graph.get("nodes")
    links = graph.get("links")
    require(isinstance(nodes, list), path, f"{scope}: nodes is not a list")
    require(isinstance(links, list), path, f"{scope}: links is not a list")

    node_ids = [node.get("id") for node in nodes]
    require(len(node_ids) == len(set(node_ids)), path, f"{scope}: duplicate node ID")
    nodes_by_id = node_map(graph)

    link_ids = [link_field(link, "id") for link in links]
    require(len(link_ids) == len(set(link_ids)), path, f"{scope}: duplicate link ID")
    links_by_id = link_map(graph)
    boundary_inputs = graph.get("inputs", [])
    boundary_outputs = graph.get("outputs", [])

    for link in links:
        link_id = link_field(link, "id")
        origin_id = link_field(link, "origin_id")
        origin_slot = link_field(link, "origin_slot")
        target_id = link_field(link, "target_id")
        target_slot = link_field(link, "target_slot")
        value_type = link_field(link, "type")

        if origin_id == -10:
            require(
                isinstance(origin_slot, int) and 0 <= origin_slot < len(boundary_inputs),
                path,
                f"{scope}: link {link_id} has invalid graph-input slot {origin_slot}",
            )
            boundary = boundary_inputs[origin_slot]
            require(link_id in boundary.get("linkIds", []), path, f"{scope}: link {link_id} absent from graph input")
            require(socket_accepts(boundary["type"], value_type), path, f"{scope}: link {link_id} graph-input type mismatch")
        else:
            require(origin_id >= 0 and origin_id in nodes_by_id, path, f"{scope}: link {link_id} has unknown origin {origin_id}")
            outputs = nodes_by_id[origin_id].get("outputs", [])
            require(isinstance(origin_slot, int) and 0 <= origin_slot < len(outputs), path, f"{scope}: link {link_id} has invalid origin slot")
            output = outputs[origin_slot]
            require(link_id in (output.get("links") or []), path, f"{scope}: link {link_id} absent from origin output")
            require(socket_accepts(output["type"], value_type), path, f"{scope}: link {link_id} origin type mismatch")

        if target_id == -20:
            require(
                isinstance(target_slot, int) and 0 <= target_slot < len(boundary_outputs),
                path,
                f"{scope}: link {link_id} has invalid graph-output slot {target_slot}",
            )
            boundary = boundary_outputs[target_slot]
            require(link_id in boundary.get("linkIds", []), path, f"{scope}: link {link_id} absent from graph output")
            require(socket_accepts(boundary["type"], value_type), path, f"{scope}: link {link_id} graph-output type mismatch")
        else:
            require(target_id >= 0 and target_id in nodes_by_id, path, f"{scope}: link {link_id} has unknown target {target_id}")
            inputs = nodes_by_id[target_id].get("inputs", [])
            require(isinstance(target_slot, int) and 0 <= target_slot < len(inputs), path, f"{scope}: link {link_id} has invalid target slot")
            target_input = inputs[target_slot]
            require(target_input.get("link") == link_id, path, f"{scope}: link {link_id} absent from target input")
            require(socket_accepts(target_input["type"], value_type), path, f"{scope}: link {link_id} target type mismatch")

    for node in nodes:
        for slot, node_input in enumerate(node.get("inputs", [])):
            link_id = node_input.get("link")
            if link_id is None:
                continue
            require(link_id in links_by_id, path, f"{scope}: node {node['id']} input {slot} has stale link {link_id}")
            link = links_by_id[link_id]
            require(
                (link_field(link, "target_id"), link_field(link, "target_slot")) == (node["id"], slot),
                path,
                f"{scope}: node {node['id']} input {slot} endpoint mismatch",
            )
        for slot, output in enumerate(node.get("outputs", [])):
            for link_id in output.get("links") or []:
                require(link_id in links_by_id, path, f"{scope}: node {node['id']} output {slot} has stale link {link_id}")
                link = links_by_id[link_id]
                require(
                    (link_field(link, "origin_id"), link_field(link, "origin_slot")) == (node["id"], slot),
                    path,
                    f"{scope}: node {node['id']} output {slot} endpoint mismatch",
                )

    for slot, boundary in enumerate(boundary_inputs):
        for link_id in boundary.get("linkIds", []):
            require(link_id in links_by_id, path, f"{scope}: graph input {slot} has stale link {link_id}")
            link = links_by_id[link_id]
            require(
                (link_field(link, "origin_id"), link_field(link, "origin_slot")) == (-10, slot),
                path,
                f"{scope}: graph input {slot} endpoint mismatch",
            )
    for slot, boundary in enumerate(boundary_outputs):
        for link_id in boundary.get("linkIds", []):
            require(link_id in links_by_id, path, f"{scope}: graph output {slot} has stale link {link_id}")
            link = links_by_id[link_id]
            require(
                (link_field(link, "target_id"), link_field(link, "target_slot")) == (-20, slot),
                path,
                f"{scope}: graph output {slot} endpoint mismatch",
            )
    return link_ids


def validate_workflow_structure(path: Path, workflow: dict[str, Any]) -> None:
    require(workflow.get("version") == 0.4, path, "unsupported workflow version")
    all_link_ids: list[int] = []
    all_numeric_node_ids: list[int] = []
    for scope, graph in graph_scopes(workflow):
        all_link_ids.extend(validate_links(path, scope, graph))
        all_numeric_node_ids.extend(node["id"] for node in graph["nodes"] if isinstance(node.get("id"), int))
        for node in graph["nodes"]:
            values = node.get("widgets_values")
            named = node.get("widgets_values_named")
            if isinstance(values, list) and isinstance(named, dict):
                require(values == list(named.values()), path, f"{scope}: node {node['id']} list/named widget mismatch")

    require(len(all_link_ids) == len(set(all_link_ids)), path, "link ID reused across graph scopes")
    require(workflow.get("last_node_id", -1) >= max(all_numeric_node_ids), path, "last_node_id is stale")
    require(workflow.get("last_link_id", -1) >= max(all_link_ids), path, "last_link_id is stale")

    definitions = {
        graph["id"]: graph for graph in (workflow.get("definitions") or {}).get("subgraphs", [])
    }
    hosts = [node for node in workflow["nodes"] if node.get("type") in definitions]
    require(len(hosts) == len(definitions), path, "subgraph definition/host mismatch")
    for host in hosts:
        definition = definitions[host["type"]]
        require(len(host.get("outputs", [])) == len(definition.get("outputs", [])), path, f"subgraph host {host['id']} output mismatch")


def nodes_of_type(graph: dict[str, Any], node_type: str) -> list[dict[str, Any]]:
    return [node for node in graph["nodes"] if node.get("type") == node_type]


def exactly_one(path: Path, graph: dict[str, Any], node_type: str) -> dict[str, Any]:
    matches = nodes_of_type(graph, node_type)
    require(len(matches) == 1, path, f"expected one {node_type}, found {len(matches)}")
    return matches[0]


def execution_graph(path: Path, workflow: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        graph for _, graph in graph_scopes(workflow)
        if any(node.get("type") == "UNETLoader" for node in graph["nodes"])
    ]
    require(len(candidates) == 1, path, f"expected one H3 execution graph, found {len(candidates)}")
    return candidates[0]


def input_source(
    path: Path,
    graph: dict[str, Any],
    node: dict[str, Any],
    input_name: str,
) -> tuple[int, int]:
    slots = [slot for slot, item in enumerate(node.get("inputs", [])) if item.get("name") == input_name]
    require(len(slots) == 1, path, f"node {node['id']} expected one input named {input_name}")
    link_id = node["inputs"][slots[0]].get("link")
    require(link_id is not None, path, f"node {node['id']} input {input_name} is not linked")
    link = link_map(graph).get(link_id)
    require(link is not None, path, f"node {node['id']} input {input_name} has missing link")
    return link_field(link, "origin_id"), link_field(link, "origin_slot")


def host_for_graph(path: Path, workflow: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any] | None:
    if graph is workflow:
        return None
    matches = [node for node in workflow["nodes"] if node.get("type") == graph.get("id")]
    require(len(matches) == 1, path, "execution subgraph does not have exactly one host")
    return matches[0]


def effective_widget(
    path: Path,
    workflow: dict[str, Any],
    graph: dict[str, Any],
    node: dict[str, Any],
    widget_name: str,
) -> Any:
    named = node.get("widgets_values_named", {})
    require(widget_name in named, path, f"node {node['id']} lacks widget {widget_name}")
    matching_inputs = [item for item in node.get("inputs", []) if item.get("name") == widget_name]
    if not matching_inputs or matching_inputs[0].get("link") is None:
        return named[widget_name]

    link = link_map(graph)[matching_inputs[0]["link"]]
    origin_id = link_field(link, "origin_id")
    if origin_id != -10:
        return named[widget_name]
    boundary_slot = link_field(link, "origin_slot")
    require(0 <= boundary_slot < len(graph.get("inputs", [])), path, f"node {node['id']} has invalid boundary widget link")
    boundary_name = graph["inputs"][boundary_slot]["name"]
    host = host_for_graph(path, workflow, graph)
    require(host is not None, path, f"node {node['id']} boundary widget has no host")
    host_named = host.get("widgets_values_named", {})
    require(boundary_name in host_named, path, f"subgraph host lacks promoted widget {boundary_name}")
    return host_named[boundary_name]


def task_for_name(path: Path) -> str:
    for task in ("t2v", "i2v", "r2v"):
        if f"h3_{task}_" in path.name:
            return task
    raise WorkflowValidationError(f"{path}: cannot infer task")


def resolution_dimensions(aspect: str, megapixels: float, multiple: int) -> tuple[int, int]:
    width_ratio, height_ratio = ASPECT_RATIOS[aspect]
    scale = math.sqrt(megapixels * 1024 * 1024 / (width_ratio * height_ratio))
    return (
        round(width_ratio * scale / multiple) * multiple,
        round(height_ratio * scale / multiple) * multiple,
    )


def validate_selector(
    path: Path,
    graph: dict[str, Any],
    selector: dict[str, Any],
    source_type: str,
    value_type: str,
    device: str,
) -> None:
    require(selector.get("widgets_values") == [device], path, f"selector {selector['id']} list widget is malformed")
    require(selector.get("widgets_values_named") == {"device": device}, path, f"selector {selector['id']} named widget is malformed")
    inputs = selector.get("inputs", [])
    outputs = selector.get("outputs", [])
    source_name = value_type.lower()
    require(len(inputs) == 2, path, f"selector {selector['id']} input count is malformed")
    require(inputs[0].get("name") == source_name and inputs[0].get("type") == value_type, path, f"selector {selector['id']} source input is malformed")
    require(inputs[1].get("name") == "device" and inputs[1].get("type") == "COMBO", path, f"selector {selector['id']} device input is malformed")
    require(len(outputs) == 1 and outputs[0].get("type") == value_type, path, f"selector {selector['id']} output is malformed")
    require(selector.get("properties", {}).get("cnr_id") == "comfy-core", path, f"selector {selector['id']} is not serialized as a core node")

    source_id, source_slot = input_source(path, graph, selector, source_name)
    source = node_map(graph)[source_id]
    require(source.get("type") == source_type and source_slot == 0, path, f"selector {selector['id']} is attached to the wrong loader")
    source_targets = {
        link_field(link_map(graph)[link_id], "target_id")
        for link_id in source["outputs"][0].get("links") or []
    }
    require(source_targets == {selector["id"]}, path, f"{source_type} bypasses selector {selector['id']}")
    require(bool(outputs[0].get("links")), path, f"selector {selector['id']} has no downstream consumer")


def validate_semantics(path: Path, workflow: dict[str, Any]) -> None:
    graph = execution_graph(path, workflow)
    graph_nodes = graph["nodes"]
    nodes_by_id = node_map(graph)
    name = path.name
    task = task_for_name(path)
    family = "ref2va" if task == "r2v" else "fl2va"
    precision = "bf16" if "bf16" in name else "int8"
    is_preview = path.parent.name == "preview"
    is_dual = path.parent.name == "dual_gpu"

    note_text = "\n".join(
        node.get("widgets_values_named", {}).get("text", "")
        for _, scope_graph in graph_scopes(workflow)
        for node in scope_graph["nodes"]
        if node.get("type") == "MarkdownNote"
    )
    require("Output is up to 2K resolution" not in note_text, path, "misleading local 2K claim")
    require("MiniMax's separate 2K regeneration stage is API-only" in note_text, path, "local output-limit note missing")

    loader = exactly_one(path, graph, "UNETLoader")
    expected_model = MODEL_NAMES[(family, precision)]
    require(loader["widgets_values_named"].get("unet_name") == expected_model, path, "wrong DiT model")
    require(effective_widget(path, workflow, graph, loader, "unet_name") == expected_model, path, "subgraph host overrides DiT model")

    resolution = exactly_one(path, workflow, "ResolutionSelector")
    expected_aspect = "1:1 (Square)" if task == "i2v" else "16:9 (Widescreen)"
    expected_megapixels = 0.4 if is_preview else (0.5625 if task == "i2v" else 0.98)
    expected_dimensions = (640, 640) if is_preview and task == "i2v" else (
        (864, 480) if is_preview else ((768, 768) if task == "i2v" else (1344, 768))
    )
    resolution_named = resolution["widgets_values_named"]
    require(resolution_named.get("aspect_ratio") == expected_aspect, path, "wrong aspect ratio")
    require(resolution_named.get("megapixels") == expected_megapixels, path, "wrong megapixel target")
    require(resolution_named.get("multiple") == 32, path, "resolution is not aligned to a multiple of 32")
    actual_dimensions = resolution_dimensions(expected_aspect, expected_megapixels, 32)
    require(actual_dimensions == expected_dimensions, path, f"effective resolution is {actual_dimensions}, expected {expected_dimensions}")

    sampler = exactly_one(path, graph, "KSamplerSelect")
    require(sampler["widgets_values_named"].get("sampler_name") == "res_multistep", path, "wrong sampler")
    scheduler = exactly_one(path, graph, "BasicScheduler")
    expected_scheduler = "beta" if task == "r2v" and not is_preview else "simple"
    require(scheduler["widgets_values_named"].get("scheduler") == expected_scheduler, path, "wrong scheduler")

    switches = nodes_of_type(graph, "ComfySwitchNode")
    model_switches = [node for node in switches if "model" in node.get("title", "").lower()]
    step_switches = [node for node in switches if "steps" in node.get("title", "").lower()]
    require(len(model_switches) == 1 and len(step_switches) == 1, path, "Turbo model/step switches are missing")
    model_switch = model_switches[0]
    step_switch = step_switches[0]

    full_id, _ = input_source(path, graph, step_switch, "on_false")
    turbo_steps_id, _ = input_source(path, graph, step_switch, "on_true")
    full_steps = nodes_by_id[full_id]
    turbo_steps = nodes_by_id[turbo_steps_id]
    require(full_steps.get("type") == "PrimitiveInt", path, "full-step switch branch is not a PrimitiveInt")
    require(turbo_steps.get("type") == "PrimitiveInt", path, "Turbo-step switch branch is not a PrimitiveInt")
    require(effective_widget(path, workflow, graph, full_steps, "value") == (20 if is_preview else 25), path, "wrong full-model step count")
    expected_turbo_steps = 4 if task == "r2v" else 8
    require(effective_widget(path, workflow, graph, turbo_steps, "value") == expected_turbo_steps, path, "wrong Turbo step count")
    require(input_source(path, graph, scheduler, "steps")[0] == step_switch["id"], path, "scheduler does not use the Turbo/full step switch")

    turbo_nodes = [
        node for node in graph_nodes
        if node.get("type") == "PrimitiveBoolean" and "Enable Lightning LoRA" in node.get("title", "")
    ]
    require(len(turbo_nodes) == 1, path, "Turbo Boolean is missing")
    turbo = turbo_nodes[0]
    require(effective_widget(path, workflow, graph, turbo, "value") is is_preview, path, "wrong effective Turbo state")
    require(input_source(path, graph, model_switch, "switch")[0] == turbo["id"], path, "Turbo Boolean does not control model switch")
    require(input_source(path, graph, step_switch, "switch")[0] == turbo["id"], path, "Turbo Boolean does not control step switch")

    base_model_source = input_source(path, graph, model_switch, "on_false")
    lora_source = input_source(path, graph, model_switch, "on_true")
    lora = nodes_by_id[lora_source[0]]
    require(lora.get("type") == "LoraLoaderModelOnly", path, "Turbo model branch does not use LoraLoaderModelOnly")
    require(input_source(path, graph, lora, "model") == base_model_source, path, "Turbo LoRA and full branch use different base models")
    require(effective_widget(path, workflow, graph, lora, "lora_name") == TURBO_LORAS[family], path, "wrong Turbo LoRA")
    require(effective_widget(path, workflow, graph, lora, "strength_model") == 1, path, "wrong Turbo LoRA strength")

    guider = exactly_one(path, graph, "BasicGuider")
    require(input_source(path, graph, guider, "model")[0] == model_switch["id"], path, "sampler guider bypasses Turbo/full model switch")
    advanced_sampler = exactly_one(path, graph, "SamplerCustomAdvanced")
    require(input_source(path, graph, advanced_sampler, "sampler")[0] == sampler["id"], path, "sampler selection is bypassed")
    require(input_source(path, graph, advanced_sampler, "sigmas")[0] == scheduler["id"], path, "scheduler is bypassed")
    require(input_source(path, graph, advanced_sampler, "guider")[0] == guider["id"], path, "guider is bypassed")

    selectors = [node for node in graph_nodes if node.get("type", "").startswith("Select")]
    if is_dual:
        model_selectors = nodes_of_type(graph, "SelectModelDevice")
        clip_selectors = nodes_of_type(graph, "SelectCLIPDevice")
        vae_selectors = nodes_of_type(graph, "SelectVAEDevice")
        require(len(model_selectors) == 1 and len(clip_selectors) == 1 and len(vae_selectors) == 2, path, "wrong selector node count")
        validate_selector(path, graph, model_selectors[0], "UNETLoader", "MODEL", "gpu:0")
        validate_selector(path, graph, clip_selectors[0], "CLIPLoader", "CLIP", "gpu:1")
        for selector in vae_selectors:
            validate_selector(path, graph, selector, "VAELoader", "VAE", "gpu:1")
        require(base_model_source[0] == model_selectors[0]["id"], path, "full model branch bypasses GPU 0 selector")
    else:
        require(not selectors, path, "unexpected device selector")
        require(nodes_by_id[base_model_source[0]].get("type") == "UNETLoader", path, "full model branch bypasses UNETLoader")

    reference_nodes = nodes_of_type(graph, "MiniMaxH3ReferenceToVideo")
    if task == "r2v":
        require(len(reference_nodes) == 1, path, "reference-conditioning node is missing")
        expected_reference_size = "max" if "max_identity" in name else "match"
        require(reference_nodes[0]["widgets_values_named"].get("ref_image_size") == expected_reference_size, path, "wrong reference-image sizing mode")
    else:
        require(not reference_nodes, path, "unexpected reference-conditioning node")


def edge_set(graph: dict[str, Any], collapse_selectors: bool = False) -> set[tuple[Any, ...]]:
    edges = {
        tuple(link_field(link, field) for field in ("origin_id", "origin_slot", "target_id", "target_slot", "type"))
        for link in graph["links"]
    }
    if not collapse_selectors:
        return edges
    selector_ids = {
        node["id"] for node in graph["nodes"] if node.get("type", "").startswith("Select")
    }
    for selector_id in selector_ids:
        incoming = [edge for edge in edges if edge[2] == selector_id]
        outgoing = [edge for edge in edges if edge[0] == selector_id]
        if len(incoming) != 1 or not outgoing:
            raise WorkflowValidationError(f"selector {selector_id}: cannot collapse malformed routing")
        source = incoming[0]
        edges.remove(source)
        for edge in outgoing:
            edges.remove(edge)
            edges.add((source[0], source[1], edge[2], edge[3], edge[4]))
    return edges


def validate_dual_topology(
    path: Path,
    workflow: dict[str, Any],
    base_path: Path,
    base_workflow: dict[str, Any],
) -> None:
    dual_scopes = {scope: graph for scope, graph in graph_scopes(workflow)}
    base_scopes = {scope: graph for scope, graph in graph_scopes(base_workflow)}
    require(dual_scopes.keys() == base_scopes.keys(), path, "dual workflow graph scopes differ from quality base")
    for scope in dual_scopes:
        require(
            edge_set(dual_scopes[scope], collapse_selectors=True) == edge_set(base_scopes[scope]),
            path,
            f"{scope}: selector-collapsed topology differs from {base_path.name}",
        )


def main() -> int:
    paths = sorted(WORKFLOW_ROOT.glob("*/*.json"))
    actual_paths = {str(path.relative_to(WORKFLOW_ROOT)) for path in paths}
    if actual_paths != EXPECTED_PATHS:
        missing = sorted(EXPECTED_PATHS - actual_paths)
        extra = sorted(actual_paths - EXPECTED_PATHS)
        print(f"FAIL workflow set mismatch; missing={missing}, extra={extra}")
        return 1

    workflows: dict[Path, dict[str, Any]] = {}
    errors: list[str] = []
    for path in paths:
        try:
            workflows[path] = json.loads(path.read_text(encoding="utf-8"))
            validate_workflow_structure(path, workflows[path])
            validate_semantics(path, workflows[path])
        except (WorkflowValidationError, KeyError, IndexError, TypeError, ValueError) as error:
            errors.append(str(error))

    for path in paths:
        if path.parent.name != "dual_gpu" or path not in workflows:
            continue
        base_path = WORKFLOW_ROOT / "quality" / path.name.replace("_dual_stage", "")
        if base_path not in workflows:
            errors.append(f"{path}: missing quality workflow {base_path.name}")
            continue
        try:
            validate_dual_topology(path, workflows[path], base_path, workflows[base_path])
        except WorkflowValidationError as error:
            errors.append(str(error))

    if errors:
        for error in errors:
            print(f"FAIL {error}")
        print(f"Validation failed with {len(errors)} error(s).")
        return 1

    for path in paths:
        print(f"OK {path.relative_to(PROJECT_ROOT)}")
    print(f"Validated {len(paths)} workflows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
