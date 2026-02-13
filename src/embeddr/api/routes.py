"""Legacy compatibility router.

All Gen-1 routes (images, workspace, jobs, workflows, generations,
datasets, captioning, comfy) have been removed.  Their functionality
is now served by the /api/v1/* route modules mounted in serve.py.

Only /collections and /plugins remain here for frontend compat.
"""

from fastapi import APIRouter

from embeddr.api.v1.collections import router as collections_router_v2
from embeddr.api.v1.plugins import router as plugins_router_v2

router = APIRouter()

router.include_router(
    collections_router_v2, prefix="/collections", tags=["collections"]
)
router.include_router(
    plugins_router_v2, prefix="/plugins", tags=["plugins"]
)
