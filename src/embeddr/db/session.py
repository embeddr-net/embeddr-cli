from functools import lru_cache
from pathlib import Path
import os
import shutil
import time
import logging
import typing

try:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.runtime.migration import MigrationContext
except ImportError:
    # Alembic might not be available in all contexts (e.g. SDK inspection)
    command = None
    Config = None
    ScriptDirectory = None
    MigrationContext = None

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine.url import make_url
from sqlmodel import Session, SQLModel

from embeddr.core.config import settings
from embeddr.db.adapters import get_adapter

logger = logging.getLogger(__name__)


@lru_cache()
def get_engine():
    provider = os.environ.get("EMBEDDR_DB_PROVIDER")
    adapter = get_adapter(settings.DATABASE_URL, provider=provider)
    return adapter.create_engine(settings.DATABASE_URL)


def get_engine_isolated():
    """
    Returns a new engine instance for isolated operations (like background workers).
    This avoids sharing the connection pool with the main API loop if desired,
    though simpler logic effectively uses get_engine() often.
    For SQLite with WAL, sharing the engine (and pool) is actually preferred.
    So we align this to return get_engine().
    """
    return get_engine()


def backup_database():
    url = make_url(settings.DATABASE_URL)
    if url.drivername == "sqlite":
        db_path = Path(url.database)
        if db_path.exists():
            backup_dir = db_path.parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            timestamp = int(time.time())
            backup_path = backup_dir / f"{db_path.name}.{timestamp}.bak"
            shutil.copy2(db_path, backup_path)
            logger.info(f"Database backed up to {backup_path}")
            return backup_path
    return None


def run_migrations():
    if Config is None:
        logger.warning("Alembic not installed, skipping migrations")
        return

    ini_path = Path(__file__).parent / "alembic.ini"
    alembic_cfg = Config(str(ini_path))

    engine = get_engine()
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    is_fresh_db = len(tables) == 0

    # Check if we need to stamp an existing DB that has tables but no alembic_version
    if "alembic_version" not in tables and tables:
        expected_tables = set(SQLModel.metadata.tables.keys())
        missing_tables = sorted(expected_tables - set(tables))
        if missing_tables:
            logger.error(
                "Existing database is missing tables required by current schema: %s",
                ", ".join(missing_tables),
            )
            raise RuntimeError(
                "Database schema is incomplete. Consider re-initializing the workspace or resetting the database."
            )
        logger.info(
            "Existing database detected without migration history. Stamping head.")
        command.stamp(alembic_cfg, "head")
        return

    # Check for pending migrations
    script = ScriptDirectory.from_config(alembic_cfg)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_rev = context.get_current_revision()
        head_rev = script.get_current_head()

    if current_rev:
        try:
            script.get_revision(current_rev)
        except Exception:
            logger.warning(
                "Current revision %s is missing from migration scripts.",
                current_rev,
            )
            migrations_mode = os.environ.get(
                "EMBEDDR_MIGRATIONS_MODE", "apply").lower()
            if migrations_mode in {"verify", "check", "strict"}:
                raise RuntimeError(
                    "Database migration history is missing or corrupted. "
                    f"Current: {current_rev or 'none'}, Head: {head_rev}."
                )
            logger.info("Stamping head to reconcile migration history.")
            backup_database()
            command.stamp(alembic_cfg, "head")
            return

    migrations_mode = os.environ.get(
        "EMBEDDR_MIGRATIONS_MODE", "apply").lower()

    if current_rev != head_rev:
        if migrations_mode in {"verify", "check", "strict"}:
            raise RuntimeError(
                "Database schema is out of date. "
                f"Current: {current_rev or 'none'}, Head: {head_rev}. "
                "Run 'embeddr db upgrade' (or allow auto-migrate) to continue."
            )
        if is_fresh_db and current_rev is None:
            logger.info("Initializing database schema...")
        else:
            logger.info(
                f"Pending migrations detected (Current: {current_rev}, Head: {head_rev}). Backing up..."
            )
            backup_database()
        logger.info("Applying migrations...")
        try:
            command.upgrade(alembic_cfg, "head")
            logger.info("Migrations applied successfully.")
        except OperationalError as exc:
            message = str(exc).lower()
            if "already exists" in message and current_rev is None:
                expected_tables = set(SQLModel.metadata.tables.keys())
                inspector = inspect(engine)
                existing_tables = set(inspector.get_table_names())
                missing_tables = sorted(expected_tables - existing_tables)
                if missing_tables:
                    logger.error(
                        "Migration encountered existing tables but schema is incomplete. Missing: %s",
                        ", ".join(missing_tables),
                    )
                    raise RuntimeError(
                        "Database schema is incomplete. Consider resetting the database in this workspace."
                    )
                logger.warning(
                    "Detected existing tables during initial migration. Stamping head to sync state."
                )
                command.stamp(alembic_cfg, "head")
                return
            raise
        except Exception:
            logger.exception("Migration failed unexpectedly.")
            raise
    else:
        logger.debug("Database is up to date.")


def create_db_and_tables():
    run_migrations()


def get_session():
    with Session(get_engine()) as session:
        yield session
