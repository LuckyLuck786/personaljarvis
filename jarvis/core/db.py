"""SQLite access + migrations.

SQLite in WAL mode is the structured store for everything on the hub
(events, tasks, bus, audit, entities). Deliberate choice: disk-backed,
zero-server, well within the 6 GB RAM ceiling. Connections are short-lived
and cheap; WAL allows concurrent readers with a single writer.

Migrations are plain numbered .sql files in jarvis/migrations/, applied in
order and recorded in schema_migrations. Forward-only by design.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from jarvis.core.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    # Cap SQLite's page cache: ~8 MB instead of the default. RAM frugality
    # beats marginal read speed on the 6 GB hub.
    conn.execute("PRAGMA cache_size=-8000")
    return conn


def migrate(db_path: str | Path, migrations_dir: Path | None = None) -> list[str]:
    """Apply pending migrations in filename order. Returns those applied.

    executescript() implicitly commits any open transaction, so the
    BEGIN/COMMIT pair is embedded in the script itself — the migration body
    and its schema_migrations row land atomically or not at all.
    """
    migrations_dir = migrations_dir or MIGRATIONS_DIR
    conn = connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at REAL NOT NULL)"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        applied = []
        for sql_file in sorted(migrations_dir.glob("*.sql")):
            version = sql_file.name
            if version in done:
                continue
            if not _VERSION_RE.match(version):
                raise ValueError(f"unsafe migration filename: {version!r}")
            script = (
                "BEGIN;\n"
                f"{sql_file.read_text()}\n;\n"
                "INSERT INTO schema_migrations (version, applied_at)"
                f" VALUES ('{version}', {time.time()});\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            applied.append(version)
            log.info("migration_applied", version=version)
        return applied
    finally:
        conn.close()
