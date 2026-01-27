from typing import List, Dict, Any, Optional, Set
import logging
import asyncio
from uuid import UUID

from sqlmodel import Session, select
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.config import AutoAnalysisConfig
from embeddr_core.models.analysis_capability import AnalysisCapability
from embeddr_core.services.config_service import resolve_plugin_config
from embeddr.core.plugin_loader import get_plugins_by_intent, _EVENT_BUS
from embeddr_core.plugin_interface import PluginIntent, EmbeddrEvent
from embeddr.services.analysis_dispatcher import AnalysisDispatcher
from embeddr.db.session import get_session, get_engine
# from embeddr.services.action_manager import execute_action

logger = logging.getLogger(__name__)


class AutomationManager:
    """
    Central orchestrator for the Auto-Analysis Framework.
    Listens to system events and triggers relevant plugin actions based on configuration.
    """

    def __init__(self):
        self.bus = _EVENT_BUS
        self.is_running = False
        self.queue: Optional[asyncio.Queue] = None
        self.worker_tasks: List[asyncio.Task] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.concurrency = 1  # Single worker to prevent SQLite lock contention

        # Dedicated Engine for this worker to prevent pool starvation
        from embeddr.core.config import settings
        from sqlmodel import create_engine
        from sqlalchemy.pool import QueuePool

        connect_args = {"check_same_thread": False,
                        "timeout": 30} if "sqlite" in settings.DATABASE_URL else {}
        self.engine = create_engine(
            settings.DATABASE_URL,
            connect_args=connect_args,
            poolclass=QueuePool,
            pool_size=2,
            max_overflow=5
        )

        # Batching support
        # "plugin:cap" -> {plugin, capability, items: []}
        self.buffers: Dict[str, Dict[str, Any]] = {}
        # "plugin:cap" -> timer task
        self.flush_tasks: Dict[str, asyncio.Task] = {}

    def start(self):
        if self.is_running:
            return
        logger.warning(
            "AutomationManager V1 is deprecated. Use automation_manager_v2 instead."
        )
        logger.info(
            f"AutomationManager: Starting up with {self.concurrency} workers...")

        # Initialize queue here to ensure we are in the correct event loop
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()

        # Start multiple workers
        for i in range(self.concurrency):
            self.worker_tasks.append(asyncio.create_task(self._worker(i)))

        self.bus.subscribe("artifact.created", self.handle_artifact_created)
        # Also listen to updates to ensure re-scraped/resumed items are processed (lazily)
        self.bus.subscribe("artifact.updated", self.handle_artifact_created)
        self.is_running = True

        try:
            self._log_active_automations()
        except Exception as e:
            logger.warning(
                f"AutomationManager: Failed to log active automations: {e}")

    async def _worker(self, worker_id: int):
        logger.debug(f"AutomationManager: Worker {worker_id} started 🚀")
        while self.is_running and self.queue:
            try:
                # artifact_id = await self.queue.get()
                # Wait with check
                artifact_id = await self.queue.get()

                try:
                    await self._process_artifact(artifact_id)
                except Exception as e:
                    logger.error(
                        f"AutomationManager: Error processing queued artifact {artifact_id}: {e}")
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"AutomationManager: Worker {worker_id} loop error: {e}")
                # Brief sleep to avoid rapid loops on error
                await asyncio.sleep(1)

    def _log_active_automations(self):
        plugins = get_plugins_by_intent(PluginIntent.AUTO_ANALYSIS)
        if not plugins:
            logger.info("AutomationManager: No analysis plugins loaded.")
            return

        logger.info("AutomationManager: 🤖 Loaded Analysis Plugins:")
        # Use our dedicated engine!
        with Session(self.engine) as session:
            dispatcher = AnalysisDispatcher(session)
            for p in plugins:
                # Check global enable status for the plugin
                # We pass None as artifact_id to bypass collection checks and check global config
                enabled = dispatcher.should_run_analysis(None, p.name)
                status_icon = "✅" if enabled else "❌"
                logger.info(f"   {status_icon} {p.name}")

                if p.analysis_capabilities:
                    for cap in p.analysis_capabilities:
                        cap_full_name = f"{p.name}:{cap.name}"
                        cap_enabled = dispatcher.should_run_analysis(
                            None, cap_full_name)
                        cap_icon = "🟢" if cap_enabled else "🔴"
                        if not enabled:
                            cap_icon = "⚪"  # Greyed out if plugin is disabled

                        logger.info(
                            f"      {cap_icon} {cap.name}: {cap.label}")

    async def _enqueue_task(self, plugin, capability, artifact_id: UUID):
        """
        Adds a task to the execution buffer or executes immediately if batching is not supported.
        """
        cap_id = f"{plugin.name}:{capability.name}"
        batch_size = max(1, getattr(capability, 'batch_size', 1))

        if batch_size <= 1:
            # No batching, execute immediately
            await self._execute_task(plugin, capability, [artifact_id])
            return

        # Add to buffer
        if cap_id not in self.buffers:
            batch_wait_s = 2.0
            if plugin.name == "embeddr-embeddings":
                try:
                    with Session(self.engine) as session:
                        cfg = resolve_plugin_config(
                            session=session,
                            plugin_name="embeddr-embeddings",
                            scope="global",
                            scope_id=None,
                            config_id="embeddr-embeddings.config",
                        ) or {}
                    batch_wait_s = float(cfg.get("auto_batch_wait_s") or 5.0)
                except Exception:
                    batch_wait_s = 5.0
            self.buffers[cap_id] = {
                "plugin": plugin,
                "capability": capability,
                "items": [],
                "batch_wait_s": batch_wait_s,
            }

        buffer = self.buffers[cap_id]

        # Avoid duplicates in buffer if possible?
        # For simplicity, assume unique stream of events or idempotent plugins.
        if artifact_id not in buffer["items"]:
            buffer["items"].append(artifact_id)

        count = len(buffer["items"])
        logger.debug(
            f"AutomationManager: Buffer {cap_id} count: {count}/{batch_size}")

        if count >= batch_size:
            await self._flush_buffer(cap_id)
        else:
            self._schedule_flush(cap_id)

    def _schedule_flush(self, cap_id: str):
        """Schedules a flush after a timeout to prevent stragglers."""
        if cap_id in self.flush_tasks:
            return  # Timer already running

        async def delayed_flush():
            buffer = self.buffers.get(cap_id) or {}
            delay = float(buffer.get("batch_wait_s") or 2.0)
            await asyncio.sleep(delay)
            try:
                await self._flush_buffer(cap_id)
            except Exception as e:
                logger.error(
                    f"AutomationManager: Error in delayed flush for {cap_id}: {e}")

        # Store task
        self.flush_tasks[cap_id] = asyncio.create_task(delayed_flush())

    async def _flush_buffer(self, cap_id: str):
        """Flushes the buffer for a specific capability."""
        # Cancel timer if it exists (since we are flushing now)
        if cap_id in self.flush_tasks:
            t = self.flush_tasks.pop(cap_id)
            t.cancel()

        if cap_id not in self.buffers:
            return

        data = self.buffers[cap_id]
        items = data["items"]  # List of UUIDs

        if not items:
            return

        # Copy and clear buffer
        current_batch = list(items)
        data["items"] = []

        plugin = data["plugin"]
        capability = data["capability"]

        logger.info(
            f"AutomationManager: Flushing batch of {len(current_batch)} for {cap_id}")
        await self._execute_task(plugin, capability, current_batch)

    async def _execute_task(self, plugin, capability, artifact_ids: List[UUID]):
        """Executes the plugin action via ExecutionSpine."""
        from embeddr_core.models.artifact_execution import ArtifactExecution
        from embeddr_core.plugin_interface import EmbeddrEvent

        cap_id = f"{plugin.name}:{capability.name}"

        # Construct Inputs
        inputs = {"artifact_ids": [str(x) for x in artifact_ids]}
        if len(artifact_ids) == 1:
            inputs["artifact_id"] = str(artifact_ids[0])

        logger.info(
            f"AutomationManager: Submitting job {cap_id} for {len(artifact_ids)} items to Spine")

        try:
            # Basic heuristic for resource allocation
            resource_class = "cpu"
            name_lower = capability.name.lower()
            if "embedding" in name_lower or "gpu" in name_lower:
                resource_class = "gpu"
            elif "thumbnail" in name_lower:
                # Thumbnails are CPU/IO bound
                resource_class = "cpu"

            # Use local engine to avoid global pool starvation
            # Manual job insertion instead of calling ExecutionSpine.submit_job
            with Session(self.engine) as session:
                job = ArtifactExecution(
                    type=capability.name,
                    plugin_name=plugin.name,
                    inputs=inputs,
                    resource_class=resource_class,
                    priority=capability.priority,
                    trigger="automation",
                    status="pending"
                )
                session.add(job)
                session.commit()
                session.refresh(job)

                # We must manually trigger event too
                # Using run_coroutine_threadsafe if possible or just fire-and-forget publish
                # publish is non-blocking usually
                self.bus.publish(EmbeddrEvent(
                    event_type="execution.created",
                    source="spine",  # Pretend to be spine
                    payload={
                        "id": str(job.id),
                        "type": job.type,
                        "status": "pending",
                        "plugin_name": job.plugin_name,
                        "created_at": job.created_at.isoformat()
                    }
                ))
        except Exception as e:
            logger.error(f"AutomationManager: Failed to submit {cap_id}: {e}")

    def queue_artifact(self, artifact_id: UUID):
        """
        Public method to queue an artifact for automation processing.
        Thread-safe.
        """
        logger.info(
            f"AutomationManager: Queuing artifact {artifact_id} for analysis")

        # Thread safety fix for calls from ephemeral loops or threads
        if self.queue and self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(
                    self.queue.put_nowait, artifact_id)
            except Exception as e:
                logger.error(
                    f"AutomationManager: Failed to queue artifact thread-safe: {e}")
        elif self.queue:
            # Just try put_nowait if we are in the same loop context (risky if cross-thread)
            # But if loop is closed, we can't do much.
            try:
                self.queue.put_nowait(artifact_id)
            except Exception:
                pass
        else:
            logger.warning(
                "AutomationManager: Queue not initialized, cannot queue artifact.")

    def handle_artifact_created(self, event: EmbeddrEvent):
        """
        Triggered when a new artifact is created.
        Finds all applicable analysis capabilities and queues them.
        """
        # print(f"DEBUG: AutomationManager.handle_artifact_created called with payload keys: {event.payload.keys()}")
        raw_id = event.payload.get("id")
        if not raw_id:
            logger.debug(
                "AutomationManager: No artifact_id in payload, skipping.")
            return

        try:
            # Ensure we have a UUID object, as payload might differ (str vs UUID)
            if isinstance(raw_id, str):
                artifact_id = UUID(raw_id)
            else:
                artifact_id = raw_id
        except ValueError:
            logger.warning(
                f"AutomationManager: Invalid artifact UUID: {raw_id}")
            return

        self.queue_artifact(artifact_id)

    async def _process_artifact(self, artifact_id: UUID):
        tasks = []

        # Phase 1: Decision Making (DB Session Held)
        # Use our dedicated engine
        with Session(self.engine) as session:
            artifact = session.get(Artifact, artifact_id)
            if not artifact:
                logger.warning(
                    f"AutomationManager: Artifact {artifact_id} not found in DB during processing.")
                return

            metadata = artifact.metadata_json or {}
            external = metadata.get("external") if isinstance(
                metadata, dict) else None
            is_stash_import = bool(
                isinstance(external, dict) and external.get(
                    "source") == "stash"
            )

            # 1. Identify applicable plugins
            plugins = get_plugins_by_intent(PluginIntent.AUTO_ANALYSIS)
            if not plugins:
                return

            dispatcher = AnalysisDispatcher(session)

            for plugin in plugins:
                for capability in plugin.analysis_capabilities:
                    if is_stash_import and plugin.name == "embeddr-thumbnailer":
                        continue
                    # Check Trigger
                    if capability.trigger_event != "artifact.created":
                        continue

                    # Check Type Support
                    # This is simple string matching for now.
                    # "image" matches "image", "image:comfy", etc if we use containment?
                    # Let's use the explicit list.
                    is_supported = False
                    for supported_type in capability.supported_types:
                        if supported_type == "*":
                            is_supported = True
                            break
                        if artifact.type_name and supported_type in artifact.type_name:
                            is_supported = True
                            break
                        if artifact.base_type_name == supported_type:
                            is_supported = True
                            break

                    if not is_supported:
                        continue

                    # Check Configuration (User enabled/disabled)
                    # We check per-plugin (legacy style) AND per-capability?
                    # The dispatcher currently checks per plugin_name.
                    # We might want to extend dispatcher to check "plugin_name:capability_name"

                    # Check 1: Is the plugin enabled?
                    if not dispatcher.should_run_analysis(artifact.id, plugin.name):
                        logger.debug(
                            f"AutomationManager: Plugin {plugin.name} disabled for {artifact.id}")
                        continue

                    # Check 2: Is the specific capability enabled? (e.g. "embeddr-analysis:phash")
                    capability_id = f"{plugin.name}:{capability.name}"
                    if not dispatcher.should_run_analysis(artifact.id, capability_id):
                        logger.debug(
                            f"AutomationManager: Capability {capability_id} disabled for {artifact.id}")
                        continue

                    # Queue Execution
                    # Get priority: Check capability config, then plugin config, then capability default
                    capability_id = f"{plugin.name}:{capability.name}"

                    # 1. Configured Priority for Capability
                    prio = dispatcher.get_priority(
                        capability_id, default_priority=-9999)
                    if prio == -9999:
                        # 2. Configured Priority for Plugin
                        prio = dispatcher.get_priority(
                            plugin.name, default_priority=-9999)

                    if prio == -9999:
                        # 3. Default from Capability definition
                        prio = capability.priority

                    tasks.append({
                        "priority": prio,
                        "plugin": plugin,
                        "capability": capability,
                        "id": capability_id
                    })

        # Phase 2: Execution (No DB Session Held)
        # Sort by Priority (Descending: High number = First)
        tasks.sort(key=lambda x: x["priority"], reverse=True)

        if tasks:
            logger.info(f"AutomationManager: Found {len(tasks)} tasks for {artifact_id}. Execution Order: " +
                        ", ".join([f"{t['capability'].name} ({t['priority']})" for t in tasks]))

        for task in tasks:
            plugin = task["plugin"]
            capability = task["capability"]
            capability_id = task["id"]

            # We can execute directly if the intent is EXECUTION_HANDLER
            if PluginIntent.EXECUTION_HANDLER in plugin.intents:
                try:
                    await self._enqueue_task(plugin, capability, artifact_id)
                except Exception as e:
                    logger.error(
                        f"AutomationManager: Failed to enqueue/execute {capability_id}: {e}")


# Global instance
automation_manager = AutomationManager()
