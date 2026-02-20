import asyncio
import os

from sqlmodel import Session

from embeddr.core.config import refresh_settings
from embeddr.services.job_runtime import JobRuntime
from embeddr_core.models.automation import Automation
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr.core.execution_spine import ExecutionSpine


def test_process_event_submits_job(monkeypatch, engine):
    """When _process_event fires, matching rules submit jobs via ExecutionSpine."""
    manager = JobRuntime()
    # Replace the runtime's engine with the test engine (StaticPool in-memory)
    # so all sessions share the same DB and tables already exist from conftest.
    manager.engine = engine

    with Session(engine) as session:
        rule = Automation(
            name="auto-thumb",
            trigger_event="artifact.created",
            actions=[
                {
                    "job_type": "generate_thumbnail",
                    "inputs": {"artifact_id": "$payload.id"},
                    "resource_class": "cpu",
                    "priority": 10,
                }
            ],
        )
        session.add(rule)
        session.commit()

    calls = {}

    class DummyJob:
        id = "job-id"

    def fake_submit_job(cls, **kwargs):
        calls.update(kwargs)
        return DummyJob()

    monkeypatch.setattr(ExecutionSpine, "submit_job",
                        classmethod(fake_submit_job))

    event = EmbeddrEvent(
        event_type="artifact.created",
        source="test",
        payload={"id": "abc123"},
    )

    asyncio.run(manager._process_event(event))

    assert calls["job_type"] == "generate_thumbnail"
    assert calls["inputs"]["artifact_id"] == "abc123"
    assert calls["plugin_name"] == "automation"
    assert calls["resource_class"] == "cpu"
    assert calls["priority"] == 10
    assert calls["trigger"] == "automation"


def test_handle_event_enqueues_job(monkeypatch, engine):
    """handle_event enqueues the event and the background worker processes it."""
    runtime = JobRuntime()
    runtime.engine = engine

    with Session(engine) as session:
        rule = Automation(
            name="auto-thumb",
            trigger_event="artifact.created",
            actions=[
                {
                    "job_type": "generate_thumbnail",
                    "inputs": {"artifact_id": "$payload.id"},
                    "resource_class": "cpu",
                    "priority": 5,
                }
            ],
        )
        session.add(rule)
        session.commit()

    calls = {}

    class DummyJob:
        id = "job-id"

    def fake_submit_job(cls, **kwargs):
        calls.update(kwargs)
        return DummyJob()

    monkeypatch.setattr(ExecutionSpine, "submit_job",
                        classmethod(fake_submit_job))

    event = EmbeddrEvent(
        event_type="artifact.created",
        source="test",
        payload={"id": "abc123"},
    )

    async def _run() -> None:
        await runtime.start()
        runtime.handle_event(event)
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert calls["job_type"] == "generate_thumbnail"
    assert calls["inputs"]["artifact_id"] == "abc123"
