from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

_engine_kwargs: dict = {"future": True}
if _settings.database_url.startswith("sqlite"):
    # Required so a single connection can be shared across threads
    # (NiceGUI/uvicorn workers + RQ workers share the file).
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(_settings.database_url, **_engine_kwargs)


# SQLite ignores ON DELETE CASCADE unless foreign keys are explicitly enabled
# per connection. Without this, cascade deletes silently leave orphans.
if _settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
