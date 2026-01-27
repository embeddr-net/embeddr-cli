import typer
from typing import Optional, List
from sqlmodel import Session, select, func, col
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree

from embeddr.db.session import get_engine
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_type import ArtifactType
from embeddr_core.models.artifact_embedding import ArtifactEmbedding
from embeddr_core.models.artifact_feature import ArtifactFeatureRef
from embeddr_core.models.artifact_annotation import ArtifactAnnotation
from embeddr_core.models.artifact_relation import ArtifactRelation
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.plugin_registry import PluginRegistry
from embeddr_core.models.tag import Tag

app = typer.Typer(help="Inspect the database state, artifacts, and metadata.")
console = Console()


@app.command()
def stats():
    """
    Show high-level statistics about the Embeddr database.
    """
    engine = get_engine()
    with Session(engine) as session:
        # Counts
        artifact_count = session.exec(
            select(func.count()).select_from(Artifact)).one()
        embedding_count = session.exec(
            select(func.count()).select_from(ArtifactEmbedding)).one()
        feature_count = session.exec(
            select(func.count()).select_from(ArtifactFeatureRef)).one()
        annotation_count = session.exec(
            select(func.count()).select_from(ArtifactAnnotation)).one()
        relation_count = session.exec(
            select(func.count()).select_from(ArtifactRelation)).one()
        lineage_count = session.exec(
            select(func.count()).select_from(ArtifactLineage)).one()
        plugin_count = session.exec(
            select(func.count()).select_from(PluginRegistry)).one()
        tag_count = session.exec(
            select(func.count()).select_from(Tag)).one()
        collection_count = session.exec(
            select(func.count())
            .select_from(Artifact)
            .where(Artifact.base_type_name == "collection")
        ).one()

        # Artifacts by Type
        artifacts_by_type = session.exec(
            select(Artifact.type_name, func.count())
            .select_from(Artifact)
            .group_by(Artifact.type_name)
        ).all()

        # Embeddings by Model
        embeddings_by_model = session.exec(
            select(ArtifactEmbedding.model_name, func.count())
            .select_from(ArtifactEmbedding)
            .group_by(ArtifactEmbedding.model_name)
        ).all()

    # Display
    console.print(Panel.fit("[bold blue]Embeddr Core Statistics[/bold blue]"))

    table = Table(title="Entity Counts")
    table.add_column("Entity", style="cyan")
    table.add_column("Count", style="magenta")

    table.add_row("Artifacts", str(artifact_count))
    table.add_row("Embeddings", str(embedding_count))
    table.add_row("Feature Refs", str(feature_count))
    table.add_row("Annotations", str(annotation_count))
    table.add_row("Relations", str(relation_count))
    table.add_row("Lineage Edges", str(lineage_count))
    table.add_row("Plugins", str(plugin_count))
    table.add_row("Tags", str(tag_count))
    table.add_row("Collections", str(collection_count))

    console.print(table)

    if artifacts_by_type:
        type_table = Table(title="Artifacts by Type")
        type_table.add_column("Type", style="green")
        type_table.add_column("Count", style="yellow")
        for type_name, count in artifacts_by_type:
            type_table.add_row(type_name, str(count))
        console.print(type_table)

    if embeddings_by_model:
        emb_table = Table(title="Embeddings by Model")
        emb_table.add_column("Model/Space", style="green")
        emb_table.add_column("Count", style="yellow")
        for model, count in embeddings_by_model:
            emb_table.add_row(str(model), str(count))
        console.print(emb_table)


@app.command("list")
def list_artifacts(
    type_name: Optional[str] = typer.Argument(
        None, help="Filter by artifact type (e.g. image, text)"),
    limit: int = typer.Option(20, help="Max items to show")
):
    """
    List artifacts in the database.
    """
    engine = get_engine()
    with Session(engine) as session:
        query = select(Artifact)
        if type_name:
            # Simple wildcard support
            if "*" in type_name:
                query = query.where(col(Artifact.type_name).like(
                    type_name.replace("*", "%")))
            else:
                query = query.where(Artifact.type_name == type_name)

        query = query.limit(limit)
        results = session.exec(query).all()

        if not results:
            console.print("[yellow]No artifacts found.[/yellow]")
            return

        table = Table(title=f"Artifacts ({len(results)} shown)")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Type", style="green")
        table.add_column("URI / Name", style="white")
        table.add_column("Created At", style="dim")

        for art in results:
            # Try to get a decent name/URI
            name = art.uri
            if art.metadata_json and "filename" in art.metadata_json:
                name = art.metadata_json["filename"]

            table.add_row(str(art.id), art.type_name,
                          name or "N/A", str(art.created_at))

        console.print(table)


@app.command()
def show(artifact_id: str):
    """
    Show detailed information about a single artifact.
    """
    engine = get_engine()
    with Session(engine) as session:
        # Fetch artifact
        art = session.get(Artifact, artifact_id)
        if not art:
            console.print(f"[red]Artifact {artifact_id} not found.[/red]")
            return

        # Fetch related data
        embeddings = session.exec(
            select(ArtifactEmbedding).where(
                ArtifactEmbedding.artifact_id == art.id)
        ).all()

        features = session.exec(
            select(ArtifactFeatureRef).where(
                ArtifactFeatureRef.artifact_id == art.id)
        ).all()

        annotations = session.exec(
            select(ArtifactAnnotation).where(
                ArtifactAnnotation.artifact_id == art.id)
        ).all()

        # Parent lineage (where child_id == art.id)
        parents = session.exec(
            select(ArtifactLineage).where(ArtifactLineage.child_id == art.id)
        ).all()

        # Children lineage (where parent_id == art.id)
        children = session.exec(
            select(ArtifactLineage).where(ArtifactLineage.parent_id == art.id)
        ).all()

        # Display
        console.print(Panel.fit(f"[bold]Artifact Detail: {art.id}[/bold]"))

        # Metadata Tree
        tree = Tree(f"[bold cyan]{art.type_name}[/bold cyan] - {art.uri}")
        tree.add(f"ID: {art.id}")
        tree.add(f"Base Type: {art.base_type_name}")
        tree.add(f"Created: {art.created_at}")

        meta = tree.add("Metadata")
        if art.metadata_json:
            for k, v in art.metadata_json.items():
                meta.add(f"{k}: {v}")
        else:
            meta.add("[dim]Empty[/dim]")

        # Embeddings
        emb_node = tree.add(f"Embeddings ({len(embeddings)})")
        for e in embeddings:
            emb_node.add(
                f"[green]{e.model_name}[/green] (dim: {e.vector_dim}, space: {e.space})")

        # Feature refs
        feat_node = tree.add(f"Feature Refs ({len(features)})")
        for f in features:
            feat_node.add(
                f"[green]{f.feature_type}[/green] {f.name} ({f.storage_kind})")

        # Annotations
        ann_node = tree.add(f"Annotations ({len(annotations)})")
        for a in annotations:
            ann_node.add(
                f"[{a.annotation_type}] {a.text[:50]}... (conf: {a.confidence})")

        # Lineage
        lin_node = tree.add("Lineage")
        if parents:
            p_node = lin_node.add("Parents (Derived From)")
            for p in parents:
                p_node.add(f"Parent ID: {p.parent_id}")
        if children:
            c_node = lin_node.add("Children (Generated)")
            for c in children:
                c_node.add(f"Child ID: {c.child_id}")

        console.print(tree)


@app.command()
def sample_json(
    limit: int = typer.Option(5, help="Number of items to sample")
):
    """
    Dump a JSON sample of the DB (Artifacts + Relations + Annotations).
    Useful for showing DB structure to other agents.
    """
    import json
    import uuid
    from datetime import datetime

    def json_serial(obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, (datetime)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return str(obj)

    engine = get_engine()
    with Session(engine) as session:
        # Get Sample Artifacts (stratified by type)
        types = session.exec(select(Artifact.type_name).distinct()).all()
        artifacts: List[Artifact] = []
        for t in types:
            artifacts.extend(session.exec(select(Artifact).where(
                Artifact.type_name == t).limit(limit)).all())

        output_data = []
        for art in artifacts:
            # Fetch Relations
            rels_out = session.exec(select(ArtifactRelation).where(
                ArtifactRelation.source_id == art.id)).all()
            rels_in = session.exec(select(ArtifactRelation).where(
                ArtifactRelation.target_id == art.id)).all()

            # Fetch Annotations
            anns = session.exec(select(ArtifactAnnotation).where(
                ArtifactAnnotation.artifact_id == art.id)).all()

            # Fetch Embeddings
            embs = session.exec(select(ArtifactEmbedding).where(
                ArtifactEmbedding.artifact_id == art.id)).all()

            art_dict = art.model_dump()
            art_dict['relations_outgoing'] = [r.model_dump() for r in rels_out]
            art_dict['relations_incoming'] = [r.model_dump() for r in rels_in]
            art_dict['annotations'] = [a.model_dump() for a in anns]
            # Exclude heavy vector data
            art_dict['embeddings'] = [{k: v for k, v in e.model_dump().items() if k != 'vector_json'}
                                      for e in embs]

            output_data.append(art_dict)

        print(json.dumps(output_data, default=json_serial, indent=2))
