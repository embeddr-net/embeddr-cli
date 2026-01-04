import json
import logging
from typing import Dict, Any
from datetime import datetime

from sqlmodel import Session, select

from embeddr.core.config import settings
from embeddr.models.workflow import Workflow

logger = logging.getLogger(__name__)


class WorkflowManager:
    def __init__(self, session: Session):
        self.session = session
        self.workflows_dir = settings.WORKFLOWS_DIR

    def sanitize_filename(self, name: str) -> str:
        """Create a safe filename from the workflow name."""
        safe_name = "".join(
            c for c in name if c.isalnum() or c in (" ", "-", "_")
        ).strip()
        return f"{safe_name}.json"

    def convert_to_embeddr_nodes(self, workflow_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert standard ComfyUI nodes to Embeddr specific nodes.
        Handles both API format (dict of nodes) and Standard format (graph with 'nodes' list).
        """
        import copy

        new_data = copy.deepcopy(workflow_data)

        # Check for Standard format
        if "nodes" in new_data and isinstance(new_data["nodes"], list):
            for node in new_data["nodes"]:
                node_type = node.get("type")

                if node_type == "LoadImage":
                    node["type"] = "embeddr.LoadImage"
                    # LoadImage has [image_name, upload_method]
                    # EmbeddrLoadImage has [image_url]
                    # Reset to match EmbeddrLoadImage signature
                    node["widgets_values"] = [""]

                elif node_type == "SaveImage" or node_type == "embeddr.SaveToFolder":
                    is_conversion = node_type == "SaveImage"
                    node["type"] = "embeddr.SaveToFolder"

                    # Schema: caption, parent_ids, library, collection, tags, save_backup
                    defaults = ["", "", "Default", "None", "", False]

                    if is_conversion:
                        # Reset to defaults
                        node["widgets_values"] = defaults
                    else:
                        # Ensure we have enough values
                        current = node.get("widgets_values", [])
                        if not isinstance(current, list):
                            current = []
                        if len(current) < len(defaults):
                            current.extend(defaults[len(current) :])
                        node["widgets_values"] = current

                    # Rename input 'images' to 'image'
                    if "inputs" in node:
                        for inp in node["inputs"]:
                            if inp["name"] == "images":
                                inp["name"] = "image"

            return new_data

        # API Format
        for node_id, node in new_data.items():
            class_type = node.get("class_type")
            inputs = node.get("inputs", {})

            if class_type == "LoadImage":
                # Convert to EmbeddrLoadImage
                node["class_type"] = "embeddr.LoadImage"

                # Map 'image' (filename) to 'image_url'
                # We keep the value as a placeholder or clear it
                if "image" in inputs:
                    # If it's a link (list), keep it? LoadImage usually doesn't have linked image input
                    # If it's a value, move it to image_url
                    val = inputs.pop("image")
                    if isinstance(val, str):
                        # It's a filename. We can't really use it as a URL directly unless we serve it.
                        # But for now, let's just map it so the input exists.
                        inputs["image_url"] = ""
                    else:
                        inputs["image_url"] = val

                # Remove 'upload' input if present
                if "upload" in inputs:
                    inputs.pop("upload")

            elif class_type == "SaveImage" or class_type == "embeddr.SaveToFolder":
                # Convert to EmbeddrSaveToFolder or ensure defaults
                node["class_type"] = "embeddr.SaveToFolder"

                # Map 'images' to 'image'
                if "images" in inputs:
                    inputs["image"] = inputs.pop("images")

                # Remove filename_prefix as Embeddr handles naming
                if "filename_prefix" in inputs:
                    inputs.pop("filename_prefix")

                # Ensure defaults for all widgets
                if "library" not in inputs:
                    inputs["library"] = "Default"
                if "collection" not in inputs:
                    inputs["collection"] = "None"
                if "caption" not in inputs:
                    inputs["caption"] = ""
                if "tags" not in inputs:
                    inputs["tags"] = ""
                if "save_backup" not in inputs:
                    inputs["save_backup"] = False
                if "parent_ids" not in inputs:
                    inputs["parent_ids"] = ""

        return new_data

    def sync_from_disk(self):
        """
        Read all JSON files from the workflows directory and update the database.
        """
        if not self.workflows_dir.exists():
            logger.warning(f"Workflows directory {self.workflows_dir} does not exist.")
            return

        logger.info(f"Syncing workflows from {self.workflows_dir}")

        # Track seen names to handle deletions if we wanted to (but we won't delete for now)
        seen_names = set()

        for file_path in self.workflows_dir.glob("*.json"):
            try:
                with open(file_path, "r") as f:
                    data = json.load(f)

                # Determine format
                name = file_path.stem
                workflow_data = data
                meta = {}
                description = None

                if (
                    isinstance(data, dict)
                    and "data" in data
                    and isinstance(data["data"], dict)
                ):
                    # Embeddr format
                    workflow_data = data["data"]
                    meta = data.get("meta", {})
                    description = data.get("description")
                    if "name" in data:
                        name = data["name"]

                # Auto-convert nodes - DISABLED
                # We want to preserve the original workflow structure in the DB
                # Conversion should happen at runtime in GenerationService
                # workflow_data = self.convert_to_embeddr_nodes(workflow_data)

                # Update DB
                statement = select(Workflow).where(Workflow.name == name)
                existing = self.session.exec(statement).first()

                if existing:
                    existing.data = workflow_data
                    existing.meta = meta
                    existing.description = description
                    existing.updated_at = datetime.utcnow()
                    self.session.add(existing)
                    logger.info(f"Updated workflow: {name}")
                else:
                    new_workflow = Workflow(
                        name=name,
                        data=workflow_data,
                        meta=meta,
                        description=description,
                    )
                    self.session.add(new_workflow)
                    logger.info(f"Created workflow: {name}")

                seen_names.add(name)

            except Exception as e:
                logger.error(f"Failed to load workflow from {file_path}: {e}")

        # Prune workflows that are no longer on disk
        # We only prune workflows that match the naming convention (or all?)
        # For now, let's prune all workflows that were not seen in this sync
        # This assumes the disk is the source of truth

        all_workflows = self.session.exec(select(Workflow)).all()
        for wf in all_workflows:
            if wf.name not in seen_names:
                logger.info(f"Removing workflow '{wf.name}' as it is missing from disk")
                self.session.delete(wf)

        self.session.commit()

    def save_to_disk(self, workflow: Workflow):
        """
        Save a workflow to the workflows directory.
        """
        filename = self.sanitize_filename(workflow.name)
        file_path = self.workflows_dir / filename

        export_data = {
            "name": workflow.name,
            "description": workflow.description,
            "data": workflow.data,
            "meta": workflow.meta,
            "version": "1.0",
            "updated_at": workflow.updated_at.isoformat()
            if workflow.updated_at
            else None,
        }

        with open(file_path, "w") as f:
            json.dump(export_data, f, indent=2)

        logger.info(f"Saved workflow to {file_path}")

    def delete_from_disk(self, workflow: Workflow):
        """
        Delete a workflow file from disk.
        """
        filename = self.sanitize_filename(workflow.name)
        file_path = self.workflows_dir / filename
        if file_path.exists():
            file_path.unlink()
            logger.info(f"Deleted workflow file {file_path}")
