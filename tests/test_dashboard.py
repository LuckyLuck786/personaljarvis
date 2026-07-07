"""Dashboard: shell HTML loads unauthenticated (no data in it), every data
endpoint requires the key, and the kill switch keeps the dashboard reachable
so the operator can always resume."""


def test_login_shell_loads_without_key(client):
    # the client fixture sends a key by default; strip it for these
    r = client.get("/", headers={"x-api-key": ""})
    assert r.status_code == 200
    assert "JARVIS" in r.text
    assert "sessionStorage" in r.text  # key entered client-side, never in URL


def test_dashboard_shell_loads_without_key(client):
    r = client.get("/dashboard", headers={"x-api-key": ""})
    assert r.status_code == 200
    assert "Model routing" in r.text


def test_data_endpoints_require_key(client):
    for path in ("/dashboard/api/overview", "/dashboard/api/tasks",
                 "/dashboard/api/timeline", "/dashboard/api/logs"):
        assert client.get(path, headers={"x-api-key": ""}).status_code == 401


def test_overview_shape(client):
    o = client.get("/dashboard/api/overview").json()
    assert "killswitch" in o and "nodes" in o and "tier_usage" in o
    assert "open_tasks" in o and "memory_docs" in o
    assert isinstance(o["tools"], list) and o["tools"]  # tools discovered


def test_search_endpoint(client):
    client.post("/memory/ingest", json={"text": "dashboard test fact about otters"})
    r = client.get("/dashboard/api/search", params={"q": "otters"})
    assert any("otters" in x["text"] for x in r.json()["results"])


def test_tasks_endpoint(client):
    # create a task through the tool path so the board has something
    import json

    from fastapi.testclient import TestClient

    from jarvis.core import db

    conn = db.connect(client.app.state.cfg.db_path)
    conn.execute("INSERT INTO tasks (title, status, created_ts) VALUES"
                 " ('demo board task', 'open', 0)")
    conn.close()
    r = client.get("/dashboard/api/tasks")
    assert any(t["title"] == "demo board task" for t in r.json()["tasks"])


def test_dashboard_reachable_while_paused(client):
    client.post("/control/pause", json={"reason": "test"})
    # shell + read-only data both survive the pause so the operator can resume
    assert client.get("/dashboard", headers={"x-api-key": ""}).status_code == 200
    assert client.get("/dashboard/api/overview").status_code == 200
    assert client.get("/dashboard/api/overview").json()["killswitch"]["state"] == "paused"
    # but a normal mutating endpoint is still blocked
    assert client.post("/bus/publish", json={"topic": "t", "payload": {}}).status_code == 503
    client.post("/control/resume")


def test_root_not_globally_exempt_from_data(client):
    """Sanity: exempting '/' from the kill switch must not exempt other paths."""
    client.post("/control/pause", json={"reason": "test"})
    assert client.post("/chat", json={"text": "hi"}).status_code == 503
    client.post("/control/resume")
