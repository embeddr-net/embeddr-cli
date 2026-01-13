import logging
import json
import asyncio
from typing import Dict, Any, List, Optional
from sqlmodel import Session, select
from embeddr_core.models.automation import Automation
from embeddr.core.execution_spine import ExecutionSpine
from embeddr.db.session import get_engine
from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger("embeddr.automation")


class AutomationManager:
    def __init__(self):
        # Create a dedicated engine for automation worker to avoid pool starvation from API
        # We manually call create_engine instead of get_engine to get a fresh pool
        from embeddr.core.config import settings
        from sqlmodel import create_engine
        from sqlalchemy.pool import QueuePool

        connect_args = {"check_same_thread": False,
                        "timeout": 30} if "sqlite" in settings.DATABASE_URL else {}

        # Dedicated pool for automation worker
        # Max concurrency is usually 1 here, so pool_size 2 is plenty.
        self.engine = create_engine(
            settings.DATABASE_URL,
            connect_args=connect_args,
            poolclass=QueuePool,
            pool_size=2,
            max_overflow=5
        )

        self.queue: Optional[asyncio.Queue] = None
        self.is_running = False
        self.worker_task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self):
        """Start the background worker."""
        if self.is_running:
            return

        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.is_running = True
        self.worker_task = asyncio.create_task(self._worker())
        logger.info("AutomationManager V2: Started background worker 🚀")

    async def handle_event(self, event: EmbeddrEvent):
        """
        Enqueue event for evaluation. Can be called from any thread.
        """
        if not self.is_running or not self.queue or not self.loop:
            # If not started, likely during shutdown or before startup
            return

        # Thread-safe enqueue
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except Exception:
            # Loop might be closed or we are in a weird state
            pass

    async def _worker(self):
        """
        Evaluate event against all active automations (running in worker).
        """
        # Using a new session for thread safety
        with Session(self.engine) as session:
            # Fetch all automations that match this event type
            rules = session.exec(
                select(Automation).where(
                    Automation.is_active == True,
                    Automation.trigger_event == event.event_type
                )
            ).all()

            for rule in rules:
                if self._matches(rule, event):
                    self._execute_rule(rule, event)

    def _matches(self, rule: Automation, event: EmbeddrEvent) -> bool:
        """
        Check if event payload matches rule conditions.
        Supported logic: Simple key-value exact match for now.
        e.g. { "type": "image" } in conditions means payload["type"] must be "image"
        """
        conditions = rule.trigger_conditions
        if not conditions:
            return True

        payload = event.payload
        if not payload:
            return False

        # Recursive check helper
        def check_condition(cond: Dict, data: Dict) -> bool:
            for k, v in cond.items():
                if k not in data:
                    return False
                if isinstance(v, dict) and isinstance(data[k], dict):
                    if not check_condition(v, data[k]):
                        return False
                elif data[k] != v:
                    return False
            return True

        return check_condition(conditions, payload)

    def _execute_rule(self, rule: Automation, event: EmbeddrEvent):
        """
        Trigger the actions defined in the rule.
        """
        logger.info(
            f"Automation '{rule.name}' triggered by {event.event_type}")

        for action in rule.actions:
            job_type = action.get("job_type")
            inputs = action.get("inputs", {}).copy()

            # Simple variable substitution
            # e.g. inputs["artifact_id"] = "$payload.id"
            for k, v in inputs.items():
                if isinstance(v, str) and v.startswith("$payload."):
                    key = v.split("$payload.")[1]
                    if key in event.payload:
                        inputs[k] = event.payload[key]

            if job_type:
                # Use local engine to avoid global pool starvation
                # Manual job insertion instead of calling ExecutionSpine.submit_job
                from embeddr_core.models.artifact_execution import ArtifactExecution
                from embeddr_core.plugin_interface import EmbeddrEvent
                from embeddr.core.plugin_loader import _EVENT_BUS
                from sqlmodel import Session

                try:
                    with Session(self.engine) as session:
                        job = ArtifactExecution(
                            type=job_type,
                            plugin_name="automation",
                            inputs=inputs,
                            resource_class=action.get("resource_class", "cpu"),
                            priority=action.get("priority", 0),
                            trigger="automation",
                            status="pending"
                        )
                        session.add(job)
                        session.commit()
                        session.refresh(job)

                        # Fire event manually since we bypassed Spine.submit_job
                        _EVENT_BUS.publish(EmbeddrEvent(
                            event_type="execution.created",
                            source="spine",
                            payload={
                                "id": str(job.id),
                                "type": job.type,
                                "status": "pending",
                                "plugin_name": job.plugin_name,
                                "created_at": job.created_at.isoformat()
                            }
                        ))
                        logger.info(
                            f"AutomationManager V2: Submitted job {job.id} ({job_type})")
                except Exception as e:
                    logger.error(
                        f"AutomationManager V2: Failed to submit job {job_type}: {e}")


# Global instance
automation_manager = AutomationManager()
