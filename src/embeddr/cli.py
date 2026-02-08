import os
import sys
from pathlib import Path

import typer

from embeddr.commands import config, serve, db, init_v2, process, inspect, tui, plugins, system, lotus, manage, nelumbo

from embeddr.core.config import get_data_dir, refresh_settings
from embeddr.core.project import find_project_root, load_project_config
from embeddr.core.plugin_loader import load_python_plugins, initialize_all_plugins, startup_all_plugins

# Import get_engine to clear its cache when settings change
from embeddr.db.session import get_engine

app = typer.Typer()
serve.register(app)

app.add_typer(config.app, name="config")
app.add_typer(db.app, name="db")
app.add_typer(plugins.app, name="plugins")
app.add_typer(init_v2.app, name="init-v2")
app.add_typer(process.app, name="process")
app.add_typer(inspect.app, name="inspect")
app.add_typer(tui.app, name="tui")
app.add_typer(system.app, name="system")
app.add_typer(lotus.app, name="lotus")
app.add_typer(manage.app, name="manage")
app.add_typer(nelumbo.app, name="nelumbo")
# app.add_typer(fixtures.app, name="fixtures") # Moved to Plugin


def _load_cli_plugins() -> None:
    # Skip loading if we are just running the server (it loads them itself)
    # Robust check for serve/init commands anywhere in args
    is_serve = "serve" in sys.argv
    is_init = "init" in sys.argv or "init-v2" in sys.argv
    is_plugins_deps = "plugins" in sys.argv and "deps" in sys.argv
    is_remote_lotus = "lotus" in sys.argv and "--local" not in sys.argv
    is_manage = "manage" in sys.argv
    skip_plugins = os.environ.get(
        "EMBEDDR_SKIP_CLI_PLUGINS", "false").lower() == "true"

    if is_serve or is_init or is_plugins_deps or is_remote_lotus or is_manage or skip_plugins:
        return

    allow_dev_plugins = os.environ.get(
        "EMBEDDR_ALLOW_DEV_PLUGINS", "false").lower() == "true"

    # 1. Environment Variable (Check both singular and plural)
    env_plugin_dir = os.environ.get(
        "EMBEDDR_PLUGIN_DIR") or os.environ.get("EMBEDDR_PLUGINS_DIR")
    if env_plugin_dir:
        path = Path(env_plugin_dir)
        if path.exists():
            load_python_plugins(path, cli_app=app)

    # 2. Workspace plugins (data dir)
    user_plugins = get_data_dir() / "plugins"
    if user_plugins.exists():
        load_python_plugins(user_plugins, cli_app=app)

    # 3. Dev Path (explicit opt-in)
    if allow_dev_plugins:
        dev_plugins = Path(__file__).parents[3] / "embeddr-plugins" / "plugins"
        if dev_plugins.exists():
            load_python_plugins(dev_plugins, cli_app=app)

    # Initialize all discovered plugins
    initialize_all_plugins()
    startup_all_plugins()

    # START JOB RUNTIME (Headless Mode)
    # When running CLI commands, we might trigger artifact creation
    # so we should have the runtime running if events fire.
    try:
        from embeddr.services.job_runtime import runtime
        from embeddr.core.event_bus import _EVENT_BUS
        import asyncio

        asyncio.run(runtime.start())
        _EVENT_BUS.subscribe("*", runtime.handle_event)
    except Exception as e:
        print(f"Failed to start JobRuntime: {e}")


@app.command()
def init(
    name: str = typer.Option(None, help="Project name"),
    path: Path = typer.Option(
        Path.cwd(), help="Path to initialize project in"),
):
    """Initialize a new Embeddr project in the current directory."""
    from embeddr.commands.init_wizard import run_init_wizard

    run_init_wizard(path, name)


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
        print(f"DEBUG: Setting EMBEDDR_DATA_DIR to {data_dir}")
        os.environ["EMBEDDR_DATA_DIR"] = data_dir
        refresh_settings()
        get_engine.cache_clear()
        _load_cli_plugins()
        return

    # 2. Env var already set (shell, systemd, etc.)
    if os.environ.get("EMBEDDR_DATA_DIR"):
        print(
            f"DEBUG: EMBEDDR_DATA_DIR already set to {os.environ.get('EMBEDDR_DATA_DIR')}")
        refresh_settings()
        get_engine.cache_clear()
        _load_cli_plugins()
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

        # CORS configuration
        cors_config = config.get("cors") if isinstance(config, dict) else None
        if isinstance(cors_config, dict):
            origins = cors_config.get("origins")
            if isinstance(origins, list):
                cleaned = [str(o).strip() for o in origins if str(o).strip()]
                if cleaned:
                    os.environ.setdefault(
                        "EMBEDDR_CORS_ORIGINS", ",".join(cleaned)
                    )
            allow_dev = cors_config.get("allow_dev_origins")
            if isinstance(allow_dev, bool):
                os.environ.setdefault(
                    "EMBEDDR_ALLOW_DEV_ORIGINS", str(allow_dev).lower()
                )

        # Determine data directory
        if "paths" in config and "data_dir" in config["paths"]:
            # Resolve relative to project root
            custom_data_dir = project_root / config["paths"]["data_dir"]
            os.environ["EMBEDDR_DATA_DIR"] = str(custom_data_dir)
        else:
            # Default to .embeddr in project root
            default_project_data = project_root / ".embeddr"
            os.environ["EMBEDDR_DATA_DIR"] = str(default_project_data)

        # Determine plugins directory
        if "paths" in config and "plugins_dir" in config["paths"]:
            custom_plugins_dir = project_root / config["paths"]["plugins_dir"]
            os.environ["EMBEDDR_PLUGINS_DIR"] = str(custom_plugins_dir)

        # Database configuration (optional)
        db_config = config.get("database", {}) if isinstance(
            config, dict) else {}
        db_provider = db_config.get("provider") if isinstance(
            db_config, dict) else None
        db_url = db_config.get("url") if isinstance(db_config, dict) else None

        if db_provider:
            os.environ["EMBEDDR_DB_PROVIDER"] = str(db_provider)
        if db_url:
            os.environ["DATABASE_URL"] = str(db_url)

        refresh_settings()
        get_engine.cache_clear()
        _load_cli_plugins()
        return

    # 4. Default OS location
    default = get_data_dir()

    if default.exists():
        os.environ["EMBEDDR_DATA_DIR"] = str(default)
        refresh_settings()
        get_engine.cache_clear()
        _load_cli_plugins()
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
            get_engine.cache_clear()
            _load_cli_plugins()
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
