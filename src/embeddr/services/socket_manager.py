import asyncio
import json
import logging
import uuid
import base64
import struct
from typing import List
from fastapi import WebSocket
import websockets
from sqlmodel import Session, select

from embeddr.core.config import settings
from embeddr.db.session import get_engine
from embeddr.services.generation_service import GenerationService
from embeddr.models.generation import Generation

logger = logging.getLogger(__name__)

# Generate a persistent Client ID for this instance
CLIENT_ID = str(uuid.uuid4())


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        if not self.active_connections:
            return

        # logger.debug(f"Broadcasting message to {len(self.active_connections)} clients: {message.get('type')}")
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                # Optionally remove dead connection here


manager = ConnectionManager()


async def monitor_comfy_events():
    """
    Connects to ComfyUI's WebSocket and forwards messages to our connected clients.
    Also polls for stuck generations.
    """
    comfy_ws_url = (
        settings.COMFYUI_URL.replace("http://", "ws://")
        .replace("https://", "wss://")
        .rstrip("/")
        + f"/ws?clientId={CLIENT_ID}"
    )

    logger.info(f"Connecting to ComfyUI WebSocket at {comfy_ws_url}")

    while True:
        try:
            async with websockets.connect(comfy_ws_url) as websocket:
                logger.info("Connected to ComfyUI WebSocket")

                # Start a background poller when connected
                poller_task = asyncio.create_task(poll_stuck_generations())

                try:
                    while True:
                        message = await websocket.recv()
                        # ComfyUI sends JSON messages or binary (previews)
                        # We are mostly interested in JSON status updates
                        if isinstance(message, str):
                            # Debug logging
                            print(f"ComfyUI Message: {message[:200]}")
                            try:
                                data = json.loads(message)
                                msg_type = data.get("type")
                                msg_data = data.get("data")

                                # logger.debug(f"Received ComfyUI event: {msg_type}")

                                # 1. Update Database State
                                if msg_type in [
                                    "execution_start",
                                    "executed",
                                    "execution_error",
                                ]:
                                    try:
                                        engine = get_engine()
                                        with Session(engine) as session:
                                            service = GenerationService(session)
                                            await service.handle_comfy_event(
                                                msg_type, msg_data
                                            )
                                    except Exception as e:
                                        logger.error(
                                            f"Failed to update generation state: {e}"
                                        )

                                # 2. Broadcast to Frontend
                                await manager.broadcast(
                                    {
                                        "source": "comfyui",
                                        "type": msg_type,
                                        "data": msg_data,
                                    }
                                )
                            except json.JSONDecodeError:
                                pass
                        elif isinstance(message, bytes):
                            try:
                                # Binary message (usually preview)
                                # Format: 4 bytes type, 4 bytes format, image data
                                if len(message) > 8:
                                    msg_type = struct.unpack(">I", message[:4])[0]

                                    if msg_type == 1:  # Preview
                                        image_data = message[8:]
                                        b64_img = base64.b64encode(image_data).decode(
                                            "utf-8"
                                        )

                                        await manager.broadcast(
                                            {
                                                "source": "comfyui",
                                                "type": "preview",
                                                "data": f"data:image/jpeg;base64,{b64_img}",
                                            }
                                        )
                            except Exception as e:
                                logger.error(f"Error processing binary message: {e}")
                finally:
                    poller_task.cancel()

        except Exception as e:
            logger.error(f"ComfyUI WebSocket connection error: {e}")
            await asyncio.sleep(5)  # Wait before reconnecting


async def poll_stuck_generations():
    """
    Periodically checks for generations that are 'queued' or 'processing' but might have finished.
    """
    from embeddr.services.comfy import AsyncComfyClient

    client = AsyncComfyClient()

    while True:
        try:
            await asyncio.sleep(10)  # Check every 10 seconds

            engine = get_engine()
            with Session(engine) as session:
                # Find pending/processing generations
                statement = select(Generation).where(
                    Generation.status.in_(["queued", "processing"])
                )
                generations = session.exec(statement).all()

                if not generations:
                    continue

                # Check history for each
                for gen in generations:
                    if not gen.prompt_id:
                        continue

                    try:
                        history = await client.get_history(gen.prompt_id)
                        if gen.prompt_id in history:
                            # It finished!
                            logger.info(
                                f"Found completed generation {gen.id} (prompt {gen.prompt_id}) via polling"
                            )
                            service = GenerationService(session)

                            # Simulate executed event
                            output_data = history[gen.prompt_id].get("outputs", {})
                            # The history format is slightly different from WS event
                            # WS event: { "output": { "images": ... } }
                            # History API: { "outputs": { "node_id": { "images": ... } } }

                            # We need to flatten the history outputs to match what _complete_generation expects
                            # or update _complete_generation to handle both.
                            # Let's flatten it here to match WS 'output' structure roughly

                            flat_images = []
                            flat_embeddr_ids = []

                            for node_id, node_output in output_data.items():
                                if "images" in node_output:
                                    flat_images.extend(node_output["images"])
                                if "embeddr_ids" in node_output:
                                    flat_embeddr_ids.extend(node_output["embeddr_ids"])

                            simulated_output = {
                                "images": flat_images,
                                "embeddr_ids": flat_embeddr_ids,
                            }

                            await service._complete_generation(
                                gen.prompt_id, simulated_output
                            )

                            # Also broadcast to frontend so it updates
                            await manager.broadcast(
                                {
                                    "source": "comfyui",
                                    "type": "executed",
                                    "data": {
                                        "prompt_id": gen.prompt_id,
                                        "output": simulated_output,
                                    },
                                }
                            )

                    except Exception:
                        # 404 means not found in history (maybe still running, or cleared)
                        # If it's been running for too long, maybe mark failed?
                        pass

        except Exception as e:
            logger.error(f"Error in poll_stuck_generations: {e}")
            await asyncio.sleep(10)
