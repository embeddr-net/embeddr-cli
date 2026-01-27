from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from uuid import UUID, uuid4

from PIL import Image, ImageOps
from sqlmodel import Session

from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact_relation import ArtifactRelation
from embeddr.services.transform_registry import register_transform


def _resolve_image_input(
    image_input: Any,
    session: Optional[Session],
) -> Tuple[Path, Optional[UUID]]:
    if image_input is None:
        raise ValueError("Missing required input: image")

    parent_id: Optional[UUID] = None
    image_path: Optional[Path] = None

    if isinstance(image_input, dict):
        if image_input.get("id"):
            image_input = image_input["id"]
        elif image_input.get("artifact_id"):
            image_input = image_input["artifact_id"]
        elif image_input.get("uri"):
            image_input = image_input["uri"]
        elif image_input.get("path"):
            image_input = image_input["path"]

    if isinstance(image_input, str):
        if image_input.startswith("file://"):
            image_path = Path(image_input.replace("file://", ""))
        else:
            try:
                parent_id = UUID(image_input)
            except Exception:
                image_path = Path(image_input)

    if parent_id and session:
        artifact = session.get(Artifact, parent_id)
        if not artifact:
            raise ValueError(f"Artifact {parent_id} not found")
        if not artifact.uri or not artifact.uri.startswith("file://"):
            raise ValueError("Artifact must reference a local file")
        image_path = Path(artifact.uri.replace("file://", ""))

    if not image_path:
        raise ValueError("Unable to resolve image input")

    if not image_path.exists():
        raise ValueError(f"Image file not found: {image_path}")

    return image_path, parent_id


async def execute_image_flip(
    inputs: Dict[str, Any],
    output_dir: str,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    image_path, parent_id = _resolve_image_input(inputs.get("image"), session)
    flip_horizontal = bool(inputs.get("flip_horizontal", False))

    with Image.open(image_path) as img:
        if flip_horizontal:
            img = ImageOps.mirror(img)

        os.makedirs(output_dir, exist_ok=True)
        output_filename = f"workflow_flip_{uuid4().hex}.png"
        output_path = Path(output_dir) / output_filename
        img.save(output_path)

        if not session:
            return {"image": {"uri": f"file://{output_path}"}}

        new_artifact = Artifact(
            id=uuid4(),
            type_name="image",
            uri=f"file://{output_path}",
            metadata_json={
                "name": "Flipped Image",
                "width": img.width,
                "height": img.height,
                "source": {
                    "kind": "core-transform",
                    "operation": "image_flip",
                },
                "parent_artifact_id": str(parent_id) if parent_id else None,
            },
        )
        session.add(new_artifact)

        if parent_id:
            relation = ArtifactRelation(
                source_id=new_artifact.id,
                target_id=parent_id,
                relation_type="derived_from",
                source_namespace="core-transform",
            )
            lineage = ArtifactLineage(
                parent_id=parent_id,
                child_id=new_artifact.id,
                relationship_metadata={
                    "operation": "image_flip",
                    "flip_horizontal": flip_horizontal,
                },
            )
            session.add(relation)
            session.add(lineage)

        session.commit()
        session.refresh(new_artifact)

        return {
            "image": {
                "id": str(new_artifact.id),
                "uri": new_artifact.uri,
            }
        }


def register_image_flip_transform() -> None:
    register_transform("image_flip", execute_image_flip)


register_image_flip_transform()
