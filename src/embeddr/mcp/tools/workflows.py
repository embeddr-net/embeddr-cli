import base64
import copy
import os
from typing import Dict, Any, Optional
from sqlmodel import select
from embeddr.mcp.instance import mcp
from embeddr.mcp.utils import get_db_session
from embeddr.models.workflow import Workflow
from embeddr.services.comfy import ComfyClient, AsyncComfyClient


@mcp.tool()
def list_workflows() -> str:
    """List all available ComfyUI workflows."""
    with get_db_session() as session:
        workflows = session.exec(select(Workflow).where(Workflow.is_active)).all()
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
        # Structure: { "exposed_inputs": { "node_id": { "input_name": { "description": "...", "type": "..." } } } }
        exposed = workflow.meta.get("exposed_inputs", {})
        if not exposed:
            details.append("  None")
        else:
            for node_id, inputs in exposed.items():
                for input_name, info in inputs.items():
                    desc = info.get("description", "No description")
                    details.append(f"  - Node {node_id}, Input '{input_name}': {desc}")

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
        workflow = session.get(Workflow, workflow_id)
        if not workflow:
            return f"Workflow with ID {workflow_id} not found"

        # 1. Prepare the workflow data (graph)
        graph = copy.deepcopy(workflow.data)

        # 2. Patch the graph with inputs
        patched_count = 0
        for node_id, node_inputs in inputs.items():
            if node_id not in graph:
                continue

            for input_name, value in node_inputs.items():
                if "inputs" in graph[node_id]:
                    graph[node_id]["inputs"][input_name] = value
                    patched_count += 1

        # 3. Send to ComfyUI
        client = AsyncComfyClient()
        try:
            if not await client.is_available():
                return f"Error: ComfyUI backend is not available at {client.url}"

            try:
                prompt_id = await client.queue_prompt(graph)
            except Exception as e:
                return f"Error queuing workflow: {str(e)}"

            # 4. Wait for result
            history = await client.wait_for_completion(prompt_id, timeout=120)
            if not history:
                return f"Workflow queued (ID: {prompt_id}), but timed out waiting for completion."
        finally:
            await client.close()

        # 5. Parse result
        outputs = history.get("outputs", {})
        results = []

        for node_id, output_data in outputs.items():
            if "images" in output_data:
                for img in output_data["images"]:
                    fname = img.get("filename")
                    results.append(f"Generated image: {fname}")
            if "text" in output_data:
                results.append(f"Node {node_id} output text: {output_data['text']}")

            # Check for custom outputs
            if "embeddr_ids" in output_data:
                ids = output_data["embeddr_ids"]
                if isinstance(ids, list):
                    for uid in ids:
                        results.append(f"Embeddr Image ID: {uid}")
                else:
                    results.append(f"Embeddr Image ID: {ids}")
            elif "embeddr_id" in output_data:
                results.append(f"Embeddr Image ID: {output_data['embeddr_id']}")

        if not results:
            return "Workflow completed successfully, but no explicit image outputs were found in history."

        return "\n".join(results)


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
