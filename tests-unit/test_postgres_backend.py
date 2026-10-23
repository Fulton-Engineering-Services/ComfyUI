"""Integration tests for the assets system against a real PostgreSQL server.

These run only when a PostgreSQL DSN is provided, via either:

  * ``ASSETS_TEST_POSTGRES_URL`` (dedicated), or
  * ``--db-url`` / ``ASSETS_TEST_DB_URL`` when the value is a postgresql:// URL.

Locally, start a throwaway server and point the tests at it:

    docker run -d --rm --name pgtest -e POSTGRES_PASSWORD=t -p 55432:5432 postgres:17
    ASSETS_TEST_POSTGRES_URL=postgresql+psycopg://postgres:t@127.0.0.1:55432/postgres \\
        python -m pytest tests-unit/test_postgres_backend.py -q
    docker rm -f pgtest

They are skipped (not failed) when no DSN is configured or the psycopg driver is absent,
so the default SQLite-only test run is unaffected. Each test starts from a clean schema:
the integration database is treated as disposable, never a shared catalog.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# This file sits directly in tests-unit/, so the repo root is one level up.
REPO_ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("sqlalchemy")
psycopg = pytest.importorskip("psycopg", reason="postgresql driver (psycopg) not installed")


def _configured_url():
    url = os.environ.get("ASSETS_TEST_POSTGRES_URL") or os.environ.get("ASSETS_TEST_DB_URL")
    if url and url.startswith("postgresql"):
        return url
    return None


pytestmark = pytest.mark.skipif(
    _configured_url() is None, reason="set ASSETS_TEST_POSTGRES_URL to a postgresql:// DSN"
)


def _reset_schema(url):
    """Drop and recreate the public schema so each test starts clean."""
    # psycopg accepts the postgresql:// DSN; strip a +psycopg SQLAlchemy suffix if present.
    dsn = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


@pytest.fixture
def postgres_db(monkeypatch):
    """Fresh schema + module wired to the configured server, sessions reset after."""
    import app.database.db as db_module

    url = _configured_url()
    _reset_schema(url)
    monkeypatch.setattr(db_module.args, "database_url", url)
    monkeypatch.setattr(db_module, "Session", None)
    monkeypatch.setattr(db_module, "WriteSession", None)
    yield db_module
    monkeypatch.setattr(db_module, "Session", None)
    monkeypatch.setattr(db_module, "WriteSession", None)


def test_migrations_reach_head_and_roundtrip_a_row(postgres_db):
    from app.assets.database.models import AssetContent

    postgres_db.init_db()

    session = postgres_db.create_write_session()
    try:
        session.add(AssetContent(path="/x", is_missing=False))
        session.commit()
    finally:
        session.close()

    read = postgres_db.create_session()
    try:
        assert read.query(AssetContent).count() == 1
    finally:
        read.close()


def test_partial_unique_index_keeps_its_predicate_on_postgres(postgres_db):
    import sqlalchemy as sa

    postgres_db.init_db()
    session = postgres_db.create_session()
    try:
        indexdef = session.execute(
            sa.text("select indexdef from pg_indexes where indexname='uq_asset_contents_path_live'")
        ).scalar_one()
    finally:
        session.close()
    # SQLAlchemy drops sqlite_where on other dialects; postgresql_where must keep the
    # predicate, or the index would forbid retired-content rows sharing a path.
    assert "WHERE" in indexdef
    assert "is_missing" in indexdef


def test_one_live_row_per_path_but_retired_rows_allowed(postgres_db):
    from app.assets.database.models import AssetContent
    from app.assets.database.queries.records import create_content_reporting_insert

    postgres_db.init_db()
    session = postgres_db.create_write_session()
    try:
        _, first_created = create_content_reporting_insert(session, "/dup", size_bytes=1)
        session.commit()
        _, second_created = create_content_reporting_insert(session, "/dup", size_bytes=2)
        session.commit()
        assert first_created is True
        assert second_created is False  # the live-uniqueness index returned the winner

        # A retired (is_missing=True) row on the same path is allowed alongside the live one.
        session.add(AssetContent(path="/dup", is_missing=True))
        session.commit()

        live = (
            session.query(AssetContent)
            .filter(AssetContent.path == "/dup", AssetContent.is_missing == False)  # noqa: E712
            .count()
        )
        all_rows = session.query(AssetContent).filter(AssetContent.path == "/dup").count()
    finally:
        session.close()
    assert live == 1
    assert all_rows == 2


def test_concurrent_instances_migrate_and_write_without_deadlock(postgres_db):
    """Several processes starting at once must not race on alembic_version.

    Reproduces the fleet condition (master + workers pointing at one database from a
    cold schema): the failures without the migration advisory lock were concurrent
    CREATE TABLE alembic_version / UniqueViolation.
    """
    import app.database.db as db_module

    url = _configured_url()
    _reset_schema(url)

    worker = "\n".join(
        [
            "import sys",
            "import app.database.db as d",
            f"d.args.database_url = {url!r}",
            "d.init_db()",
            "from app.assets.database.queries.records import create_content_reporting_insert",
            "w = int(sys.argv[1])",
            "s = d.create_write_session()",
            "for i in range(20):",
            "    create_content_reporting_insert(s, f'/w{w}/i{i}')",
            "    s.commit()",
            "s.close()",
            "print('ok')",
        ]
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(w)],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for w in range(4)
    ]
    outs = [(p.wait(timeout=180), p.communicate()) for p in procs]
    for code, (out, err) in outs:
        assert code == 0, err

    # Bind this process's sessions to the (now migrated) database to count rows.
    db_module.init_db()
    session = db_module.create_session()
    try:
        from app.assets.database.models import AssetContent

        assert session.query(AssetContent).count() == 80
    finally:
        session.close()
