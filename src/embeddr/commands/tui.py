import typer

app = typer.Typer(help="Interactive TUI Explorer (Textual)")


@app.command()
def explore():
    """
    Start the Textual TUI Explorer.
    """
    try:
        from embeddr.commands.tui_impl import EmbeddrTuiApp
        tui_app = EmbeddrTuiApp()
        tui_app.run()
    except ImportError as e:
        typer.echo(f"Failed to load TUI components: {e}", err=True)
        typer.echo("This might be due to missing 'textual' dependency.", err=True)
