"""
Tests for the ExecutionSpine — the central job scheduling and execution system.

These tests define the API contract for:
- Job submission (submit_job)
- Job lifecycle (pending → running → completed/failed/canceled)
- Job cancellation (cancel_job)
- Job deferral and resume (waiting → pending)
- Artifact linking (link_artifact / link_artifacts)
- Child execution trees (submit_child)
- Event recording
- Handler registration & dispatch
"""

import asyncio
from uuid import uuid4, UUID

import pytest
from sqlmodel import Session, select
from unittest.mock import MagicMock

from embeddr.core.execution_spine import (
    ExecutionSpine,
    DBJobContext,
    JobDeferred,
)
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.artifact_execution_event import ArtifactExecutionEvent
from embeddr_core.models.execution_artifact_link import ExecutionArtifactLink
from embeddr_core.models.artifact import Artifact


# ---------------------------------------------------------------------------
# submit_job
# ---------------------------------------------------------------------------

class TestSubmitJob:
    """submit_job creates a pending execution record in the DB."""

    def test_creates_pending_execution(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={"message": "hello"},
            plugin_name="test-plugin",
            session=session,
        )

        assert job.id is not None
        assert job.status == "pending"
        assert job.type == "test.echo"
        assert job.plugin_name == "test-plugin"
        assert job.inputs == {"message": "hello"}

    def test_default_values(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.noop",
            inputs={},
            session=session,
        )

        assert job.resource_class == "cpu"
        assert job.priority == 0
        assert job.trigger == "user"
        assert job.plugin_name == "core"
        assert job.parent_execution_id is None

    def test_custom_resource_class(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.gpu_work",
            inputs={},
            resource_class="gpu",
            session=session,
        )

        assert job.resource_class == "gpu"

    def test_parent_execution_chain(self, clean_spine, session, spine_engine):
        parent = clean_spine.submit_job(
            job_type="test.parent",
            inputs={},
            session=session,
        )

        child = clean_spine.submit_job(
            job_type="test.child",
            inputs={},
            parent_execution_id=parent.id,
            session=session,
        )

        assert child.parent_execution_id == parent.id

    def test_creates_execution_created_event(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={"key": "val"},
            session=session,
        )

        events = session.exec(
            select(ArtifactExecutionEvent)
            .where(ArtifactExecutionEvent.execution_id == job.id)
        ).all()

        event_types = [e.event_type for e in events]
        assert "execution.created" in event_types

    def test_returns_artifact_execution_model(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        assert isinstance(job, ArtifactExecution)

    def test_primary_artifact_id(self, clean_spine, session, spine_engine):
        art_id = uuid4()
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            primary_artifact_id=art_id,
            session=session,
        )

        assert job.primary_artifact_id == art_id


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------

class TestCancelJob:
    """cancel_job transitions pending/running/waiting → canceled."""

    def test_cancel_pending_job(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        result = clean_spine.cancel_job(job.id)

        assert result is not None
        assert result.status == "canceled"
        assert result.finished_at is not None

    def test_cancel_returns_none_for_completed(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        # Manually mark completed
        job.status = "completed"
        session.add(job)
        session.commit()

        result = clean_spine.cancel_job(job.id)
        assert result is None

    def test_cancel_nonexistent_returns_none(self, clean_spine, spine_engine):
        result = clean_spine.cancel_job(uuid4())
        assert result is None

    def test_cancel_records_event(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        clean_spine.cancel_job(job.id)

        # Read from a fresh session since cancel_job uses its own session
        with Session(spine_engine) as s2:
            events = s2.exec(
                select(ArtifactExecutionEvent)
                .where(ArtifactExecutionEvent.execution_id == job.id)
            ).all()

        event_types = [e.event_type for e in events]
        assert "execution.canceled" in event_types


# ---------------------------------------------------------------------------
# resume_job
# ---------------------------------------------------------------------------

class TestResumeJob:
    """resume_job transitions waiting → pending and merges inputs."""

    def test_resume_waiting_job(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={"original": True},
            session=session,
        )

        # Mark as waiting
        job.status = "waiting"
        session.add(job)
        session.commit()

        result = clean_spine.resume_job(job.id, message="user approved")
        assert result is not None

        # Re-read from our session since resume_job uses its own session
        session.refresh(job)
        assert job.status == "pending"
        assert job.message == "user approved"

    def test_resume_merges_inputs(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={"original": True, "keep": "this"},
            session=session,
        )

        job.status = "waiting"
        session.add(job)
        session.commit()

        result = clean_spine.resume_job(
            job.id,
            inputs_update={"new_key": "new_val", "original": False},
        )
        assert result is not None

        # Re-read from our session since resume_job uses its own session
        session.refresh(job)
        assert job.inputs["original"] is False
        assert job.inputs["keep"] == "this"
        assert job.inputs["new_key"] == "new_val"


# ---------------------------------------------------------------------------
# DBJobContext
# ---------------------------------------------------------------------------

class TestDBJobContext:
    """DBJobContext wraps a session + execution for handler use."""

    def test_execution_id_property(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={"msg": "hi"},
            session=session,
        )

        ctx = DBJobContext(session, job)
        assert ctx.execution_id == job.id

    def test_inputs_property(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={"msg": "hi", "count": 3},
            session=session,
        )

        ctx = DBJobContext(session, job)
        assert ctx.inputs == {"msg": "hi", "count": 3}

    def test_set_progress(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)
        ctx.set_progress(50, "halfway there")

        session.refresh(job)
        assert job.progress == 50
        assert job.message == "halfway there"

    def test_set_progress_clamps(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)
        ctx.set_progress(150, "over")
        session.refresh(job)
        assert job.progress == 100

        ctx.set_progress(-10, "under")
        session.refresh(job)
        assert job.progress == 0

    def test_log_records_event(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)
        ctx.log("something happened", level="warning")

        events = session.exec(
            select(ArtifactExecutionEvent)
            .where(ArtifactExecutionEvent.execution_id == job.id)
            .where(ArtifactExecutionEvent.event_type == "execution.log")
        ).all()

        assert len(events) >= 1
        assert events[-1].message == "something happened"
        assert events[-1].level == "warning"

    def test_defer_raises_job_deferred(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)

        with pytest.raises(JobDeferred) as exc_info:
            ctx.defer("need approval", payload={"reason": "expensive"})

        assert exc_info.value.message == "need approval"
        assert exc_info.value.payload == {"reason": "expensive"}

    def test_submit_child(self, clean_spine, session, spine_engine):
        parent = clean_spine.submit_job(
            job_type="test.parent",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, parent)
        child = ctx.submit_child(
            job_type="test.child",
            inputs={"from_parent": True},
            plugin_name="test-plugin",
        )

        assert child.parent_execution_id == parent.id
        assert child.type == "test.child"
        assert child.inputs == {"from_parent": True}

    def test_is_cancelled(self, clean_spine, session, spine_engine):
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)
        assert ctx.is_cancelled() is False

        job.status = "canceled"
        session.add(job)
        session.commit()

        assert ctx.is_cancelled() is True


# ---------------------------------------------------------------------------
# Artifact linking
# ---------------------------------------------------------------------------

class TestArtifactLinking:
    """link_artifact / link_artifacts create ExecutionArtifactLink rows."""

    def _create_artifact(self, session: Session) -> Artifact:
        art = Artifact(
            id=uuid4(),
            type_name="test_image",
            base_type_name="image",
            uri=f"file:///tmp/test_{uuid4().hex[:8]}.png",
            metadata_json={},
        )
        session.add(art)
        session.flush()
        return art

    def test_link_single_artifact(self, clean_spine, session, spine_engine):
        art = self._create_artifact(session)
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)
        ctx.link_artifact(art.id, action="created", detail="generated image")
        session.commit()

        links = session.exec(
            select(ExecutionArtifactLink)
            .where(ExecutionArtifactLink.execution_id == job.id)
        ).all()

        assert len(links) == 1
        assert links[0].artifact_id == art.id
        assert links[0].action == "created"
        assert links[0].detail == "generated image"

    def test_link_multiple_artifacts(self, clean_spine, session, spine_engine):
        arts = [self._create_artifact(session) for _ in range(3)]
        job = clean_spine.submit_job(
            job_type="test.batch",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)
        ctx.link_artifacts(
            [a.id for a in arts],
            action="modified",
            detail="batch update",
        )
        session.commit()

        links = session.exec(
            select(ExecutionArtifactLink)
            .where(ExecutionArtifactLink.execution_id == job.id)
        ).all()

        assert len(links) == 3
        assert all(l.action == "modified" for l in links)

    def test_link_records_event(self, clean_spine, session, spine_engine):
        art = self._create_artifact(session)
        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={},
            session=session,
        )

        ctx = DBJobContext(session, job)
        ctx.link_artifact(art.id, action="created")
        session.commit()

        events = session.exec(
            select(ArtifactExecutionEvent)
            .where(ArtifactExecutionEvent.execution_id == job.id)
            .where(ArtifactExecutionEvent.event_type == "artifact.linked")
        ).all()

        assert len(events) >= 1


# ---------------------------------------------------------------------------
# Handler registration & async dispatch
# ---------------------------------------------------------------------------

class TestHandlerRegistration:
    """register_handler + spine worker dispatch."""

    def test_register_sync_handler(self, clean_spine):
        def my_handler(ctx):
            return {"ok": True}

        clean_spine.register_handler("test.sync", my_handler)
        assert "test.sync" in clean_spine._handlers

    def test_register_async_handler(self, clean_spine):
        async def my_handler(ctx):
            return {"ok": True}

        clean_spine.register_handler("test.async", my_handler)
        assert "test.async" in clean_spine._handlers

    async def test_sync_handler_execution(self, clean_spine, session, spine_engine):
        """End-to-end: submit job → handler runs → job completes."""
        results = {}

        def echo_handler(ctx):
            results["got"] = ctx.inputs.get("message")
            return {"echo": ctx.inputs.get("message")}

        clean_spine.register_handler("test.echo", echo_handler)

        job = clean_spine.submit_job(
            job_type="test.echo",
            inputs={"message": "ping"},
            session=session,
        )

        # Run one tick of the spine worker
        spine = clean_spine()
        await spine._process_tick()

        # Give it a moment for the task to complete
        await asyncio.sleep(0.5)

        # Re-read job from DB
        with Session(spine_engine) as s2:
            updated = s2.get(ArtifactExecution, job.id)
            assert updated.status == "completed"
            assert updated.outputs == {"echo": "ping"}
            assert results["got"] == "ping"

    async def test_async_handler_execution(self, clean_spine, session, spine_engine):
        """Async handlers are awaited directly (no to_thread)."""

        async def async_echo(ctx):
            await asyncio.sleep(0.01)
            return {"async_echo": ctx.inputs.get("message")}

        clean_spine.register_handler("test.async_echo", async_echo)

        job = clean_spine.submit_job(
            job_type="test.async_echo",
            inputs={"message": "async_ping"},
            session=session,
        )

        spine = clean_spine()
        await spine._process_tick()
        await asyncio.sleep(0.5)

        with Session(spine_engine) as s2:
            updated = s2.get(ArtifactExecution, job.id)
            assert updated.status == "completed"
            assert updated.outputs == {"async_echo": "async_ping"}

    async def test_handler_failure_marks_job_failed(self, clean_spine, session, spine_engine):
        """If a handler raises, the job should be marked failed."""

        def bad_handler(ctx):
            raise ValueError("something broke")

        clean_spine.register_handler("test.fail", bad_handler)

        job = clean_spine.submit_job(
            job_type="test.fail",
            inputs={},
            session=session,
        )

        spine = clean_spine()
        await spine._process_tick()
        await asyncio.sleep(0.5)

        with Session(spine_engine) as s2:
            updated = s2.get(ArtifactExecution, job.id)
            assert updated.status == "failed"

    async def test_handler_defer_marks_waiting(self, clean_spine, session, spine_engine):
        """If a handler calls context.defer(), job goes to waiting."""

        def defer_handler(ctx):
            ctx.defer("need input", payload={"question": "approve?"})

        clean_spine.register_handler("test.defer", defer_handler)

        job = clean_spine.submit_job(
            job_type="test.defer",
            inputs={},
            session=session,
        )

        spine = clean_spine()
        await spine._process_tick()
        await asyncio.sleep(0.5)

        with Session(spine_engine) as s2:
            updated = s2.get(ArtifactExecution, job.id)
            assert updated.status == "waiting"

    async def test_process_tick_drains_available_resource_slots(
        self,
        clean_spine,
        session,
        spine_engine,
    ):
        """One tick should launch as many jobs as there are free slots."""

        async def slow_echo(ctx):
            await asyncio.sleep(0.05)
            return {"ok": ctx.inputs.get("n")}

        clean_spine.register_handler("test.bulk_cpu", slow_echo)

        jobs = [
            clean_spine.submit_job(
                job_type="test.bulk_cpu",
                inputs={"n": idx},
                resource_class="cpu",
                session=session,
            )
            for idx in range(3)
        ]

        spine = clean_spine()
        await spine._process_tick()
        await asyncio.sleep(0.2)

        with Session(spine_engine) as s2:
            refreshed = [s2.get(ArtifactExecution, job.id) for job in jobs]
            assert all(job and job.status == "completed" for job in refreshed)

    async def test_worker_loop_wakes_when_resource_slot_is_released(
        self,
        clean_spine,
        session,
        spine_engine,
    ):
        """A finished GPU job should wake the scheduler and start the next one immediately."""

        async def short_gpu_job(ctx):
            await asyncio.sleep(0.05)
            return {"ok": True}

        clean_spine.register_handler("test.gpu_queue", short_gpu_job)

        jobs = [
            clean_spine.submit_job(
                job_type="test.gpu_queue",
                inputs={"n": idx},
                resource_class="gpu",
                session=session,
            )
            for idx in range(2)
        ]

        spine = clean_spine()
        worker_task = asyncio.create_task(spine.start_worker())
        try:
            await asyncio.sleep(0.35)

            with Session(spine_engine) as s2:
                refreshed = [s2.get(ArtifactExecution, job.id) for job in jobs]
                assert all(job and job.status == "completed" for job in refreshed)
        finally:
            spine._running = False
            spine._job_submitted.set()
            await asyncio.wait_for(worker_task, timeout=1.0)

    async def test_process_tick_dispatches_distinct_dynamic_resource_lanes(
        self,
        clean_spine,
        session,
        spine_engine,
    ):
        """Endpoint-scoped GPU lanes should run concurrently instead of sharing one global slot."""

        async def short_job(ctx):
            await asyncio.sleep(0.05)
            return {"ok": ctx.inputs.get("endpoint")}

        clean_spine.register_handler("test.comfy_endpoint_lane", short_job)

        jobs = [
            clean_spine.submit_job(
                job_type="test.comfy_endpoint_lane",
                inputs={"endpoint": "a"},
                resource_class="gpu:comfy:endpoint-a",
                session=session,
            ),
            clean_spine.submit_job(
                job_type="test.comfy_endpoint_lane",
                inputs={"endpoint": "b"},
                resource_class="gpu:comfy:endpoint-b",
                session=session,
            ),
        ]

        spine = clean_spine()
        await spine._process_tick()
        await asyncio.sleep(0.2)

        with Session(spine_engine) as s2:
            refreshed = [s2.get(ArtifactExecution, job.id) for job in jobs]
            assert all(job and job.status == "completed" for job in refreshed)


# ---------------------------------------------------------------------------
# Input redaction
# ---------------------------------------------------------------------------

class TestInputRedaction:
    """Sensitive inputs should be redacted in logs."""

    def test_redact_api_key(self):
        from embeddr.core.execution_spine import _redact_inputs

        inputs = {"api_key": "sk-secret-123", "model": "gpt-4", "token": "tok"}
        redacted = _redact_inputs(inputs)

        assert redacted["api_key"] == "***"
        assert redacted["token"] == "***"
        assert redacted["model"] == "gpt-4"

    def test_redact_none_inputs(self):
        from embeddr.core.execution_spine import _redact_inputs
        assert _redact_inputs(None) == {}
