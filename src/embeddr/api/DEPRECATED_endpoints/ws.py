import json
import logging
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select
from sqlalchemy import func

from embeddr.services.socket_manager import manager
from embeddr.db.session import get_engine
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.automation import Automation

logger = logging.getLogger(__name__)
router = APIRouter()


async def _build_status_payload() -> dict:
    queue_status = {"remaining": 0}
    running_executions = []

    try:
        def get_running_executions_sync():
            engine = get_engine()
            with Session(engine) as session:
                statement = select(ArtifactExecution).where(
                    ArtifactExecution.status.in_([
                        "queued",
                        "running",
                        "pending",
                        "processing",
                    ])
                )
                results = session.exec(statement).all()
                return [
                    g.model_dump() if hasattr(g, "model_dump") else g.dict()
                    for g in results
                ]

        running_executions = await asyncio.to_thread(get_running_executions_sync)
        for g in running_executions:
            if g.get("created_at"):
                g["created_at"] = g["created_at"].isoformat()
            if g.get("updated_at"):
                g["updated_at"] = g["updated_at"].isoformat()
    except Exception as e:
        logger.error(f"Failed to get running executions: {e}")

    automation_status = {"total": 0, "active": 0}
    try:
        def get_automation_status_sync():
            engine = get_engine()
            with Session(engine) as session:
                total = session.exec(
                    select(func.count()).select_from(Automation)
                ).one()
                active = session.exec(
                    select(func.count()).select_from(Automation).where(
                        Automation.is_active == True
                    )
                ).one()
                return {"total": total, "active": active}

        automation_status = await asyncio.to_thread(get_automation_status_sync)
    except Exception as e:
        logger.error(f"Failed to get automation status: {e}")

    return {
        "queue_status": queue_status,
        "running_generations": [],
        "running_executions": running_executions,
        "automation_status": automation_status,
    }


async def broadcast_status() -> None:
    payload = await _build_status_payload()
    await manager.broadcast({
        "type": "status_response",
        "source": "embeddr",
        "data": payload,
    })


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
            try:
                data = await websocket.receive_text()
            except RuntimeError as e:
                if "WebSocket is not connected" in str(e):
                    logger.debug(
                        f"Client {client_id} disconnected abruptly (RuntimeError)")
                    break
                raise e
            except WebSocketDisconnect:
                logger.debug(f"Client {client_id} disconnected")
                break

            try:
                message = json.loads(data)
                if message.get("type") == "request_status":
                    payload = await _build_status_payload()
                    await manager.send_personal_message(
                        {
                            "type": "status_response",
                            "source": "embeddr",
                            "data": payload,
                        },
                        websocket,
                    )
            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"Error processing websocket message: {e}")
    finally:
        manager.disconnect(websocket)
