from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()


def _get_themes_root() -> Path | None:
    try:
        cli_root = Path(__file__).resolve().parents[4]
        repo_root = cli_root.parent
        themes_root = repo_root / "embeddr-themes" / "themes"
        return themes_root if themes_root.exists() else None
    except Exception:
        return None


@router.get("")
def list_themes() -> dict[str, list[dict[str, Any]]]:
    themes_root = _get_themes_root()
    if not themes_root:
        return {"packs": []}

    packs: list[dict[str, Any]] = []
    for theme_dir in sorted([p for p in themes_root.iterdir() if p.is_dir()]):
        manifest_path = theme_dir / "theme.json"
        if not manifest_path.exists():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        pack = dict(payload)
        icon = pack.pop("icon", None)
        banner = pack.pop("banner", None)
        css_file = pack.pop("cssFile", None)

        if icon and not pack.get("iconUrl"):
            pack["iconUrl"] = f"/themes/{theme_dir.name}/{icon}"
        if banner and not pack.get("bannerUrl"):
            pack["bannerUrl"] = f"/themes/{theme_dir.name}/{banner}"
        if css_file and not pack.get("cssUrl"):
            pack["cssUrl"] = f"/themes/{theme_dir.name}/{css_file}"

        packs.append(pack)

    return {"packs": packs}
