import typer
from pathlib import Path
from sqlmodel import Session
from embeddr.db.session import get_engine
from embeddr_core.services.scanner import scan_path
from embeddr_core.services.embedding_manager import generate_embeddings_for_artifacts
from embeddr_core.services.vector_store import VectorStoreService

app = typer.Typer(help="Process artifacts (scan, embed, analyze)")


@app.command()
def scan(
    path: str = typer.Argument(..., help="Path to scan"),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", help="Scan recursively")
):
    """
    Scan a filesystem path and register artifacts.
    """
    engine = get_engine()
    with Session(engine) as session:
        count = scan_path(session, path, recursive=recursive)
        typer.secho(
            f"Scanned and added {count} artifacts.", fg=typer.colors.GREEN)


@app.command()
def embed(
    model: str = typer.Option(
        "openai/clip-vit-base-patch32", help="Model name"),
    force: bool = typer.Option(
        False, "--force", help="Force recompute existing embeddings")
):
    """
    Generate embeddings for artifacts that need them.
    Checks ArtifactEmbedding table for missing entries.
    """
    engine = get_engine()

    # We need to initialize the VectorService if we rely on it for injection
    # But embedding_manager generates and inserts directly for now.

    with Session(engine) as session:
        def progress(current, total, msg):
            typer.echo(f"[{current}/{total}] {msg}")

        generate_embeddings_for_artifacts(
            session,
            model_name=model,
            force_recompute=force,
            progress_callback=progress
        )

    typer.secho("Embedding generation complete.", fg=typer.colors.GREEN)


@app.command()
def search(
    query: str = typer.Argument(
        ..., help="Text query to search for (requires text-to-vector capability not yet impl in CLI)"),
    limit: int = 10
):
    """
    Search for artifacts.
    """
    typer.echo("Search execution not fully implemented in CLI yet.")
