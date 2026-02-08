import logging
from datetime import datetime, timezone

from embeddr_core.models.artifact import Artifact
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr.core.plugin_loader import _EVENT_BUS

logger = logging.getLogger(__name__)


def publish_artifact_created(artifact: Artifact, source: str) -> None:
    if not artifact:
        return

    try:
        created_at = (
            artifact.created_at.isoformat()
            if getattr(artifact, "created_at", None)
            else datetime.now(timezone.utc).isoformat()
        )
        event_payload = {
            "id": str(artifact.id),
            "uri": artifact.uri,
            "type_name": artifact.type_name,
            "metadata_json": artifact.metadata_json,
            "created_at": created_at,
        }
        _EVENT_BUS.publish(
            EmbeddrEvent(
                event_type="artifact.created",
                source=source,
                payload=event_payload,
            )
        )
    except Exception as exc:
        logger.warning("Failed to publish artifact.created", exc_info=exc)
