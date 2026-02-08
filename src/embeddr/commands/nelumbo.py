from __future__ import annotations

from pathlib import Path
import os

import typer
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from embeddr.nelumbo.app import create_app

app = typer.Typer(help="Serve Nelumbo bootstrap APIs")


def _mount_frontend(app: FastAPI, frontend_dir: Path) -> None:
    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        return
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="nelumbo")


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind"),
    port: int = typer.Option(8898, help="Port to bind"),
    cors_origins: str = typer.Option(
        "*",
        help="Comma-separated CORS origins (use * for all)",
    ),
    frontend_dir: Path = typer.Option(
        None,
        help="Optional path to Nelumbo build output to serve",
    ),
    reload: bool = typer.Option(False, help="Reload on file changes"),
):
    os.environ["EMBEDDR_NELUMBO_CORS_ORIGINS"] = cors_origins
    api_app = create_app()

    if frontend_dir:
        _mount_frontend(api_app, frontend_dir)

    uvicorn.run(api_app, host=host, port=port, reload=reload)
