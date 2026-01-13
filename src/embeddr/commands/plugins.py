import typer
import sys
import subprocess
import os
from pathlib import Path
from typing import Optional, List

from embeddr.core.config import get_data_dir

app = typer.Typer(help="Manage Embeddr Plugins")


def _get_plugin_search_paths() -> List[Path]:
    paths = []

    # 1. Env Var
    env_dir = os.environ.get("EMBEDDR_PLUGINS_DIR")
    if env_dir:
        paths.append(Path(env_dir))

    # 2. User Data Dir
    paths.append(get_data_dir() / "plugins")

    # 3. Repo/Dev Dir (relative to this file)
    # embeddr/commands/plugins.py -> src/embeddr/commands/ -> src/embeddr -> src -> embeddr-cli -> root -> embeddr-plugins
    repo_plugins = Path(__file__).resolve().parents[4] / "embeddr-plugins"
    if repo_plugins.exists():
        paths.append(repo_plugins)

    return paths


@app.command("install-deps")
def install_deps(
    plugin_name: str = typer.Argument(...,
                                      help="Name of the plugin directory"),
    upgrade: bool = typer.Option(False, help="Upgrade packages")
):
    """
    Install Python dependencies for a specific plugin from requirements.txt.
    """
    search_paths = _get_plugin_search_paths()
    plugin_path: Optional[Path] = None

    typer.echo(f"🔎 Searching for plugin '{plugin_name}'...")

    for base_path in search_paths:
        candidate = base_path / plugin_name
        if candidate.exists() and candidate.is_dir():
            plugin_path = candidate
            break

    if not plugin_path:
        typer.secho(
            f"❌ Plugin '{plugin_name}' not found in search paths:", fg=typer.colors.RED)
        for p in search_paths:
            typer.echo(f"  - {p}")
        raise typer.Exit(1)

    typer.secho(f"✅ Found plugin at: {plugin_path}", fg=typer.colors.GREEN)

    req_file = plugin_path / "requirements.txt"
    if not req_file.exists():
        typer.secho(
            f"ℹ️  No requirements.txt found for {plugin_name}", fg=typer.colors.YELLOW)
        return

    typer.echo(f"📦 Installing dependencies from {req_file}...")

    # Try using uv pip first if available, as it's faster and cleaner
    try:
        subprocess.check_call(["uv", "pip", "install", "-r", str(req_file)])
        typer.secho(f"✅ Dependencies installed successfully (via uv)!",
                    fg=typer.colors.GREEN)
        return
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass  # Fallback to pip

    # Fallback to python -m pip
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
    if upgrade:
        cmd.append("--upgrade")

    try:
        subprocess.check_call(cmd)
        typer.secho(
            f"✅ Dependencies installed successfully for {plugin_name}!", fg=typer.colors.GREEN)
    except subprocess.CalledProcessError as e:
        typer.secho(
            f"❌ Failed to install dependencies: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command("list")
def list_plugins():
    """List available plugins and their paths."""
    search_paths = _get_plugin_search_paths()
    seen = set()

    typer.secho("🔌 Available Plugins:", bold=True)

    for base_path in search_paths:
        if not base_path.exists():
            continue

        typer.secho(f"\n  In {base_path}:", fg=typer.colors.BLUE)
        for plugin_dir in base_path.iterdir():
            if plugin_dir.is_dir() and plugin_dir.name not in seen:
                # Check if it looks like a plugin (has plugin.py)
                if (plugin_dir / "plugin.py").exists():
                    has_reqs = (plugin_dir / "requirements.txt").exists()
                    req_badge = "📦" if has_reqs else "  "
                    typer.echo(f"    {req_badge} {plugin_dir.name}")
                    seen.add(plugin_dir.name)
