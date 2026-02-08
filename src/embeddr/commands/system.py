import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn
from embeddr_core.services.resource_manager import resource_manager, ResourceStatus
import requests
import os

app = typer.Typer(help="Manage system resources and models.")
console = Console()


@app.command()
def status(
    remote: bool = typer.Option(
        True, help="Fetch status from the running server if available")
):
    """
    Show current system resource usage and loaded models.
    """
    resources = []
    total_mem = 0

    if remote:
        host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
        port = os.environ.get("EMBEDDR_PORT", "8003")
        try:
            resp = requests.get(
                f"http://{host}:{port}/api/v1/system/resources", timeout=2)
            if resp.ok:
                data = resp.json()
                resources = data.get("resources", [])
                total_mem = data.get("total_memory_bytes", 0)
        except Exception:
            console.print(
                "[yellow]Warning: Could not connect to server, showing local process status instead.[/yellow]")
            resources = [r.model_dump()
                         for r in resource_manager.list_resources()]
            total_mem = resource_manager.get_total_memory_usage()
    else:
        resources = [r.model_dump() for r in resource_manager.list_resources()]
        total_mem = resource_manager.get_total_memory_usage()

    if not resources:
        console.print("No managed resources found.")
        return

    table = Table(title="Embeddr System Resources")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Plugin", style="blue")
    table.add_column("Status", style="magenta")
    table.add_column("Memory", justify="right", style="green")
    table.add_column("Device", style="yellow")

    def format_bytes(b):
        if b == 0:
            return "0 B"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if b < 1024:
                return f"{b:.2f} {unit}"
            b /= 1024
        return f"{b:.2f} PB"

    for r in resources:
        status_color = "green" if r['status'] == ResourceStatus.LOADED else "yellow" if r['status'] == ResourceStatus.LOADING else "dim"
        table.add_row(
            r['id'],
            r['name'],
            r['plugin_name'],
            f"[{status_color}]{r['status']}[/{status_color}]",
            format_bytes(r['memory_usage_bytes']),
            r['device']
        )

    console.print(table)
    console.print(
        f"\n[bold]Total Memory Usage:[/bold] [green]{format_bytes(total_mem)}[/green]")


@app.command()
def unload(resource_id: str):
    """
    Unload a specific model or resource.
    """
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    try:
        resp = requests.post(
            f"http://{host}:{port}/api/v1/system/resources/unload?resource_id={resource_id}", timeout=5)
        if resp.ok:
            console.print(
                f"[green]Successfully requested unload for {resource_id}[/green]")
        else:
            console.print(
                f"[red]Failed to unload {resource_id}: {resp.text}[/red]")
    except Exception as e:
        console.print(f"[red]Error connecting to server: {e}[/red]")


@app.command()
def unload_all():
    """
    Unload all models.
    """
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    try:
        resp = requests.post(
            f"http://{host}:{port}/api/v1/system/resources/unload_all", timeout=5)
        if resp.ok:
            console.print(
                "[green]Successfully requested unload for all resources[/green]")
        else:
            console.print(
                f"[red]Failed to unload all resources: {resp.text}[/red]")
    except Exception as e:
        console.print(f"[red]Error connecting to server: {e}[/red]")
