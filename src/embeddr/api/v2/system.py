from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
import subprocess
import shutil
import sys
from typing import List, Dict, Any
from embeddr_core.services.resource_manager import resource_manager
from embeddr.services.socket_manager import manager

router = APIRouter()


@router.get("/debug/clients")
def get_connected_clients():
    return {"clients": manager.get_connected_clients()}


@router.post("/debug/message")
async def send_debug_message(client_id: str, message: Dict[str, Any]):
    await manager.send_to_client(client_id, message)
    return {"status": "sent"}


@router.get("/routes")
def get_routes(request: Request) -> Dict[str, List[Dict[str, Any]]]:
    routes = []
    # Collect routes from the main app
    # Included routers are flattened in starlette/fastapi app.routes
    for route in request.app.routes:
        if hasattr(route, "path"):
            routes.append({
                "path": route.path,
                "methods": list(route.methods) if hasattr(route, "methods") else ["ALL"],
                "name": route.name,
                "tags": getattr(route, "tags", [])
            })
    return {"routes": routes}


@router.get("/resources")
def get_resources():
    return {
        "resources": [r.model_dump() for r in resource_manager.list_resources()],
        "total_memory_bytes": resource_manager.get_total_memory_usage()
    }


@router.post("/resources/unload")
def unload_resource(resource_id: str):
    """Request unloading of a specific resource."""
    resource_manager.request_unload(resource_id)
    return {"status": "ok"}


@router.post("/resources/unload_all")
def unload_all_resources():
    """Request unloading of all managed resources."""
    for r in resource_manager.list_resources():
        resource_manager.request_unload(r.id)
    return {"status": "ok"}


class CliCommandRequest(BaseModel):
    args: List[str]


@router.post("/cli")
def run_cli_command(cmd: CliCommandRequest):
    """
    Execute a CLI command. 
    Ideally, this calls the python module directly to avoid path issues.
    """

    # Construct command: python -m embeddr.cli [args]
    # We use sys.executable to ensure we use the same venv
    full_cmd = [sys.executable, "-m", "embeddr.cli"] + cmd.args
    print(f"Executing CLI command: {' '.join(full_cmd)}")

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=120  # Increased timeout for longer operations
        )

        # Log output to server console for debugging
        if result.stdout:
            print(f"CLI STDOUT:\n{result.stdout}")
        if result.stderr:
            print(f"CLI STDERR:\n{result.stderr}")

        return {
            "success": result.returncode == 0,
            "return_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command": " ".join(full_cmd)
        }
    except Exception as e:
        print(f"CLI EXECUTION ERROR: {e}")
        return {
            "success": False,
            "error": str(e),
            "command": " ".join(full_cmd)
        }
