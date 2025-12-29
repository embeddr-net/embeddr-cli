from fastapi import APIRouter
from embeddr_core.services.job_manager import job_manager
from typing import Dict, Any
import shutil

router = APIRouter()


@router.get("/status")
def get_job_status():
    """Get the status of the current background job."""
    return job_manager.get_status()


@router.post("/stop")
def stop_job():
    """Stop the currently running job."""
    job_manager.stop_job()
    return {"message": "Stop signal sent"}


@router.get("/stats")
def get_system_stats() -> Dict[str, Any]:
    """Get system stats including GPU usage if available."""
    stats = {"gpu": None, "disk": {}}

    # Disk usage
    try:
        total, used, free = shutil.disk_usage("/")
        stats["disk"] = {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "percent": round((used / total) * 100, 1),
        }
    except:
        pass

    # GPU usage via torch if available
    try:
        import torch

        if torch.cuda.is_available():
            stats["gpu"] = {
                "name": torch.cuda.get_device_name(0),
                "memory_allocated_mb": round(
                    torch.cuda.memory_allocated(0) / (1024**2), 2
                ),
                "memory_reserved_mb": round(
                    torch.cuda.memory_reserved(0) / (1024**2), 2
                ),
                "max_memory_allocated_mb": round(
                    torch.cuda.max_memory_allocated(0) / (1024**2), 2
                ),
            }
    except ImportError:
        pass
    except Exception as e:
        stats["gpu_error"] = str(e)

    return stats
