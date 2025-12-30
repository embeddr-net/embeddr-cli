import typer
import os
import sys
from embeddr.commands import serve, config
from embeddr.core.config import refresh_settings, get_data_dir
from embeddr.core.project import find_project_root, load_project_config
from pathlib import Path


app = typer.Typer()
serve.register(app)

app.add_typer(config.app, name="config")


@app.command()
def init(
    name: str = typer.Option(None, help="Project name"),
    path: Path = typer.Option(
        Path.cwd(), help="Path to initialize project in"),
):
    """Initialize a new Embeddr project in the current directory."""
    from embeddr.core.project import create_default_config, CONFIG_FILENAME

    if (path / CONFIG_FILENAME).exists():
        typer.secho(
            f"Project already initialized at {path}", fg=typer.colors.YELLOW)
        return

    project_name = name or path.name
    create_default_config(path, project_name)
    typer.secho(
        f"Initialized Embeddr project at {path}", fg=typer.colors.GREEN)
    typer.echo(f"Created {CONFIG_FILENAME}")


@app.callback()
def callback(
    data_dir: str = typer.Option(
        None,
        "--data-dir",
        "-d",
        help="Path to the data directory (defaults depend on OS if not set)",
        envvar="EMBEDDR_DATA_DIR",
    ),
):
    # 1. Explicit override always wins
    if data_dir:
        os.environ["EMBEDDR_DATA_DIR"] = data_dir
        refresh_settings()
        return

    # 2. Env var already set (shell, systemd, etc.)
    if os.environ.get("EMBEDDR_DATA_DIR"):
        refresh_settings()
        return

    # 3. Check for existing project (embeddr.toml)
    project_root = find_project_root(Path.cwd())
    if project_root:
        config = load_project_config(project_root)

        # Set server defaults if present
        if "server" in config:
            if "host" in config["server"]:
                os.environ.setdefault("EMBEDDR_HOST", config["server"]["host"])
            if "port" in config["server"]:
                os.environ.setdefault(
                    "EMBEDDR_PORT", str(config["server"]["port"]))

        # Determine data directory
        if "paths" in config and "data_dir" in config["paths"]:
            # Resolve relative to project root
            custom_data_dir = project_root / config["paths"]["data_dir"]
            os.environ["EMBEDDR_DATA_DIR"] = str(custom_data_dir)
        else:
            # Default to .embeddr in project root
            default_project_data = project_root / ".embeddr"
            os.environ["EMBEDDR_DATA_DIR"] = str(default_project_data)

        refresh_settings()
        return

    # 4. Default OS location
    default = get_data_dir()

    if default.exists():
        os.environ["EMBEDDR_DATA_DIR"] = str(default)
        refresh_settings()
        return

    # 5. First-run: prompt
    if sys.stdin.isatty():
        if typer.confirm(
            f"No Embeddr data directory found.\n"
            f"Create one at the default location?\n\n{default}",
            default=True,
        ):
            default.mkdir(parents=True, exist_ok=True)
            os.environ["EMBEDDR_DATA_DIR"] = str(default)
            refresh_settings()
            return

        typer.secho(
            "Aborted. Please specify a data directory with "
            "--data-dir or EMBEDDR_DATA_DIR.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    # 6. Non-interactive: fail loudly
    typer.secho(
        "No Embeddr data directory found and prompting is disabled.\n"
        "Set EMBEDDR_DATA_DIR or use --data-dir.",
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


def main():
    app()


if __name__ == "__main__":
    main()
