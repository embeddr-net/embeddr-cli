import asyncio
import json
import logging
import uuid
import base64
import struct
from datetime import date, datetime
from typing import List, Dict, Optional, Any
from fastapi import WebSocket
import websockets
from starlette.websockets import WebSocketState
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
        self.client_meta: Dict[str, Dict[str, Optional[str]]] = {}
        self.user_clients: Dict[str, set[str]] = {}

    def _extract_client_meta(self, websocket: WebSocket) -> Dict[str, Optional[str]]:
        try:
            client = getattr(websocket, "client", None)
            address = None
            if client and getattr(client, "host", None):
                port = getattr(client, "port", None)
                address = f"{client.host}:{port}" if port else str(client.host)

            headers = getattr(websocket, "headers", None)
            user_agent = None
            origin = None
            forwarded_for = None
            if headers:
                user_agent = headers.get("user-agent")
                origin = headers.get("origin")
                forwarded_for = headers.get("x-forwarded-for")

            url = getattr(websocket, "url", None)
            path = getattr(url, "path", None)

            return {
                "address": address,
                "user_agent": user_agent,
                "origin": origin,
                "forwarded_for": forwarded_for,
                "path": path,
            }
        except Exception:
            return {
                "address": None,
                "user_agent": None,
                "origin": None,
                "forwarded_for": None,
                "path": None,
            }

    class EmbeddrJSONEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, uuid.UUID):
                return str(obj)
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            return super().default(obj)

    async def connect(
        self,
        websocket: WebSocket,
        *,
        user_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        username: Optional[str] = None,
        api_key_id: Optional[str] = None,
        is_admin: bool = False,
        is_root: bool = False,
    ) -> str:
        await websocket.accept()
        client_id = str(uuid.uuid4())
        self.active_connections[client_id] = websocket
        meta = self._extract_client_meta(websocket)
        if user_id:
            meta["user_id"] = user_id
        if operator_id:
            meta["operator_id"] = operator_id
        if username:
            meta["username"] = username
        if api_key_id:
            meta["api_key_id"] = api_key_id
        meta["is_admin"] = bool(is_admin)
        meta["is_root"] = bool(is_root)
        self.client_meta[client_id] = meta
        if user_id:
            self.user_clients.setdefault(user_id, set()).add(client_id)
        logger.info("WS client connected %s meta=%s", client_id, meta)

        # Notify others (and self) about the new connection
        audience: Optional[Dict[str, Any]] = None
        if operator_id:
            audience = {"operator_id": operator_id}
        elif user_id:
            audience = {"user_id": user_id}
        asyncio.create_task(self.broadcast_event(
            "client_connected",
            {"client_id": client_id, "total": len(self.active_connections)},
            audience=audience,
        ))

        return client_id

    def disconnect(self, websocket: WebSocket):
        for client_id, ws in list(self.active_connections.items()):
            if ws == websocket:
                del self.active_connections[client_id]
                meta = self.client_meta.pop(client_id, None) or {}
                user_id = meta.get("user_id")
                if user_id and user_id in self.user_clients:
                    self.user_clients[user_id].discard(client_id)
                    if not self.user_clients[user_id]:
                        del self.user_clients[user_id]

                # Notify about disconnection
                audience: Optional[Dict[str, Any]] = None
                if meta.get("operator_id"):
                    audience = {"operator_id": meta.get("operator_id")}
                elif user_id:
                    audience = {"user_id": user_id}
                asyncio.create_task(self.broadcast_event(
                    "client_disconnected",
                    {"client_id": client_id, "total": len(
                        self.active_connections)},
                    audience=audience,
                ))
                break

    @staticmethod
    def _normalize_audience_ids(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            return {str(v) for v in value if v}
        return {str(value)}

    def _client_matches_audience(self, client_id: str, audience: Optional[Dict[str, Any]]) -> bool:
        if not audience:
            return True

        meta = self.client_meta.get(client_id, {})

        client_ids = self._normalize_audience_ids(
            audience.get("client_ids") or audience.get("client_id")
        )
        user_ids = self._normalize_audience_ids(
            audience.get("user_ids") or audience.get("user_id")
        )
        operator_ids = self._normalize_audience_ids(
            audience.get("operator_ids") or audience.get("operator_id")
        )
        api_key_ids = self._normalize_audience_ids(
            audience.get("api_key_ids") or audience.get("api_key_id")
        )

        if client_ids and client_id not in client_ids:
            return False
        if user_ids and str(meta.get("user_id") or "") not in user_ids:
            return False
        if operator_ids and str(meta.get("operator_id") or "") not in operator_ids:
            return False
        if api_key_ids and str(meta.get("api_key_id") or "") not in api_key_ids:
            return False

        return True

    async def broadcast_event(
        self,
        event_type: str,
        data: Any,
        source: str = "embeddr",
        audience: Optional[Dict[str, Any]] = None,
    ):
        """
        Structured broadcast that ensures messages follow the standard envelope:
        { "source": ..., "type": ..., "data": ... }
        """
        message = {
            "source": source,
            "type": event_type,
            "data": data
        }
        await self.broadcast(message, audience=audience)

    async def broadcast(self, message: dict, audience: Optional[Dict[str, Any]] = None):
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
                if not self._client_matches_audience(client_id, audience):
                    continue
                if getattr(connection, "client_state", None) == WebSocketState.DISCONNECTED:
                    dead_client_ids.append(client_id)
                    continue
                if getattr(connection, "application_state", None) == WebSocketState.DISCONNECTED:
                    dead_client_ids.append(client_id)
                    continue
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
            if getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED:
                return
            if getattr(websocket, "application_state", None) == WebSocketState.DISCONNECTED:
                return
            serialized_message = json.dumps(
                message, cls=self.EmbeddrJSONEncoder)
            await websocket.send_text(serialized_message)
        except Exception as e:
            if "Cannot call \"send\" once a close message has been sent" in str(e):
                logger.debug("Attempted to send on closed websocket.")
            else:
                logger.error(f"Error sending personal message: {e}")

    async def send_to_client(self, client_id: str, message: dict):
        if client_id in self.active_connections:
            await self.send_personal_message(message, self.active_connections[client_id])

    async def send_to_user(self, user_id: str, message: dict):
        client_ids = list(self.user_clients.get(user_id, set()))
        for client_id in client_ids:
            await self.send_to_client(client_id, message)

    def get_connected_clients(self) -> List[str]:
        return list(self.active_connections.keys())

    def get_connected_client_info(self) -> List[Dict[str, Optional[str]]]:
        info: List[Dict[str, Optional[str]]] = []
        for client_id in self.active_connections.keys():
            meta = self.client_meta.get(client_id, {})
            info.append({"client_id": client_id, **meta})
        return info


manager = ConnectionManager()


def process_event_sync(msg_type, msg_data):
    # DEPRECATED: Handled via EventBus now
    pass


