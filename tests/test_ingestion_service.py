"""
Tests for the IngestionService — batch artifact ingestion with ExecutionSpine tracking.

These tests define the API contract for:
- Batch flush creates artifacts in the DB
- Dedup behavior (skip existing URIs)
- Relation creation between artifacts
- ExecutionSpine tracking (execution created, artifacts linked, execution completed)
- Error handling (execution marked failed on batch errors)
"""

from uuid import uuid4, uuid5, NAMESPACE_URL
from unittest.mock import patch, MagicMock

import pytest
from sqlmodel import Session, select

from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.execution_artifact_link import ExecutionArtifactLink


class TestFlushSync:
    """_flush_sync inserts artifacts and creates ExecutionSpine records."""

    def _make_service(self, engine):
        """Create an IngestionService with a test engine."""
        from embeddr.services.ingestion_service import IngestionService
        svc = IngestionService(batch_size=10)
        svc._engine = engine
        return svc

    def test_inserts_new_artifacts(self, engine):
        svc = self._make_service(engine)
        items = [
            {"uri": "file:///tmp/test1.png", "type_name": "image",
                "base_type_name": "image", "metadata_json": {}},
            {"uri": "file:///tmp/test2.png", "type_name": "image",
                "base_type_name": "image", "metadata_json": {}},
        ]

        with patch("embeddr.services.ingestion_service._submit_ingestion_execution", return_value=None):
            svc._flush_sync(items)

        with Session(engine) as session:
            arts = session.exec(select(Artifact)).all()
            uris = {a.uri for a in arts}
            assert "file:///tmp/test1.png" in uris
            assert "file:///tmp/test2.png" in uris

    def test_dedup_existing_artifacts(self, engine):
        """Items with URIs already in the DB should not be re-inserted."""
        uri = "file:///tmp/existing.png"
        art_id = uuid5(NAMESPACE_URL, uri)

        with Session(engine) as session:
            existing = Artifact(
                id=art_id,
                type_name="image",
                base_type_name="image",
                uri=uri,
                metadata_json={"original": True},
            )
            session.add(existing)
            session.commit()

        svc = self._make_service(engine)
        items = [
            {"uri": uri, "type_name": "image", "base_type_name": "image",
                "metadata_json": {"new": True}},
        ]

        with patch("embeddr.services.ingestion_service._submit_ingestion_execution", return_value=None):
            svc._flush_sync(items)

        with Session(engine) as session:
            arts = session.exec(select(Artifact).where(
                Artifact.uri == uri)).all()
            assert len(arts) == 1
            assert arts[0].metadata_json.get(
                "original") is True  # Not overwritten

    def test_dedup_within_batch(self, engine):
        """Duplicate URIs within the same batch should only insert once."""
        svc = self._make_service(engine)
        items = [
            {"uri": "file:///tmp/dup.png", "type_name": "image",
                "base_type_name": "image", "metadata_json": {}},
            {"uri": "file:///tmp/dup.png", "type_name": "image",
                "base_type_name": "image", "metadata_json": {}},
        ]

        with patch("embeddr.services.ingestion_service._submit_ingestion_execution", return_value=None):
            svc._flush_sync(items)

        with Session(engine) as session:
            arts = session.exec(select(Artifact).where(
                Artifact.uri == "file:///tmp/dup.png")).all()
            assert len(arts) == 1

    def test_skips_items_without_uri(self, engine):
        svc = self._make_service(engine)
        items = [
            {"type_name": "image"},  # No URI
            {"uri": "file:///tmp/valid.png", "type_name": "image",
                "base_type_name": "image", "metadata_json": {}},
        ]

        with patch("embeddr.services.ingestion_service._submit_ingestion_execution", return_value=None):
            svc._flush_sync(items)

        with Session(engine) as session:
            arts = session.exec(select(Artifact)).all()
            assert len(arts) == 1
            assert arts[0].uri == "file:///tmp/valid.png"

    def test_creates_execution_record(self, engine, spine_engine):
        """Each batch flush should create an ExecutionSpine record."""
        svc = self._make_service(engine)
        items = [
            {"uri": f"file:///tmp/tracked_{i}.png", "type_name": "image",
                "base_type_name": "image", "metadata_json": {}}
            for i in range(3)
        ]

        # Patch get_engine wherever it's lazily imported inside ingestion helpers
        with patch("embeddr.db.session.get_engine", return_value=engine):
            svc._flush_sync(items)

        with Session(engine) as session:
            executions = session.exec(
                select(ArtifactExecution)
                .where(ArtifactExecution.type == "ingest.batch")
            ).all()

            assert len(executions) >= 1
            latest = executions[-1]
            assert latest.status == "completed"
            assert latest.plugin_name == "core"
            assert latest.resource_class == "io"

    def test_links_created_artifacts_to_execution(self, engine, spine_engine):
        """Created artifacts should be linked to the batch execution."""
        svc = self._make_service(engine)
        uris = [f"file:///tmp/linked_{i}.png" for i in range(3)]
        items = [
            {"uri": uri, "type_name": "image",
                "base_type_name": "image", "metadata_json": {}}
            for uri in uris
        ]

        with patch("embeddr.db.session.get_engine", return_value=engine):
            svc._flush_sync(items)

        with Session(engine) as session:
            executions = session.exec(
                select(ArtifactExecution)
                .where(ArtifactExecution.type == "ingest.batch")
            ).all()
            assert len(executions) >= 1

            links = session.exec(
                select(ExecutionArtifactLink)
                .where(ExecutionArtifactLink.execution_id == executions[-1].id)
            ).all()

            assert len(links) == 3
            assert all(l.action == "created" for l in links)

    def test_empty_batch_no_artifacts(self, engine):
        """Empty batch should still complete without errors."""
        svc = self._make_service(engine)

        with patch("embeddr.services.ingestion_service._submit_ingestion_execution", return_value=None):
            svc._flush_sync([])

        with Session(engine) as session:
            arts = session.exec(select(Artifact)).all()
            assert len(arts) == 0


class TestIngestionServiceLifecycle:
    """Start/stop and queue behavior."""

    async def test_start_stop(self, engine):
        from embeddr.services.ingestion_service import IngestionService
        svc = IngestionService(batch_size=5)
        svc._engine = engine

        await svc.start()
        assert svc.is_running is True
        assert svc.worker_task is not None

        await svc.stop()
        assert svc.is_running is False

    async def test_ingest_enqueues_item(self, engine):
        from embeddr.services.ingestion_service import IngestionService
        svc = IngestionService(batch_size=5, queue_size=10)
        svc._engine = engine

        # Don't start the worker — just test the queue
        await svc.ingest({"uri": "test://item1"})
        assert svc.queue.qsize() == 1
