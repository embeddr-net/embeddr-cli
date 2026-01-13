from typing import Any, Dict
from embeddr_core.models.workflow import WorkflowArtifactMetadata, WorkflowImplementation
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


# Register default templates
register_template("empty", get_empty_template)
