from fastapi import APIRouter

from embeddr.api.v2.collections import router as collections_router_v2
from embeddr.api.v2.config import router as config_router_v2

router = APIRouter()

# Temporarily disabled legacy routes during Core V2 migration
# router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
# router.include_router(images.router, prefix="/images", tags=["images"])
# router.include_router(system.router, prefix="/system", tags=["system"])
# router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])

# Mount V2 Collections on /collections for now (or /api/v2/collections if we want strict versioning)
# For the frontend compat, let's put it on /collections
router.include_router(collections_router_v2,
                      prefix="/collections", tags=["collections"])

# Config router moved to V2
# router.include_router(config_router_v2, prefix="/config", tags=["config"])

# router.include_router(workflows.router, prefix="/workflows", tags=["workflows"])
# router.include_router(generations.router, prefix="/generations", tags=["generations"])
# router.include_router(datasets.router, prefix="/datasets", tags=["datasets"])
# router.include_router(captioning.router, prefix="/captioning", tags=["captioning"])

# Potentially safe?
# router.include_router(comfy.router, prefix="/comfy", tags=["comfy"])
# router.include_router(plugins.router, prefix="/plugins", tags=["plugins"])
# router.include_router(ws.router, tags=["websocket"])
