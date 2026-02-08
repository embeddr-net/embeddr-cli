from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from embeddr.nelumbo.router import router as nelumbo_router


def _cors_origins() -> list[str]:
    raw = os.environ.get("EMBEDDR_NELUMBO_CORS_ORIGINS", "*")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    if not origins or origins == ["*"]:
        return ["*"]
    return origins


def create_app() -> FastAPI:
    app = FastAPI(title="Embeddr Nelumbo Bootstrap")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(nelumbo_router)
    return app
