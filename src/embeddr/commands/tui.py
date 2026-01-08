import typer
import json
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, ListView, ListItem, Label, Input, Static, TabbedContent, TabPane
from textual import on, work
from sqlmodel import Session, select, col
from embeddr.db.session import get_engine
from embeddr_core.models import Artifact, ArtifactEmbedding, ArtifactAnnotation, ArtifactLineage

app = typer.Typer(help="Interactive TUI Explorer (Textual)")


class EmbeddrTuiApp(App):
    CSS = """
    Screen {
        layout: horizontal;
    }
    #sidebar {
        width: 30%;
        height: 100%;
        background: $surface;
        border-right: solid $primary;
        dock: left;
    }
    #main-content {
        width: 70%;
        height: 100%;
    }
    #sidebar-header {
        padding: 1;
        background: $primary;
        color: $text;
        text-align: center;
        text-style: bold;
    }
    #search-box {
        dock: top;
        margin: 1;
    }
    ListView {
        height: 100%;
    }
    ListItem {
        padding: 1;
    }
    ListItem:hover {
        background: $accent 20%;
    }
    #details-container {
        padding: 1;
        height: 100%;
    }
    .info-label {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        ("d", "toggle_dark", "Toggle dark mode"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.engine = get_engine()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Container(id="sidebar"):
            yield Label("EMBEDDR EXPLORER", id="sidebar-header")
            yield Input(placeholder="Search artifacts...", id="search-box")
            yield ListView(id="artifact-list")

        with Container(id="main-content"):
            with ScrollableContainer(id="details-container"):
                yield Label("Select an artifact from the sidebar to view details.", id="placeholder-text")

        yield Footer()

    def on_mount(self):
        self.load_artifacts()

    @work(exclusive=True, thread=True)
    def load_artifacts(self, search_query: str = ""):
        # Fetch data in background
        with Session(self.engine) as session:
            query = select(Artifact).limit(50).order_by(
                col(Artifact.created_at).desc())
            if search_query:
                # Naive search on URI or filename in metadata if possible
                # Creating a boolean clause for search
                query = query.where(col(Artifact.uri).contains(search_query))

            results = session.exec(query).all()

            # Prepare data to send back to UI thread
            artifact_data = []
            for art in results:
                name = art.uri or "No URI"
                if art.metadata_json and "filename" in art.metadata_json:
                    name = art.metadata_json["filename"]
                artifact_data.append({
                    "id": art.id,
                    "type": art.type_name,
                    "name": name
                })

            # Update UI on main thread
            self.call_from_thread(self.update_list, artifact_data)

    def update_list(self, artifact_data):
        list_view = self.query_one("#artifact-list", ListView)
        list_view.clear()

        if not artifact_data:
            list_view.append(ListItem(Label("No results found")))
            return

        for item_data in artifact_data:
            # Create Custom ListItem or just robust Label
            label_text = f"[{item_data['type']}] {item_data['name']}"
            item = ListItem(Label(label_text))
            # Tag the item with ID so we can retrieve it on selection
            item.embeddr_id = item_data['id']
            list_view.append(item)

    @on(Input.Changed, "#search-box")
    def on_search_changed(self, event: Input.Changed):
        self.load_artifacts(event.value)

    @on(ListView.Selected, "#artifact-list")
    def on_artifact_selected(self, event: ListView.Selected):
        if hasattr(event.item, "embeddr_id"):
            self.show_artifact_details(event.item.embeddr_id)

    def show_artifact_details(self, artifact_id):
        container = self.query_one("#details-container", ScrollableContainer)
        # Clear existing
        container.remove_children()

        with Session(self.engine) as session:
            art = session.get(Artifact, artifact_id)
            if not art:
                container.mount(Label("Error: Artifact not found in DB."))
                return

            embeddings = session.exec(select(ArtifactEmbedding).where(
                ArtifactEmbedding.artifact_id == art.id)).all()
            annotations = session.exec(select(ArtifactAnnotation).where(
                ArtifactAnnotation.artifact_id == art.id)).all()

            parents = session.exec(select(ArtifactLineage).where(
                ArtifactLineage.child_id == art.id)).all()
            children = session.exec(select(ArtifactLineage).where(
                ArtifactLineage.parent_id == art.id)).all()

            # --- Info Tab Widgets ---
            # NOTE: Textual Widgets must be Instantiated Freshly if mounting again

            info_widgets = [
                Label(f"[bold]ID:[/bold] {str(art.id)}", classes="info-label"),
                Label(
                    f"[bold]URI:[/bold] {str(art.uri)}", classes="info-label"),
                Label(
                    f"[bold]Type:[/bold] {str(art.type_name)}", classes="info-label"),
                # Label(f"[bold]Base Type:[/bold] {str(art.base_type_name)}", classes="info-label"),
                Label(
                    f"[bold]Created:[/bold] {str(art.created_at)}", classes="info-label"),
            ]

            if art.metadata_json:
                info_widgets.append(
                    Label("[bold]Metadata:[/bold]", classes="info-label"))
                info_widgets.append(
                    Static(json.dumps(art.metadata_json, indent=2), classes="info-label"))

            # --- AI Data Tab Widgets ---
            ai_widgets = [Label("[bold underline]Embeddings[/bold underline]")]
            if embeddings:
                for e in embeddings:
                    ai_widgets.append(Label(
                        f"• [green]{e.model_name}[/green] (dim: {e.vector_dim}, space: {e.space})"))
            else:
                ai_widgets.append(Label("No embeddings found."))

            ai_widgets.append(
                Label("\n[bold underline]Annotations[/bold underline]"))
            if annotations:
                for a in annotations:
                    ai_widgets.append(
                        Label(f"• [{a.annotation_type}] {a.text} (conf: {a.confidence})"))
            else:
                ai_widgets.append(Label("No annotations found."))

            # --- Construct Tabs ---
            tabs = TabbedContent()
            container.mount(tabs)

            pane_info = TabPane("General Info")
            tabs.mount(pane_info)
            pane_info.mount(*info_widgets)

            pane_ai = TabPane("AI & Vectors")
            tabs.mount(pane_ai)
            pane_ai.mount(*ai_widgets)

            pane_rel = TabPane("Relations")
            tabs.mount(pane_rel)

            rel_widgets = []
            rel_widgets.append(
                Label("[bold underline]Parents (Derived From)[/bold underline]"))
            if parents:
                for p in parents:
                    rel_widgets.append(Label(f"• Parent ID: {p.parent_id}"))
            else:
                rel_widgets.append(Label("No parents found."))

            rel_widgets.append(
                Label("\n[bold underline]Children (Generated)[/bold underline]"))
            if children:
                for c in children:
                    rel_widgets.append(Label(f"• Child ID: {c.child_id}"))
            else:
                rel_widgets.append(Label("No children found."))

            pane_rel.mount(*rel_widgets)


@app.command()
def explore():
    """
    Start the Textual TUI Explorer.
    """
    tui_app = EmbeddrTuiApp()
    tui_app.run()
