import tarfile

import pytest

from jarvis.core import db
from jarvis.hub.backup import create_backup, restore_backup, verify_backup
from jarvis.hub.doctor import FAIL, OK, run_doctor


# -- backup / restore -----------------------------------------------------------

def _seed(cfg, marker="BACKUP-MARKER"):
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO memory_docs (kind, source, content_enc, ts)"
                 " VALUES ('note','test',?,0)", (marker,))
    conn.close()


def test_backup_creates_verifiable_archive(cfg):
    _seed(cfg)
    archive = create_backup(cfg.data_dir)
    assert archive.exists()
    check = verify_backup(archive)
    assert check["ok"] is True
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "jarvis.db" in names and "manifest.json" in names


def test_restore_roundtrip(cfg):
    _seed(cfg, "UNIQUE-CONTENT-42")
    archive = create_backup(cfg.data_dir)

    # wipe the row, then restore
    conn = db.connect(cfg.db_path)
    conn.execute("DELETE FROM memory_docs")
    conn.close()

    restore_backup(archive, cfg.data_dir, force=True)
    conn = db.connect(cfg.db_path)
    rows = conn.execute("SELECT content_enc FROM memory_docs").fetchall()
    conn.close()
    assert any(r["content_enc"] == "UNIQUE-CONTENT-42" for r in rows)


def test_restore_refuses_to_clobber_without_force(cfg):
    _seed(cfg)
    archive = create_backup(cfg.data_dir)
    with pytest.raises(FileExistsError):
        restore_backup(archive, cfg.data_dir, force=False)


def test_restore_rejects_corrupted_archive(cfg, tmp_path):
    _seed(cfg)
    archive = create_backup(cfg.data_dir)
    # corrupt: append junk so the db checksum won't match
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(bytes(data))
    with pytest.raises(Exception):
        # either tar read error or checksum failure — both must refuse
        restore_backup(bad, cfg.data_dir, force=True)


def test_restore_moves_existing_data_aside(cfg):
    _seed(cfg, "ORIGINAL")
    archive = create_backup(cfg.data_dir)
    restore_backup(archive, cfg.data_dir, force=True)
    # a .pre-restore-* copy of the old db should exist
    backups = list(cfg.data_dir.glob("jarvis.db.pre-restore-*"))
    assert backups, "existing data was not preserved before restore"


# -- doctor ---------------------------------------------------------------------

def test_doctor_healthy_config_passes(cfg):
    checks, healthy = run_doctor(cfg)
    names = {c.name: c.status for c in checks}
    assert names["API key set"] == OK
    assert "Master key valid (encryption works)" in names
    assert names["Master key valid (encryption works)"] == OK
    # no ollama nodes configured in the test cfg → that's a FAIL, so overall unhealthy,
    # but the crypto/db checks must still pass individually
    assert any(c.name.startswith("Database") and c.status == OK for c in checks)


def test_doctor_flags_missing_api_key(tmp_path):
    from jarvis.core.config import Config, Secrets
    from jarvis.core.db import migrate
    from tests.conftest import TEST_MASTER_KEY

    cfg = Config(data_dir=tmp_path,
                 secrets=Secrets(_env_file=None, jarvis_api_key="",
                                 jarvis_master_key=TEST_MASTER_KEY))
    migrate(cfg.db_path)
    checks, healthy = run_doctor(cfg)
    assert healthy is False
    assert any(c.name == "API key set" and c.status == FAIL for c in checks)


def test_doctor_flags_invalid_master_key(tmp_path):
    from jarvis.core.config import Config, Secrets
    from jarvis.core.db import migrate

    cfg = Config(data_dir=tmp_path,
                 secrets=Secrets(_env_file=None, jarvis_api_key="k",
                                 jarvis_master_key="not-valid"))
    migrate(cfg.db_path)
    checks, healthy = run_doctor(cfg)
    assert healthy is False
    assert any("Master key" in c.name and c.status == FAIL for c in checks)


def test_doctor_detects_broken_audit_chain(cfg):
    from jarvis.core.audit import AuditLog

    audit = AuditLog(cfg.db_path)
    audit.record("system", "a")
    bad = audit.record("system", "b")
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE audit_log SET outcome='tampered' WHERE id=?", (bad,))
    conn.close()

    checks, _ = run_doctor(cfg)
    assert any(c.name == "Audit chain integrity" and c.status == FAIL for c in checks)
