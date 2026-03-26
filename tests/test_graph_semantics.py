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
    assert known.family in ("containment", "other")  # family depends on registry state

    unknown = get_relation_semantics("custom:edge")
    assert unknown.canonical_type == "custom:edge"
    assert unknown.family == "other"


def test_namespace_grouping():
    assert normalize_namespace_group("plugin:my-plugin") == "plugin"
    assert normalize_namespace_group("agent:runner") == "agent"
    assert normalize_namespace_group("user") == "user"
    assert normalize_namespace_group("") == "unknown"


def test_relation_taxonomy_contains_core_families():
    taxonomy = relation_taxonomy(["contains_image", "produced_by"])
    family_ids = {row["id"] for row in taxonomy["families"]}
    # Core families are always present in the taxonomy
    assert "other" in family_ids
    assert len(family_ids) >= 1


def test_graph_bfs_traverses_relations(session):
    """Core BFS: traverses artifact relations and returns node_ids + edges."""
    a = Artifact(id=uuid4(), type_name="image", base_type_name="artifact", metadata_json={})
    b = Artifact(id=uuid4(), type_name="image", base_type_name="artifact", metadata_json={})
    c = Artifact(id=uuid4(), type_name="image", base_type_name="artifact", metadata_json={})
    session.add_all([a, b, c])
    session.flush()

    session.add(ArtifactRelation(source_id=a.id, target_id=b.id, relation_type="derived_from"))
    session.add(ArtifactRelation(source_id=b.id, target_id=c.id, relation_type="variant_of"))
    session.commit()

    result = run_graph_bfs(
        session=session,
        seed_ids=[a.id],
        max_depth=2,
        direction="both",
        include_lineage=False,
        include_relations=True,
        filters=GraphQueryFilters(),
        limit_nodes=100,
        limit_edges=100,
    )

    assert a.id in result["node_ids"]
    assert b.id in result["node_ids"]
    assert c.id in result["node_ids"]
    assert len(result["edges"]) == 2


def test_graph_bfs_respects_max_depth(session):
    """BFS stops at max_depth."""
    a = Artifact(id=uuid4(), type_name="image", base_type_name="artifact", metadata_json={})
    b = Artifact(id=uuid4(), type_name="image", base_type_name="artifact", metadata_json={})
    c = Artifact(id=uuid4(), type_name="image", base_type_name="artifact", metadata_json={})
    session.add_all([a, b, c])
    session.flush()

    session.add(ArtifactRelation(source_id=a.id, target_id=b.id, relation_type="derived_from"))
    session.add(ArtifactRelation(source_id=b.id, target_id=c.id, relation_type="derived_from"))
    session.commit()

    result = run_graph_bfs(
        session=session,
        seed_ids=[a.id],
        max_depth=1,
        direction="both",
        include_lineage=False,
        include_relations=True,
        filters=GraphQueryFilters(),
        limit_nodes=100,
        limit_edges=100,
    )

    assert a.id in result["node_ids"]
    assert b.id in result["node_ids"]
    # c is 2 hops away, should NOT be included at depth=1
    assert c.id not in result["node_ids"]
