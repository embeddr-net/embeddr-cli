from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from embeddr.core.plugin_loader import get_lotus_registry
from embeddr_core.models.lotus import LotusCapability, LotusKind


logger = logging.getLogger("embeddr.resources.adapter-registry")


def _trace_enabled() -> bool:
    return os.environ.get("EMBEDDR_RESOURCE_TRACE") == "1" or os.environ.get("EMBEDDR_LOTUS_TRACE") == "1"


def _cap_exposes_api(cap: LotusCapability) -> bool:
    action = getattr(cap, "action", None)
    expose = (getattr(action, "expose", None) or {}) if action else {}
    if not expose:
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
    caps += reg.list(kind=LotusKind.resolver, slot="resource.adapter")

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
    uri_prefixes = _matches_prefix(url, match.get("uri_prefixes"))

    scheme = ""
    try:
        scheme = urlparse(url).scheme or ""
    except Exception:
        scheme = ""
    uri_schemes = match.get("uri_schemes") or []
    scheme_match = [scheme] if scheme and scheme in uri_schemes else []

    path = _safe_path(url)
    path_prefixes = _matches_prefix(path, match.get("path_prefixes"))
    url_contains = [p for p in (
        match.get("url_contains") or []) if p and p in url]
    path_contains = [p for p in (
        match.get("path_contains") or []) if p and p in path]

    max_prefix = max(
        [len(p) for p in (prefixes + uri_prefixes + path_prefixes)],
        default=0,
    )
    if scheme_match:
        max_prefix = max(max_prefix, len(scheme) + 3)

    max_contains = max(
        [len(p) for p in (url_contains + path_contains)],
        default=0,
    )
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
        if cap and cap.kind in (LotusKind.action, LotusKind.resolver):
            return cap
        return None

    caps = [
        c
        for c in (
            reg.list(kind=LotusKind.action, slot="resource.adapter")
            + reg.list(kind=LotusKind.resolver, slot="resource.adapter")
        )
        if _cap_exposes_api(c)
    ]

    if _trace_enabled():
        logger.warning(
            "[ResourceTrace] select_resource_adapter artifact_id=%s url=%s adapter_id=%s candidates=%s",
            artifact_id,
            url,
            adapter_id,
            [
                {
                    "id": c.id,
                    "plugin": c.plugin,
                    "kind": str(c.kind),
                    "slot": c.slot,
                }
                for c in caps
            ],
        )

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
            if _trace_enabled():
                logger.warning(
                    "[ResourceTrace] adapter score id=%s plugin=%s prefix=%s contains=%s match=%s",
                    cap.id,
                    cap.plugin,
                    max_prefix,
                    max_contains,
                    match,
                )
            if max_prefix > 0 or max_contains > 0:
                scored.append((max_prefix, max_contains, cap))

        if scored:
            scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
            if _trace_enabled():
                logger.warning(
                    "[ResourceTrace] selected adapter id=%s plugin=%s score=%s",
                    scored[0][2].id,
                    scored[0][2].plugin,
                    (scored[0][0], scored[0][1]),
                )
            return scored[0][2]

    if _trace_enabled():
        logger.warning("[ResourceTrace] no adapter selected")

    return None
