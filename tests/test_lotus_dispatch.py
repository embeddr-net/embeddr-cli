"""
Tests for lotus_dispatch_action — the Lotus capability dispatch system.

These tests define the API contract for:
- lotus_dispatch_action creates an execution via ExecutionSpine
- Input merging with plugin config
- Resource class inference from capability
- Parent execution chain
- Error handling for missing plugins
"""

from uuid import UUID, uuid4
from unittest.mock import patch, MagicMock

import pytest
from sqlmodel import Session, select

from embeddr_core.models.artifact_execution import ArtifactExecution


class _AuthStub:
    def __init__(self):
        self.mode = "multi"
        self.user_id = uuid4()
        self.operator_id = uuid4()
        self.is_admin = False
        self.permissions = {"lotus:dispatch"}

    @property
    def is_open(self):
        return False


class TestLotusDispatchAction:
    """lotus_dispatch_action queues work via ExecutionSpine."""

    def test_creates_execution(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        mock_plugin = MagicMock()
        mock_plugin.name = "test-plugin"

        with patch("embeddr.api.v1.lotus_service.get_plugin_instance", return_value=mock_plugin), \
                patch("embeddr.api.v1.lotus_service.lotus_prepare_inputs", return_value={"key": "val"}):

            result = lotus_dispatch_action(
                result_id="test-result",
                plugin_name="test-plugin",
                action_name="test.action",
                inputs={"key": "val"},
                session=session,
            )

        assert result["ok"] is True
        assert result["kind"] == "action"
        assert result["status"] == "pending"
        assert "execution_id" in result

    def test_returns_execution_id(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        mock_plugin = MagicMock()
        with patch("embeddr.api.v1.lotus_service.get_plugin_instance", return_value=mock_plugin), \
                patch("embeddr.api.v1.lotus_service.lotus_prepare_inputs", return_value={}):

            result = lotus_dispatch_action(
                result_id="test",
                plugin_name="test-plugin",
                action_name="do_thing",
                inputs={},
                session=session,
            )

        # Verify execution exists in DB
        exec_id = UUID(result["execution_id"])
        job = session.get(ArtifactExecution, exec_id)
        assert job is not None
        assert job.type == "do_thing"
        assert job.plugin_name == "test-plugin"

    def test_resource_class_from_capability(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        mock_plugin = MagicMock()
        mock_cap = MagicMock()
        mock_cap.action.exec = {"resource_class": "gpu"}

        with patch("embeddr.api.v1.lotus_service.get_plugin_instance", return_value=mock_plugin), \
                patch("embeddr.api.v1.lotus_service.lotus_prepare_inputs", return_value={}):

            result = lotus_dispatch_action(
                result_id="test",
                plugin_name="test-plugin",
                action_name="render",
                inputs={},
                session=session,
                capability=mock_cap,
            )

        job = session.get(ArtifactExecution, UUID(result["execution_id"]))
        assert job.resource_class == "gpu"

    def test_invalid_resource_class_defaults_to_cpu(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        mock_plugin = MagicMock()
        mock_cap = MagicMock()
        mock_cap.action.exec = {"resource_class": "quantum"}  # Invalid

        with patch("embeddr.api.v1.lotus_service.get_plugin_instance", return_value=mock_plugin), \
                patch("embeddr.api.v1.lotus_service.lotus_prepare_inputs", return_value={}):

            result = lotus_dispatch_action(
                result_id="test",
                plugin_name="test-plugin",
                action_name="do_thing",
                inputs={},
                session=session,
                capability=mock_cap,
            )

        job = session.get(ArtifactExecution, UUID(result["execution_id"]))
        assert job.resource_class == "cpu"

    def test_parent_execution_chain(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        mock_plugin = MagicMock()
        parent_id = str(uuid4())

        with patch("embeddr.api.v1.lotus_service.get_plugin_instance", return_value=mock_plugin), \
                patch("embeddr.api.v1.lotus_service.lotus_prepare_inputs", return_value={}):

            result = lotus_dispatch_action(
                result_id="test",
                plugin_name="test-plugin",
                action_name="child_action",
                inputs={},
                session=session,
                parent_execution_id=parent_id,
            )

        job = session.get(ArtifactExecution, UUID(result["execution_id"]))
        assert str(job.parent_execution_id) == parent_id

    def test_missing_plugin_raises(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        with patch("embeddr.api.v1.lotus_service.get_plugin_instance", return_value=None):

            with pytest.raises(ValueError, match="Plugin not found"):
                lotus_dispatch_action(
                    result_id="test",
                    plugin_name="nonexistent",
                    action_name="do_thing",
                    inputs={},
                    session=session,
                )

    def test_missing_action_name_raises(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        with pytest.raises(ValueError, match="action_name"):
            lotus_dispatch_action(
                result_id="test",
                plugin_name="test-plugin",
                action_name="",
                inputs={},
                session=session,
            )

    def test_embeds_auth_context_in_execution_inputs(self, session, spine_engine):
        from embeddr.api.v1.lotus_service import lotus_dispatch_action

        mock_plugin = MagicMock()
        auth = _AuthStub()
        with patch("embeddr.api.v1.lotus_service.get_plugin_instance", return_value=mock_plugin), \
                patch("embeddr.api.v1.lotus_service.lotus_prepare_inputs", return_value={"x": 1}):
            result = lotus_dispatch_action(
                result_id="test",
                plugin_name="test-plugin",
                action_name="do_thing",
                inputs={"x": 1},
                session=session,
                auth=auth,
            )

        job = session.get(ArtifactExecution, UUID(result["execution_id"]))
        assert job is not None
        assert job.inputs.get("__embeddr_auth") is not None
        assert job.inputs["__embeddr_auth"]["user_id"] == str(auth.user_id)
        assert job.inputs["__embeddr_auth"]["operator_id"] == str(
            auth.operator_id)
        assert job.inputs["__embeddr_auth"]["is_admin"] is False


class TestLotusPrepareInputs:
    """lotus_prepare_inputs merges caller inputs with plugin config."""

    def test_caller_overrides_config(self, session):
        from embeddr.api.v1.lotus_service import lotus_prepare_inputs

        with patch("embeddr.api.v1.lotus_service.resolve_plugin_config", return_value={"api_key": "saved", "model": "default"}), \
                patch("embeddr.api.v1.lotus_service.get_lotus_registry") as mock_reg:
            mock_reg.return_value.list.return_value = []

            merged = lotus_prepare_inputs(
                plugin_name="test",
                inputs={"model": "override", "prompt": "hello"},
                session=session,
            )

        assert merged["model"] == "override"  # Caller wins
        assert merged["api_key"] == "saved"  # Config fills in
        assert merged["prompt"] == "hello"

    def test_drops_none_inputs(self, session):
        from embeddr.api.v1.lotus_service import lotus_prepare_inputs

        with patch("embeddr.api.v1.lotus_service.resolve_plugin_config", return_value={}), \
                patch("embeddr.api.v1.lotus_service.get_lotus_registry") as mock_reg:
            mock_reg.return_value.list.return_value = []

            merged = lotus_prepare_inputs(
                plugin_name="test",
                inputs={"keep": "value", "drop": None},
                session=session,
            )

        assert "keep" in merged
        assert "drop" not in merged
