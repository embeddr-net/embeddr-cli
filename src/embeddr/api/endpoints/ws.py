from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from embeddr.services.socket_manager import manager

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, maybe handle client messages if needed
            # For now, we just push updates
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
