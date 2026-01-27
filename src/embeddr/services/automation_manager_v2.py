import logging
import asyncio
import re
from typing import Dict, Any, List, Optional
from sqlmodel import Session, SQLModel, select
from embeddr_core.models.automation import Automation
from embeddr.core.execution_spine import ExecutionSpine
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr_core.services.config_service import resolve_plugin_config
from sqlalchemy import event
from sqlalchemy.exc import OperationalError


logger = logging.getLogger("embeddr.automation")


class AutomationManager:
    def __init__(self):
        from embeddr.core.config import settings
        from sqlmodel import create_engine
        from sqlalchemy.pool import NullPool, QueuePool

        db_url = settings.DATABASE_URL
        is_sqlite = "sqlite" in db_url.lower()

        # SQLite: prefer fewer connections. Pooling doesn't help writers.
        if is_sqlite:
            poolclass = NullPool
            connect_args = {
                "check_same_thread": False,
                "timeout": 30,  # sqlite busy timeout at driver level
            }
        else:
            poolclass = QueuePool
            connect_args = {}

        self.engine = create_engine(
            db_url,
            connect_args=connect_args,
            poolclass=poolclass,
            pool_pre_ping=True,
        )

        # IMPORTANT: apply PRAGMAs for sqlite on every connection
        if is_sqlite:
            @event.listens_for(self.engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA busy_timeout=30000;")  # 30s
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.close()

        self.queue: Optional[asyncio.Queue] = None
        self.is_running = False
        self.worker_task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._batch_buffers: Dict[str, Dict[str, Any]] = {}
        self._batch_flush_tasks: Dict[str, asyncio.Task] = {}

    async def start(self):
        """Start the background worker."""
        if self.is_running:
            return

        self._ensure_tables()
        logger.info("AutomationManager V2: Skipping default automation seeding")
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.is_running = True
        self.worker_task = asyncio.create_task(self._worker())
        logger.info("AutomationManager V2: Started background worker 🚀")

    def _ensure_default_automations(self) -> None:
        """Deprecated: default automation seeding is disabled."""
        return

    def _ensure_tables(self) -> None:
        """Ensure automation tables exist."""
        try:
            SQLModel.metadata.create_all(
                self.engine, tables=[Automation.__table__])
        except Exception as e:
            logger.error(f"AutomationManager V2: Failed to ensure tables: {e}")

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
        Consume events from queue and evaluate against automations.
        """
        if not self.queue:
            return

        while self.is_running:
            event: EmbeddrEvent = await self.queue.get()
            try:
                # Fire and forget processing to avoid blocking the queue for long workflows
                asyncio.create_task(self._process_event(event))
            except Exception as e:
                logger.error(
                    f"AutomationManager V2: Failed to process event: {e}")
            finally:
                self.queue.task_done()

    async def _process_event(self, event: EmbeddrEvent) -> None:
        """
        Evaluate a single event against all active automations.
        """
        with Session(self.engine) as session:
            rules = session.exec(
                select(Automation).where(
                    Automation.is_active == True,
                    Automation.trigger_event == event.event_type
                )
            ).all()

            for rule in rules:
                if self._matches(rule, event):
                    await self._execute_rule(rule, event)

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
                    logger.warning(
                        f"[AutoMatch] Rule '{rule.name}' FAIL: '{k}' not in payload")
                    return False
                if isinstance(v, dict) and isinstance(data[k], dict):
                    if not check_condition(v, data[k]):
                        return False
                elif data[k] != v:
                    logger.warning(
                        f"[AutoMatch] Rule '{rule.name}' FAIL: payload['{k}']='{data[k]}' != '{v}'")
                    return False
            return True

        match = check_condition(conditions, payload)
        if match:
            logger.warning(
                f"[AutoMatch] Rule '{rule.name}' MATCHED event {event.event_type}")
        return match

    async def _execute_rule(self, rule: Automation, event: EmbeddrEvent):
        """
        Trigger the actions defined in the rule.
        """
        logger.info(
            f"Automation '{rule.name}' triggered by {event.event_type}")

        # Try to resolve parent execution from metadata (e.g. from ComfyUI job)
        parent_execution_id = None
        try:
            payload = event.payload or {}
            meta = payload.get("metadata_json") or {}
            # Check for sys_job_id in tags (comfy_meta or top level)
            tags = (meta.get("comfy_meta") or {}).get(
                "tags") or meta.get("tags") or []
            if isinstance(tags, str):
                tags = tags.split(",")

            for tag in tags:
                if isinstance(tag, str) and tag.strip().startswith("sys_job_id:"):
                    from uuid import UUID
                    try:
                        parent_execution_id = UUID(
                            tag.strip().split(":", 1)[1])
                    except ValueError:
                        pass
        except Exception:
            pass

        def resolve_value(value: Any, payload: Dict[str, Any]) -> Any:
            if isinstance(value, dict):
                return {k: resolve_value(v, payload) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve_value(v, payload) for v in value]
            if not isinstance(value, str):
                return value

            def repl(match: re.Match) -> str:
                path = match.group(1).strip()
                val = payload
                found = True

                # Debug logging for variable resolution
                logger.warning(
                    f"[AutoResolve] Resolving path='{path}' in payload keys={list(payload.keys())}")

                for part in path.split('.'):
                    if isinstance(val, dict) and part in val:
                        val = val.get(part)
                    else:
                        logger.warning(
                            f"[AutoResolve] Failed to find '{part}' in {type(val)}")
                        found = False
                        val = None
                        break

                if found:
                    logger.warning(
                        f"[AutoResolve] Resolved '{path}' -> '{val}'")
                    return str(val)

                logger.warning(
                    f"[AutoResolve] Could not resolve variable: {match.group(0)}")
                return match.group(0)

            # Support ${payload.id} style with nested keys (e.g. payload.metadata.foo)
            rendered = re.sub(r"\$\{payload\.([^}]+)\}", repl, value)

            # Support legacy $payload.id style
            if rendered.startswith("$payload."):
                key = rendered.split("$payload.", 1)[1]
                if key in payload:
                    return payload[key]

            return rendered

        # Context to store outputs from steps
        # Map step index to execution result
        step_results: Dict[int, Any] = {}

        payload = event.payload or {}
        meta = payload.get("metadata_json") or {}
        external = meta.get("external") if isinstance(meta, dict) else None
        is_stash_import = bool(
            isinstance(external, dict) and external.get("source") == "stash"
        )

        for idx, action in enumerate(rule.actions):
            job_type = action.get("job_type")
            plugin_name = action.get("plugin_name", "automation")

            if is_stash_import and (
                plugin_name == "embeddr-thumbnailer"
                or job_type in {"generate_thumbnail", "preview.thumbnail.generate"}
            ):
                logger.info(
                    "AutomationManager V2: Skipping thumbnail job for stash import job_type=%s",
                    job_type,
                )
                continue

            # If the input dictionary has 'inputs' key which is a dict, we should probably resolve recursively?
            # Wait, resolve_value currently only resolves if 'value' is string.
            # If 'value' is a dict, it iterates it.
            # So if inputs={"inputs": {"artifact_id": "${payload.id}"}}, it should recurse.
            # Let's ensure raw_inputs structure is correct before resolving.

            # Resolve Inputs
            raw_inputs = action.get("inputs", {}) or {}

            # DEBUG: Log inputs before link injection
            logger.warning(
                f"[AutoManager] Step {idx} raw_inputs before link: {raw_inputs}")

            # 1. Apply UI links (output from previous steps)
            # This mutates raw_inputs before variable substitution
            ui_meta = action.get("ui", {})
            # We also need to check if ANY previous step links TO this step
            # Actually the schema seems to be "outgoing" on the source.

            for prev_idx, prev_action in enumerate(rule.actions):
                if prev_idx >= idx:
                    break
                prev_ui = prev_action.get("ui", {})
                for out_link in prev_ui.get("outgoing", []):
                    if out_link.get("to") == idx:
                        # Found a link from prev_idx to current idx
                        # e.g. "artifact_id" or "inputs.34.artifact_id"?
                        input_key = out_link.get("inputKey")
                        output_key = out_link.get(
                            "outputKey")  # e.g. "artifact"

                        # Retrieve output
                        prev_result = step_results.get(prev_idx)
                        if not prev_result or prev_result.status != "completed":
                            continue  # Should probably fail here

                        # Extract value from result.outputs
                        outputs = prev_result.outputs or {}
                        val_to_inject = None

                        if output_key == "artifact":
                            # Special handling for Embeddr outputs
                            artifacts = outputs.get("artifacts", [])
                            if artifacts:
                                val_to_inject = artifacts[0].get("id")
                        else:
                            val_to_inject = outputs.get(output_key)

                        if val_to_inject:
                            # Inject into inputs. logic to handle nested keys?
                            # For now assuming flat or known Comfy structure
                            # Comfy inputs are often nested under "inputs" -> "node_id"
                            # The inputKey might be just "artifact_id" which is at top level?
                            # User example: "inputs": { "34": { "artifact_id": "..." } }
                            # So inputKey needs to target that.
                            # If inputKey is simple "artifact_id", we might need to recursively search or assume top level.
                            # Based on user snippet, the automation definition has "artifact_id" at top level of "inputs" dict?
                            # Ah, the user snippet for Node 2:
                            # "inputs": { "artifact_id": "${payload.id}", "inputs": { "30":..., "34":... } }
                            # NO, the user snippet for Node 2 inputs is:
                            # { "artifact_id": "${payload.id}", "inputs": { ... "34": { "artifact_id": ... } } }
                            # Wait, the automation "inputs" field contains the whole payload for the job.
                            # So inputs["inputs"]["34"]["artifact_id"] is the target.

                            # Recursive injection if the key matches
                            found_any = False

                            def recursive_set(d, k, v):
                                nonlocal found_any
                                if k in d:
                                    d[k] = v
                                    found_any = True
                                for sub_v in d.values():
                                    if isinstance(sub_v, dict):
                                        recursive_set(sub_v, k, v)

                            recursive_set(raw_inputs, input_key, val_to_inject)
                            if not found_any:
                                raw_inputs[input_key] = val_to_inject

            inputs = resolve_value(raw_inputs, event.payload)

            # DEBUG: Log inputs after resolution
            logger.warning(
                f"[AutoManager] Step {idx} resolved inputs: {inputs}")

            if job_type:
                if (
                    plugin_name == "embeddr-embeddings"
                    and job_type in {"generate_embedding", "features.embeddings.generate"}
                    and isinstance(inputs, dict)
                    and inputs.get("artifact_id")
                ):
                    await self._enqueue_embedding_batch(
                        job_type=job_type,
                        inputs=inputs,
                        plugin_name=plugin_name,
                        resource_class=action.get("resource_class", "cpu"),
                        priority=action.get("priority", 0),
                        trigger="automation",
                        parent_execution_id=parent_execution_id,
                    )
                    continue

                job_id = await self._submit_job_with_retry(
                    job_type=job_type,
                    inputs=inputs,
                    plugin_name=plugin_name,
                    resource_class=action.get("resource_class", "cpu"),
                    priority=action.get("priority", 0),
                    trigger="automation",
                    parent_execution_id=parent_execution_id,
                )

                # Check if next steps depend on this one
                has_dependents = False
                ui_meta = action.get("ui", {})
                outgoing = ui_meta.get("outgoing", [])
                if outgoing:
                    has_dependents = True

                if has_dependents and job_id:
                    # Wait for it
                    logger.info(
                        f"AutomationManager V2: Waiting for job {job_id} to support dependency...")
                    try:
                        finished_job = await ExecutionSpine.wait_for_job_async(job_id)
                        step_results[idx] = finished_job
                        if finished_job.status != "completed":
                            logger.error(
                                f"AutomationManager V2: Dependent step {idx} failed/canceled. Stopping chain.")
                            break
                    except Exception as e:
                        logger.error(
                            f"AutomationManager V2: Error waiting for step {idx}: {e}")
                        break

    def _get_embeddings_batch_config(self) -> tuple[int, float]:
        try:
            with Session(self.engine) as session:
                cfg = resolve_plugin_config(
                    session=session,
                    plugin_name="embeddr-embeddings",
                    scope="global",
                    scope_id=None,
                    config_id="embeddr-embeddings.config",
                ) or {}
            batch_size = int(cfg.get("auto_batch_size") or 50)
            batch_wait_s = float(cfg.get("auto_batch_wait_s") or 5.0)
            return max(1, batch_size), max(0.5, batch_wait_s)
        except Exception:
            return 50, 5.0

    async def _enqueue_embedding_batch(
        self,
        *,
        job_type: str,
        inputs: Dict[str, Any],
        plugin_name: str,
        resource_class: str,
        priority: int,
        trigger: str,
        parent_execution_id: Optional[Any],
    ) -> None:
        model_name = inputs.get("model_name") or "default"
        key = f"{job_type}:{model_name}"
        batch_size, batch_wait_s = self._get_embeddings_batch_config()

        if key not in self._batch_buffers:
            base_inputs = {k: v for k, v in inputs.items() if k !=
                           "artifact_id"}
            self._batch_buffers[key] = {
                "job_type": job_type,
                "plugin_name": plugin_name,
                "resource_class": resource_class,
                "priority": priority,
                "trigger": trigger,
                "parent_execution_id": parent_execution_id,
                "base_inputs": base_inputs,
                "artifact_ids": [],
                "batch_size": batch_size,
                "batch_wait_s": batch_wait_s,
            }

        buffer = self._batch_buffers[key]
        artifact_id = inputs.get("artifact_id")
        if artifact_id and artifact_id not in buffer["artifact_ids"]:
            buffer["artifact_ids"].append(artifact_id)

        if len(buffer["artifact_ids"]) >= int(buffer["batch_size"]):
            await self._flush_embedding_batch(key)
        else:
            self._schedule_embedding_flush(key)

    def _schedule_embedding_flush(self, key: str) -> None:
        if key in self._batch_flush_tasks:
            return

        async def delayed_flush() -> None:
            buffer = self._batch_buffers.get(key) or {}
            delay = float(buffer.get("batch_wait_s") or 5.0)
            await asyncio.sleep(delay)
            try:
                await self._flush_embedding_batch(key)
            except Exception as exc:
                logger.error(
                    "AutomationManager V2: batch flush failed: %s", exc)

        self._batch_flush_tasks[key] = asyncio.create_task(delayed_flush())

    async def _flush_embedding_batch(self, key: str) -> None:
        task = self._batch_flush_tasks.pop(key, None)
        if task:
            task.cancel()

        buffer = self._batch_buffers.get(key)
        if not buffer:
            return

        artifact_ids = list(buffer.get("artifact_ids") or [])
        if not artifact_ids:
            return

        buffer["artifact_ids"] = []
        inputs = {**(buffer.get("base_inputs") or {}),
                  "artifact_ids": artifact_ids}

        logger.info(
            "AutomationManager V2: batching embeddings job_type=%s count=%s",
            buffer.get("job_type"),
            len(artifact_ids),
        )

        job_type = buffer.get("job_type")
        plugin_name = buffer.get("plugin_name")
        resource_class = buffer.get("resource_class") or "cpu"
        trigger = buffer.get("trigger") or "automation"
        priority = buffer.get("priority")
        if not job_type or not plugin_name:
            return

        await self._submit_job_with_retry(
            job_type=job_type,
            inputs=inputs,
            plugin_name=plugin_name,
            resource_class=resource_class,
            priority=int(priority or 0),
            trigger=trigger,
            parent_execution_id=buffer.get("parent_execution_id"),
        )

    async def _submit_job_with_retry(
        self,
        job_type: str,
        inputs: Dict[str, Any],
        plugin_name: str,
        resource_class: str,
        priority: int,
        trigger: str,
        max_attempts: int = 3,
        parent_execution_id: Optional[Any] = None,
    ) -> Optional[Any]:
        """
        Submit job via ExecutionSpine with retry on sqlite locks.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                job = ExecutionSpine.submit_job(
                    job_type=job_type,
                    inputs=inputs,
                    plugin_name=plugin_name,
                    resource_class=resource_class,
                    priority=priority,
                    trigger=trigger,
                    parent_execution_id=parent_execution_id,
                )
                logger.info(
                    f"AutomationManager V2: Submitted job {job.id} ({job_type}) parent={parent_execution_id}")
                return job.id
            except OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < max_attempts:
                    await asyncio.sleep(0.1 * attempt)
                    continue
                logger.error(
                    f"AutomationManager V2: Failed to submit job {job_type}: {e}")
                return None
            except Exception as e:
                logger.error(
                    f"AutomationManager V2: Failed to submit job {job_type}: {e}")
                return None

        return None


# Global instance
automation_manager = AutomationManager()
