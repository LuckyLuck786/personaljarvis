from jarvis.core import db
from jarvis.core.audit import AuditLog


def test_chain_verifies(cfg):
    audit = AuditLog(cfg.db_path)
    audit.record("system", "hub.start")
    audit.record("operator", "control.pause", {"reason": "x"})
    audit.record("jarvis", "tool.web_fetch", {"url": "https://example.com"}, "ok")
    v = audit.verify()
    assert v == {"ok": True, "entries": 3, "first_bad_id": None}


def test_tampering_is_detected(cfg):
    audit = AuditLog(cfg.db_path)
    audit.record("system", "a")
    tampered_id = audit.record("jarvis", "tool.shell_exec", {"cmd": "rm -rf /"}, "denied")
    audit.record("system", "b")

    # attacker rewrites history to hide the denied command
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE audit_log SET outcome='ok' WHERE id=?", (tampered_id,))
    conn.close()

    v = audit.verify()
    assert v["ok"] is False
    assert v["first_bad_id"] == tampered_id


def test_deleting_a_row_is_detected(cfg):
    audit = AuditLog(cfg.db_path)
    audit.record("system", "a")
    victim = audit.record("system", "b")
    audit.record("system", "c")

    conn = db.connect(cfg.db_path)
    conn.execute("DELETE FROM audit_log WHERE id=?", (victim,))
    conn.close()

    assert audit.verify()["ok"] is False
