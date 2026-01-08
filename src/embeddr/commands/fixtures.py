import typer
import random
import uuid
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select, text
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_type import ArtifactType
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact_embedding import ArtifactEmbedding
from embeddr_core.models.artifact_annotation import ArtifactAnnotation
from embeddr.db.session import get_engine

app = typer.Typer(help="Manage test fixtures and example data")


@app.command()
def load(
    clean: bool = typer.Option(
        False, "--clean", help="Delete existing artifacts before seeding"),
    count: int = typer.Option(
        10, "--count", help="Number of family trees to generate")
):
    """Load example data into the database."""
    engine = get_engine()

    with Session(engine) as session:
        if clean:
            typer.echo("Cleaning existing data...")
            # Naive delete
            # Delete lineage first due to foreign keys if cascade setup, though explicit here is safer
            session.exec(text("DELETE FROM artifactlineage"))
            session.exec(text("DELETE FROM artifactembedding"))
            session.exec(text("DELETE FROM artifactannotation"))
            session.exec(text("DELETE FROM artifact"))
            session.commit()

        # Ensure types exist
        image_type = session.exec(select(ArtifactType).where(
            ArtifactType.name == "image")).first()
        if not image_type:
            typer.echo(
                "Error: 'image' type not found. Run 'embeddr init-v2 run' first.")
            raise typer.Exit(code=1)

        typer.echo(f"Generating {count} artifact families...")

        subjects = ["cat", "dog", "robot", "landscape",
                    "portrait", "cyberpunk city", "flower", "nebula"]
        styles = ["oil painting", "digital art", "sketch",
                  "photorealistic", "anime", "watercolor"]

        for i in range(count):
            subject = random.choice(subjects)
            style = random.choice(styles)

            # 1. Create Parent (Source Image)
            parent_id = uuid.uuid4()
            parent = Artifact(
                id=parent_id,
                type_name="image",
                base_type_name="image",
                uri=f"/tmp/embeddr_fixtures/{parent_id}.png",
                metadata_json={"label": f"Source {subject}",
                               "width": 1024, "height": 1024, "source": "upload"},
                created_at=datetime.now(timezone.utc) -
                timedelta(days=random.randint(1, 30))
            )
            session.add(parent)

            # Annotation for Parent
            session.add(ArtifactAnnotation(
                artifact_id=parent_id,
                annotation_type="caption",
                text=f"A photo of a {subject}",
                plugin_name="dummy-captioner",
                confidence=0.95
            ))

            # Embedding for Parent
            session.add(ArtifactEmbedding(
                artifact_id=parent_id,
                model_name="clip-vit-l-14",
                vector_dim=4,
                vector_json=[random.random() for _ in range(4)],
                plugin_name="system"
            ))

            # 2. Create Child (Processed)
            child_id = uuid.uuid4()
            child = Artifact(
                id=child_id,
                type_name="image",
                base_type_name="image",
                uri=f"/tmp/embeddr_fixtures/{child_id}.png",
                metadata_json={"label": f"{style} {subject}",
                               "width": 1024, "height": 1024, "steps": 20, "cfg": 7.0},
                created_at=datetime.now(timezone.utc)
            )
            session.add(child)

            # Link Parent -> Child
            link = ArtifactLineage(
                parent_id=parent_id,
                child_id=child_id,
                relationship_metadata={"type": "derived",
                                       "workflow": "img2img", "strength": 0.75}
            )
            session.add(link)

            # 3. Create a Grandchild (Upscale) - Optional
            if random.random() > 0.5:
                grandchild_id = uuid.uuid4()
                grandchild = Artifact(
                    id=grandchild_id,
                    type_name="image",
                    base_type_name="image",
                    uri=f"/tmp/embeddr_fixtures/{grandchild_id}.png",
                    metadata_json={
                        "label": f"{style} {subject} (Upscaled)", "width": 2048, "height": 2048, "upscaler": "ESRGAN"},
                    created_at=datetime.now(timezone.utc)
                )
                session.add(grandchild)

                link_gc = ArtifactLineage(
                    parent_id=child_id,
                    child_id=grandchild_id,
                    relationship_metadata={
                        "type": "derived", "workflow": "upscale"}
                )
                session.add(link_gc)

            typer.echo(f"  Created family for {subject}")

        session.commit()
        typer.echo("Done!")
