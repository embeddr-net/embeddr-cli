from typing import Any, Dict, List
from embeddr_core.models.workflow import WorkflowArtifactMetadata, WorkflowPort, WorkflowImplementation


def parse_comfy_graph(graph: Dict[str, Any], name_prefix: str = "comfy") -> WorkflowArtifactMetadata:
    """
    Parses a ComfyUI workflow graph and generates a WorkflowArtifactMetadata structure.

    This is a best-effort parser. It identifies potential inputs and outputs 
    but defaults them to 'internal' exposure.
    """

    inputs: Dict[str, WorkflowPort] = {}
    outputs: Dict[str, WorkflowPort] = {}
    side_effects: List[str] = []

    # ComfyUI API format is { "node_id": { "inputs": {...}, "class_type": "..." } }
    # ComfyUI Client Export format is { "nodes": [...], "links": [...], ... }

    # We expect API format for execution, but users might upload Editor format.
    # For now, let's assume we handle the API format (what is sent to /prompt).
    # If it has "nodes" key, it's likely the full editor state.

    is_api_format = "nodes" not in graph and any(isinstance(
        v, dict) and "class_type" in v for v in graph.values())

    # TODO: Handle Editor format conversion if necessary.
    # For now, we wrap whatever we get.

    if is_api_format:
        for node_id, node in graph.items():
            class_type = node.get("class_type", "")

            # Detect Inputs
            # Heuristic: KSampler seed, steps, cfg are common inputs
            if class_type == "KSampler":
                inputs[f"{node_id}_seed"] = WorkflowPort(
                    name="seed",
                    type="number",
                    default=node.get("inputs", {}).get("seed"),
                    exposure="internal",
                    group="Sampling"
                )
                inputs[f"{node_id}_steps"] = WorkflowPort(
                    name="steps",
                    type="number",
                    default=node.get("inputs", {}).get("steps", 20),
                    exposure="internal",
                    group="Sampling"
                )
                inputs[f"{node_id}_cfg"] = WorkflowPort(
                    name="cfg",
                    type="number",
                    default=node.get("inputs", {}).get("cfg", 8.0),
                    exposure="internal",
                    group="Sampling"
                )
                side_effects.append("gpu.compute")

            # Checkpoint Loader
            elif class_type == "CheckpointLoaderSimple":
                inputs[f"{node_id}_ckpt"] = WorkflowPort(
                    name="checkpoint",
                    type="string",  # enum actually
                    default=node.get("inputs", {}).get("ckpt_name"),
                    exposure="internal",
                    group="Model"
                )

            # Load Image
            elif class_type == "LoadImage":
                inputs[f"{node_id}_image"] = WorkflowPort(
                    name="image",
                    type="image",
                    default=None,
                    exposure="internal",  # Default to internal until user exposes it
                    widget="file_upload"
                )

            # Detect Outputs
            elif class_type in ["SaveImage", "PreviewImage", "EmbeddrSaveImage"]:
                outputs[f"{node_id}_out"] = WorkflowPort(
                    name="output_image",
                    type="image",
                    exposure="ui"
                )
                if class_type == "SaveImage":
                    side_effects.append("filesystem.write")

    else:
        # Client format (Nodes list)
        # This is harder to parse for 'inputs' as values are in 'widgets_values' potentially
        pass

    return WorkflowArtifactMetadata(
        inputs=inputs,
        outputs=outputs,
        side_effects=list(set(side_effects)),
        implementation=WorkflowImplementation(
            type="comfyui-graph",
            payload=graph
        )
    )
