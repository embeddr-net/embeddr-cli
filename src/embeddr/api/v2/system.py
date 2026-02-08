from fastapi import APIRouter, Request, HTTPException, Response, Depends, Body
from starlette.routing import Mount
from pydantic import BaseModel
import subprocess
import shutil
import sys
import time
from pathlib import Path
from uuid import UUID
from typing import List, Dict, Any, Optional
from importlib.metadata import version as pkg_version, PackageNotFoundError
from sqlalchemy.engine.url import make_url
from sqlalchemy import text
from alembic.runtime.migration import MigrationContext
from embeddr_core.services.resource_manager import resource_manager
from embeddr_core.services.config_service import set_plugin_config, resolve_plugin_config
from embeddr.services.blob_registry import (
    list_providers,
    list_resolvers,
    list_provider_resolvers,
    set_default_provider,
    set_default_resolver,
    get_default_provider_name,
    get_default_resolver_name,
)
from embeddr.services.socket_manager import manager
from sqlmodel import Session, select, func
from embeddr.db.session import get_engine, backup_database, get_session
from embeddr_core.models.automation import Automation
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_blob import ArtifactBlob
from embeddr.core.config import settings
from embeddr.api.security import (
    COOKIE_NAME,
    get_auth_context,
    check_auth_enabled,
    require_permission,
)
from embeddr.core.plugin_loader import get_lotus_registry
from embeddr_core.models.lotus import LotusKind

router = APIRouter()


class AuthSessionRequest(BaseModel):
    clear: bool = False


class InstanceProfile(BaseModel):
    name: Optional[str] = None
    logo_url: Optional[str] = None
    description: Optional[str] = None


class InstanceProfileUpdate(BaseModel):
    name: Optional[str] = None
    logo_url: Optional[str] = None
    description: Optional[str] = None
    confirm: bool = False


class ArtifactRegistryResponse(BaseModel):
    providers: List[str]
    resolvers: List[str]
    provider_resolvers: Dict[str, str]
    storage_backends: List[str]
    type_names: List[str]
    base_type_names: List[str]
    import_sources: List[str]
    import_instances: List[str]
    origins: List[str]
    sample_size: int
    total_artifacts: int
    sampled: bool


class ArtifactTypeCountsResponse(BaseModel):
    type_counts: Dict[str, int]
    base_type_counts: Dict[str, int]
    total_artifacts: int


def _load_instance_profile(session: Session) -> Dict[str, Any]:
    fallback = {
        "name": getattr(settings, "PROJECT_NAME", None) or "Embeddr",
        "logo_url": None,
        "description": None,
    }
    try:
        value = resolve_plugin_config(
            session=session,
            plugin_name="embeddr-core",
            scope="global",
            scope_id=None,
            config_id="embeddr-core.instance.profile",
        )
        if isinstance(value, dict):
            return {**fallback, **value}
    except Exception:
        pass
    return fallback


@router.post("/auth/session")
def set_auth_session(
    request: Request,
    response: Response,
    payload: AuthSessionRequest = Body(default=AuthSessionRequest()),
    auth_context=Depends(get_auth_context),
):
    if not check_auth_enabled():
        return {"ok": True, "enabled": False}

    if payload.clear:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True, "cleared": True}

    cookie_value = auth_context.raw_key
    secure = request.url.scheme == "https"
    samesite = "none" if secure else "lax"

    response.set_cookie(
        COOKIE_NAME,
        cookie_value,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )
    return {
        "ok": True,
        "cookie": COOKIE_NAME,
        "samesite": samesite,
        "secure": secure,
    }


def _get_backend_version() -> str:
    try:
        return pkg_version("embeddr-cli")
    except PackageNotFoundError:
        return "unknown"


def _get_db_revision(engine) -> Optional[str]:
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return context.get_current_revision()
    except Exception:
        return None


def _format_db_url(url: str) -> Dict[str, Any]:
    parsed = make_url(url)
    return {
        "driver": parsed.drivername,
        "dialect": parsed.drivername.split("+")[0],
        "username": parsed.username,
        "password": "***" if parsed.password else None,
        "host": parsed.host,
        "port": parsed.port,
        "database": parsed.database,
    }


def _get_db_health(engine) -> Dict[str, Any]:
    connected = False
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    try:
        start = time.perf_counter()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        connected = True
        latency_ms = (time.perf_counter() - start) * 1000
    except Exception as exc:
        error = str(exc)
    return {
        "connected": connected,
        "latency_ms": latency_ms,
        "error": error,
    }


def _count_sql(engine, sql: str, params: Optional[Dict[str, Any]] = None) -> int:
    with engine.connect() as connection:
        result = connection.execute(text(sql), params or {})
        return int(result.scalar_one())


def _get_latest_sqlite_backup() -> Optional[str]:
    url = make_url(settings.DATABASE_URL)
    if url.drivername != "sqlite" or not url.database:
        return None
    try:
        db_path = Path(url.database)
        backup_dir = db_path.parent / "backups"
        if not backup_dir.exists():
            return None
        backups = sorted(backup_dir.glob(f"{db_path.name}.*.bak"))
        return str(backups[-1]) if backups else None
    except Exception:
        return None


@router.get("/debug/clients")
def get_connected_clients(auth=Depends(require_permission("system:read"))):
    return {
        "clients": manager.get_connected_clients(),
        "sessions": manager.get_connected_client_info(),
    }


@router.post("/debug/message")
async def send_debug_message(
    client_id: str,
    message: Dict[str, Any],
    auth=Depends(require_permission("system:write")),
):
    await manager.send_to_client(client_id, message)
    return {"status": "sent"}


@router.get("/routes")
def get_routes(
    request: Request,
    auth=Depends(require_permission("system:read")),
) -> Dict[str, List[Dict[str, Any]]]:
    routes: List[Dict[str, Any]] = []

    def _collect(route_list, prefix: str = "") -> None:
        for route in route_list:
            if isinstance(route, Mount):
                mount_path = f"{prefix}{route.path}"
                routes.append({
                    "path": mount_path,
                    "methods": ["ALL"],
                    "name": route.name,
                    "tags": [],
                })
                if hasattr(route.app, "routes"):
                    _collect(route.app.routes, mount_path)
                continue

            if hasattr(route, "path"):
                routes.append({
                    "path": f"{prefix}{route.path}",
                    "methods": list(route.methods) if getattr(route, "methods", None) else ["ALL"],
                    "name": route.name,
                    "tags": getattr(route, "tags", []),
                })

    # Collect routes from the main app (includes routers + mounted subapps)
    _collect(request.app.routes)
    return {"routes": routes}


@router.get("/artifact-registry", response_model=ArtifactRegistryResponse)
def get_artifact_registry(
    sample_limit: int = 5000,
    auth=Depends(require_permission("system:read")),
):
    providers = list_providers()
    resolvers = list_resolvers()
    provider_resolvers = list_provider_resolvers()

    with Session(get_engine()) as session:
        total_artifacts = session.exec(select(func.count(Artifact.id))).one()
        storage_backends = session.exec(
            select(ArtifactBlob.storage_backend).distinct()
        ).all()

        query = select(
            Artifact.type_name,
            Artifact.base_type_name,
            Artifact.metadata_json,
        ).order_by(Artifact.created_at.desc())

        sampled = False
        if sample_limit and sample_limit > 0:
            query = query.limit(sample_limit)
            sampled = total_artifacts > sample_limit

        type_names: set[str] = set()
        base_type_names: set[str] = set()
        import_sources: set[str] = set()
        import_instances: set[str] = set()
        origins: set[str] = set()

        for row in session.exec(query):
            type_names.add(row.type_name)
            base_type_names.add(row.base_type_name)

            metadata = row.metadata_json or {}
            external = metadata.get("external")
            if isinstance(external, dict):
                source = external.get("source")
                if source:
                    import_sources.add(str(source))
                instance = external.get("instance")
                if instance:
                    import_instances.add(str(instance))

            origin = metadata.get("origin")
            if not origin:
                comfy_meta = metadata.get("comfy_meta") or {}
                if isinstance(comfy_meta, dict):
                    origin = comfy_meta.get("origin")
            if origin:
                origins.add(str(origin))

    return ArtifactRegistryResponse(
        providers=sorted(providers),
        resolvers=sorted(resolvers),
        provider_resolvers=provider_resolvers,
        storage_backends=sorted({b for b in storage_backends if b}),
        type_names=sorted(type_names),
        base_type_names=sorted(base_type_names),
        import_sources=sorted(import_sources),
        import_instances=sorted(import_instances),
        origins=sorted(origins),
        sample_size=min(sample_limit, total_artifacts)
        if sample_limit and sample_limit > 0
        else total_artifacts,
        total_artifacts=total_artifacts,
        sampled=sampled,
    )


@router.get("/artifact-type-counts", response_model=ArtifactTypeCountsResponse)
def get_artifact_type_counts(
    auth=Depends(require_permission("system:read")),
):
    with Session(get_engine()) as session:
        total = session.exec(select(func.count(Artifact.id))).one()

        type_rows = session.exec(
            select(Artifact.type_name, func.count(Artifact.id))
            .group_by(Artifact.type_name)
        ).all()
        base_rows = session.exec(
            select(Artifact.base_type_name, func.count(Artifact.id))
            .group_by(Artifact.base_type_name)
        ).all()

    type_counts = {name: count for name, count in type_rows if name}
    base_counts = {name: count for name, count in base_rows if name}

    return ArtifactTypeCountsResponse(
        type_counts=type_counts,
        base_type_counts=base_counts,
        total_artifacts=total,
    )


@router.get("/resources")
def get_resources(auth=Depends(require_permission("system:read"))):
    return {
        "resources": [r.model_dump() for r in resource_manager.list_resources()],
        "total_memory_bytes": resource_manager.get_total_memory_usage()
    }


@router.get("/automation/status")
def get_automation_status(auth=Depends(require_permission("system:read"))):
    with Session(get_engine()) as session:
        total = session.exec(
            select(func.count()).select_from(Automation)).one()
        active = session.exec(
            select(func.count()).select_from(Automation).where(
                Automation.is_active == True)
        ).one()
    return {"total": total, "active": active}


@router.get("/automation/list")
def list_automations(auth=Depends(require_permission("system:read"))) -> Dict[str, Any]:
    with Session(get_engine()) as session:
        automations = session.exec(select(Automation)).all()
    return {
        "items": [
            {
                "id": str(rule.id),
                "name": rule.name,
                "description": rule.description,
                "is_active": rule.is_active,
                "trigger_event": rule.trigger_event,
                "trigger_conditions": rule.trigger_conditions,
                "actions": rule.actions,
                "metadata_json": rule.metadata_json,
                "created_at": rule.created_at,
                "updated_at": rule.updated_at,
            }
            for rule in automations
        ],
        "total": len(automations),
    }


class AutomationUpsertRequest(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    is_active: bool = True
    trigger_event: str
    trigger_conditions: Dict[str, Any] = {}
    actions: List[Dict[str, Any]] = []
    metadata_json: Optional[Dict[str, Any]] = None


class IngestionPipelineConfigRequest(BaseModel):
    pipeline_id: Optional[str] = None


class IngestionDefaultsRequest(BaseModel):
    enabled_plugins: Optional[List[str]] = None


@router.post("/automation/upsert")
def upsert_automation(
    payload: AutomationUpsertRequest,
    auth=Depends(require_permission("system:write")),
) -> Dict[str, Any]:
    with Session(get_engine()) as session:
        automation: Optional[Automation] = None
        if payload.id:
            try:
                automation = session.get(Automation, UUID(payload.id))
            except Exception:
                automation = None

        if automation is None:
            automation = Automation(
                name=payload.name,
                description=payload.description,
                is_active=payload.is_active,
                trigger_event=payload.trigger_event,
                trigger_conditions=payload.trigger_conditions,
                actions=payload.actions,
                metadata_json=payload.metadata_json or {},
            )
            session.add(automation)
        else:
            automation.name = payload.name
            automation.description = payload.description
            automation.is_active = payload.is_active
            automation.trigger_event = payload.trigger_event
            automation.trigger_conditions = payload.trigger_conditions
            automation.actions = payload.actions
            if payload.metadata_json is not None:
                automation.metadata_json = payload.metadata_json

        session.commit()
        session.refresh(automation)

    return {
        "ok": True,
        "item": {
            "id": str(automation.id),
            "name": automation.name,
            "description": automation.description,
            "is_active": automation.is_active,
            "trigger_event": automation.trigger_event,
            "trigger_conditions": automation.trigger_conditions,
            "actions": automation.actions,
            "metadata_json": automation.metadata_json,
            "created_at": automation.created_at,
            "updated_at": automation.updated_at,
        },
    }


@router.delete("/automation/{automation_id}")
def delete_automation(
    automation_id: str,
    auth=Depends(require_permission("system:write")),
) -> Dict[str, Any]:
    with Session(get_engine()) as session:
        try:
            rule_id = UUID(automation_id)
            automation = session.get(Automation, rule_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid UUID format")

        if not automation:
            raise HTTPException(status_code=404, detail="Automation not found")

        session.delete(automation)
        session.commit()
    return {"ok": True, "id": automation_id}


@router.get("/ingestion/pipeline")
def get_ingestion_pipeline(
    auth=Depends(require_permission("system:read")),
) -> Dict[str, Any]:
    with Session(get_engine()) as session:
        cfg = resolve_plugin_config(
            session=session,
            plugin_name="embeddr-core",
            scope="global",
            config_id="embeddr-core.ingest.pipeline",
        )
    return {
        "pipeline_id": cfg.get("pipeline_id") or cfg.get("automation_id"),
        "raw": cfg,
    }


@router.post("/ingestion/pipeline")
def set_ingestion_pipeline(
    payload: IngestionPipelineConfigRequest,
    auth=Depends(require_permission("system:write")),
) -> Dict[str, Any]:
    with Session(get_engine()) as session:
        cfg = set_plugin_config(
            session=session,
            plugin_name="embeddr-core",
            scope="global",
            config_id="embeddr-core.ingest.pipeline",
            value={"pipeline_id": payload.pipeline_id},
        )
    return {"ok": True, "pipeline_id": cfg.get("pipeline_id")}


def _supports_artifact_input(cap) -> bool:
    data = cap.data or {}
    schema = ((data.get("input") or {}).get("schema")
              or {}) if isinstance(data, dict) else {}
    props = (schema.get("properties") or {}
             ) if isinstance(schema, dict) else {}
    return "artifact_id" in props or "artifact_ids" in props


def _is_backfill_action(cap) -> bool:
    title = str(cap.title or cap.id or "").lower()
    return "backfill" in title


def _pick_ingest_cap(plugin_name: str, caps: List[Any]):
    candidates = [
        cap
        for cap in caps
        if cap.plugin == plugin_name
        and cap.kind in {LotusKind.action, LotusKind.feature}
        and "ingest" in (cap.tags or [])
        and _supports_artifact_input(cap)
        and not _is_backfill_action(cap)
    ]

    if not candidates:
        return None

    def score(cap) -> int:
        title = str(cap.title or cap.id or "").lower()
        s = 0
        if "generate" in title:
            s += 3
        if "thumbnail" in title:
            s += 3
        if "embedding" in title:
            s += 2
        if "ingest" in title:
            s += 1
        return s

    candidates.sort(key=score, reverse=True)
    return candidates[0]


@router.post("/ingestion/defaults")
def apply_ingestion_defaults(
    payload: IngestionDefaultsRequest,
    auth=Depends(require_permission("system:write")),
) -> Dict[str, Any]:
    registry = get_lotus_registry()
    caps = registry.list(kind=LotusKind.action) + \
        registry.list(kind=LotusKind.feature)

    default_priorities = {
        "embeddr-thumbnailer": 20,
        "embeddr-embeddings": 10,
    }

    enabled_plugins = payload.enabled_plugins
    if enabled_plugins is None:
        enabled_plugins = list(default_priorities.keys())

    actions: List[Dict[str, Any]] = []
    for plugin_name in enabled_plugins:
        cap = _pick_ingest_cap(plugin_name, caps)
        if not cap:
            continue
        data = cap.data or {}
        job_type = data.get("job_type") or data.get("action") or cap.id
        action: Dict[str, Any] = {
            "plugin_name": cap.plugin,
            "job_type": job_type,
            "inputs": {"artifact_id": "${payload.id}"},
            "priority": default_priorities.get(plugin_name, 0),
        }
        if isinstance(data, dict):
            resource_class = data.get("resource_class")
            if resource_class:
                action["resource_class"] = resource_class
        actions.append(action)

    with Session(get_engine()) as session:
        automation = session.exec(
            select(Automation).where(
                Automation.name == "default.core_ingestion")
        ).first()

        if automation is None:
            automation = Automation(
                name="default.core_ingestion",
                description="Core ingestion defaults (thumbnails + embeddings).",
                is_active=bool(actions),
                trigger_event="artifact.created",
                trigger_conditions={},
                actions=actions,
                metadata_json={"source": "ingestion-defaults"},
            )
            session.add(automation)
        else:
            automation.description = "Core ingestion defaults (thumbnails + embeddings)."
            automation.is_active = bool(actions)
            automation.trigger_event = "artifact.created"
            automation.trigger_conditions = {}
            automation.actions = actions
            if automation.metadata_json is None:
                automation.metadata_json = {}
            automation.metadata_json["source"] = "ingestion-defaults"
            session.add(automation)

        session.commit()
        session.refresh(automation)

        pipeline_id = str(automation.id) if actions else None
        cfg = set_plugin_config(
            session=session,
            plugin_name="embeddr-core",
            scope="global",
            config_id="embeddr-core.ingest.pipeline",
            value={"pipeline_id": pipeline_id},
        )

    return {
        "ok": True,
        "automation_id": str(automation.id),
        "pipeline_id": cfg.get("pipeline_id"),
        "actions": actions,
    }


@router.get("/info")
def get_system_info(auth=Depends(require_permission("system:read"))) -> Dict[str, Any]:
    engine = get_engine()
    db_url = settings.DATABASE_URL
    db_provider = (settings.DATABASE_URL or "").split(":", 1)[0]
    db_meta = _format_db_url(db_url)
    db_health = _get_db_health(engine)
    db_revision = _get_db_revision(engine)
    supports_backup = db_meta.get("dialect") == "sqlite"

    try:
        artifacts = _count_sql(engine, "SELECT COUNT(*) FROM artifact")
        images = _count_sql(
            engine,
            """
            SELECT COUNT(*)
            FROM artifact
            WHERE base_type_name = :base
               OR type_name = :type
               OR type_name LIKE :type_like
            """,
            {"base": "image", "type": "image", "type_like": "image:%"},
        )
        libraries = _count_sql(
            engine,
            "SELECT COUNT(*) FROM artifact WHERE type_name = :type",
            {"type": "collection:directory"},
        )
        collections = _count_sql(
            engine,
            """
            SELECT COUNT(*)
            FROM artifact
            WHERE base_type_name = :base
               OR type_name LIKE :type_like
            """,
            {"base": "collection", "type_like": "collection:%"},
        )
    except Exception:
        images = 0
        libraries = 0
        artifacts = 0
        collections = 0

    with Session(get_engine()) as session:
        instance_profile = _load_instance_profile(session)
    return {
        "version": _get_backend_version(),
        "dev_mode": bool(getattr(settings, "DEV_MODE", False)),
        "instance": instance_profile,
        "db_version": db_revision,
        "db": {
            "provider": db_provider,
            "url": db_meta,
            **db_health,
            "supports_backup": supports_backup,
            "latest_backup": _get_latest_sqlite_backup(),
        },
        "stats": {
            "images": images,
            "libraries": libraries,
            "artifacts": artifacts,
            "collections": collections,
        },
    }


@router.get("/instance", response_model=InstanceProfile)
def get_instance_profile(
    auth=Depends(require_permission("system:read")),
    session: Session = Depends(get_session),
):
    profile = _load_instance_profile(session)
    return InstanceProfile(**profile)


@router.put("/instance", response_model=InstanceProfile)
def set_instance_profile(
    payload: InstanceProfileUpdate,
    auth=Depends(require_permission("system:write")),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")

    current = _load_instance_profile(session)
    update = {
        "name": payload.name,
        "logo_url": payload.logo_url,
        "description": payload.description,
    }
    next_value = {**current, **{k: v for k,
                                v in update.items() if v is not None}}
    value = set_plugin_config(
        session=session,
        plugin_name="embeddr-core",
        scope="global",
        scope_id=None,
        config_id="embeddr-core.instance.profile",
        value=next_value,
    )
    return InstanceProfile(**(value or next_value))


class BackupRequest(BaseModel):
    confirm: bool = False


@router.post("/db/backup")
def backup_db(
    req: BackupRequest,
    auth=Depends(require_permission("system:write")),
) -> Dict[str, Any]:
    if not req.confirm:
        raise HTTPException(400, "Backup requires confirm=true")

    path = backup_database()
    if not path:
        raise HTTPException(400, "Backup is only supported for SQLite")
    return {"status": "ok", "backup_path": str(path)}


@router.post("/resources/unload")
def unload_resource(
    resource_id: str,
    auth=Depends(require_permission("system:write")),
):
    """Request unloading of a specific resource."""
    resource_manager.request_unload(resource_id)
    return {"status": "ok"}


@router.post("/resources/unload_all")
def unload_all_resources(auth=Depends(require_permission("system:write"))):
    """Request unloading of all managed resources."""
    for r in resource_manager.list_resources():
        resource_manager.request_unload(r.id)
    return {"status": "ok"}


class CliCommandRequest(BaseModel):
    args: List[str]


class BlobDefaultsRequest(BaseModel):
    default_provider: Optional[str] = None
    default_resolver: Optional[str] = None


@router.post("/cli")
def run_cli_command(
    cmd: CliCommandRequest,
    auth=Depends(require_permission("system:write")),
):
    """
    Execute a CLI command. 
    Ideally, this calls the python module directly to avoid path issues.
    """

    # Construct command: python -m embeddr.cli [args]
    # We use sys.executable to ensure we use the same venv
    full_cmd = [sys.executable, "-m", "embeddr.cli"] + cmd.args
    print(f"Executing CLI command: {' '.join(full_cmd)}")

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=120  # Increased timeout for longer operations
        )

        # Log output to server console for debugging
        if result.stdout:
            print(f"CLI STDOUT:\n{result.stdout}")
        if result.stderr:
            print(f"CLI STDERR:\n{result.stderr}")

        return {
            "success": result.returncode == 0,
            "return_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command": " ".join(full_cmd)
        }
    except Exception as e:
        print(f"CLI EXECUTION ERROR: {e}")
        return {
            "success": False,
            "error": str(e),
            "command": " ".join(full_cmd)
        }


@router.get("/blob-registry")
def get_blob_registry(auth=Depends(require_permission("system:read"))) -> Dict[str, Any]:
    providers = sorted(list_providers().keys())
    resolvers = sorted(list_resolvers().keys())
    provider_resolvers = list_provider_resolvers()
    with Session(get_engine()) as session:
        defaults = resolve_plugin_config(
            session=session,
            plugin_name="embeddr-core",
            config_id="embeddr-core.blob.defaults",
        ) or {}
    return {
        "providers": providers,
        "resolvers": resolvers,
        "provider_resolvers": provider_resolvers,
        "default_provider": get_default_provider_name(),
        "default_resolver": get_default_resolver_name(),
        "config": defaults,
    }


@router.post("/blob-registry/defaults")
def set_blob_defaults(
    payload: BlobDefaultsRequest,
    auth=Depends(require_permission("system:write")),
) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}
    if payload.default_provider:
        set_default_provider(payload.default_provider)
        updates["default_provider"] = payload.default_provider
    if payload.default_resolver:
        set_default_resolver(payload.default_resolver)
        updates["default_resolver"] = payload.default_resolver

    if updates:
        with Session(get_engine()) as session:
            set_plugin_config(
                session=session,
                plugin_name="embeddr-core",
                config_id="embeddr-core.blob.defaults",
                scope="global",
                value=updates,
            )

    return {
        "ok": True,
        "default_provider": get_default_provider_name(),
        "default_resolver": get_default_resolver_name(),
    }
