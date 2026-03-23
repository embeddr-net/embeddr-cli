from uuid import uuid4

from embeddr.services.graph_query_service import GraphQueryFilters, run_graph_bfs
from embeddr_core.graph_semantics import (
    get_relation_semantics,
    normalize_namespace_group,
    relation_taxonomy,
)
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_relation import ArtifactRelation


def test_relation_semantics_known_and_unknown():
    known = get_relation_semantics("contains_image")
    assert known.canonical_type == "contains_image"
    assert known.family == "containment"

    unknown = get_relation_semantics("custom:edge")
    assert unknown.canonical_type == "custom:edge"
    assert unknown.family == "other"


def test_namespace_grouping():
    assert normalize_namespace_group("plugin:stash") == "plugin"
    assert normalize_namespace_group("agent:runner") == "agent"
    assert normalize_namespace_group("user") == "user"
    assert normalize_namespace_group("") == "unknown"


def test_relation_taxonomy_contains_core_families():
    taxonomy = relation_taxonomy(["contains_image", "produced_by"])
    family_ids = {row["id"] for row in taxonomy["families"]}
    assert "containment" in family_ids
    assert "provenance" in family_ids


def test_run_graph_bfs_filters_legacy_stash_contains(session):
    a = Artifact(id=uuid4(), type_name="artifact", base_type_name="artifact", metadata_json={})
    b = Artifact(id=uuid4(), type_name="artifact", base_type_name="artifact", metadata_json={})
    c = Artifact(id=uuid4(), type_name="artifact", base_type_name="artifact", metadata_json={})
    session.add(a)
    session.add(b)
    session.add(c)
    session.flush()

    session.add(
        ArtifactRelation(
            source_id=a.id,
            target_id=b.id,
            relation_type="contains",
            source_namespace="plugin:stash",
        )
    )
    session.add(
        ArtifactRelation(
            source_id=a.id,
            target_id=c.id,
            relation_type="contains_image",
            source_namespace="plugin:stash-preview",
        )
    )
    session.commit()

    result_default = run_graph_bfs(
        session=session,
        seed_ids=[a.id],
        max_depth=1,
        direction="both",
        include_lineage=False,
        include_relations=True,
        filters=GraphQueryFilters(),
        limit_nodes=1500,
        limit_edges=6000,
    )
    assert all(
        not (
            edge["relation_type_raw"] == "contains"
            and edge["source_namespace"] == "plugin:stash"
        )
        for edge in result_default["edges"]
    )
    assert any(
        edge["relation_type_raw"] == "contains_image"
        for edge in result_default["edges"]
    )

    result_with_legacy = run_graph_bfs(
        session=session,
        seed_ids=[a.id],
        max_depth=1,
        direction="both",
        include_lineage=False,
        include_relations=True,
        filters=GraphQueryFilters(include_legacy_stash_contains=True),
        limit_nodes=1500,
        limit_edges=6000,
    )
    assert any(
        edge["relation_type_raw"] == "contains"
        and edge["source_namespace"] == "plugin:stash"
        for edge in result_with_legacy["edges"]
    )
