from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from embeddr.services.socket_manager import manager
from embeddr.services import auth_service
from embeddr.db.session import get_engine
from sqlmodel import Session
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def _extract_ws_credential(websocket: WebSocket, query_api_key: str | None) -> str | None:
    header_key = websocket.headers.get(
        "x-api-key") if websocket.headers else None
    cookie_key = websocket.cookies.get(
        "embeddr_auth") if websocket.cookies else None
    return header_key or cookie_key or query_api_key


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    api_key: str = Query(None)
):
    # Manual Auth Check for WebSockets
    auth_mode = auth_service.get_auth_mode()
    auth_context = None
    if auth_mode != "open":
        raw_credential = _extract_ws_credential(websocket, api_key)
        if not raw_credential:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            logger.warning("WebSocket auth failed: Missing credentials")
            return
        with Session(get_engine()) as session:
            auth_context = auth_service.resolve_auth_context(
                session, raw_credential)
            if not auth_context:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                logger.warning("WebSocket auth failed: Invalid credential")
                return

    client_id = await manager.connect(
        websocket,
        user_id=str(auth_context.user_id) if auth_context else None,
        operator_id=str(auth_context.operator_id)
        if auth_context and auth_context.operator_id
        else None,
        username=auth_context.username if auth_context else None,
        api_key_id=str(auth_context.api_key_id)
        if auth_context and auth_context.api_key_id
        else None,
        is_admin=bool(getattr(auth_context, "is_admin", False))
        if auth_context
        else False,
        is_root=bool(getattr(auth_context, "is_root", False))
        if auth_context
        else False,
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
