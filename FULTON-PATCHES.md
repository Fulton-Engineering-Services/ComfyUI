# FULTON PATCHES

This fork (`Fulton-Engineering-Services/ComfyUI`) carries a small, reviewed patch set
that lets ComfyUI core's **assets system** run against a **PostgreSQL** database instead
of the hard-wired SQLite backend. It exists so the GX10 ComfyUI fleet (master + workers)
can share **one fleet-wide asset catalog** on a PostgreSQL server hosted on the Dell
host, with several instances reading and writing concurrently.

Upstream has no non-SQLite support and no plan for it; multi-instance sharing of one
database is blocked by design (SQLite `FileLock` + single-writer model). These patches
unblock exactly that, and are written to be **upstream-PR candidates**.

## Branch model

- Maintained branch: **`fulton/patches`**, based on the upstream tag the fleet pins
  (`COMFYUI_REF` in `agents/fulton-comfyui/images/runtime/Dockerfile`).
- Every core bump = `git rebase fulton/patches` onto the new upstream tag, then rebuild.
  The image build asserts the fork branch still contains `COMFYUI_REF`
  (`git merge-base --is-ancestor`), so a mis-rebased fork fails the build instead of
  silently shipping a different core.
- Current base: upstream **v0.40.0**, commit `3541246a9c997f4b8c32f9f88f3c985d02021c79`.

## Root cause → patch map

Every change is a **no-op for SQLite**: the SQLite code path, its tests, and its
behaviour are unchanged. Only the non-SQLite path is new.

| File / symbol | What upstream did | Patch |
| --- | --- | --- |
| `app/database/db.py:get_db_path()` | Raised `ValueError` for any non-`sqlite:///` URL | Returns the file path for file-backed SQLite, `None` for a server backend (no file to lock/copy/back up) |
| `app/database/db.py:init_db()` | Split only memory vs file SQLite | Adds a third `_init_server_db(db_url)` branch for non-SQLite URLs |
| `app/database/db.py:_init_server_db()` | Did not exist | New: two engines (read + write, no SQLite PRAGMAs / `BEGIN IMMEDIATE`), Alembic upgrade on the read engine, `get_alembic_config()`/`_upgrade_discards_the_catalog` reused, `Session`/`WriteSession` bound |
| `app/database/db.py:_init_server_db()` migration serialization | n/a | Postgres session **advisory lock** (`pg_advisory_lock`) held across migration inspection + upgrade, so N fleet instances starting at once do not race on `alembic_version` (the losers wait, then no-op). Runtime concurrency needs no lock: MVCC + `IntegrityError` handling |
| `app/database/db.py:lock_holder_db_path()` | `except ValueError` only | Also returns `None` when `get_db_path()` is `None` (server backend has no lock file) |
| `main.py:database_failure_message()` | Rejected any URL not starting `sqlite:///` as `unsupported_url` | Accepts `sqlite` and `postgresql` backends (`make_url(...).get_backend_name()`); for a server URL the location is rendered **without credentials** |
| `app/assets/database/models.py` + `alembic_db/versions/0007_record_content_split.py` | Partial unique index only via `sqlite_where=text("is_missing = 0")` | Adds `postgresql_where=text("is_missing = false")`. **Upstream bug:** SQLAlchemy silently ignores `sqlite_where` on other dialects, so on Postgres the index degraded to a plain unique index on `path`, forbidding the retired-content (many-rows-per-path) model |
| `alembic_db/versions/0005_allow_case_sensitive_tags.py` | Passed the already-rendered constraint name to `op.drop_constraint`/`op.create_check_constraint` | `op.f(...)` marks the name final; without it Alembic re-applies the `ck_%(table_name)s_%(constraint_name)s` convention and looks for `ck_tags_ck_tags_ck_tags_lowercase`. **Upstream bug:** the non-SQLite branch was never exercised |
| `alembic_db/versions/0009_created_at_from_mtime.py` | Used SQLite-only `julianday()` | Dialect-aware "within one second": `julianday(...)` on SQLite, `EXTRACT(EPOCH FROM (...))` on Postgres. **Upstream bug** |
| `requirements.txt` | No Postgres driver | Adds `psycopg[binary]==3.3.6` (psycopg3; `cp312` aarch64 wheel exists for the GB10 image) |
| `app/assets/services/ingest.py:_discard_unreferenced_content` docstring | Claimed the SAVEPOINT insert survives rollback (pysqlite-only) | Comment corrected: real SAVEPOINT on Postgres, so no leak there; cleanup kept as harmless best-effort for both dialects |

`app/assets/database/queries/records.py:is_live_path_conflict()` already had a
`postgres_names_the_index` branch (compares `orig.diag.constraint_name` to
`uq_asset_contents_path_live`) — verified it matches the name Postgres reports; **no
change**.

`app/assets/event_log.py` classifies `sqlite3.Error` codes; a Postgres
`OperationalError` falls through to a generic kind. **No change** (verify-only).

## Upstream-PR candidates

3b/3c/3d/3i (the general "let the assets system run on a server database" branch) are
generalizable. The `0005` (`op.f`), `0007` (`postgresql_where`) and `0009`
(`julianday`) fixes are genuine upstream bugs that affect any non-SQLite backend, and
are the cleanest standalone PRs.

## Build / test

The fork is consumed by `agents/fulton-comfyui` (image build clones this branch). To
verify the patches locally against a real Postgres, without the fleet:

```sh
docker run -d --rm --name pgtest -e POSTGRES_PASSWORD=t -p 55432:5432 postgres:17
python3 -m venv .venv && . .venv/bin/activate
pip install 'psycopg[binary]==3.3.6' 'sqlalchemy>=2.0' alembic blake3 filelock pytest pyyaml
python -c "
import app.database.db as d
d.args.database_url = 'postgresql+psycopg://postgres:t@127.0.0.1:55432/postgres'
d.init_db()
from app.assets.database.models import AssetContent
s = d.create_write_session(); s.add(AssetContent(path='/x', is_missing=False)); s.commit()
print('rows:', d.create_session().query(AssetContent).count())"
python -m pytest tests-unit/app_test/database_path_test.py \
  tests-unit/app_test/test_db_init_locking.py \
  tests-unit/app_test/test_migrations.py \
  tests-unit/app_test/test_migration_000{7,8,9}.py \
  tests-unit/app_test/test_migration_roundtrip.py \
  tests-unit/app_test/test_migration_warning.py \
  tests-unit/app_test/test_db_write_txn.py -q
docker rm -f pgtest
```

Expected: the Alembic chain runs to `0009_created_at_from_mtime`, `rows: 1`, and the
core SQLite/migration tests pass unchanged (SQLite path unregressed).
