import typer
import sys
import subprocess
import os
import re
import importlib
import importlib.util
from pathlib import Path
from typing import Optional, List, Tuple
from importlib import metadata as importlib_metadata

from embeddr.core.config import get_data_dir

app = typer.Typer(help="Manage Embeddr Plugins")


_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_TOP_LEVEL_SEPARATOR = re.compile(r"\s+")


def _iter_distribution_modules(dist_name: str) -> List[str]:
    try:
        dist = importlib_metadata.distribution(dist_name)
    except importlib_metadata.PackageNotFoundError:
        return [dist_name.replace("-", "_")]

    top_level = dist.read_text("top_level.txt")
    if not top_level:
        return [dist_name.replace("-", "_")]

    modules: List[str] = []
    for raw in top_level.splitlines():
        name = _TOP_LEVEL_SEPARATOR.split(raw.strip())[0]
        if name:
            modules.append(name)

    return modules or [dist_name.replace("-", "_")]


def _parse_requirement_name(line: str) -> Optional[str]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("-r") or line.startswith("--"):
        return None
    name = _REQ_LINE_RE.match(line)
    if not name:
        return None
    return name.group(1)


def _missing_requirements(req_file: Path) -> List[str]:
    missing: List[str] = []
    for raw in req_file.read_text().splitlines():
        name = _parse_requirement_name(raw)
        if not name:
            continue
        try:
            importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            missing.append(name)
    return missing


def _missing_imports(req_file: Path) -> List[str]:
    missing: List[str] = []
    for raw in req_file.read_text().splitlines():
        name = _parse_requirement_name(raw)
        if not name:
            continue
        module_candidates = _iter_distribution_modules(name)
        import_errors: List[str] = []
        resolved = False
        for mod in module_candidates:
            try:
                if importlib.util.find_spec(mod) is None:
                    import_errors.append(f"{mod}: not found")
                    continue
                importlib.import_module(mod)
                resolved = True
                break
            except Exception as exc:
                import_errors.append(f"{mod}: {type(exc).__name__}: {exc}")

        if not resolved:
            detail = "; ".join(
                import_errors) if import_errors else "import failed"
            missing.append(f"{name} ({detail})")
    return missing


def _get_plugin_search_paths(override: Optional[Path] = None) -> List[Path]:
    paths = []

    if override:
        paths.append(override)
        return paths

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


@app.command("deps")
def check_deps(
    install: bool = typer.Option(
        False, "--install", help="Install missing requirements"),
    upgrade: bool = typer.Option(False, help="Upgrade packages on install"),
    repair: bool = typer.Option(
        False,
        "--repair",
        help="Force reinstall when import checks fail",
    ),
    plugins_dir: Optional[Path] = typer.Option(
        None,
        "--plugins-dir",
        help="Override plugin search path",
    ),
):
    """
    Check (and optionally install) plugin requirements across all plugins.
    """
    search_paths = _get_plugin_search_paths(plugins_dir)
    rows: List[Tuple[str, Path, List[str]]] = []

    for base_path in search_paths:
        if not base_path.exists():
            continue
        for plugin_dir in base_path.iterdir():
            if not plugin_dir.is_dir():
                continue
            if not (plugin_dir / "plugin.py").exists():
                continue
            req_file = plugin_dir / "requirements.txt"
            if not req_file.exists():
                continue
            missing = _missing_requirements(req_file)
            missing_imports = _missing_imports(req_file)
            rows.append((plugin_dir.name, req_file, missing, missing_imports))

    if not rows:
        typer.echo("ℹ️  No plugin requirements found.")
        return

    for name, req_file, missing, missing_imports in rows:
        if missing:
            typer.secho(
                f"❌ {name} missing: {', '.join(missing)}", fg=typer.colors.RED)
        elif missing_imports:
            typer.secho(
                f"⚠️  {name} import check failed: {', '.join(missing_imports)}",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.secho(f"✅ {name} dependencies OK", fg=typer.colors.GREEN)

        if install and (missing or missing_imports):
            typer.echo(f"📦 Installing dependencies for {name}...")
            force_reinstall = repair and bool(missing_imports)
            try:
                cmd = ["uv", "pip", "install", "-r", str(req_file)]
                if upgrade:
                    cmd.append("--upgrade")
                if force_reinstall:
                    cmd.append("--force-reinstall")
                subprocess.check_call(cmd)
            except (FileNotFoundError, subprocess.CalledProcessError):
                cmd = [sys.executable, "-m", "pip",
                       "install", "-r", str(req_file)]
                if upgrade:
                    cmd.append("--upgrade")
                if force_reinstall:
                    cmd.append("--force-reinstall")
                subprocess.check_call(cmd)
