"""Debug and inspect commands for new subsystems.

Usage:
    embeddr debug status          # Overview of all subsystems
    embeddr debug executions      # List recent executions
    embeddr debug execution ID    # Deep-dive a single execution + events
    embeddr debug panels          # List panel sessions
    embeddr debug auth            # Auth mode, users, keys, roles, permissions
    embeddr debug lotus           # Lotus registry capabilities
    embeddr debug lotus --plugin  # Filter by plugin
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree
from rich.text import Text
from rich import box
from sqlmodel import Session, select, func, col

from embeddr.db.session import get_engine

app = typer.Typer(
    help="Debug and inspect executions, sessions, auth, and Lotus.")
console = Console()


# ---------------------------------------------------------------------------
# embeddr debug status
# ---------------------------------------------------------------------------

@app.command()
def status():
    """Combined overview: execution counts, panel sessions, auth state, lotus caps."""
    engine = get_engine()

    # Helper to safely query — tables may not exist yet
    def _safe_count(session, model):
        try:
            return session.exec(select(func.count()).select_from(model)).one()
        except Exception:
            return None

    def _safe_query(session, stmt):
        try:
            return list(session.exec(stmt).all())
        except Exception:
            return []

    with Session(engine) as session:
        # --- Execution counts ---
        from embeddr_core.models.artifact_execution import ArtifactExecution
        from embeddr_core.models.artifact_execution_event import ArtifactExecutionEvent

        total_execs = _safe_count(session, ArtifactExecution)
        by_status = _safe_query(session, select(
            ArtifactExecution.status, func.count()).group_by(ArtifactExecution.status))
        total_events = _safe_count(session, ArtifactExecutionEvent)

        # --- Panel session counts ---
        from embeddr_core.models.panel_session import PanelSession

        total_panels = _safe_count(session, PanelSession)
        # simplified — both may be None
        active_panels = _safe_count(session, PanelSession)
        try:
            active_panels = session.exec(
                select(func.count()).select_from(PanelSession)
                .where(PanelSession.closed_at == None)  # noqa: E711
            ).one()
        except Exception:
            active_panels = None
        panels_by_type = _safe_query(session, select(PanelSession.panel_type, func.count()).where(PanelSession.closed_at == None).group_by(PanelSession.panel_type))  # noqa: E711

        # --- Auth counts ---
        from embeddr_core.models.user_account import Client
        from embeddr_core.models.api_key import ClientCredential
        from embeddr_core.models.role import Role, RolePermission
        from embeddr.services.auth_service import get_auth_mode

        auth_mode = get_auth_mode()
        user_count = _safe_count(session, Client)
        key_count = _safe_count(session, ClientCredential)
        role_count = _safe_count(session, Role)
        perm_count = _safe_count(session, RolePermission)

    # --- Lotus registry ---
    try:
        from embeddr.core.plugin_loader import get_lotus_registry
        from embeddr_core.models.lotus import LotusKind
        reg = get_lotus_registry()
        actions = reg.list(kind=LotusKind.action)
        features = reg.list(kind=LotusKind.feature)
        lotus_loaded = True
    except Exception:
        actions, features = [], []
        lotus_loaded = False

    # --- Render ---
    console.print()
    console.print(
        Panel.fit("[bold cyan]Embeddr Debug Status[/bold cyan]", border_style="cyan"))

    def _v(val):
        return str(val) if val is not None else "[dim]N/A (table missing)[/dim]"

    # Executions
    t = Table(title="Executions", box=box.ROUNDED, title_style="bold yellow")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", style="white")
    t.add_row("Total executions", _v(total_execs))
    t.add_row("Total events", _v(total_events))
    for s, c in by_status:
        color = {"completed": "green", "failed": "red",
                 "running": "yellow", "pending": "dim"}.get(s, "white")
        t.add_row(f"  {s}", f"[{color}]{c}[/{color}]")
    console.print(t)

    # Panels
    t = Table(title="Panel Sessions", box=box.ROUNDED,
              title_style="bold yellow")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", style="white")
    t.add_row("Total (all time)", _v(total_panels))
    t.add_row("Active", f"[green]{_v(active_panels)}[/green]")
    for pt, c in panels_by_type:
        t.add_row(f"  {pt}", str(c))
    console.print(t)

    # Auth
    t = Table(title="Auth & Security", box=box.ROUNDED,
              title_style="bold yellow")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", style="white")
    mode_color = {"open": "red", "single": "yellow",
                  "multi": "green", "db": "green"}.get(auth_mode, "white")
    t.add_row("Auth mode", f"[{mode_color}]{auth_mode}[/{mode_color}]")
    t.add_row("Users (clients)", _v(user_count))
    t.add_row("Credentials (API keys)", _v(key_count))
    t.add_row("Roles", _v(role_count))
    t.add_row("Permission grants", _v(perm_count))
    console.print(t)

    # Lotus
    t = Table(title="Lotus Registry", box=box.ROUNDED,
              title_style="bold yellow")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", style="white")
    t.add_row("Registry loaded",
              "[green]yes[/green]" if lotus_loaded else "[red]no[/red]")
    t.add_row("Actions", str(len(actions)))
    t.add_row("Features", str(len(features)))
    console.print(t)
    console.print()


# ---------------------------------------------------------------------------
# embeddr debug executions
# ---------------------------------------------------------------------------

@app.command()
def executions(
    limit: int = typer.Option(20, help="Max rows to show"),
    status_filter: Optional[str] = typer.Option(
        None, "--status", "-s", help="Filter by status"),
    plugin: Optional[str] = typer.Option(
        None, "--plugin", "-p", help="Filter by plugin"),
):
    """List recent executions with status, plugin, timing."""
    from embeddr_core.models.artifact_execution import ArtifactExecution

    engine = get_engine()
    with Session(engine) as session:
        stmt = select(ArtifactExecution).order_by(
            col(ArtifactExecution.created_at).desc())
        if status_filter:
            stmt = stmt.where(ArtifactExecution.status == status_filter)
        if plugin:
            stmt = stmt.where(ArtifactExecution.plugin_name == plugin)
        stmt = stmt.limit(limit)
        try:
            rows = session.exec(stmt).all()
        except Exception as e:
            console.print(
                f"[red]Cannot query executions (table may not exist): {e}[/red]")
            console.print(
                "[dim]Run migrations to create the artifactexecution table.[/dim]")
            return

    if not rows:
        console.print("[dim]No executions found.[/dim]")
        return

    t = Table(title=f"Recent Executions (last {limit})", box=box.ROUNDED)
    t.add_column("ID", style="dim", max_width=12)
    t.add_column("Type", style="cyan")
    t.add_column("Plugin", style="magenta")
    t.add_column("Status", style="white")
    t.add_column("Priority", justify="right")
    t.add_column("Progress", justify="right")
    t.add_column("Trigger", style="dim")
    t.add_column("Created", style="dim")
    t.add_column("Duration", justify="right")

    for row in rows:
        sid = str(row.id)[:8]
        status_color = {
            "completed": "green", "failed": "red", "running": "yellow",
            "pending": "dim", "canceled": "strike",
        }.get(row.status, "white")

        duration = ""
        if row.started_at and row.finished_at:
            dt = (row.finished_at - row.started_at).total_seconds()
            duration = f"{dt:.1f}s"
        elif row.started_at:
            dt = (datetime.now(timezone.utc) - row.started_at).total_seconds()
            duration = f"{dt:.0f}s (running)"

        t.add_row(
            sid,
            row.type,
            row.plugin_name,
            f"[{status_color}]{row.status}[/{status_color}]",
            str(row.priority),
            f"{row.progress}%",
            row.trigger or "",
            row.created_at.strftime(
                "%m/%d %H:%M:%S") if row.created_at else "",
            duration,
        )

    console.print(t)


# ---------------------------------------------------------------------------
# embeddr debug execution <ID>
# ---------------------------------------------------------------------------

@app.command()
def execution(
    execution_id: str = typer.Argument(help="Execution UUID (prefix ok)"),
):
    """Deep-dive a single execution: details + event log."""
    from embeddr_core.models.artifact_execution import ArtifactExecution
    from embeddr_core.models.artifact_execution_event import ArtifactExecutionEvent

    engine = get_engine()
    with Session(engine) as session:
        # Support prefix matching
        stmt = select(ArtifactExecution)
        if len(execution_id) < 36:
            stmt = stmt.where(ArtifactExecution.id.cast(
                str).startswith(execution_id))  # type: ignore
        else:
            try:
                uid = UUID(execution_id)
                stmt = stmt.where(ArtifactExecution.id == uid)
            except ValueError:
                console.print(f"[red]Invalid UUID: {execution_id}[/red]")
                return

        try:
            ex = session.exec(stmt).first()
        except Exception as e:
            console.print(f"[red]Cannot query executions: {e}[/red]")
            return
        if not ex:
            console.print(f"[red]Execution not found: {execution_id}[/red]")
            return

        # Events
        try:
            events = session.exec(
                select(ArtifactExecutionEvent)
                .where(ArtifactExecutionEvent.execution_id == ex.id)
                .order_by(col(ArtifactExecutionEvent.created_at).asc())
            ).all()
        except Exception:
            events = []

    # Render details
    tree = Tree(f"[bold cyan]Execution {str(ex.id)[:8]}[/bold cyan]")
    tree.add(f"[bold]Type:[/bold] {ex.type}")
    tree.add(f"[bold]Plugin:[/bold] {ex.plugin_name}")
    status_color = {"completed": "green", "failed": "red",
                    "running": "yellow"}.get(ex.status, "white")
    tree.add(
        f"[bold]Status:[/bold] [{status_color}]{ex.status}[/{status_color}]")
    tree.add(f"[bold]Resource class:[/bold] {ex.resource_class}")
    tree.add(f"[bold]Priority:[/bold] {ex.priority}")
    tree.add(f"[bold]Progress:[/bold] {ex.progress}%")
    tree.add(f"[bold]Trigger:[/bold] {ex.trigger}")
    if ex.message:
        tree.add(f"[bold]Message:[/bold] {ex.message}")
    if ex.error:
        tree.add(f"[bold red]Error:[/bold red] {ex.error}")
    if ex.operator_id:
        tree.add(f"[bold]Operator:[/bold] {ex.operator_id}")
    if ex.api_key_id:
        tree.add(f"[bold]API Key:[/bold] {ex.api_key_id}")
    if ex.parent_execution_id:
        tree.add(
            f"[bold]Parent execution:[/bold] {str(ex.parent_execution_id)[:8]}")
    if ex.primary_artifact_id:
        tree.add(
            f"[bold]Primary artifact:[/bold] {str(ex.primary_artifact_id)[:8]}")

    tree.add(f"[bold]Created:[/bold] {ex.created_at}")
    if ex.started_at:
        tree.add(f"[bold]Started:[/bold] {ex.started_at}")
    if ex.finished_at:
        tree.add(f"[bold]Finished:[/bold] {ex.finished_at}")

    if ex.inputs:
        inputs_node = tree.add("[bold]Inputs:[/bold]")
        for k, v in (ex.inputs or {}).items():
            val_str = str(v)[:120]
            inputs_node.add(f"{k}: {val_str}")

    if ex.outputs:
        outputs_node = tree.add("[bold]Outputs:[/bold]")
        for k, v in (ex.outputs or {}).items():
            val_str = str(v)[:120]
            outputs_node.add(f"{k}: {val_str}")

    if ex.tags:
        tags_node = tree.add("[bold]Tags:[/bold]")
        for k, v in (ex.tags or {}).items():
            tags_node.add(f"{k}={v}")

    console.print(tree)

    # Render events
    if events:
        console.print()
        et = Table(title="Execution Events", box=box.ROUNDED)
        et.add_column("Time", style="dim", max_width=20)
        et.add_column("Type", style="cyan")
        et.add_column("Level", style="white")
        et.add_column("Message", style="white", max_width=60)
        et.add_column("Payload keys", style="dim")

        for ev in events:
            level_color = {"error": "red", "warning": "yellow",
                           "debug": "dim"}.get(ev.level, "white")
            payload_keys = ", ".join(
                (ev.payload or {}).keys()) if ev.payload else ""
            et.add_row(
                ev.created_at.strftime("%H:%M:%S.%f")[
                    :12] if ev.created_at else "",
                ev.event_type,
                f"[{level_color}]{ev.level}[/{level_color}]",
                (ev.message or "")[:60],
                payload_keys,
            )
        console.print(et)
    else:
        console.print("[dim]No events recorded.[/dim]")


# ---------------------------------------------------------------------------
# embeddr debug panels
# ---------------------------------------------------------------------------

@app.command()
def panels(
    panel_type: Optional[str] = typer.Option(
        None, "--type", "-t", help="Filter by panel type"),
    include_closed: bool = typer.Option(
        False, "--closed", help="Include closed panels"),
    limit: int = typer.Option(30, help="Max rows"),
):
    """List panel sessions with ownership and activity info."""
    from embeddr_core.models.panel_session import PanelSession

    engine = get_engine()
    with Session(engine) as session:
        stmt = select(PanelSession).order_by(
            col(PanelSession.last_active_at).desc())
        if not include_closed:
            stmt = stmt.where(PanelSession.closed_at == None)  # noqa: E711
        if panel_type:
            stmt = stmt.where(PanelSession.panel_type == panel_type)
        stmt = stmt.limit(limit)
        try:
            rows = session.exec(stmt).all()
        except Exception as e:
            console.print(
                f"[red]Cannot query panel sessions (table may not exist): {e}[/red]")
            console.print(
                "[dim]Run migrations to create the panelsession table.[/dim]")
            return

    if not rows:
        console.print("[dim]No panel sessions found.[/dim]")
        return

    t = Table(title="Panel Sessions", box=box.ROUNDED)
    t.add_column("ID", style="dim", max_width=8)
    t.add_column("Panel ID", style="cyan")
    t.add_column("Type", style="magenta")
    t.add_column("Title", style="white", max_width=25)
    t.add_column("Window", style="dim")
    t.add_column("Client", style="dim", max_width=8)
    t.add_column("Credential", style="dim", max_width=8)
    t.add_column("Items", justify="right")
    t.add_column("Last Active", style="dim")
    t.add_column("Status", style="white")

    for row in rows:
        item_count = str(len(row.items)) if row.items else "0"
        active_str = row.last_active_at.strftime(
            "%m/%d %H:%M") if row.last_active_at else ""
        status = "[green]active[/green]" if not row.closed_at else "[red]closed[/red]"

        t.add_row(
            str(row.id)[:8],
            row.panel_id,
            row.panel_type,
            row.title or "",
            row.window_id or "",
            str(row.client_id)[:8] if row.client_id else "",
            str(row.credential_id)[:8] if row.credential_id else "",
            item_count,
            active_str,
            status,
        )

    console.print(t)


# ---------------------------------------------------------------------------
# embeddr debug auth
# ---------------------------------------------------------------------------

@app.command()
def auth():
    """Inspect authentication mode, users, credentials, roles, and permissions."""
    from embeddr_core.models.user_account import Client
    from embeddr_core.models.api_key import ClientCredential
    from embeddr_core.models.role import Role, RolePermission
    from embeddr_core.models.operator import Operator
    from embeddr.services.auth_service import get_auth_mode

    engine = get_engine()
    auth_mode = get_auth_mode()

    def _safe_all(session, model):
        try:
            return list(session.exec(select(model)).all())
        except Exception:
            return []

    with Session(engine) as session:
        operators = _safe_all(session, Operator)
        users = _safe_all(session, Client)
        keys = _safe_all(session, ClientCredential)
        roles = _safe_all(session, Role)
        role_perms = _safe_all(session, RolePermission)

    # Mode
    mode_color = {"open": "red", "single": "yellow",
                  "multi": "green", "db": "green"}.get(auth_mode, "white")
    console.print(Panel.fit(
        f"[bold]Auth Mode:[/bold] [{mode_color}]{auth_mode}[/{mode_color}]",
        title="Authentication",
        border_style="cyan",
    ))

    # Operators
    if operators:
        t = Table(title="Operators", box=box.ROUNDED)
        t.add_column("ID", style="dim", max_width=8)
        t.add_column("Name", style="cyan")
        t.add_column("Display Name", style="white")
        t.add_column("Root", style="yellow")
        t.add_column("Active", style="green")
        for op in operators:
            t.add_row(
                str(op.id)[:8],
                op.name,
                op.display_name or "",
                "yes" if op.is_root else "",
                "yes" if op.is_active else "[red]no[/red]",
            )
        console.print(t)

    # Users
    if users:
        t = Table(title="Users (Clients)", box=box.ROUNDED)
        t.add_column("ID", style="dim", max_width=8)
        t.add_column("Username", style="cyan")
        t.add_column("Display Name", style="white")
        t.add_column("Admin", style="yellow")
        t.add_column("Active", style="green")
        t.add_column("Operator", style="dim", max_width=8)
        for u in users:
            t.add_row(
                str(u.id)[:8],
                u.username,
                u.display_name or "",
                "yes" if u.is_admin else "",
                "yes" if u.is_active else "[red]no[/red]",
                str(u.operator_id)[:8] if u.operator_id else "",
            )
        console.print(t)

    # Credentials
    if keys:
        t = Table(title="Client Credentials (API Keys)", box=box.ROUNDED)
        t.add_column("ID", style="dim", max_width=8)
        t.add_column("Name", style="cyan")
        t.add_column("Prefix", style="dim")
        t.add_column("Scopes", style="magenta", max_width=40)
        t.add_column("Active", style="green")
        t.add_column("User", style="dim", max_width=8)
        t.add_column("Last Used", style="dim")
        for k in keys:
            scopes_str = ", ".join(k.scopes) if k.scopes else "[dim]none[/dim]"
            t.add_row(
                str(k.id)[:8],
                k.name,
                k.key_prefix,
                scopes_str,
                "yes" if k.is_active else "[red]no[/red]",
                str(k.user_id)[:8] if k.user_id else "",
                k.last_used_at.strftime(
                    "%m/%d %H:%M") if k.last_used_at else "[dim]never[/dim]",
            )
        console.print(t)

    # Roles
    if roles:
        t = Table(title="Roles", box=box.ROUNDED)
        t.add_column("ID", style="dim", max_width=8)
        t.add_column("Name", style="cyan")
        t.add_column("Description", style="white", max_width=40)
        for r in roles:
            t.add_row(str(r.id)[:8], r.name, r.description or "")
        console.print(t)

    # Permissions
    if role_perms:
        # Group by role
        perm_tree = Tree("[bold]Role → Permissions[/bold]")
        by_role: dict[UUID, list[str]] = {}
        for rp in role_perms:
            by_role.setdefault(rp.role_id, []).append(rp.permission)

        role_map = {r.id: r.name for r in roles}
        for rid, perms in by_role.items():
            role_node = perm_tree.add(
                f"[cyan]{role_map.get(rid, str(rid)[:8])}[/cyan]")
            for p in sorted(perms):
                role_node.add(p)
        console.print(perm_tree)

    if not operators and not users and not keys and not roles:
        console.print(
            "[dim]No auth entities found (auth mode is likely 'open').[/dim]")


# ---------------------------------------------------------------------------
# embeddr debug lotus
# ---------------------------------------------------------------------------

@app.command()
def lotus(
    plugin: Optional[str] = typer.Option(
        None, "--plugin", "-p", help="Filter by plugin"),
    show_schema: bool = typer.Option(
        False, "--schema", help="Show input schemas"),
    show_expose: bool = typer.Option(
        False, "--expose", help="Show expose flags"),
):
    """Inspect the Lotus capability registry."""
    try:
        from embeddr.core.plugin_loader import get_lotus_registry
        from embeddr_core.models.lotus import LotusKind
    except Exception as e:
        console.print(f"[red]Lotus registry not available: {e}[/red]")
        return

    reg = get_lotus_registry()
    caps = reg.list(kind=LotusKind.action) + reg.list(kind=LotusKind.feature)

    if plugin:
        caps = [c for c in caps if c.plugin == plugin]

    if not caps:
        console.print("[dim]No capabilities found.[/dim]")
        return

    # Group by plugin
    by_plugin: dict[str, list] = {}
    for cap in caps:
        by_plugin.setdefault(cap.plugin or "unknown", []).append(cap)

    for pname, pcaps in sorted(by_plugin.items()):
        console.print(Panel.fit(
            f"[bold magenta]{pname}[/bold magenta] ({len(pcaps)} capabilities)",
            border_style="magenta",
        ))

        t = Table(box=box.SIMPLE_HEAD)
        t.add_column("ID", style="cyan")
        t.add_column("Kind", style="dim")
        t.add_column("Title", style="white", max_width=30)
        t.add_column("Action", style="yellow")
        if show_expose:
            t.add_column("Expose", style="dim", max_width=30)
        if show_schema:
            t.add_column("Schema", style="dim", max_width=50)

        for cap in pcaps:
            act = cap.action
            action_name = (
                act.action if act and act.action else "") if act else ""
            expose_str = ""
            schema_str = ""

            if show_expose and act and act.expose:
                expose_str = json.dumps(act.expose, separators=(",", ":"))
            elif show_expose:
                data = cap.data or {}
                if isinstance(data, dict) and data.get("expose"):
                    expose_str = json.dumps(
                        data["expose"], separators=(",", ":"))

            if show_schema and act and act.input and isinstance(act.input, dict):
                schema = act.input.get("schema")
                if schema and isinstance(schema, dict):
                    props = list(schema.get("properties", {}).keys())
                    schema_str = ", ".join(props[:8])
                    if len(props) > 8:
                        schema_str += f" (+{len(props)-8})"

            row = [
                cap.id,
                str(cap.kind) if cap.kind else "",
                cap.title or "",
                action_name,
            ]
            if show_expose:
                row.append(expose_str)
            if show_schema:
                row.append(schema_str)

            t.add_row(*row)

        console.print(t)
        console.print()
