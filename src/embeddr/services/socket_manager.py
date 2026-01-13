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
# GenerationService dependency removed
from embeddr.core.plugin_loader import _EVENT_BUS
from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger(__name__)

# Generate a persistent Client ID for this instance
CLIENT_ID = str(uuid.uuid4())


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    class EmbeddrJSONEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, uuid.UUID):
                return str(obj)
            return super().default(obj)

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

        # Prepare message once with robust serialization (handling UUIDs etc)
        try:
            serialized_message = json.dumps(
                message, cls=self.EmbeddrJSONEncoder)
        except Exception as e:
            logger.error(f"Error serializing message for broadcast: {e}")
            return

        # Track dead clients for cleanup
        dead_client_ids = []

        # logger.debug(f"Broadcasting message to {len(self.active_connections)} clients: {message.get('type')}")
        # Iterate over items to get client_id for cleanup
        for client_id, connection in self.active_connections.items():
            try:
                await connection.send_text(serialized_message)
            except Exception as e:
                # If the socket is closed, we should clean it up
                if "Cannot call \"send\" once a close message has been sent" in str(e):
                    logger.info(
                        f"Client {client_id} disconnected (send after close), cleaning up.")
                else:
                    logger.error(
                        f"Error broadcasting to client {client_id}: {e}")

                dead_client_ids.append(client_id)

        # Remove dead connections
        for client_id in dead_client_ids:
            if client_id in self.active_connections:
                del self.active_connections[client_id]

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            serialized_message = json.dumps(
                message, cls=self.EmbeddrJSONEncoder)
            await websocket.send_text(serialized_message)
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")

    async def send_to_client(self, client_id: str, message: dict):
        if client_id in self.active_connections:
            await self.send_personal_message(message, self.active_connections[client_id])

    def get_connected_clients(self) -> List[str]:
        return list(self.active_connections.keys())


manager = ConnectionManager()


def process_event_sync(msg_type, msg_data):
    # DEPRECATED: Handled via EventBus now
    pass


async def monitor_comfy_events():
    """
    LEGACY / DEPRECATED
    Logic moved to embeddr-plugins/plugins/core/embeddr_comfyui/monitor.py
    This function should no longer be called.
    """
    pass


async def poll_stuck_generations():
    """
    LEGACY / DEPRECATED
    """
    pass
