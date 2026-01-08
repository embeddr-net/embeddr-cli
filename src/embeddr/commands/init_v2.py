import typer
from sqlmodel import SQLModel, Session, select
from embeddr.db.session import get_engine
# Import all models to ensure they are registered in metadata
from embeddr_core.models import (
    Artifact,
    ArtifactType,
    ArtifactLineage,
    ArtifactRelation,
    Transformation,
    PluginRegistry,
    Tag,
    ArtifactTagLink,
    ArtifactEmbedding,
    ArtifactAnnotation,
    Collection,
    CollectionItem
)

app = typer.Typer(help="Initialize V2 schema and data")


@app.command()
def run(
    drop_all: bool = typer.Option(
        False, "--drop-all", help="Drop all tables before creating"),
    seed: bool = typer.Option(True, help="Seed base types")
):
    """
    Initialize the V2 database schema. 
    This is a destructive operation if --drop-all is used!
    """
    engine = get_engine()

    if drop_all:
        import click
        if click.confirm("WARNING: Are you sure you want to drop ALL tables? This destroys ALL DATA.", abort=True):
            SQLModel.metadata.drop_all(engine)
            typer.secho("Dropped all tables.", fg=typer.colors.RED)

    # Create tables
    typer.echo("Creating V2 tables...")
    SQLModel.metadata.create_all(engine)
    typer.secho("Created V2 tables.", fg=typer.colors.GREEN)

    if seed:
        with Session(engine) as session:
            seed_base_types(session)
            session.commit()
        typer.secho("Seeding complete.", fg=typer.colors.GREEN)


def seed_base_types(session: Session):
    # Check if exists
    if session.exec(select(ArtifactType)).first():
        typer.echo("Artifact types already exist, skipping seed.")
        return

    typer.echo("Seeding base Artifact Types...")

    # Base 'artifact'
    root = ArtifactType(
        name="artifact",
        description="Root type for all artifacts",
        parent_name=None,
        default_capabilities=["viewable", "taggable"]
    )
    session.add(root)

    # 'image'
    image = ArtifactType(
        name="image",
        description="Static 2D image",
        parent_name="artifact",
        default_capabilities=["viewable", "taggable", "processable", "visual"]
    )
    session.add(image)

    # 'empty:folder'
    folder = ArtifactType(
        name="empty:folder",
        description="Logical container",
        parent_name="artifact",
        default_capabilities=["container", "nestable"]
    )
    session.add(folder)

    typer.echo("Seeded base types: artifact, image, empty:folder")
