from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from sqlmodel import Session

from embeddr_core.models.artifact import Artifact
from embeddr_core.plugin_interface import PluginContext


_GRAPH_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "embeddr-plugins"
    / "plugins"
    / "core"
    / "embeddr-core"
    / "src"
    / "capabilities"
    / "graph.py"
)


def _load_graph_module():
    spec = importlib.util.spec_from_file_location(
        "embeddr_core_graph_cap", _GRAPH_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Unable to load graph capability module: {_GRAPH_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lotus_graph_update_denies_non_owner(engine):
    graph = _load_graph_module()

    owner_user_id = uuid4()
    owner_operator_id = uuid4()
    regular_user_id = uuid4()

    with Session(engine) as session:
        art = Artifact(
            type_name="artifact",
            base_type_name="artifact",
            uri="memory://root-owned",
            metadata_json={"label": "orig", "visibility": "private"},
            owner_user_id=owner_user_id,
            owner_operator_id=owner_operator_id,
        )
        session.add(art)
        session.commit()
        session.refresh(art)
        artifact_id = art.id

    context = PluginContext(
        user_id=regular_user_id,
        operator_id=owner_operator_id,
        is_admin=False,
    )

    with patch.object(graph, "get_engine", return_value=engine):
        result = graph.handle_action(
            "artifact.update",
            execution_id=None,
            inputs={
                "artifact_id": str(artifact_id),
                "metadata_json": {"label": "hacked"},
            },
            context=context,
            plugin_name="embeddr-core",
        )

    assert result["ok"] is False
    assert result["error"] == "not_found"

    with Session(engine) as session:
        saved = session.get(Artifact, artifact_id)
        assert saved is not None
        assert saved.metadata_json.get("label") == "orig"


def test_lotus_graph_update_allows_owner(engine):
    graph = _load_graph_module()

    owner_user_id = uuid4()
    owner_operator_id = uuid4()

    with Session(engine) as session:
        art = Artifact(
            type_name="artifact",
            base_type_name="artifact",
            uri="memory://owned",
            metadata_json={"label": "orig"},
            owner_user_id=owner_user_id,
            owner_operator_id=owner_operator_id,
        )
        session.add(art)
        session.commit()
        session.refresh(art)
        artifact_id = art.id

    context = PluginContext(
        user_id=owner_user_id,
        operator_id=owner_operator_id,
        is_admin=False,
    )

    with patch.object(graph, "get_engine", return_value=engine):
        result = graph.handle_action(
            "artifact.update",
            execution_id=None,
            inputs={
                "artifact_id": str(artifact_id),
                "metadata_json": {"label": "updated"},
            },
            context=context,
            plugin_name="embeddr-core",
        )

    assert result["ok"] is True

    with Session(engine) as session:
        saved = session.get(Artifact, artifact_id)
        assert saved is not None
        assert saved.metadata_json.get("label") == "updated"
