# Just a marker file to simulate a registry
from typing import Dict, Any, Callable
from embeddr_core.models.workflow import WorkflowArtifactMetadata

_registry: Dict[str, Callable[[], WorkflowArtifactMetadata]] = {}


def register_template(name: str, factory: Callable[[], WorkflowArtifactMetadata]):
    _registry[name] = factory


def get_template(name: str) -> WorkflowArtifactMetadata:
    if name in _registry:
        return _registry[name]()
    # Fallback to empty if not found
    from embeddr.defaults.transformations import get_empty_template
    return get_empty_template()


def list_templates() -> Dict[str, str]:
    # Return name -> description (if we had descriptions)
    return {name: name for name in _registry.keys()}
