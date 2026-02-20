from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from embeddr.services import nelumbo_service

router = APIRouter(prefix="/nelumbo", tags=["nelumbo"])


class RegistryItemResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    author: Optional[str] = None
    author_url: Optional[str] = None
    author_profile_url: Optional[str] = None
    author_logo_url: Optional[str] = None
    author_icon_url: Optional[str] = None
    homepage_url: Optional[str] = None
    repository_url: Optional[str] = None
    license: Optional[str] = None
    screenshots: Optional[List[str]] = None
    logo_url: Optional[str] = None
    icon_url: Optional[str] = None
    banner_url: Optional[str] = None
    tokens: Optional[dict] = None
    source: str
    tags: List[str] = Field(default_factory=list)
    version: Optional[str] = None
    intents: Optional[List[str]] = None
    actions: Optional[List[dict]] = None
    frontend_actions: Optional[List[dict]] = None
    frontend_components: Optional[List[dict]] = None
    panels: Optional[List[dict]] = None
    config_renderers: Optional[List[dict]] = None
    pages: Optional[List[dict]] = None
    widgets: Optional[List[dict]] = None
    lotus_capabilities: Optional[List[dict]] = None
    url: Optional[str] = None
    css_url: Optional[str] = None


class SystemCheckResponse(BaseModel):
    platform: str
    python_version: str
    uv_available: bool
    git_available: bool
    node_available: bool
    cwd: str
    repo_root: str
    local_plugins: int
    local_themes: int
    plugins_dir: str
    themes_dir: str


class NelumboContextResponse(BaseModel):
    workspace_detected: bool
    project_root: Optional[str] = None
    api_base_url: Optional[str] = None
    selected_workspace: Optional[str] = None


class ServerStatusResponse(BaseModel):
    running: bool
    pid: Optional[int] = None
    host: Optional[str] = None
    port: Optional[int] = None
    base_url: Optional[str] = None
    started_at: Optional[int] = None
    log_path: Optional[str] = None
    message: Optional[str] = None
    root_key: Optional[str] = None
    user_key: Optional[str] = None


class ServerStartRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8003
    data_dir: Optional[str] = None
    plugins_dir: Optional[str] = None
    workspace_path: Optional[str] = None
    enable_docs: bool = False
    allow_dev_origins: bool = False
    confirm: bool = False


class ServerStopRequest(BaseModel):
    confirm: bool = False


class ServerLogsResponse(BaseModel):
    ok: bool
    log_path: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None


class WorkspaceCreateRequest(BaseModel):
    name: str
    path: str
    auth_mode: str
    plugins: List[str] = Field(default_factory=list)
    themes: List[str] = Field(default_factory=list)
    operator_name: str = "user"
    admin_username: str = "user"
    user_operator_name: Optional[str] = None
    user_client_name: Optional[str] = None
    database_provider: str = "sqlite"
    database_url: Optional[str] = None
    confirm: bool = False


class WorkspaceCreateResponse(BaseModel):
    workspace_path: str
    config_path: str
    plan_path: str
    data_dir: str
    plugins_dir: str
    installed_plugins: List[str] = Field(default_factory=list)
    missing_plugins: List[str] = Field(default_factory=list)


class WorkspaceSummary(BaseModel):
    name: str
    path: str
    auth_mode: str
    api_base_url: Optional[str] = None
    is_selected: bool = False


class WorkspaceDiscoverResponse(BaseModel):
    ok: bool
    root: str
    count: int
    workspaces: List[WorkspaceSummary] = Field(default_factory=list)
    message: Optional[str] = None


class WorkspaceSelectRequest(BaseModel):
    workspace_path: str
    confirm: bool = False


class WorkspaceSelectResponse(BaseModel):
    ok: bool
    message: Optional[str] = None
    workspace_path: Optional[str] = None


class WorkspaceCurrentResponse(BaseModel):
    ok: bool
    selected_workspace: Optional[str] = None
    active_workspace: str


class DatabaseTestRequest(BaseModel):
    database_provider: str = "sqlite"
    database_url: Optional[str] = None
    workspace_path: Optional[str] = None
    confirm: bool = False


class DatabaseTestResponse(BaseModel):
    ok: bool
    message: str
    resolved_url: Optional[str] = None
    sqlite_path: Optional[str] = None


class WorkspacePluginsResponse(BaseModel):
    ok: bool
    message: Optional[str] = None
    plugins: List[str] = Field(default_factory=list)
    plugins_dir: Optional[str] = None


class RegistryPluginInstallRequest(BaseModel):
    plugin_ids: List[str] = Field(default_factory=list)
    api_key: Optional[str] = None
    reinstall: bool = False
    confirm: bool = False


class RegistryPluginInstallResponse(BaseModel):
    ok: bool
    message: Optional[str] = None
    installed: List[str] = Field(default_factory=list)
    skipped: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    errors: dict = Field(default_factory=dict)


class WorkspaceDisabledPluginsResponse(BaseModel):
    ok: bool
    message: Optional[str] = None
    disabled_plugins: List[str] = Field(default_factory=list)
    data_dir: Optional[str] = None


class WorkspaceDisabledPluginsRequest(BaseModel):
    disabled_plugins: List[str] = Field(default_factory=list)
    confirm: bool = False


class RegistryStatusResponse(BaseModel):
    enabled: bool
    base_url: Optional[str] = None
    ok: bool
    error: Optional[str] = None
    plugins: int = 0
    themes: int = 0


class RegistryConfigRequest(BaseModel):
    enabled: bool
    base_url: Optional[str] = None


class RegistryConfigResponse(BaseModel):
    enabled: bool
    base_url: Optional[str] = None


@router.get("/system-check", response_model=SystemCheckResponse)
def get_system_check() -> SystemCheckResponse:
    return SystemCheckResponse(**nelumbo_service.get_system_check())


@router.get("/context", response_model=NelumboContextResponse)
def get_context() -> NelumboContextResponse:
    return NelumboContextResponse(**nelumbo_service.get_nelumbo_context())


@router.get("/server/status", response_model=ServerStatusResponse)
def get_server_status() -> ServerStatusResponse:
    return ServerStatusResponse(**nelumbo_service.get_embeddr_server_status())


@router.post("/server/start", response_model=ServerStatusResponse)
def start_server(payload: ServerStartRequest) -> ServerStatusResponse:
    result = nelumbo_service.start_embeddr_server(
        host=payload.host,
        port=payload.port,
        data_dir=payload.data_dir,
        plugins_dir=payload.plugins_dir,
        workspace_path=payload.workspace_path,
        enable_docs=payload.enable_docs,
        allow_dev_origins=payload.allow_dev_origins,
        confirm=payload.confirm,
    )
    return ServerStatusResponse(**result)


@router.post("/server/stop", response_model=ServerStatusResponse)
def stop_server(payload: ServerStopRequest) -> ServerStatusResponse:
    result = nelumbo_service.stop_embeddr_server(confirm=payload.confirm)
    return ServerStatusResponse(**result)


@router.get("/server/logs", response_model=ServerLogsResponse)
def get_server_logs(lines: int = 200) -> ServerLogsResponse:
    result = nelumbo_service.get_embeddr_server_logs(lines=lines)
    return ServerLogsResponse(**result)


@router.get("/registry/plugins", response_model=list[RegistryItemResponse])
def list_plugins() -> list[RegistryItemResponse]:
    return [
        RegistryItemResponse(**item.__dict__)
        for item in nelumbo_service.list_registry_plugins_merged()
    ]


@router.get("/registry/themes", response_model=list[RegistryItemResponse])
def list_themes() -> list[RegistryItemResponse]:
    return [
        RegistryItemResponse(**item.__dict__)
        for item in nelumbo_service.list_registry_themes_merged()
    ]


@router.get("/registry/remote/plugins", response_model=list[RegistryItemResponse])
def list_registry_plugins() -> list[RegistryItemResponse]:
    return [
        RegistryItemResponse(**item.__dict__)
        for item in nelumbo_service.list_registry_plugins()
    ]


@router.get("/registry/remote/themes", response_model=list[RegistryItemResponse])
def list_registry_themes() -> list[RegistryItemResponse]:
    return [
        RegistryItemResponse(**item.__dict__)
        for item in nelumbo_service.list_registry_themes()
    ]


@router.get("/registry/status", response_model=RegistryStatusResponse)
def registry_status() -> RegistryStatusResponse:
    return RegistryStatusResponse(**nelumbo_service.get_registry_status())


@router.get("/registry/config", response_model=RegistryConfigResponse)
def registry_config() -> RegistryConfigResponse:
    return RegistryConfigResponse(**nelumbo_service.get_registry_config())


@router.post("/registry/config", response_model=RegistryConfigResponse)
def update_registry_config(payload: RegistryConfigRequest) -> RegistryConfigResponse:
    return RegistryConfigResponse(
        **nelumbo_service.set_registry_config(
            enabled=payload.enabled,
            base_url=payload.base_url,
        )
    )


@router.get("/assets/plugins/{plugin_id}/{asset_name}")
def get_plugin_asset(plugin_id: str, asset_name: str):
    if ".." in plugin_id or ".." in asset_name:
        raise HTTPException(status_code=404, detail="Asset not found")
    safe_asset = Path(asset_name).name
    safe_plugin = Path(plugin_id).name
    asset_path = nelumbo_service.get_local_plugin_asset_path(
        safe_plugin, safe_asset)
    if not asset_path:
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(asset_path)


@router.post("/workspaces", response_model=WorkspaceCreateResponse)
def create_workspace(payload: WorkspaceCreateRequest) -> WorkspaceCreateResponse:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    if payload.auth_mode not in {"open", "single", "multi", "db"}:
        raise HTTPException(status_code=400, detail="Invalid auth mode")
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Workspace name required")
    if not payload.path.strip():
        raise HTTPException(status_code=400, detail="Workspace path required")

    target_dir = Path(payload.path).expanduser()
    if not target_dir.is_absolute():
        target_dir = Path.cwd() / target_dir

    result = nelumbo_service.create_workspace_destructive(
        name=payload.name.strip(),
        target_dir=target_dir,
        auth_mode=payload.auth_mode,
        plugins=payload.plugins,
        themes=payload.themes,
        operator_name=payload.operator_name.strip() or "user",
        admin_username=payload.admin_username.strip() or "user",
        user_operator_name=(payload.user_operator_name or "").strip() or None,
        user_client_name=(payload.user_client_name or "").strip() or None,
        database_provider=payload.database_provider,
        database_url=payload.database_url,
    )
    return WorkspaceCreateResponse(**result)


@router.get("/workspaces/discover", response_model=WorkspaceDiscoverResponse)
def discover_workspaces(root: Optional[str] = None, max_depth: int = 4) -> WorkspaceDiscoverResponse:
    result = nelumbo_service.discover_workspaces(
        search_root=root,
        max_depth=max(1, min(max_depth, 8)),
    )
    return WorkspaceDiscoverResponse(**result)


@router.get("/workspace/current", response_model=WorkspaceCurrentResponse)
def get_current_workspace() -> WorkspaceCurrentResponse:
    return WorkspaceCurrentResponse(**nelumbo_service.get_selected_workspace())


@router.post("/workspace/select", response_model=WorkspaceSelectResponse)
def select_workspace(payload: WorkspaceSelectRequest) -> WorkspaceSelectResponse:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    if not payload.workspace_path.strip():
        raise HTTPException(status_code=400, detail="Workspace path required")
    result = nelumbo_service.set_selected_workspace(
        workspace_path=payload.workspace_path.strip(),
    )
    return WorkspaceSelectResponse(**result)


@router.post("/database/test", response_model=DatabaseTestResponse)
def test_database(payload: DatabaseTestRequest) -> DatabaseTestResponse:
    result = nelumbo_service.test_database_connection(
        database_provider=payload.database_provider,
        database_url=payload.database_url,
        workspace_path=payload.workspace_path,
        confirm=payload.confirm,
    )
    return DatabaseTestResponse(**result)


@router.get("/workspace/plugins", response_model=WorkspacePluginsResponse)
def get_workspace_plugins() -> WorkspacePluginsResponse:
    return WorkspacePluginsResponse(**nelumbo_service.list_workspace_plugins())


@router.post(
    "/registry/plugins/install", response_model=RegistryPluginInstallResponse
)
def install_registry_plugins(
    payload: RegistryPluginInstallRequest,
) -> RegistryPluginInstallResponse:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    result = nelumbo_service.install_registry_plugins_destructive(
        plugin_ids=payload.plugin_ids,
        api_key=payload.api_key,
        reinstall=payload.reinstall,
        confirm=payload.confirm,
    )
    return RegistryPluginInstallResponse(**result)


@router.get(
    "/workspace/plugins/disabled",
    response_model=WorkspaceDisabledPluginsResponse,
)
def get_workspace_disabled_plugins() -> WorkspaceDisabledPluginsResponse:
    return WorkspaceDisabledPluginsResponse(
        **nelumbo_service.list_workspace_disabled_plugins()
    )


@router.post(
    "/workspace/plugins/disabled",
    response_model=WorkspaceDisabledPluginsResponse,
)
def set_workspace_disabled_plugins(
    payload: WorkspaceDisabledPluginsRequest,
) -> WorkspaceDisabledPluginsResponse:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    result = nelumbo_service.set_workspace_disabled_plugins_destructive(
        disabled_plugins=payload.disabled_plugins,
        confirm=payload.confirm,
    )
    return WorkspaceDisabledPluginsResponse(**result)
