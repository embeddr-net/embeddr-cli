from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from embeddr.services.socket_manager import manager
from embeddr.services import auth_service
from embeddr.db.session import get_engine
from sqlmodel import Session
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    api_key: str = Query(None)
):
    # Manual Auth Check for WebSockets
    auth_mode = auth_service.get_auth_mode()
    auth_context = None
    if auth_mode != "open":
        if not api_key:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            logger.warning("WebSocket auth failed: Missing API key")
            return
        with Session(get_engine()) as session:
            api_key_obj = auth_service.lookup_api_key(session, api_key)
            if not api_key_obj:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                logger.warning("WebSocket auth failed: Invalid API key")
                return
            auth_context = auth_service.build_auth_context(
                session, api_key_obj, api_key
            )

    client_id = await manager.connect(
        websocket,
        user_id=str(auth_context.user_id) if auth_context else None,
        username=auth_context.username if auth_context else None,
        api_key_id=str(auth_context.api_key_id)
        if auth_context and auth_context.api_key_id
        else None,
    )
    await manager.send_personal_message(
        {
            "source": "embeddr",
            "type": "client_hello",
            "data": {
                "client_id": client_id,
                "user_id": str(auth_context.user_id)
                if auth_context and auth_context.user_id
                else None,
                "username": auth_context.username if auth_context else None,
            },
        },
        websocket,
    )
    await manager.send_personal_message(
        {
            "source": "embeddr",
            "type": "welcome",
            "data": {"client_id": client_id},
        },
        websocket,
    )
    logger.info(f"WebSocket client connected: {client_id}")
    try:
        while True:
            # Keep the connection open and listen for messages
            data = await websocket.receive_text()
            # Currently we don't process incoming messages deeply,
            # but this loop is required to keep the socket alive.
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        logger.info(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
        manager.disconnect(websocket)  # Ensure cleanup
