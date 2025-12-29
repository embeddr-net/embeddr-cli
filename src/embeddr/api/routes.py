from fastapi import APIRouter
from embeddr.api.endpoints import workspace, images, system, jobs, collections

router = APIRouter()

router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
router.include_router(images.router, prefix="/images", tags=["images"])
router.include_router(system.router, prefix="/system", tags=["system"])
router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
router.include_router(collections.router, prefix="/collections", tags=["collections"])
