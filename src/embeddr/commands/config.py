import typer
from embeddr.core.config import settings
from embeddr.core.project import create_default_config, CONFIG_FILENAME
from pathlib import Path
import os

app = typer.Typer()


@app.command()
def init(
    name: str = typer.Option(None, help="Project name"),
    path: Path = typer.Option(
        Path.cwd(), help="Path to initialize project in"),
):
    """Initialize the configuration."""
    if (path / CONFIG_FILENAME).exists():
        typer.secho(
            f"Project already initialized at {path}", fg=typer.colors.YELLOW)
        return

    project_name = name or path.name
    create_default_config(path, project_name)
    typer.secho(
        f"Initialized Embeddr project at {path}", fg=typer.colors.GREEN)
    typer.echo(f"Created {CONFIG_FILENAME}")


@app.command()
def show():
    """Show the current configuration and paths."""
    print(f"Project Name: {settings.PROJECT_NAME}")
    print(f"Data Directory: {settings.DATA_DIR}")
    print(f"Database URL: {settings.DATABASE_URL}")
    print(f"Thumbnails Dir: {settings.THUMBNAILS_DIR}")
    print(f"Vector Storage Dir: {settings.VECTOR_STORAGE_DIR}")

    # Calculate Frontend Dir (same logic as serve.py)
    root_dir = Path(__file__).resolve().parents[3]
    frontend_dir = os.environ.get(
        "EMBEDDR_FRONTEND_DIR", str(root_dir / "static" / "dist")
    )
    print(f"Frontend Dir: {frontend_dir}")
    print(f"Frontend Exists: {os.path.exists(frontend_dir)}")

    # Check for key files
    db_path = settings.DATABASE_URL.replace("sqlite:///", "")
    print("\nFile Status:")
    print(f"  Database File Exists: {os.path.exists(db_path)} ({db_path})")
    print(
        f"  Vector Storage Exists: {os.path.exists(settings.VECTOR_STORAGE_DIR)}")

    # Show environment variables
    print("\nEnvironment Variables:")
    print(
        f"  EMBEDDR_DATA_DIR: {os.environ.get('EMBEDDR_DATA_DIR', 'Not Set')}")
    print(
        f"  EMBEDDR_FRONTEND_DIR: {os.environ.get('EMBEDDR_FRONTEND_DIR', 'Not Set')}"
    )
