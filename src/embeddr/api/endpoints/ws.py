import json
import logging
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from embeddr.services.socket_manager import manager
from embeddr.services.comfy import ComfyClient
from embeddr.db.session import get_engine
from embeddr.models.generation import Generation

logger = logging.getLogger(__name__)
router = APIRouter()
comfy_client = ComfyClient()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = await manager.connect(websocket)

    # Send welcome message
    await manager.send_personal_message({
        "type": "welcome",
        "source": "embeddr",
        "data": {
            "client_id": client_id
        }
    }, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                if message.get("type") == "request_status":
                    # 1. Get Queue Status
                    queue_status = {"remaining": 0}
                    try:
                        # Run blocking call in thread to avoid blocking event loop
                        queue = await asyncio.to_thread(comfy_client.get_queue)
                        remaining = len(queue.get("queue_running", [])) + \
                            len(queue.get("queue_pending", []))
                        queue_status["remaining"] = remaining
                    except Exception as e:
                        logger.error(f"Failed to get queue status: {e}")

                    # 2. Get Running Generations from DB
                    running_generations = []
                    try:
                        def get_running_generations_sync():
                            engine = get_engine()
                            with Session(engine) as session:
                                statement = select(Generation).where(
                                    Generation.status.in_(["pending", "processing"]))
                                results = session.exec(statement).all()
                                # Convert to dicts using model_dump if available (Pydantic v2) or dict (v1)
                                return [
                                    g.model_dump() if hasattr(g, "model_dump") else g.dict()
                                    for g in results
                                ]

                        running_generations = await asyncio.to_thread(get_running_generations_sync)

                        # Handle datetime serialization if needed (usually fastapi handles it, but we are sending raw json)
                        # We need to convert datetime objects to strings
                        for g in running_generations:
                            if g.get("created_at"):
                                g["created_at"] = g["created_at"].isoformat()
                            if g.get("updated_at"):
                                g["updated_at"] = g["updated_at"].isoformat()
                    except Exception as e:
                        logger.error(f"Failed to get running generations: {e}")

                    response = {
                        "type": "status_response",
                        "source": "embeddr",
                        "data": {
                            "queue_status": queue_status,
                            "running_generations": running_generations
                        }
                    }
                    await manager.send_personal_message(response, websocket)

            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"Error processing websocket message: {e}")

    except WebSocketDisconnect:
        manager.disconnect(websocket)
