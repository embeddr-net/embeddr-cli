from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


def _repo_root() -> Path:
    cli_root = Path(__file__).resolve().parents[3]
    return cli_root.parent


def _plugins_dist_base() -> Path:
    return _repo_root() / "embeddr-plugins" / "dist"


def find_plugin_dist_root(plugin_name: str) -> Optional[Path]:
    dist_base = _plugins_dist_base()
    if not dist_base.exists():
        return None

    flat = dist_base / plugin_name
    if flat.exists():
        return flat

    for prefix_dir in sorted(dist_base.iterdir()):
        if not prefix_dir.is_dir():
            continue
        candidate = prefix_dir / plugin_name
        if candidate.exists():
            return candidate

    return None


def find_plugin_dist_root_candidates(plugin_names: Iterable[str]) -> Optional[Path]:
    seen: set[str] = set()
    for name in plugin_names:
        cleaned = str(name).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        candidate = find_plugin_dist_root(cleaned)
        if candidate is not None:
            return candidate
    return None


def resolve_plugin_frontend_dist_dir(
    *,
    plugin_name: str,
    source_path: Optional[Path],
) -> Optional[Path]:
    if source_path and (source_path / "dist").exists():
        return source_path / "dist"

    names = [plugin_name]
    if source_path is not None:
        names.insert(0, source_path.name)

    dist_root = find_plugin_dist_root_candidates(names)
    if dist_root is None:
        return None

    dist_dir = dist_root / "dist"
    if dist_dir.exists():
        return dist_dir

    return None


def resolve_plugin_bundle_name(dist_dir: Path, bundle_base: str) -> Optional[str]:
    preferred_bundle = dist_dir / f"{bundle_base}.umd.js"
    if preferred_bundle.exists():
        return preferred_bundle.name

    fallback_bundle = dist_dir / "index.umd.js"
    if fallback_bundle.exists():
        return fallback_bundle.name

    return None


@dataclass(frozen=True)
class PluginStaticLayout:
    static_root: Path
    public_root: Path
    public_dist_root: Optional[Path]
    public_assets_root: Optional[Path]


def resolve_plugin_static_layout(plugin_name: str, source_path: Path) -> PluginStaticLayout:
    dist_root = find_plugin_dist_root(plugin_name)

    static_root = dist_root if dist_root and dist_root.exists() else source_path
    public_root = static_root
    if dist_root:
        public_web_root = dist_root / "web" / "dist"
        if public_web_root.exists():
            public_root = public_web_root

    public_dist_root: Optional[Path] = None
    if dist_root:
        candidate = dist_root / "dist"
        if candidate.exists():
            public_dist_root = candidate

    public_assets_root: Optional[Path] = None
    if dist_root:
        dist_assets_root = dist_root / "assets"
        if dist_assets_root.exists():
            public_assets_root = dist_assets_root

    if public_assets_root is None:
        source_assets_root = source_path / "assets"
        if source_assets_root.exists():
            public_assets_root = source_assets_root

    return PluginStaticLayout(
        static_root=static_root,
        public_root=public_root,
        public_dist_root=public_dist_root,
        public_assets_root=public_assets_root,
    )
