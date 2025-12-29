import typer
import os
from embeddr.commands import serve, config
from embeddr.core.config import refresh_settings

app = typer.Typer()
serve.register(app)

app.add_typer(config.app, name="config")


@app.callback()
def callback(
    data_dir: str = typer.Option(
        None,
        "--data-dir",
        "-d",
        help="Path to the data directory (defaults to ~/.local/share/embeddr)",
        envvar="EMBEDDR_DATA_DIR",
    ),
):
    if data_dir:
        os.environ["EMBEDDR_DATA_DIR"] = data_dir
        refresh_settings()


def main():
    app()


if __name__ == "__main__":
    main()
