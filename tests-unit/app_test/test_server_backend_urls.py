"""Unit tests for the server-database URL classification and the file-path helpers.

Torch-free: these exercise app.database.db directly, so they run in any environment
(no GPU, no PostgreSQL) and cover the decisions the assets startup path makes purely
from the URL string.
"""

import pytest

from app.database import db as db_module


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///:memory:", "memory"),
        ("sqlite://", "memory"),
        ("sqlite:////abs/path.db", "file"),
        ("sqlite:///relative/path.db", "file"),
        ("postgresql://user:secret@localhost:5432/comfy", "server"),
        ("postgresql+psycopg://user:secret@localhost:5432/comfy", "server"),
        # A SQLite URL carrying a host is not the file form and is not accepted; neither
        # is a non-sqlite/non-postgres backend.
        ("sqlite://user:secret@host/x.db", None),
        ("sqlite:relative.db", None),
        ("mysql://user:secret@host/x", None),
        ("", None),
    ],
)
def test_classify_db_url(monkeypatch, url, expected):
    monkeypatch.setattr(db_module.args, "database_url", url)
    assert db_module.classify_db_url(url) == expected


def test_get_db_path_is_the_file_only_for_file_backed_sqlite(monkeypatch, tmp_path):
    path = str(tmp_path / "comfyui.db")

    monkeypatch.setattr(db_module.args, "database_url", f"sqlite:///{path}")
    assert db_module.get_db_path() == path

    # A server backend has no file to lock, copy, or back up.
    monkeypatch.setattr(db_module.args, "database_url", "postgresql://u:p@h:5432/d")
    assert db_module.get_db_path() is None

    # An in-memory database keeps upstream's literal ":memory:" path; the lock check
    # still finds no file and returns None.
    monkeypatch.setattr(db_module.args, "database_url", "sqlite:///:memory:")
    assert db_module.get_db_path() == ":memory:"
    assert db_module.lock_holder_db_path() is None


def test_lock_holder_db_path_is_none_for_a_server_backend(monkeypatch):
    monkeypatch.setattr(db_module.args, "database_url", "postgresql://u:p@h:5432/d")
    # No lock file exists for a server backend; the check must not raise and must not
    # manufacture a path.
    assert db_module.lock_holder_db_path() is None


@pytest.mark.parametrize(
    "url",
    ["sqlite:relative.db", "sqlite://user:secret@host/x.db", "mysql://u:p@h/x", ""],
)
def test_lock_holder_db_path_never_raises_on_an_unusable_url(monkeypatch, url):
    # The probe runs even when assets are off (main.warn_if_database_in_use), so a
    # malformed URL must not escape as an unhandled exception — it must return None.
    monkeypatch.setattr(db_module.args, "database_url", url)
    assert db_module.lock_holder_db_path() is None


def test_lock_holder_db_path_still_finds_a_held_sqlite_lock(monkeypatch, tmp_path):
    from filelock import FileLock

    path = str(tmp_path / "comfyui.db")
    monkeypatch.setattr(db_module.args, "database_url", f"sqlite:///{path}")
    holder = FileLock(path + ".lock")
    holder.acquire(timeout=0)
    try:
        assert db_module.lock_holder_db_path() == path
    finally:
        holder.release()
