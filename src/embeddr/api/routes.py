from fastapi import APIRouter

from embeddr.api.endpoints import (
    # captioning,
    # collections,
    comfy,
    # datasets,
    # images,
    # jobs,
    # system,
    # workflows,
    # workspace,
    # ws,
    # generations,
    plugins,
)
from embeddr.api.v2 import artifacts as artifacts_v2

router = APIRouter()

# v2 API
router.include_router(artifacts_v2.router,
                      prefix="/v2/artifacts", tags=["artifacts"])

# Temporarily disabled legacy routes during Core V2 migration
# router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
# router.include_router(images.router, prefix="/images", tags=["images"])
# router.include_router(system.router, prefix="/system", tags=["system"])
# router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
# router.include_router(collections.router, prefix="/collections", tags=["collections"])
# router.include_router(workflows.router, prefix="/workflows", tags=["workflows"])
# router.include_router(generations.router, prefix="/generations", tags=["generations"])
# router.include_router(datasets.router, prefix="/datasets", tags=["datasets"])
# router.include_router(captioning.router, prefix="/captioning", tags=["captioning"])

# Potentially safe?
router.include_router(comfy.router, prefix="/comfy", tags=["comfy"])
router.include_router(plugins.router, prefix="/plugins", tags=["plugins"])
# router.include_router(ws.router, tags=["websocket"])
