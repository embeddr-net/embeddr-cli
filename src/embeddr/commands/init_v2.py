import typer
from sqlmodel import SQLModel, Session, select
from embeddr.db.session import get_engine
from alembic import command
from alembic.config import Config
from pathlib import Path
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

            # Also drop alembic_version manually as it's not in metadata
            from sqlalchemy import text
            with engine.connect() as conn:
                conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
                conn.commit()

            typer.secho("Dropped all tables (including migrations).",
                        fg=typer.colors.RED)

    # Create tables
    typer.echo("Creating V2 tables...")
    SQLModel.metadata.create_all(engine)
    typer.secho("Created V2 tables.", fg=typer.colors.GREEN)

    # Stamp with alembic head so subsequent runs don't try to migrate
    try:
        ini_path = Path(__file__).parents[1] / "db" / "alembic.ini"
        if ini_path.exists():
            alembic_cfg = Config(str(ini_path))
            command.stamp(alembic_cfg, "head")
            typer.echo("Stamping database with alembic head.")
    except Exception as e:
        typer.echo(f"Warning: Could not stamp database: {e}")

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

    # 'text' - Core readable text
    text_type = ArtifactType(
        name="text",
        description="Text content",
        parent_name="artifact",
        default_capabilities=["readable", "taggable"]
    )
    session.add(text_type)

    # 'collection' - Grouping mechanism (replaces empty:folder)
    collection = ArtifactType(
        name="collection",
        description="Groups other artifacts",
        parent_name="artifact",
        default_capabilities=["container", "nestable"]
    )
    session.add(collection)

    # 'collection:directory' - Physical filesystem directory
    col_dir = ArtifactType(
        name="collection:directory",
        description="Filesystem directory",
        parent_name="collection",
        default_capabilities=["container", "nestable", "scan", "filesystem"]
    )
    session.add(col_dir)

    typer.echo(
        "Seeded base types: artifact, image, text, collection, collection:directory")
