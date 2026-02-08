import logging
import asyncio
import re
from typing import Dict, Any, Optional

from sqlmodel import Session, SQLModel, select
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from embeddr_core.models.automation import Automation
from embeddr.core.execution_spine import ExecutionSpine
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr_core.services.config_service import resolve_plugin_config
from embeddr_core.models.lotus import LotusKind
from embeddr.core.plugin_loader import get_lotus_registry


logger = logging.getLogger("embeddr.runtime")


class JobRuntime:
    """
    Official orchestration runtime.
    Central event consumer that schedules jobs through the ExecutionSpine.
    """

    def __init__(self) -> None:
        from embeddr.core.config import settings
        from sqlmodel import create_engine
        from sqlalchemy.pool import NullPool, QueuePool

        db_url = settings.DATABASE_URL
        is_sqlite = "sqlite" in db_url.lower()

        if is_sqlite:
            poolclass = NullPool
            connect_args = {
                "check_same_thread": False,
                "timeout": 30,
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

        if is_sqlite:
            @event.listens_for(self.engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA busy_timeout=30000;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.close()

        self.queue: Optional[asyncio.Queue] = None
        self.is_running = False
        self.worker_task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._batch_buffers: Dict[str, Dict[str, Any]] = {}
        self._batch_flush_tasks: Dict[str, asyncio.Task] = {}

        logger.info("JobRuntime initialized")

    async def start(self) -> None:
        if self.is_running:
            return

        self._ensure_tables()
        logger.info("JobRuntime: Skipping default automation seeding")
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.is_running = True
        self.worker_task = asyncio.create_task(self._worker())
        logger.info("JobRuntime: Started background worker")

    def _ensure_tables(self) -> None:
        try:
            SQLModel.metadata.create_all(
                self.engine, tables=[Automation.__table__])
        except Exception as e:
            logger.error("JobRuntime: Failed to ensure tables: %s", e)

    def handle_event(self, event: EmbeddrEvent) -> None:
        if event.event_type.startswith("execution.") or event.source in {"lotus", "spine"}:
            logger.debug(
                "JobRuntime: drop event=%s source=%s (exec lifecycle)",
                event.event_type,
                event.source,
            )
            return
        if not self.is_running or not self.queue or not self.loop:
            logger.debug(
                "JobRuntime: drop event=%s (runtime not ready)",
                event.event_type,
            )
            return

        try:
            logger.debug(
                "JobRuntime: enqueue event=%s source=%s",
                event.event_type,
                event.source,
            )
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except Exception as exc:
            logger.error("JobRuntime: failed to enqueue event: %s", exc)

    async def _worker(self) -> None:
        if not self.queue:
            return

        while self.is_running:
            event: EmbeddrEvent = await self.queue.get()
            try:
                asyncio.create_task(self._process_event(event))
            except Exception as e:
                logger.error("JobRuntime: Failed to process event: %s", e)
            finally:
                self.queue.task_done()

    async def _process_event(self, event: EmbeddrEvent) -> None:
        logger.info(
            "JobRuntime: processing event=%s source=%s",
            event.event_type,
            event.source,
        )

        with Session(self.engine) as session:
            rules = session.exec(
                select(Automation).where(
                    Automation.is_active == True,
                    Automation.trigger_event == event.event_type,
                )
            ).all()

            logger.debug(
                "JobRuntime: %s rules for event=%s",
                len(rules),
                event.event_type,
            )

            for rule in rules:
                if self._matches(rule, event):
                    await self._execute_rule(rule, event)

    def _matches(self, rule: Automation, event: EmbeddrEvent) -> bool:
        conditions = rule.trigger_conditions
        if not conditions:
            return True

        payload = event.payload
        if not payload:
            return False

        def check_condition(cond: Dict, data: Dict) -> bool:
            for k, v in cond.items():
                if k not in data:
                    logger.debug(
                        "JobRuntime: rule=%s miss key=%s",
                        rule.name,
                        k,
                    )
                    return False
                if isinstance(v, dict) and isinstance(data[k], dict):
                    if not check_condition(v, data[k]):
                        return False
                elif data[k] != v:
                    logger.debug(
                        "JobRuntime: rule=%s mismatch key=%s value=%s expected=%s",
                        rule.name,
                        k,
                        data[k],
                        v,
                    )
                    return False
            return True

        match = check_condition(conditions, payload)
        if match:
            logger.info(
                "JobRuntime: rule=%s matched event=%s",
                rule.name,
                event.event_type,
            )
        return match

    async def _execute_rule(self, rule: Automation, event: EmbeddrEvent) -> None:
        logger.info(
            "JobRuntime: rule=%s triggered by=%s",
            rule.name,
            event.event_type,
        )

        parent_execution_id = None
        try:
            payload = event.payload or {}
            meta = payload.get("metadata_json") or {}
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

                for part in path.split('.'):
                    if isinstance(val, dict) and part in val:
                        val = val.get(part)
                    else:
                        found = False
                        val = None
                        break

                if found:
                    return str(val)
                return match.group(0)

            rendered = re.sub(r"\$\{payload\.([^}]+)\}", repl, value)

            if rendered.startswith("$payload."):
                key = rendered.split("$payload.", 1)[1]
                if key in payload:
                    return payload[key]

            return rendered

        step_results: Dict[int, Any] = {}

        payload = event.payload or {}
        meta = payload.get("metadata_json") or {}
        external = meta.get("external") if isinstance(meta, dict) else None
        external_source = (
            external.get("source") if isinstance(external, dict) else None
        )

        for idx, action in enumerate(rule.actions):
            job_type = action.get("job_type")
            plugin_name = action.get("plugin_name", "automation")

            raw_inputs = action.get("inputs", {}) or {}

            ui_meta = action.get("ui", {})

            for prev_idx, prev_action in enumerate(rule.actions):
                if prev_idx >= idx:
                    break
                prev_ui = prev_action.get("ui", {})
                for out_link in prev_ui.get("outgoing", []):
                    if out_link.get("to") == idx:
                        input_key = out_link.get("inputKey")
                        output_key = out_link.get("outputKey")

                        prev_result = step_results.get(prev_idx)
                        if not prev_result or prev_result.status != "completed":
                            continue

                        outputs = prev_result.outputs or {}
                        if output_key == "artifact":
                            artifacts = outputs.get("artifacts", [])
                            val_to_inject = artifacts[0].get(
                                "id") if artifacts else None
                        else:
                            val_to_inject = outputs.get(output_key)

                        if val_to_inject:
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

            if job_type:
                exec_conf = self._resolve_exec_config(
                    job_type, plugin_name, action)
                if self._should_skip_action(exec_conf, external_source):
                    logger.info(
                        "JobRuntime: skipping job_type=%s due to exec policy",
                        job_type,
                    )
                    continue

                if (
                    isinstance(inputs, dict)
                    and inputs.get("artifact_id")
                    and self._should_batch(exec_conf, inputs, plugin_name)
                ):
                    await self._enqueue_batch(
                        job_type=job_type,
                        inputs=inputs,
                        plugin_name=plugin_name,
                        resource_class=action.get("resource_class", "cpu"),
                        priority=action.get("priority", 0),
                        trigger="automation",
                        parent_execution_id=parent_execution_id,
                        exec_conf=exec_conf,
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

                outgoing = (action.get("ui", {}) or {}).get("outgoing", [])
                if outgoing and job_id:
                    logger.info(
                        "JobRuntime: waiting for job=%s to support dependency",
                        job_id,
                    )
                    try:
                        finished_job = await ExecutionSpine.wait_for_job_async(job_id)
                        step_results[idx] = finished_job
                        if finished_job.status != "completed":
                            logger.error(
                                "JobRuntime: dependent step=%s failed, stopping chain",
                                idx,
                            )
                            break
                    except Exception as e:
                        logger.error(
                            "JobRuntime: error waiting for step=%s: %s",
                            idx,
                            e,
                        )
                        break

    def _resolve_capability(self, job_type: str, plugin_name: str):
        registry = get_lotus_registry()
        for cap in registry.list(kind=LotusKind.action) + registry.list(kind=LotusKind.feature):
            if cap.plugin != plugin_name:
                continue
            data = cap.data or {}
            if data.get("action") == job_type or data.get("job_type") == job_type or cap.id == job_type:
                return cap
        return None

    def _resolve_exec_config(self, job_type: str, plugin_name: str, action: Dict[str, Any]) -> Dict[str, Any]:
        cap = self._resolve_capability(job_type, plugin_name)
        cap_exec = (cap.data or {}).get("exec", {}) if cap else {}
        action_exec = action.get("exec") or {}
        return {**cap_exec, **action_exec}

    def _should_skip_action(self, exec_conf: Dict[str, Any], external_source: Optional[str]) -> bool:
        if not external_source:
            return False
        sources = exec_conf.get("skip_external_sources") or []
        return external_source in sources

    def _get_batch_config(self, plugin_name: str, exec_conf: Dict[str, Any]) -> tuple[int, float]:
        batch_size = exec_conf.get("batch_size")
        batch_wait_s = exec_conf.get("batch_wait_s")

        if batch_size is None or batch_wait_s is None:
            try:
                with Session(self.engine) as session:
                    cfg = resolve_plugin_config(
                        session=session,
                        plugin_name=plugin_name,
                        scope="global",
                        scope_id=None,
                        config_id=f"{plugin_name}.config",
                    ) or {}
                if batch_size is None:
                    batch_size = cfg.get("auto_batch_size")
                if batch_wait_s is None:
                    batch_wait_s = cfg.get("auto_batch_wait_s")
            except Exception:
                pass

        try:
            batch_size = int(batch_size or 1)
        except Exception:
            batch_size = 1
        try:
            batch_wait_s = float(batch_wait_s or 2.0)
        except Exception:
            batch_wait_s = 2.0

        return max(1, batch_size), max(0.5, batch_wait_s)

    def _should_batch(self, exec_conf: Dict[str, Any], inputs: Dict[str, Any], plugin_name: str) -> bool:
        batch_size, _ = self._get_batch_config(plugin_name, exec_conf)
        return batch_size > 1 and bool(inputs.get("artifact_id"))

    async def _enqueue_batch(
        self,
        *,
        job_type: str,
        inputs: Dict[str, Any],
        plugin_name: str,
        resource_class: str,
        priority: int,
        trigger: str,
        parent_execution_id: Optional[Any],
        exec_conf: Dict[str, Any],
    ) -> None:
        model_name = inputs.get("model_name") or "default"
        space = inputs.get("space") or "default"
        key = f"{job_type}:{model_name}:{space}"
        batch_size, batch_wait_s = self._get_batch_config(
            plugin_name, exec_conf)

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
            await self._flush_batch(key)
        else:
            self._schedule_batch_flush(key)

    def _schedule_batch_flush(self, key: str) -> None:
        if key in self._batch_flush_tasks:
            return

        async def delayed_flush() -> None:
            buffer = self._batch_buffers.get(key) or {}
            delay = float(buffer.get("batch_wait_s") or 5.0)
            await asyncio.sleep(delay)
            try:
                await self._flush_batch(key)
            except Exception as exc:
                logger.error("JobRuntime: batch flush failed: %s", exc)

        self._batch_flush_tasks[key] = asyncio.create_task(delayed_flush())

    async def _flush_batch(self, key: str) -> None:
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
            "JobRuntime: batching job_type=%s count=%s",
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
                    "JobRuntime: submitted job id=%s type=%s parent=%s",
                    job.id,
                    job_type,
                    parent_execution_id,
                )
                return job.id
            except OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < max_attempts:
                    await asyncio.sleep(0.1 * attempt)
                    continue
                logger.error(
                    "JobRuntime: failed to submit job %s: %s", job_type, e)
                return None
            except Exception as e:
                logger.error(
                    "JobRuntime: failed to submit job %s: %s", job_type, e)
                return None

        return None


# Global instance
runtime = JobRuntime()
