from typing import Dict, Any, Callable, Awaitable, Optional
from sqlmodel import Session

# Handler signature: (inputs: Dict[str, Any], output_dir: str, session: Optional[Session]) -> Dict[str, Any]
TransformHandler = Callable[[Dict[str, Any], str,
                             Optional[Session]], Awaitable[Dict[str, Any]]]

_transform_registry: Dict[str, TransformHandler] = {}


def register_transform(operation: str, handler: TransformHandler):
    """Register a handler for a specific core-transform operation."""
    _transform_registry[operation] = handler


def get_transform_handler(operation: str) -> Optional[TransformHandler]:
    return _transform_registry.get(operation)


def list_transforms() -> Dict[str, str]:
    return {k: str(v) for k, v in _transform_registry.items()}
