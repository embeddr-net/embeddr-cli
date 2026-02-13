from typing import Any, Dict
from embeddr_core.models.workflow import (
    WorkflowArtifactMetadata,
    WorkflowImplementation,
    WorkflowPort,
)
from embeddr.services.template_registry import register_template


def get_empty_template() -> WorkflowArtifactMetadata:
    return WorkflowArtifactMetadata(
        inputs={},
        outputs={},
        implementation=WorkflowImplementation(
            type="core-transform",
            payload={}
        )
    )


def get_image_flip_template() -> WorkflowArtifactMetadata:
    return WorkflowArtifactMetadata(
        inputs={
            "image": WorkflowPort(
                name="image",
                type="image",
                description="Source image artifact",
                exposure=1,
            ),
            "flip_horizontal": WorkflowPort(
                name="flip_horizontal",
                type="boolean",
                description="Flip the image horizontally",
                default=False,
                exposure=1,
            ),
            "confirm_write": WorkflowPort(
                name="confirm_write",
                type="boolean",
                description="Confirm this workflow writes files",
                default=False,
                exposure=1,
            ),
        },
        outputs={
            "image": WorkflowPort(
                name="image",
                type="image",
                description="Flipped image artifact",
                exposure=1,
            )
        },
        side_effects=[],
        implementation=WorkflowImplementation(
            type="lotus-action",
            payload={"capability_id": "embeddr-transforms.image_flip"},
        ),
    )


def get_image_ingest_default_template() -> WorkflowArtifactMetadata:
    return WorkflowArtifactMetadata(
        inputs={
            "image": WorkflowPort(
                name="image",
                type="image",
                description="Source image artifact",
                exposure=1,
            ),
        },
        outputs={},
        side_effects=[],
        implementation=WorkflowImplementation(
            type="lotus-composed",
            payload={
                "steps": [
                    {
                        "capability_id": "preview.thumbnail.generate",
                        "inputs": {"artifact_id": "${inputs.image}"},
                    },
                    {
                        "capability_id": "embeddr-embeddings.generate",
                        "inputs": {"artifact_id": "${inputs.image}"},
                        "note": "placeholder for embedding capability",
                    },
                ]
            },
        ),
    )


# Register default templates
register_template("empty", get_empty_template)
register_template("image_flip", get_image_flip_template)
register_template("image_ingest_default", get_image_ingest_default_template)
