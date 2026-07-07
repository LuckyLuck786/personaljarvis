import time

import pytest

from jarvis.capture.base import Redactor
from jarvis.capture.notes import NotesCollector
from jarvis.capture.shell import ShellHistoryCollector, parse_history_lines

REDACT_PATTERNS = [
    r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
    r"\b\d{13,19}\b",
]


def make_redactor():
    return Redactor(REDACT_PATTERNS)


def test_redaction_scrubs_secrets_and_cards():
    r = make_redactor()
    assert "hunter2" not in r.redact("my password=hunter2 ok")
    assert "sk-abc123" not in r.redact("API_KEY: sk-abc123")
    assert "4111111111111111" not in r.redact("card 4111111111111111 exp 12/28")
    assert r.redact("nothing sensitive here") == "nothing sensitive here"


def test_zsh_extended_history_parsing():
    lines = [
        ": 1700000000:0;git status",
        ": 1700000005:2;echo 'hello world'",
        "plain-format-command --flag",
    ]
    parsed = parse_history_lines(lines)
    assert parsed[0] == (1700000000.0, "git status")
    assert parsed[1][1] == "echo 'hello world'"
    assert parsed[2] == (None, "plain-format-command --flag")


async def test_notes_collector_captures_new_and_changed(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    collector = NotesCollector(
        {"watch_dirs": [str(notes_dir)]}, make_redactor(), tmp_path / "state"
    )

    (notes_dir / "ideas.md").write_text("Resume: try the Charter font")
    events = await collector.poll()
    assert len(events) == 1
    assert "Charter font" in events[0].content
    assert events[0].kind == "note"

    # unchanged → nothing
    assert await collector.poll() == []

    # modified → captured again
    (notes_dir / "ideas.md").write_text("Resume: Charter confirmed. password=oops")
    events = await collector.poll()
    assert len(events) == 1
    assert "oops" not in events[0].content  # redacted at source


async def test_shell_collector_baselines_then_tails(tmp_path):
    hist = tmp_path / ".zsh_history"
    hist.write_text(": 1700000000:0;old command from years ago\n")
    collector = ShellHistoryCollector(
        {"files": [str(hist)]}, make_redactor(), tmp_path / "state"
    )
    # first poll: baseline only — must NOT ingest pre-existing history
    assert await collector.poll() == []

    with open(hist, "a") as f:
        f.write(": 1700000100:0;git push origin main\n")
        f.write(": 1700000101:0;export API_KEY=supersecret123\n")
    events = await collector.poll()
    assert len(events) == 1
    assert "git push origin main" in events[0].content
    assert "supersecret123" not in events[0].content
    assert "old command from years ago" not in events[0].content

    # nothing new → nothing emitted
    assert await collector.poll() == []


async def test_clipboard_collector_dedupes(tmp_path, monkeypatch):
    from jarvis.capture import clipboard as clip

    collector = clip.ClipboardCollector({}, make_redactor(), tmp_path / "state")

    async def fake_read():
        return "copied text with password=abc"

    monkeypatch.setattr(clip, "read_clipboard", fake_read)
    events = await collector.poll()
    assert len(events) == 1
    assert "password=abc" not in events[0].content
    assert await collector.poll() == []  # same clipboard content → deduped


async def test_filesystem_collector_baselines_then_detects(tmp_path):
    from jarvis.capture.filesystem import FilesystemCollector

    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / "main.py").write_text("print('v1')")
    (proj / ".git" / "junk").write_text("x")

    collector = FilesystemCollector(
        {"watch_dirs": [str(proj)], "ignore": ["**/.git/**"]},
        make_redactor(), tmp_path / "state",
    )
    assert await collector.poll() == []  # baseline pass, no flood

    time.sleep(0.01)
    (proj / "main.py").write_text("print('v2')  # changed")
    (proj / ".git" / "junk").write_text("y")  # ignored path
    events = await collector.poll()
    assert len(events) == 1
    assert events[0].meta["path"].endswith("main.py")
    assert "print('v2')" in events[0].content


def test_capture_endpoint_queues_to_bus_and_reaches_memory(client):
    r = client.post("/capture/event", json={
        "source": "notes", "kind": "note",
        "content": "Note ideas.md: switch the hub to passive cooling",
    })
    assert r.status_code == 200 and r.json()["queued"]

    # the in-app bus consumer polls; give it a moment via the running loop
    deadline = time.time() + 8
    found = False
    while time.time() < deadline and not found:
        results = client.get("/memory/search", params={"q": "passive cooling"}).json()["results"]
        found = any("passive cooling" in res["text"] for res in results)
        if not found:
            time.sleep(0.3)
    assert found, "capture.event never reached the memory store"

    events = client.get("/timeline", params={"hours": 1}).json()["events"]
    assert any("passive cooling" in e["preview"] for e in events)


def test_capture_endpoint_validates_input(client):
    assert client.post("/capture/event", json={
        "source": "notes; DROP TABLE", "kind": "note", "content": "x",
    }).status_code == 422
    assert client.post("/capture/event", json={
        "source": "notes", "kind": "note", "content": "",
    }).status_code == 422


def test_capture_blocked_while_paused(client):
    client.post("/control/pause", json={"reason": "test"})
    r = client.post("/capture/event", json={
        "source": "notes", "kind": "note", "content": "should not land",
    })
    assert r.status_code == 503
    client.post("/control/resume")


def test_screen_ocr_enabled_is_truthfully_skipped(tmp_path):
    from jarvis.capture.runner import build_collectors

    collectors = build_collectors(
        {"defaults": {"redact_patterns": []},
         "collectors": {"screen_ocr": {"enabled": True}}},
        tmp_path,
    )
    assert collectors == []  # not implemented → not silently pretended
