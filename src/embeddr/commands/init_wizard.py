from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Optional
import importlib.util

import questionary
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from embeddr.core.config import refresh_settings
from embeddr.core.project import CONFIG_FILENAME, create_default_config
from embeddr.db.session import get_engine, run_migrations
from embeddr.services import auth_service
from sqlmodel import Session


DEFAULT_DIST_PLUGINS_DIR = (
    Path(__file__).resolve().parents[4] /
    "embeddr-plugins" / "dist" / "plugins"
)

# Pack Definitions - Easy to edit hardcoded packs
PACK_DEFINITIONS = {
    "standard": {
        "name": "Standard (Recommended)",
        "description": "Core workspace with search, embeddings, thumbnailing, and local storage.",
        "includes": ["minimal"],
        "plugins": [
            "embeddr-search",
            "embeddr-embeddings",
            "embeddr-thumbnailer",
            "embeddr-llm",
            "embeddr-editor-types",
            "embeddr-media-tools",
            "embeddr-transport-mcp",
            "embeddr-transport-graphql",
            "embeddr-zen-effect-grid",
        ],
    },
    "creative": {
        "name": "Creative (ComfyUI)",
        "description": "Standard plus ComfyUI integration for AI image generation workflows.",
        "includes": ["standard"],
        "plugins": [
            "embeddr-comfyui",
        ],
    },
    "full": {
        "name": "Full (All Release Plugins)",
        "description": "Everything in the 0.2.0 release — standard + creative + S3 storage.",
        "includes": ["creative"],
        "plugins": [
            "embeddr-storage-s3",
        ],
    },
    "minimal": {
        "name": "Minimal Core",
        "description": "Just the bare essentials. Good for building custom pipelines from scratch.",
        "plugins": [
            "embeddr-core",
            "embeddr-lotus",
            "embeddr-storage-local",
        ],
    },
}

# Theme for questionary to ensure consistent highlighting
WIZARD_STYLE = questionary.Style([
    # token in front of the question (violet-500)
    ('qmark', 'fg:#8b5cf6 bold'),
    ('question', 'bold'),               # question text
    ('answer', 'fg:#10b981 bold'),      # submitted answer text (emerald-500)
    # pointer used in select and checkbox prompts
    ('pointer', 'fg:#8b5cf6 bold'),
    # pointed-at choice in select and checkbox prompts
    ('highlighted', 'fg:#8b5cf6 bold'),
    ('selected', 'fg:#10b981'),         # style for a selected item of a checkbox
    ('separator', 'fg:#6b7280'),        # separator in lists (gray-500)
    # user instructions for select, rawselect, checkbox
    ('instruction', 'fg:#6b7280 italic'),
    ('text', ''),                       # plain text
    # disabled choices for select and checkbox prompts
    ('disabled', 'fg:#858585 italic')
])

CONSOLE = Console()


def _normalize_provider(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"postgres", "postgresql"}:
        return "postgresql"
    if normalized in {"mysql"}:
        return "mysql"
    return "sqlite"


def _prompt_text(message: str, default: str = "") -> str:
    """Helper for text prompts with consistent style."""
    result = questionary.text(
        message,
        default=default,
        style=WIZARD_STYLE
    ).ask()
    if result is None:
        raise typer.Exit(code=1)
    return result.strip()


def _prompt_select(message: str, choices: list) -> str:
    """Helper for select prompts with consistent style."""
    result = questionary.select(
        message,
        choices=choices,
        style=WIZARD_STYLE,
        use_indicator=True,
    ).ask()
    if result is None:
        raise typer.Exit(code=1)
    return result


def _prompt_confirm(message: str, default: bool = True) -> bool:
    """Helper for confirm prompts with consistent style."""
    result = questionary.confirm(
        message,
        default=default,
        style=WIZARD_STYLE
    ).ask()
    if result is None:
        raise typer.Exit(code=1)
    return result


def _prompt_database_config() -> tuple[str, Optional[str]]:
    provider_input = _prompt_select(
        "Database provider",
        choices=["sqlite", "postgresql", "mysql"],
    )
    provider = _normalize_provider(provider_input)

    database_url: Optional[str] = None
    if provider in {"postgresql", "mysql"}:
        url_input = _prompt_text(
            "Database URL (leave blank to set later)",
            default="",
        )
        database_url = url_input or None

    return provider, database_url


def _prompt_auth_mode() -> str:
    return _prompt_select(
        "Authentication mode",
        choices=[
            "open",
            "single",
            "multi",
        ],
    )


def _check_database_driver(provider: str) -> bool:
    if provider != "postgresql":
        return True

    return importlib.util.find_spec("psycopg2") is not None


def _list_dist_plugins(dist_dir: Path) -> list[str]:
    if not dist_dir.exists():
        return []

    plugin_names: list[str] = []
    for entry in dist_dir.iterdir():
        if entry.is_dir() and (entry / "plugin.py").exists():
            plugin_names.append(entry.name)
    return sorted(plugin_names)


def _copy_plugin(src_dir: Path, dest_dir: Path) -> None:
    if dest_dir.exists():
        raise FileExistsError(f"Plugin already exists at {dest_dir}")
    shutil.copytree(src_dir, dest_dir)


def _install_plugins_from_dist(target_plugins_dir: Path, plugin_names: Iterable[str]) -> None:
    if not plugin_names:
        return

    dist_dir = _get_dist_plugins_dir()
    if not dist_dir.exists():
        typer.secho(
            f"⚠️  plugins-dist not found at {dist_dir}. Skipping plugin install.",
            fg=typer.colors.YELLOW,
        )
        return

    available = set(_list_dist_plugins(dist_dir))
    selected = [name for name in plugin_names if name in available]
    missing = [name for name in plugin_names if name not in available]

    if missing:
        typer.secho(
            f"⚠️  Missing plugins in dist: {', '.join(missing)}",
            fg=typer.colors.YELLOW,
        )

    if not selected:
        typer.secho("No matching plugins found to install.",
                    fg=typer.colors.YELLOW)
        return

    typer.secho(
        "DESTRUCTIVE: This will copy plugins into your workspace plugins directory.",
        fg=typer.colors.YELLOW,
        bold=True,
    )
    typer.echo(f"Workspace plugins dir: {target_plugins_dir}")
    typer.echo("Only the workspace plugins directory will be touched.")
    typer.echo("No files will be deleted unless you confirm overwrites.")
    proceed = _prompt_confirm("Proceed with plugin installation?")
    if not proceed:
        typer.secho("Skipped plugin installation.", fg=typer.colors.YELLOW)
        return

    target_plugins_dir.mkdir(parents=True, exist_ok=True)

    for name in selected:
        src_dir = dist_dir / name
        dest_dir = target_plugins_dir / name
        if dest_dir.exists():
            overwrite = _prompt_confirm(
                f"Plugin '{name}' already exists in workspace. Overwrite?",
                default=False,
            )
            if not overwrite:
                continue
            shutil.rmtree(dest_dir)
        _copy_plugin(src_dir, dest_dir)
        typer.secho(f"✅ Installed {name}", fg=typer.colors.GREEN)


def _resolve_pack_plugins(pack_id: str) -> list[str]:
    """Recursively resolve plugins for a pack, handling inheritance."""
    if pack_id not in PACK_DEFINITIONS:
        return []

    pack = PACK_DEFINITIONS[pack_id]
    plugins = set(pack.get("plugins", []))

    for parent_id in pack.get("includes", []):
        plugins.update(_resolve_pack_plugins(parent_id))

    return sorted(list(plugins))


def _prompt_plugin_pack() -> tuple[str, list[str]]:
    available = _list_dist_plugins(_get_dist_plugins_dir())
    # Even if dist is empty, we show the menu so the user sees the intent (they might just skip or we warn later)

    choices = []
    default_choice = None

    # Add hardcoded packs
    for pack_id, pack_def in PACK_DEFINITIONS.items():
        c = questionary.Choice(
            f"{pack_def['name']} - {pack_def['description']}",
            value=pack_id,
        )
        choices.append(c)
        if pack_id == "standard":
            default_choice = c

    # Add dynamic/manual options
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(
        "Custom (Select individual plugins)", value="custom"))
    choices.append(questionary.Choice(
        "Skip plugin installation", value="skip"))

    choice = questionary.select(
        "Select a Plugin Pack",
        choices=choices,
        default=default_choice,
        style=WIZARD_STYLE,
        use_indicator=True,
    ).ask()

    if choice is None:
        raise typer.Exit(code=1)

    if choice == "skip":
        return "skip", []

    if choice == "custom":
        if not available:
            typer.secho(
                "No plugins found in distribution directory to select from.", fg=typer.colors.RED)
            return "custom", []

        selections = questionary.checkbox(
            "Select plugins to install",
            choices=available,
            style=WIZARD_STYLE,
        ).ask()
        if selections is None:
            raise typer.Exit(code=1)
        return "custom", [name for name in selections if name]

    # It's a pack
    if choice in PACK_DEFINITIONS:
        return choice, _resolve_pack_plugins(choice)

    return "unknown", []


def _maybe_warn_comfyui(plugin_names: list[str]) -> list[str]:
    if "embeddr-comfyui" not in plugin_names:
        return plugin_names

    CONSOLE.print(
        Panel.fit(
            "[bold yellow]ComfyUI setup required[/bold yellow]\n"
            "- Install and run ComfyUI separately.\n"
            "- Configure the ComfyUI endpoint/API key in Embeddr.\n"
            "- Install the Embeddr ComfyUI nodepack for required nodes.\n"
            "See docs: https://docs.embeddr.net",
            box=box.ROUNDED,
            style="yellow",
        )
    )

    proceed = _prompt_confirm("Continue with the ComfyUI plugin selected?")
    if proceed:
        return plugin_names

    filtered = [name for name in plugin_names if name != "embeddr-comfyui"]
    typer.secho(
        "Removed embeddr-comfyui from the selection.",
        fg=typer.colors.YELLOW,
    )
    return filtered


def _get_dist_plugins_dir() -> Path:
    env_value = os.environ.get("EMBEDDR_PLUGINS_DIST_DIR")
    if env_value:
        env_dir = Path(env_value).expanduser()
        if env_dir.exists():
            return env_dir
    return DEFAULT_DIST_PLUGINS_DIR


def _prompt_path(label: str, default_value: str) -> str:
    value = _prompt_text(label, default=default_value)
    return value or default_value


def _resolve_path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return root / candidate


def run_init_wizard(
    path: Path,
    name: Optional[str] = None,
    run_db_migrations: bool = True,
) -> None:
    if (path / CONFIG_FILENAME).exists():
        typer.secho(
            f"Project already initialized at {path}", fg=typer.colors.YELLOW
        )
        return

    CONSOLE.print(
        Panel.fit(
            "[bold]Embeddr Project Setup[/bold]\n"
            "Paths can be relative to the project root or absolute.",
            box=box.ROUNDED,
            style="cyan",
        )
    )

    if name:
        project_name = name
    else:
        project_name = _prompt_text("Project name", default=path.name)

    data_dir_input = _prompt_path(
        "Workspace data directory (relative to project root)",
        ".embeddr",
    )
    plugins_dir_input = _prompt_path(
        "Workspace plugins directory (relative to project root)",
        ".embeddr/plugins",
    )
    provider, database_url = _prompt_database_config()
    auth_mode = _prompt_auth_mode()
    operator_name: Optional[str] = None
    admin_username: Optional[str] = None
    if auth_mode != "open":
        default_operator = "local" if auth_mode == "single" else "server-admin"
        default_admin = "local" if auth_mode == "single" else "admin"
        operator_name = _prompt_text("Operator name", default=default_operator)
        admin_username = _prompt_text("Admin username", default=default_admin)
    if not _check_database_driver(provider):
        CONSOLE.print(
            Panel.fit(
                "[bold red]PostgreSQL driver not installed[/bold red]\n"
                "Embeddr requires psycopg2 to initialize a Postgres workspace.\n"
                "Install psycopg2 (or psycopg2-binary) and rerun the initializer.",
                box=box.ROUNDED,
                style="red",
            )
        )
        typer.secho(
            "Initialization cancelled: missing PostgreSQL driver.",
            fg=typer.colors.YELLOW,
        )
        return
    pack_id, plugin_names = _prompt_plugin_pack()
    plugin_names = _maybe_warn_comfyui(plugin_names)

    resolved_data_dir = _resolve_path(path, data_dir_input)
    resolved_plugins_dir = _resolve_path(path, plugins_dir_input)

    # Resolve pack display name
    if pack_id in PACK_DEFINITIONS:
        pack_display = PACK_DEFINITIONS[pack_id]["name"]
    elif pack_id == "custom":
        pack_display = f"Custom ({len(plugin_names)} plugins)"
    elif pack_id == "skip":
        pack_display = "Skipped"
    else:
        pack_display = pack_id

    summary = Table(show_header=False, box=box.SIMPLE)
    summary.add_row("Project", project_name)
    summary.add_row("Project root", str(path))
    summary.add_row("Data dir", f"{data_dir_input} → {resolved_data_dir}")
    summary.add_row(
        "Plugins dir", f"{plugins_dir_input} → {resolved_plugins_dir}")
    summary.add_row("DB provider", provider)
    summary.add_row("DB URL", database_url or "(none)")
    summary.add_row("Plugin pack", pack_display)
    summary.add_row("Auth mode", auth_mode)
    if auth_mode != "open":
        summary.add_row("Operator", operator_name or "")
        summary.add_row("Admin user", admin_username or "")

    if plugin_names:
        summary.add_row("Plugins", ", ".join(plugin_names))

    CONSOLE.print(Panel(summary, title="Summary", box=box.ROUNDED))

    CONSOLE.print(
        Panel.fit(
            "[bold yellow]DESTRUCTIVE[/bold yellow]: This will create project files and run database migrations if needed.\n"
            f"Target workspace: {path}\n"
            "Only files inside this workspace directory are affected.\n"
            "No files will be deleted unless you confirm overwrites.",
            box=box.ROUNDED,
            style="yellow",
        )
    )
    proceed = _prompt_confirm("Proceed with project initialization?")
    if not proceed:
        typer.secho("Initialization cancelled.", fg=typer.colors.YELLOW)
        return

    create_default_config(
        path,
        project_name,
        provider,
        database_url,
        data_dir_input,
        plugins_dir_input,
        auth_mode,
    )
    typer.secho(
        f"Initialized Embeddr project at {path}", fg=typer.colors.GREEN)
    typer.echo(f"Created {CONFIG_FILENAME}")

    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    resolved_plugins_dir.mkdir(parents=True, exist_ok=True)

    os.environ["EMBEDDR_DATA_DIR"] = str(resolved_data_dir)
    os.environ["EMBEDDR_DB_PROVIDER"] = provider
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    os.environ["EMBEDDR_AUTH_MODE"] = auth_mode
    refresh_settings()
    get_engine.cache_clear()

    if run_db_migrations:
        if provider == "sqlite":
            db_path = resolved_data_dir / "embeddr.db"
            if db_path.exists():
                reset_db = _prompt_confirm(
                    f"Existing database found at {db_path}. Replace it?",
                    default=True,
                )
                if reset_db:
                    db_path.unlink()
        run_migrations()

    if auth_mode != "open" and operator_name and admin_username:
        admin_username_value = None
        raw_key_value = None
        with Session(get_engine()) as session:
            result = auth_service.bootstrap_operator_flow(
                session,
                mode=auth_mode,
                operator_name=operator_name,
                admin_username=admin_username,
            )
            if result:
                user, api_key, raw_key = result
                admin_username_value = user.username
                raw_key_value = raw_key
        if admin_username_value and raw_key_value:
            CONSOLE.print(
                Panel.fit(
                    "[bold green]Bootstrap complete[/bold green]\n"
                    f"Operator: {operator_name}\n"
                    f"Admin user: {admin_username_value}\n"
                    f"Client Key: {raw_key_value}\n"
                    "Store this key securely. It will not be shown again.",
                    box=box.ROUNDED,
                    style="green",
                )
            )

    if plugin_names:
        _install_plugins_from_dist(resolved_plugins_dir, plugin_names)
