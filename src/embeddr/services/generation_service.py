import uuid
import logging
import copy
from datetime import datetime
from typing import Dict, Any

from sqlmodel import Session, select
from embeddr.models.generation import Generation
from embeddr.models.workflow import Workflow
from embeddr.services.comfy import AsyncComfyClient

logger = logging.getLogger(__name__)


class GenerationService:
    def __init__(self, session: Session):
        self.session = session
        self.comfy_client = AsyncComfyClient()

    async def create_generation(
        self, workflow_id: int, inputs: Dict[str, Any]
    ) -> Generation:
        """
        Creates a new generation record in the database.
        Does NOT submit to ComfyUI yet.
        """
        workflow = self.session.get(Workflow, workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")

        generation_id = str(uuid.uuid4())
        generation = Generation(
            id=generation_id,
            workflow_id=workflow_id,
            status="pending",
            prompt=workflow.name,  # Default prompt name
            inputs=inputs,
            created_at=datetime.utcnow(),
        )

        self.session.add(generation)
        self.session.commit()
        self.session.refresh(generation)

        return generation

    async def submit_generation(self, generation_id: str) -> Generation:
        """
        Submits a pending generation to ComfyUI.
        """
        generation = self.session.get(Generation, generation_id)
        if not generation:
            raise ValueError(f"Generation {generation_id} not found")

        if generation.status != "pending":
            # Already submitted or failed
            return generation

        workflow = self.session.get(Workflow, generation.workflow_id)
        if not workflow:
            generation.status = "failed"
            generation.error_message = "Workflow not found"
            self.session.add(generation)
            self.session.commit()
            return generation

        try:
            # 1. Prepare the graph
            graph = await self._prepare_graph(workflow, generation.inputs)

            # 2. Submit to ComfyUI
            if not await self.comfy_client.is_available():
                raise RuntimeError("ComfyUI is not available")

            # Import CLIENT_ID from socket_manager to ensure we receive events for this prompt
            from embeddr.services.socket_manager import CLIENT_ID

            prompt_id = await self.comfy_client.queue_prompt(graph, client_id=CLIENT_ID)

            # 3. Update Generation
            generation.prompt_id = prompt_id
            # Distinct from pending (created) vs queued (in comfy)
            generation.status = "queued"
            self.session.add(generation)
            self.session.commit()
            self.session.refresh(generation)

            # Broadcast generation_submitted event
            from embeddr.services.socket_manager import manager

            await manager.broadcast(
                {
                    "source": "embeddr",
                    "type": "generation_submitted",
                    "data": {
                        "id": generation.id,
                        "prompt_id": prompt_id,
                        "status": "queued",
                    },
                }
            )

            return generation

        except Exception as e:
            logger.error(f"Failed to submit generation {generation_id}: {e}")
            generation.status = "failed"
            generation.error_message = str(e)
            self.session.add(generation)
            self.session.commit()
            self.session.refresh(generation)
            return generation

    def _convert_nodes(
        self, workflow_data: Dict[str, Any], inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert standard ComfyUI nodes to Embeddr specific nodes at runtime.
        Only converts LoadImage if it is targeted by inputs.
        Always converts SaveImage to ensure output capture.
        """
        new_data = copy.deepcopy(workflow_data)

        # API Format (if we ever support direct API format workflows)
        for node_id, node in new_data.items():
            class_type = node.get("class_type")
            node_inputs = node.get("inputs", {})
            node_id_str = str(node_id)

            if class_type == "LoadImage":
                if node_id_str in inputs:
                    node["class_type"] = "embeddr.LoadImage"
                    if "image" in node_inputs:
                        val = node_inputs.pop("image")
                        if isinstance(val, str):
                            node_inputs["image_url"] = ""
                        else:
                            node_inputs["image_url"] = val
                    if "upload" in node_inputs:
                        node_inputs.pop("upload")

            elif class_type == "SaveImage":
                # Automatic conversion disabled to allow raw workflow execution
                pass

            elif class_type == "embeddr.SaveToFolder":
                if "library" not in node_inputs:
                    node_inputs["library"] = "Default"
                if "collection" not in node_inputs:
                    node_inputs["collection"] = "None"
                if "caption" not in node_inputs:
                    node_inputs["caption"] = ""
                if "tags" not in node_inputs:
                    node_inputs["tags"] = ""
                if "save_backup" not in node_inputs:
                    node_inputs["save_backup"] = False
                if "parent_ids" not in node_inputs:
                    node_inputs["parent_ids"] = ""

        return new_data

    async def _prepare_graph(
        self, workflow: Workflow, inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Prepares the ComfyUI graph by patching inputs and converting format.
        """
        # 1. Convert nodes to Embeddr specific ones (runtime only)
        graph = self._convert_nodes(workflow.data, inputs)

        # Patch API format graph
        for node_id, node_inputs in inputs.items():
            if node_id in graph:
                if "inputs" not in graph[node_id]:
                    graph[node_id]["inputs"] = {}
                for k, v in node_inputs.items():
                    graph[node_id]["inputs"][k] = v

        return graph

    async def handle_comfy_event(self, event_type: str, data: Dict[str, Any]):
        """
        Updates generation state based on ComfyUI WebSocket events.
        """
        print(f"[GenService] Handling event: {event_type} Data: {data}")
        if event_type == "execution_start":
            prompt_id = data.get("prompt_id")
            await self._update_status_by_prompt_id(prompt_id, "processing")

        elif event_type == "executed":
            prompt_id = data.get("prompt_id")
            output = data.get("output", {})
            await self._complete_generation(prompt_id, output)

        elif event_type == "execution_error":
            prompt_id = data.get("prompt_id")
            error_msg = data.get("exception_message", "Unknown error")
            await self._fail_generation(prompt_id, error_msg)

    async def _update_status_by_prompt_id(self, prompt_id: str, status: str):
        if not prompt_id:
            return

        statement = select(Generation).where(Generation.prompt_id == prompt_id)
        results = self.session.exec(statement)
        generation = results.first()

        if generation:
            print(
                f"[GenService] Updating generation {generation.id} status to {status}"
            )
            generation.status = status
            self.session.add(generation)
            self.session.commit()
        else:
            print(f"[GenService] No generation found for prompt_id {prompt_id}")

    async def _complete_generation(self, prompt_id: str, output: Dict[str, Any]):
        if not prompt_id:
            return

        statement = select(Generation).where(Generation.prompt_id == prompt_id)
        results = self.session.exec(statement)
        generation = results.first()

        if generation:
            generation.status = "completed"

            # Initialize outputs if None
            current_outputs = list(generation.outputs) if generation.outputs else []

            # Helper to check for duplicates
            def is_duplicate_image(img_list, new_img):
                for existing in img_list:
                    if (
                        existing.get("type") == "image"
                        and existing.get("filename") == new_img.get("filename")
                        and existing.get("subfolder") == new_img.get("subfolder")
                        and existing.get("comfy_type") == new_img.get("comfy_type")
                    ):
                        return True
                return False

            def is_duplicate_id(img_list, new_id_val):
                for existing in img_list:
                    if (
                        existing.get("type") == "embeddr_id"
                        and existing.get("value") == new_id_val
                    ):
                        return True
                return False

            # Normalize and append images
            if "images" in output:
                for img in output["images"]:
                    new_item = {
                        "type": "image",
                        "filename": img.get("filename"),
                        "subfolder": img.get("subfolder", ""),
                        "comfy_type": img.get("type", "output"),
                    }
                    if not is_duplicate_image(current_outputs, new_item):
                        current_outputs.append(new_item)

            # Normalize and append embeddr_ids
            if "embeddr_ids" in output:
                for eid in output["embeddr_ids"]:
                    if not is_duplicate_id(current_outputs, eid):
                        current_outputs.append({"type": "embeddr_id", "value": eid})

            generation.outputs = current_outputs
            self.session.add(generation)
            self.session.commit()

    async def _fail_generation(self, prompt_id: str, error: str):
        if not prompt_id:
            return

        statement = select(Generation).where(Generation.prompt_id == prompt_id)
        results = self.session.exec(statement)
        generation = results.first()

        if generation:
            generation.status = "failed"
            generation.error_message = error
            self.session.add(generation)
            self.session.commit()

    async def rescan_generations(self):
        """
        Scans all generations that are 'queued' or 'processing' or 'completed'
        and tries to fetch their history from ComfyUI to update/repair them.
        """
        # Find generations that might need update
        # We check 'completed' ones too in case they are missing outputs
        statement = select(Generation).where(Generation.prompt_id.is_not(None))
        generations = self.session.exec(statement).all()

        updated_count = 0

        if not await self.comfy_client.is_available():
            logger.warning("ComfyUI not available for rescan")
            return 0

        for gen in generations:
            try:
                # Fetch history
                history = await self.comfy_client.get_history(gen.prompt_id)

                if not history:
                    continue

                # History format: { prompt_id: { "outputs": { ... }, "status": { ... } } }
                if gen.prompt_id in history:
                    data = history[gen.prompt_id]

                    # Check status
                    status_data = data.get("status", {})
                    completed = status_data.get("status_str") == "success"

                    # Check outputs
                    outputs_data = data.get("outputs", {})

                    # If we have outputs, we can update
                    if outputs_data:
                        # Flatten outputs from all nodes
                        # outputs_data is { node_id: { images: [], embeddr_ids: [] } }

                        combined_output = {"images": [], "embeddr_ids": []}

                        for node_out in outputs_data.values():
                            if "images" in node_out:
                                combined_output["images"].extend(node_out["images"])
                            if "embeddr_ids" in node_out:
                                combined_output["embeddr_ids"].extend(
                                    node_out["embeddr_ids"]
                                )

                        # Update generation
                        await self._complete_generation(gen.prompt_id, combined_output)
                        updated_count += 1

            except Exception as e:
                logger.error(f"Error rescanning generation {gen.id}: {e}")

        return updated_count
