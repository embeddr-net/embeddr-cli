import base64
import copy
import os
import asyncio
from typing import Dict, Any, Optional
from sqlmodel import select
from embeddr.mcp.instance import mcp
from embeddr.mcp.utils import get_db_session
from embeddr.models.workflow import Workflow
from embeddr.services.comfy import ComfyClient, AsyncComfyClient
from embeddr.services.generation_service import GenerationService


@mcp.tool()
def list_workflows() -> str:
    """List all available ComfyUI workflows."""
    with get_db_session() as session:
        workflows = session.exec(
            select(Workflow).where(Workflow.is_active)).all()
        if not workflows:
            return "No workflows found."

        result = []
        for wf in workflows:
            result.append(
                f"ID: {wf.id} | Name: {wf.name} | Description: {wf.description or 'N/A'}"
            )
        return "\n".join(result)


@mcp.tool()
def get_workflow_details(workflow_id: int) -> str:
    """Get details of a specific workflow, including exposed inputs."""
    with get_db_session() as session:
        workflow = session.get(Workflow, workflow_id)
        if not workflow:
            return f"Workflow with ID {workflow_id} not found"

        details = [
            f"ID: {workflow.id}",
            f"Name: {workflow.name}",
            f"Description: {workflow.description or 'N/A'}",
            "Exposed Inputs:",
        ]

        # Parse metadata for exposed inputs
        # Structure: List of { "node_id": "...", "field": "...", "label": "...", ... }
        exposed = workflow.meta.get("exposed_inputs", [])
        if not exposed:
            details.append("  None")
        elif isinstance(exposed, list):
            for item in exposed:
                node_id = item.get("node_id", "?")
                field = item.get("field", "?")

                # Filter out internal fields for SaveToFolder
                # We check the node type in the workflow data
                if workflow.data:
                    node = workflow.data.get(str(node_id))
                    if node:
                        class_type = node.get("class_type") or node.get("type")
                        if class_type == "embeddr.SaveToFolder":
                            if field in ["library", "collection"]:
                                continue

                label = item.get("label", field)
                details.append(f"  - Node {node_id}, Input '{field}': {label}")
        elif isinstance(exposed, dict):
            # Fallback for legacy dictionary format
            for node_id, inputs in exposed.items():
                for input_name, info in inputs.items():
                    desc = info.get("description", "No description")
                    details.append(
                        f"  - Node {node_id}, Input '{input_name}': {desc}")

        return "\n".join(details)


@mcp.tool()
async def generate_image(workflow_id: int, inputs: Dict[str, Dict[str, Any]]) -> str:
    """
    Generate an image using a saved ComfyUI workflow.

    Args:
        workflow_id: The ID of the workflow to execute.
        inputs: A dictionary mapping Node IDs to a dictionary of input values.
                Example: { "3": { "seed": 12345, "steps": 20 }, "4": { "text": "A cat" } }
                Use `get_workflow_details` to see which inputs are exposed and their Node IDs.
    """
    with get_db_session() as session:
        service = GenerationService(session)
        workflow = session.get(Workflow, workflow_id)
        if not workflow:
            return f"Workflow with ID {workflow_id} not found"

        # Inject 'mcp' tag to SaveToFolder nodes
        if workflow.data:
            for node_id, node in workflow.data.items():
                class_type = node.get("class_type") or node.get("type")
                if class_type in ["embeddr.SaveToFolder", "SaveImage"]:
                    if node_id not in inputs:
                        inputs[node_id] = {}

                    # Get existing tags from inputs or node defaults
                    current_tags = inputs[node_id].get("tags")
                    if current_tags is None:
                        current_tags = node.get("inputs", {}).get("tags", "")

                    # Append mcp tag
                    new_tags = f"{current_tags}, mcp" if current_tags else "mcp"
                    inputs[node_id]["tags"] = new_tags

        try:
            # 1. Create Generation Record
            gen = await service.create_generation(workflow_id, inputs)

            # 2. Submit to ComfyUI
            gen = await service.submit_generation(gen.id)

            if gen.status == "failed":
                return f"Generation failed: {gen.error_message}"

            prompt_id = gen.prompt_id
            if not prompt_id:
                return "Error: No prompt ID returned from submission"

            # 3. Wait for result
            client = AsyncComfyClient()
            try:
                # Wait up to 300 seconds
                history = await client.wait_for_completion(prompt_id, timeout=300)

                if not history:
                    return f"Workflow queued (ID: {prompt_id}), but timed out waiting for completion."
            finally:
                # Ensure we close the client if it has a close method (AsyncComfyClient usually uses httpx which should be closed)
                if hasattr(client, "close"):
                    await client.close()

            # 4. Parse outputs
            outputs = history.get("outputs", {})
            results = ["Generation Complete!"]

            for node_id, output_data in outputs.items():
                if "images" in output_data:
                    for img in output_data["images"]:
                        fname = img.get("filename")
                        subfolder = img.get("subfolder", "")
                        img_type = img.get("type", "output")
                        results.append(
                            f"Generated image: {fname} (Subfolder: {subfolder}, Type: {img_type})")

                if "text" in output_data:
                    results.append(
                        f"Node {node_id} output text: {output_data['text']}")

                if "embeddr_ids" in output_data:
                    ids = output_data["embeddr_ids"]
                    if isinstance(ids, list):
                        for uid in ids:
                            results.append(f"Embeddr Image ID: {uid}")
                    else:
                        results.append(f"Embeddr Image ID: {ids}")
                elif "embeddr_id" in output_data:
                    results.append(
                        f"Embeddr Image ID: {output_data['embeddr_id']}")

            if len(results) == 1:
                return "Workflow completed successfully, but no explicit image outputs were found in history."

            return "\n".join(results)

        except Exception as e:
            return f"Error during generation: {str(e)}"


# @mcp.tool()
# def set_comfyui_url(url: str) -> str:
#     """
#     Set the ComfyUI backend URL for future sessions.
#     This creates or updates a .env file in the current directory.
#     """
#     env_path = ".env"
#     lines = []
#     if os.path.exists(env_path):
#         with open(env_path, "r") as f:
#             lines = f.readlines()

#     new_lines = []
#     found = False
#     for line in lines:
#         if line.startswith("COMFYUI_URL="):
#             new_lines.append(f"COMFYUI_URL={url}\n")
#             found = True
#         else:
#             new_lines.append(line)

#     if not found:
#         if new_lines and not new_lines[-1].endswith("\n"):
#             new_lines.append("\n")
#         new_lines.append(f"COMFYUI_URL={url}\n")

#     with open(env_path, "w") as f:
#         f.writelines(new_lines)

#     refresh_settings()

#     return f"ComfyUI URL set to {url} in .env file."


@mcp.tool()
def upload_image_to_comfy(
    image_base64: str, filename: str, overwrite: bool = False
) -> str:
    """
    Upload an image directly to ComfyUI's input directory.
    Useful for workflows that require an image input (LoadImage node).

    Args:
        image_base64: The base64 encoded image data.
        filename: The filename to save the image as (e.g., "input_image.png").
        overwrite: Whether to overwrite an existing file with the same name.
    """
    try:
        image_bytes = base64.b64decode(image_base64)
        client = ComfyClient()
        if not client.is_available():
            return f"Error: ComfyUI backend is not available at {client.url}"

        result = client.upload_image(image_bytes, filename, overwrite)

        # ComfyUI returns: {"name": "filename.png", "subfolder": "", "type": "input"}
        name = result.get("name")
        subfolder = result.get("subfolder", "")
        type_ = result.get("type", "input")

        return f"Successfully uploaded image to ComfyUI: {name} (subfolder: '{subfolder}', type: '{type_}')"
    except Exception as e:
        return f"Error uploading image to ComfyUI: {str(e)}"


@mcp.tool()
def upload_image_from_path(
    file_path: str, filename: Optional[str] = None, overwrite: bool = False
) -> str:
    """
    Upload an image from a local file path to ComfyUI's input directory.
    This is preferred over base64 upload for local files.

    Args:
        file_path: The absolute path to the local image file.
        filename: Optional filename to save as in ComfyUI. If not provided, uses the basename of file_path.
        overwrite: Whether to overwrite an existing file with the same name.
    """
    if not os.path.exists(file_path):
        return f"Error: File not found at {file_path}"

    try:
        with open(file_path, "rb") as f:
            image_bytes = f.read()

        final_filename = filename or os.path.basename(file_path)

        client = ComfyClient()
        if not client.is_available():
            return f"Error: ComfyUI backend is not available at {client.url}"

        result = client.upload_image(image_bytes, final_filename, overwrite)

        # ComfyUI returns: {"name": "filename.png", "subfolder": "", "type": "input"}
        name = result.get("name")
        subfolder = result.get("subfolder", "")
        type_ = result.get("type", "input")

        return f"Successfully uploaded image to ComfyUI: {name} (subfolder: '{subfolder}', type: '{type_}')"
    except Exception as e:
        return f"Error uploading image from path: {str(e)}"
