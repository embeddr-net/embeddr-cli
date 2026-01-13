from typing import Dict, Any, Optional
from sqlmodel import Session
from embeddr_core.models.workflow import WorkflowArtifactMetadata
from embeddr.services.transform_registry import get_transform_handler


class NativeExecutor:
    def can_execute(self, impl_type: str) -> bool:
        return impl_type == "core-transform"

    async def execute(
        self,
        workflow_meta: WorkflowArtifactMetadata,
        inputs: Dict[str, Any],
        output_dir: str,
        session: Optional[Session] = None
    ) -> Dict[str, Any]:
        """
        Executes a core-transform workflow.
        Returns a dictionary of outputs.
        """
        payload = workflow_meta.implementation.payload
        operation = payload.get("operation", "unknown")

        handler = get_transform_handler(operation)
        if not handler:
            raise ValueError(
                f"Unknown or unregistered core-transform operation: {operation}")

        return await handler(inputs, output_dir, session)


native_executor = NativeExecutor()
