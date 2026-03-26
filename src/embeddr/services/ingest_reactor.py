"""Ingest reactor — automatically processes new artifacts.

Subscribes to artifact.created events and dispatches thumbnailing
and embedding jobs for eligible artifacts. This is the "just works"
pipeline that makes the happy path functional without manual config.

Runs as an event listener during the server lifespan.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Set

from embeddr.core.event_bus import _EVENT_BUS
from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger("embeddr.services.ingest_reactor")

# Artifact base types that should get thumbnails
_THUMBNAILABLE_TYPES: Set[str] = {"image", "video"}

# Artifact base types that should get embeddings
_EMBEDDABLE_TYPES: Set[str] = {"image", "video", "document"}

# Type prefixes/names that are system/internal — skip processing
_SKIP_TYPES: Set[str] = {
    "config", "agent", "text:llm_message", "text:system_prompt",
    "chat:conversation", "action",
}

_active = False


def start() -> None:
    """Subscribe to artifact events. Call once during server startup."""
    global _active
    if _active:
        return
    _active = True
    _EVENT_BUS.subscribe("artifact.created", _on_artifact_created)
    logger.info("Ingest reactor started — listening for artifact.created events")


def _on_artifact_created(event: EmbeddrEvent) -> None:
    """React to a new artifact by dispatching processing jobs."""
    payload = event.payload if isinstance(event, EmbeddrEvent) else event
    if not isinstance(payload, dict):
        return

    artifact_id = payload.get("id") or payload.get("artifact_id")
    if not artifact_id:
        return

    type_name = str(payload.get("type_name") or payload.get("type") or "").lower()
    base_type = type_name.split(":")[0] if ":" in type_name else type_name

    # Skip system/internal types entirely
    if type_name in _SKIP_TYPES or base_type in _SKIP_TYPES:
        return

    # Skip non-media artifacts
    if base_type not in _THUMBNAILABLE_TYPES and base_type not in _EMBEDDABLE_TYPES:
        return

    logger.debug(
        "Ingest reactor: processing artifact %s (type=%s)",
        artifact_id, type_name,
    )

    # Dispatch thumbnail job
    if base_type in _THUMBNAILABLE_TYPES:
        _dispatch_thumbnail(str(artifact_id))

    # Dispatch embedding job
    if base_type in _EMBEDDABLE_TYPES:
        _dispatch_embedding(str(artifact_id))


def _dispatch_thumbnail(artifact_id: str) -> None:
    """Submit a thumbnail generation job via ExecutionSpine."""
    try:
        from embeddr.core.execution_spine import ExecutionSpine

        ExecutionSpine.submit_job(
            job_type="preview.thumbnail.generate",
            inputs={"artifact_id": artifact_id, "preview_size": 300},
            plugin_name="embeddr-thumbnailer",
            resource_class="cpu",
            trigger="ingest_reactor",
        )
        logger.debug("Dispatched thumbnail job for %s", artifact_id)
    except Exception as e:
        logger.debug("Thumbnail dispatch skipped for %s: %s", artifact_id, e)


def _dispatch_embedding(artifact_id: str) -> None:
    """Submit an embedding generation job via ExecutionSpine."""
    try:
        from embeddr.core.execution_spine import ExecutionSpine
        from embeddr.core.plugin_loader import get_lotus_registry
        from embeddr_core.models.lotus import LotusKind

        # Check if an embedding generator is available
        reg = get_lotus_registry()
        generators = reg.list(slot="feature.generator")
        if not generators:
            return

        # Use the first available generator's action
        cap = generators[0]
        action = cap.action.action if cap.action and cap.action.action else None
        plugin = cap.plugin
        if not action or not plugin:
            return

        ExecutionSpine.submit_job(
            job_type=action,
            inputs={"artifact_id": artifact_id},
            plugin_name=plugin,
            resource_class="cpu",
            trigger="ingest_reactor",
        )
        logger.debug("Dispatched embedding job for %s via %s", artifact_id, plugin)
    except Exception as e:
        logger.debug("Embedding dispatch skipped for %s: %s", artifact_id, e)
