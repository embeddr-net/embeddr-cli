from __future__ import annotations

from typing import Any, Dict, Optional

from sqlmodel import Session

from embeddr_core.services.lotus_client import invoke_action_for_plugin


class LotusContext:
    """Helper for invoking Lotus actions inside plugins."""

    def __init__(self, *, session: Optional[Session] = None, context: Any = None) -> None:
        self._session = session
        self._context = context

    def invoke_action(self, cap_id: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return invoke_action_for_plugin(
            cap_id=cap_id,
            inputs=inputs,
            context=self._context,
            session=self._session,
        )
