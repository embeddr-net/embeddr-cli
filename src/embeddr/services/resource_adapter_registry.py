from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from embeddr.core.plugin_loader import get_lotus_registry
from embeddr_core.models.lotus import LotusCapability, LotusKind


def _cap_exposes_api(cap: LotusCapability) -> bool:
    data = cap.data or {}
    expose = data.get("expose") or {}
    return bool(expose.get("api", False))


def _adapter_match(cap: LotusCapability) -> Dict[str, Any]:
    data = cap.data or {}
    adapter = data.get("adapter") or {}
    return adapter.get("match") or {}


def _matches_prefix(value: str, prefixes: List[str] | None) -> List[str]:
    if not value or not prefixes:
        return []
    return [p for p in prefixes if value.startswith(p)]


def _safe_path(value: str | None) -> str:
    if not value:
        return ""
    try:
        return urlparse(value).path or ""
    except Exception:
        return value


def list_resource_adapters() -> List[Dict[str, Any]]:
    reg = get_lotus_registry()
    caps = reg.list(kind=LotusKind.action, slot="resource.adapter")

    out: List[Dict[str, Any]] = []
    for cap in caps:
        if not _cap_exposes_api(cap):
            continue
        out.append(
            {
                "id": cap.id,
                "plugin": cap.plugin,
                "title": cap.title,
                "description": cap.description,
                "tags": cap.tags or [],
                "adapter": {
                    "match": _adapter_match(cap),
                },
            }
        )
    return out


def _score_url_match(url: str, match: Dict[str, Any]) -> Tuple[int, int]:
    prefixes = _matches_prefix(url, match.get("url_prefixes"))
    path = _safe_path(url)
    path_prefixes = _matches_prefix(path, match.get("path_prefixes"))
    url_contains = [p for p in (
        match.get("url_contains") or []) if p and p in url]
    path_contains = [p for p in (
        match.get("path_contains") or []) if p and p in path]

    max_prefix = max([len(p) for p in (prefixes + path_prefixes)], default=0)
    max_contains = max([len(p)
                       for p in (url_contains + path_contains)], default=0)
    return max_prefix, max_contains


def select_resource_adapter(
    *,
    artifact_id: Optional[str] = None,
    url: Optional[str] = None,
    adapter_id: Optional[str] = None,
) -> Optional[LotusCapability]:
    reg = get_lotus_registry()

    if adapter_id:
        cap = reg.get(adapter_id)
        if cap and cap.kind == LotusKind.action:
            return cap
        return None

    caps = [
        c for c in reg.list(kind=LotusKind.action, slot="resource.adapter") if _cap_exposes_api(c)
    ]

    if artifact_id:
        for cap in caps:
            match = _adapter_match(cap)
            if match.get("artifact_id"):
                return cap

    if url:
        scored: List[Tuple[int, int, LotusCapability]] = []
        for cap in caps:
            match = _adapter_match(cap)
            max_prefix, max_contains = _score_url_match(url, match)
            if max_prefix > 0 or max_contains > 0:
                scored.append((max_prefix, max_contains, cap))

        if scored:
            scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
            return scored[0][2]

    return None
