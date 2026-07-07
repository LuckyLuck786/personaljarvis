import time

import pytest

from jarvis.core.audit import AuditLog
from jarvis.core.bus import Bus
from jarvis.core.killswitch import KillSwitch
from jarvis.tools.registry import (
    ConfirmationRequired,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
)
from jarvis.tools.shell import command_allowed
from jarvis.tools.tasks import parse_due

PERMS = {"shell": {"allowlist": ["df -h", "uptime", "echo hello"]},
         "overrides": {}}


def make_registry(cfg, tools=(), permissions=None):
    ctx = ToolContext(cfg=cfg, store=None, router=None, bus=Bus(cfg.db_path),
                      audit=AuditLog(cfg.db_path),
                      permissions=permissions or PERMS)
    reg = ToolRegistry(ctx)
    for t in tools:
        reg.register(t)
    return reg


def simple_tool(name="echo_tool", permission="read"):
    async def handler(args, ctx):
        return f"ran with {args}"

    return Tool(name=name, description="test tool",
                params={"x": {"type": "string"}}, handler=handler,
                permission=permission, destructive=(permission == "destructive"))


# -- discovery -----------------------------------------------------------------

def test_discovery_finds_all_shipped_tools(cfg):
    reg = make_registry(cfg)
    reg.discover()
    expected = {"task_add", "task_list", "task_complete", "note_save",
                "memory_search", "web_fetch", "shell_exec", "file_read",
                "file_list", "calendar_events", "email_unread", "email_draft",
                "homelab_status", "homelab_reboot"}
    assert expected <= set(reg.tools)


# -- permission gating -----------------------------------------------------------

async def test_read_tool_runs_immediately(cfg):
    reg = make_registry(cfg, [simple_tool()])
    ks = KillSwitch(cfg.db_path)
    assert "ran with" in await reg.execute("echo_tool", {"x": 1}, "test", ks)


async def test_destructive_tool_parks_for_confirmation(cfg):
    reg = make_registry(cfg, [simple_tool("nuke", "destructive")])
    ks = KillSwitch(cfg.db_path)
    with pytest.raises(ConfirmationRequired) as exc:
        await reg.execute("nuke", {"x": "all"}, "test", ks)
    token = exc.value.token

    # confirming executes exactly once
    result = await reg.confirm(token, "test", ks)
    assert "ran with" in result
    with pytest.raises(ToolError, match="already executed"):
        await reg.confirm(token, "test", ks)


async def test_expired_confirmation_refuses(cfg):
    from jarvis.core import db

    reg = make_registry(cfg, [simple_tool("nuke", "destructive")])
    ks = KillSwitch(cfg.db_path)
    with pytest.raises(ConfirmationRequired) as exc:
        await reg.execute("nuke", {}, "test", ks)
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE pending_confirmations SET expires_ts=? WHERE token=?",
                 (time.time() - 1, exc.value.token))
    conn.close()
    with pytest.raises(ToolError, match="expired"):
        await reg.confirm(exc.value.token, "test", ks)


async def test_killswitch_blocks_all_tools(cfg):
    reg = make_registry(cfg, [simple_tool()])
    ks = KillSwitch(cfg.db_path)
    ks.pause("test")
    with pytest.raises(ToolError, match="kill switch"):
        await reg.execute("echo_tool", {}, "test", ks)


async def test_permission_override_from_config(cfg):
    perms = {**PERMS, "overrides": {"echo_tool": "destructive"}}
    reg = make_registry(cfg, [simple_tool()], permissions=perms)
    ks = KillSwitch(cfg.db_path)
    with pytest.raises(ConfirmationRequired):
        await reg.execute("echo_tool", {}, "test", ks)


async def test_tool_executions_are_audited(cfg):
    reg = make_registry(cfg, [simple_tool()])
    ks = KillSwitch(cfg.db_path)
    await reg.execute("echo_tool", {"x": 1}, "test", ks)
    entries = AuditLog(cfg.db_path).tail()
    assert entries[-1]["action"] == "tool.echo_tool"
    assert entries[-1]["outcome"] == "ok"


# -- shell allow-list -----------------------------------------------------------

def test_shell_allowlist_exact_and_safe_args():
    allow = ["df -h", "systemctl status jarvis-hub"]
    assert command_allowed("df -h", allow)
    assert command_allowed("df -h /var", allow)          # safe extra arg
    assert not command_allowed("rm -rf /", allow)
    assert not command_allowed("df -h; rm -rf /", allow)  # metachar
    assert not command_allowed("df -h $(whoami)", allow)
    assert not command_allowed("df -h | tee /etc/passwd", allow)
    assert not command_allowed("", allow)


async def test_shell_exec_refuses_unlisted(cfg):
    reg = make_registry(cfg)
    reg.discover()
    ks = KillSwitch(cfg.db_path)
    with pytest.raises(ToolError, match="allow-list"):
        await reg.execute("shell_exec", {"command": "cat /etc/shadow"}, "test", ks)


async def test_shell_exec_runs_allowlisted(cfg):
    reg = make_registry(cfg)
    reg.discover()
    ks = KillSwitch(cfg.db_path)
    out = await reg.execute("shell_exec", {"command": "echo hello"}, "test", ks)
    assert "hello" in out and "exit=0" in out


# -- files jail ------------------------------------------------------------------

async def test_file_read_jailed(cfg, tmp_path):
    cfg.tools.files_allowed_roots = [str(tmp_path / "allowed")]
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "ok.txt").write_text("fine")
    reg = make_registry(cfg)
    reg.discover()
    ks = KillSwitch(cfg.db_path)

    out = await reg.execute("file_read", {"path": str(allowed / "ok.txt")}, "t", ks)
    assert "fine" in out
    with pytest.raises(ToolError, match="outside the allowed roots"):
        await reg.execute("file_read", {"path": "/etc/passwd"}, "t", ks)
    with pytest.raises(ToolError, match="outside the allowed roots"):
        # traversal out of the jail must be caught after resolution
        await reg.execute("file_read",
                          {"path": str(allowed / ".." / ".." / "etc" / "passwd")},
                          "t", ks)


# -- time parsing -----------------------------------------------------------------

def test_parse_due_formats():
    now = time.time()
    assert parse_due("+30m") == pytest.approx(now + 1800, abs=5)
    assert parse_due("+2h") == pytest.approx(now + 7200, abs=5)
    assert parse_due(None) is None
    hh = parse_due("18:00")
    assert hh > now  # today or tomorrow, always in the future
    with pytest.raises(ToolError):
        parse_due("whenever")


# -- calendar ICS ------------------------------------------------------------------

def test_ics_parsing():
    from jarvis.tools.calendar_ics import parse_ics_events

    ics = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "DTSTART:20260707T090000Z\r\n"
        "SUMMARY:Standup with\r\n  the team\r\n"
        "LOCATION:Zoom\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    events = parse_ics_events(ics)
    assert len(events) == 1
    assert events[0]["summary"] == "Standup with the team"  # folded line unfolds
    assert events[0]["location"] == "Zoom"
    assert events[0]["start"].year == 2026
