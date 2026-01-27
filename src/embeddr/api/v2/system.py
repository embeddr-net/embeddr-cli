from fastapi import APIRouter, Request, HTTPException
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
from embeddr.db.session import get_engine, backup_database
from embeddr_core.models.automation import Automation
from embeddr_core.models.artifact import Artifact
from embeddr.core.config import settings

router = APIRouter()


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
def get_connected_clients():
    return {"clients": manager.get_connected_clients()}


@router.post("/debug/message")
async def send_debug_message(client_id: str, message: Dict[str, Any]):
    await manager.send_to_client(client_id, message)
    return {"status": "sent"}


@router.get("/routes")
def get_routes(request: Request) -> Dict[str, List[Dict[str, Any]]]:
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


@router.get("/resources")
def get_resources():
    return {
        "resources": [r.model_dump() for r in resource_manager.list_resources()],
        "total_memory_bytes": resource_manager.get_total_memory_usage()
    }


@router.get("/automation/status")
def get_automation_status():
    with Session(get_engine()) as session:
        total = session.exec(
            select(func.count()).select_from(Automation)).one()
        active = session.exec(
            select(func.count()).select_from(Automation).where(
                Automation.is_active == True)
        ).one()
    return {"total": total, "active": active}


@router.get("/automation/list")
def list_automations() -> Dict[str, Any]:
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


@router.post("/automation/upsert")
def upsert_automation(payload: AutomationUpsertRequest) -> Dict[str, Any]:
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
def delete_automation(automation_id: str) -> Dict[str, Any]:
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
def get_ingestion_pipeline() -> Dict[str, Any]:
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
def set_ingestion_pipeline(payload: IngestionPipelineConfigRequest) -> Dict[str, Any]:
    with Session(get_engine()) as session:
        cfg = set_plugin_config(
            session=session,
            plugin_name="embeddr-core",
            scope="global",
            config_id="embeddr-core.ingest.pipeline",
            value={"pipeline_id": payload.pipeline_id},
        )
    return {"ok": True, "pipeline_id": cfg.get("pipeline_id")}


@router.get("/info")
def get_system_info() -> Dict[str, Any]:
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

    return {
        "version": _get_backend_version(),
        "dev_mode": bool(getattr(settings, "DEV_MODE", False)),
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


class BackupRequest(BaseModel):
    confirm: bool = False


@router.post("/db/backup")
def backup_db(req: BackupRequest) -> Dict[str, Any]:
    if not req.confirm:
        raise HTTPException(400, "Backup requires confirm=true")

    path = backup_database()
    if not path:
        raise HTTPException(400, "Backup is only supported for SQLite")
    return {"status": "ok", "backup_path": str(path)}


@router.post("/resources/unload")
def unload_resource(resource_id: str):
    """Request unloading of a specific resource."""
    resource_manager.request_unload(resource_id)
    return {"status": "ok"}


@router.post("/resources/unload_all")
def unload_all_resources():
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
def run_cli_command(cmd: CliCommandRequest):
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
def get_blob_registry() -> Dict[str, Any]:
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
def set_blob_defaults(payload: BlobDefaultsRequest) -> Dict[str, Any]:
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
