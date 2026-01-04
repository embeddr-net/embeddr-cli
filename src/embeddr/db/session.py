from functools import lru_cache
from pathlib import Path
import shutil
import time
import logging

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine.url import make_url
from sqlmodel import Session, create_engine

from embeddr.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache()
def get_engine():
    connect_args = (
        {"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}
    )
    return create_engine(settings.DATABASE_URL, connect_args=connect_args)


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
    ini_path = Path(__file__).parent / "alembic.ini"
    alembic_cfg = Config(str(ini_path))

    engine = get_engine()
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    # Check if we need to stamp
    if "alembic_version" not in tables and "collection" in tables:
        logger.info("Existing database detected. Stamping with initial migration.")
        command.stamp(alembic_cfg, "head")
        return

    # Check for pending migrations
    script = ScriptDirectory.from_config(alembic_cfg)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_rev = context.get_current_revision()
        head_rev = script.get_current_head()

    if current_rev != head_rev:
        logger.info(
            f"Pending migrations detected (Current: {current_rev}, Head: {head_rev}). Backing up..."
        )
        backup_database()
        logger.info("Applying migrations...")
        command.upgrade(alembic_cfg, "head")
        logger.info("Migrations applied successfully.")
    else:
        logger.debug("Database is up to date.")


def create_db_and_tables():
    run_migrations()


def get_session():
    with Session(get_engine()) as session:
        yield session
