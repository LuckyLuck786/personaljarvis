from jarvis.core import db
from jarvis.core.db import migrate


def test_migrations_apply_and_are_idempotent(tmp_path):
    db_path = tmp_path / "j.db"
    applied = migrate(db_path)
    assert "0001_core.sql" in applied
    assert migrate(db_path) == []  # second run: nothing new

    conn = db.connect(db_path)
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"bus_messages", "bus_cursors", "bus_dead", "audit_log",
            "system_state", "node_status"} <= tables


def test_failed_migration_rolls_back(tmp_path):
    db_path = tmp_path / "j.db"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_bad.sql").write_text(
        "CREATE TABLE ok_table (id INTEGER);\nTHIS IS NOT SQL;"
    )
    import pytest

    with pytest.raises(Exception):
        migrate(db_path, migrations_dir=migrations)

    conn = db.connect(db_path)
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
    conn.close()
    assert "ok_table" not in tables  # atomic: partial script rolled back
    assert versions == []
