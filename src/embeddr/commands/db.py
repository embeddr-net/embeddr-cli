import typer
from alembic import command
from alembic.config import Config
from pathlib import Path

app = typer.Typer(help="Database management commands")


def get_alembic_config():
    # Assuming alembic.ini is in src/embeddr/db/alembic.ini
    # We need to find where the package is installed or located
    import embeddr.db

    db_dir = Path(embeddr.db.__file__).parent
    ini_path = db_dir / "alembic.ini"
    if not ini_path.exists():
        typer.secho(f"Could not find alembic.ini at {ini_path}", fg=typer.colors.RED)
        raise typer.Exit(1)

    return Config(str(ini_path))


@app.command()
def upgrade(revision: str = "head"):
    """Upgrade to a later version."""
    alembic_cfg = get_alembic_config()
    command.upgrade(alembic_cfg, revision)
    typer.secho(f"Upgraded database to {revision}", fg=typer.colors.GREEN)


@app.command()
def downgrade(revision: str):
    """Revert to a previous version."""
    alembic_cfg = get_alembic_config()
    command.downgrade(alembic_cfg, revision)
    typer.secho(f"Downgraded database to {revision}", fg=typer.colors.GREEN)


@app.command()
def revision(
    message: str = typer.Option(..., "-m", "--message"), autogenerate: bool = False
):
    """Create a new revision file."""
    alembic_cfg = get_alembic_config()
    command.revision(alembic_cfg, message=message, autogenerate=autogenerate)
    typer.secho(f"Created new revision: {message}", fg=typer.colors.GREEN)


@app.command()
def current():
    """Display the current revision for a database."""
    alembic_cfg = get_alembic_config()
    command.current(alembic_cfg)


@app.command()
def history():
    """List changeset scripts in chronological order."""
    alembic_cfg = get_alembic_config()
    command.history(alembic_cfg)
