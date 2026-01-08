import asyncio
import json
import logging
import uuid
import base64
import struct
from typing import List, Dict, Optional
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
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket) -> str:
        await websocket.accept()
        client_id = str(uuid.uuid4())
        self.active_connections[client_id] = websocket

        # Notify others (and self) about the new connection
        asyncio.create_task(self.broadcast_event(
            "client_connected",
            {"client_id": client_id, "total": len(self.active_connections)}
        ))

        return client_id

    def disconnect(self, websocket: WebSocket):
        for client_id, ws in list(self.active_connections.items()):
            if ws == websocket:
                del self.active_connections[client_id]

                # Notify about disconnection
                asyncio.create_task(self.broadcast_event(
                    "client_disconnected",
                    {"client_id": client_id, "total": len(
                        self.active_connections)}
                ))
                break

    async def broadcast_event(self, event_type: str, data: any, source: str = "embeddr"):
        """
        Structured broadcast that ensures messages follow the standard envelope:
        { "source": ..., "type": ..., "data": ... }
        """
        message = {
            "source": source,
            "type": event_type,
            "data": data
        }
        await self.broadcast(message)

    async def broadcast(self, message: dict):
        if not self.active_connections:
            return

        # logger.debug(f"Broadcasting message to {len(self.active_connections)} clients: {message.get('type')}")
        for connection in self.active_connections.values():
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                # Optionally remove dead connection here

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")

    async def send_to_client(self, client_id: str, message: dict):
        if client_id in self.active_connections:
            await self.send_personal_message(message, self.active_connections[client_id])

    def get_connected_clients(self) -> List[str]:
        return list(self.active_connections.keys())


manager = ConnectionManager()


def process_event_sync(msg_type, msg_data):
    try:
        engine = get_engine()
        with Session(engine) as session:
            service = GenerationService(session)
            service.handle_comfy_event_sync(msg_type, msg_data)
    except Exception as e:
        logger.error(f"Failed to update generation state: {e}")


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
            # Add timeout to prevent hanging on connection
            async with websockets.connect(comfy_ws_url, open_timeout=5, close_timeout=5) as websocket:
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
                                    await asyncio.to_thread(process_event_sync, msg_type, msg_data)

                                # 2. Broadcast to Frontend
                                await manager.broadcast_event(
                                    event_type=msg_type,
                                    data=msg_data,
                                    source="comfyui"
                                )
                            except json.JSONDecodeError:
                                pass
                        elif isinstance(message, bytes):
                            try:
                                # Binary message (usually preview)
                                # Format: 4 bytes type, 4 bytes format, image data
                                if len(message) > 8:
                                    msg_type = struct.unpack(
                                        ">I", message[:4])[0]

                                    if msg_type == 1:  # Preview
                                        image_data = message[8:]
                                        b64_img = base64.b64encode(image_data).decode(
                                            "utf-8"
                                        )

                                        await manager.broadcast_event(
                                            event_type="preview",
                                            data=f"data:image/jpeg;base64,{b64_img}",
                                            source="comfyui"
                                        )
                            except Exception as e:
                                logger.error(
                                    f"Error processing binary message: {e}")
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

    def get_pending_generations_sync():
        engine = get_engine()
        with Session(engine) as session:
            statement = select(Generation).where(
                Generation.status.in_(["queued", "processing"])
            )
            generations = session.exec(statement).all()
            return [(g.id, g.prompt_id) for g in generations]

    try:
        while True:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds

                # Offload blocking query
                pending = await asyncio.to_thread(get_pending_generations_sync)

                if not pending:
                    continue

                # Check history for each
                for gen_id, prompt_id in pending:
                    if not prompt_id:
                        continue

                    try:
                        history = await client.get_history(prompt_id)
                        if prompt_id in history:
                            # It finished!
                            logger.info(
                                f"Found completed generation {gen_id} (prompt {prompt_id}) via polling"
                            )

                            # Simulate executed event
                            output_data = history[prompt_id].get("outputs", {})

                            flat_images = []
                            flat_embeddr_ids = []

                            for node_id, node_output in output_data.items():
                                if "images" in node_output:
                                    flat_images.extend(node_output["images"])
                                if "embeddr_ids" in node_output:
                                    flat_embeddr_ids.extend(
                                        node_output["embeddr_ids"])

                            simulated_output = {
                                "images": flat_images,
                                "embeddr_ids": flat_embeddr_ids,
                            }

                            # Create session for update
                            engine = get_engine()
                            with Session(engine) as session:
                                service = GenerationService(session)
                                await service._complete_generation(
                                    prompt_id, simulated_output
                                )

                            # Also broadcast to frontend so it updates
                            await manager.broadcast_event(
                                event_type="executed",
                                data={
                                    "prompt_id": prompt_id,
                                    "output": simulated_output,
                                },
                                source="comfyui"
                            )

                    except Exception:
                        # 404 means not found in history (maybe still running, or cleared)
                        pass

            except Exception as e:
                logger.error(f"Error in poll_stuck_generations: {e}")
                await asyncio.sleep(10)
    finally:
        await client.close()
