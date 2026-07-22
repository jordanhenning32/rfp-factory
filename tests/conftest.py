"""Shared pytest fixtures for the RFP Factory test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def sample_company_profile_when_unconfigured(monkeypatch):
    """Use the tracked fictional profile when private runtime data is absent.

    The public repository intentionally excludes ``company_profile.json``.
    Unit tests must therefore be portable to a clean clone and must never
    depend on a developer's untracked company data.
    """
    from app.core import company_profile

    company_profile.get_company_profile.cache_clear()
    if not company_profile.COMPANY_PROFILE_PATH.exists():
        sample_path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "company_profile.example.json"
        )
        monkeypatch.setattr(company_profile, "COMPANY_PROFILE_PATH", sample_path)
    yield
    company_profile.get_company_profile.cache_clear()


@pytest.fixture()
def inmemory_db(monkeypatch):
    """Bind app.db.session to an in-memory SQLite engine for one test.

    Monkeypatches engine, SessionLocal, and session_scope so that every
    call to session_scope() or SessionLocal() inside the production code
    opens a session against the in-memory engine rather than data/sqlite.db.
    Restored automatically by monkeypatch at test teardown.
    """
    import app.db.session as db_session_mod

    # Import the model package so every ORM class registers on Base.metadata
    # BEFORE create_all runs. Without this, test files that only import
    # individual models (or none, before the test body runs) hit
    # "no such table" errors depending on pytest collection order.
    import app.models  # noqa: F401
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:", future=True)

    # Mirror the PRAGMA foreign_keys listener from app/db/session.py.
    @event.listens_for(engine, "connect")
    def _set_fk(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)

    InMemorySession = sessionmaker(bind=engine, autoflush=True, autocommit=False)

    # Patch the module-level symbols that production code imports directly.
    monkeypatch.setattr(db_session_mod, "engine", engine)
    monkeypatch.setattr(db_session_mod, "SessionLocal", InMemorySession)

    # Patch session_scope so it uses InMemorySession.
    from contextlib import contextmanager

    @contextmanager
    def _inmemory_session_scope():
        session = InMemorySession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(db_session_mod, "session_scope", _inmemory_session_scope)

    yield engine

    engine.dispose()
