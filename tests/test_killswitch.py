def test_pause_blocks_non_control_endpoints(client):
    r = client.post("/control/pause", json={"reason": "testing"})
    assert r.status_code == 200
    assert r.json()["state"] == "paused"
    assert r.json()["reason"] == "testing"

    # normal endpoints are refused while paused...
    assert client.post("/bus/publish", json={"topic": "t", "payload": {}}).status_code == 503
    # ...but health and control stay reachable so the operator can recover
    assert client.get("/health").status_code == 200
    assert client.get("/control/state").status_code == 200

    r = client.post("/control/resume")
    assert r.json()["state"] == "active"
    assert client.post("/bus/publish", json={"topic": "t", "payload": {}}).status_code == 200


def test_pause_resume_are_audited(client):
    client.post("/control/pause", json={"reason": "audit test"})
    client.post("/control/resume")
    entries = client.get("/audit/tail").json()["entries"]
    actions = [e["action"] for e in entries]
    assert "control.pause" in actions
    assert "control.resume" in actions


def test_killswitch_survives_restart(cfg):
    from jarvis.core.killswitch import KillSwitch

    KillSwitch(cfg.db_path).pause("persist test")
    # fresh instance == fresh process; state comes from SQLite
    assert KillSwitch(cfg.db_path).is_paused()
