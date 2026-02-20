from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
import os

import typer
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from embeddr.nelumbo.app import create_app

app = typer.Typer(help="Serve Nelumbo bootstrap APIs")


PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_NELUMBO_SOURCE_DIR = REPO_ROOT / "embeddr-nelumbo"
DEFAULT_NELUMBO_DIST_DIR = DEFAULT_NELUMBO_SOURCE_DIR / "dist"
DEFAULT_NELUMBO_DEV_DIST_DIR = DEFAULT_NELUMBO_SOURCE_DIR / "dev-dist"
DEFAULT_EMBEDDED_FRONTEND_DIR = PACKAGE_DIR / "nelumbo" / "web"


def _resolve_frontend_dir(frontend_dir: Path | None) -> Path | None:
    if frontend_dir:
        resolved = frontend_dir.expanduser().resolve()
        return resolved if (resolved / "index.html").exists() else None

    env_frontend = os.environ.get("EMBEDDR_NELUMBO_FRONTEND_DIR", "").strip()
    candidates: list[Path] = []
    if env_frontend:
        candidates.append(Path(env_frontend).expanduser().resolve())

    candidates.extend(
        [
            DEFAULT_EMBEDDED_FRONTEND_DIR,
            DEFAULT_NELUMBO_DIST_DIR,
            DEFAULT_NELUMBO_DEV_DIST_DIR,
        ]
    )

    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate

    return None


def _mount_frontend(app: FastAPI, frontend_dir: Path) -> None:
    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        return
    app.mount("/", StaticFiles(directory=str(frontend_dir),
              html=True), name="nelumbo")


@app.command("prepare-frontend")
def prepare_frontend(
    source_dir: Path = typer.Option(
        DEFAULT_NELUMBO_SOURCE_DIR,
        help="Path to the embeddr-nelumbo frontend project",
    ),
    dist_dir: Path = typer.Option(
        None,
        help="Path to built frontend output (defaults to <source_dir>/dist)",
    ),
    target_dir: Path = typer.Option(
        DEFAULT_EMBEDDED_FRONTEND_DIR,
        help="Where to copy frontend assets for embeddr nelumbo serve",
    ),
    build: bool = typer.Option(
        True,
        "--build/--no-build",
        help="Run pnpm build in source_dir before copy",
    ),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Confirm destructive copy (target directory will be replaced)",
    ),
):
    src_root = source_dir.expanduser().resolve()
    if not src_root.exists():
        typer.secho(
            f"Frontend source directory not found: {src_root}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if build:
        try:
            subprocess.run(["pnpm", "build"], cwd=str(src_root), check=True)
        except subprocess.CalledProcessError as exc:
            typer.secho(
                f"Failed to build Nelumbo frontend in {src_root}: {exc}",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)
        except FileNotFoundError:
            typer.secho(
                "pnpm not found. Install pnpm or use --no-build.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    resolved_dist = (
        dist_dir.expanduser().resolve()
        if dist_dir
        else (src_root / "dist").resolve()
    )
    index_path = resolved_dist / "index.html"
    if not index_path.exists():
        typer.secho(
            f"Built frontend index not found: {index_path}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    resolved_target = target_dir.expanduser().resolve()
    if not confirm:
        typer.secho(
            "This operation replaces the target frontend directory. Re-run with --confirm.",
            fg=typer.colors.YELLOW,
        )
        typer.echo(f"Target: {resolved_target}")
        raise typer.Exit(code=1)

    if resolved_target.exists():
        shutil.rmtree(resolved_target)
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(resolved_dist, resolved_target)

    typer.secho("Nelumbo frontend prepared successfully.",
                fg=typer.colors.GREEN)
    typer.echo(f"Source dist: {resolved_dist}")
    typer.echo(f"Target dir: {resolved_target}")


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

    resolved_frontend_dir = _resolve_frontend_dir(frontend_dir)
    if resolved_frontend_dir:
        _mount_frontend(api_app, resolved_frontend_dir)
        typer.secho(
            f"Serving Nelumbo frontend from: {resolved_frontend_dir}",
            fg=typer.colors.GREEN,
        )
    elif frontend_dir:
        typer.secho(
            f"Frontend index not found in --frontend-dir: {frontend_dir}",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho(
            "No Nelumbo frontend found. Run 'embeddr nelumbo prepare-frontend --confirm' "
            "or pass --frontend-dir.",
            fg=typer.colors.YELLOW,
        )

    uvicorn.run(api_app, host=host, port=port, reload=reload)
