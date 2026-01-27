from pathlib import Path

import typer

from embeddr.commands.init_wizard import run_init_wizard

app = typer.Typer(help="Initialize an Embeddr project (legacy alias)")


@app.command()
def run(
    name: str = typer.Option(None, help="Project name"),
    path: Path = typer.Option(
        Path.cwd(), help="Path to initialize project in"),
):
    """
    Initialize a new Embeddr project. This command is now an alias for `init`.
    """
    typer.secho(
        "init-v2 is now the standard init flow.",
        fg=typer.colors.YELLOW,
    )
    run_init_wizard(path, name)
