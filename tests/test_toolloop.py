"""Tool loop behavior with a scripted router: tool calls execute and feed
back as fenced observations; destructive calls surface a confirmation
prompt; protocol violations degrade to a plain answer."""

import json

from jarvis.cognition.toolloop import ToolLoop, _extract_json
from jarvis.core.killswitch import KillSwitch
from jarvis.router.router import RouteResult
from tests.test_tools import make_registry, simple_tool


class ScriptedRouter:
    """Returns queued responses in order; records every prompt."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, task, messages, privacy_tags=()):
        self.calls.append(messages)
        return RouteResult(self.responses.pop(0), "scripted", "m", 1.0)


def base_messages(user_text="do the thing"):
    return [{"role": "system", "content": "You are JARVIS."},
            {"role": "user", "content": user_text}]


def test_extract_json_handles_fences_and_noise():
    assert _extract_json('{"action": "final", "reply": "hi"}')["reply"] == "hi"
    assert _extract_json('```json\n{"action": "final", "reply": "hi"}\n```')
    assert _extract_json('Sure! {"action": "final", "reply": "hi"} there')
    assert _extract_json("no json here at all") is None


async def test_final_answer_passthrough(cfg):
    reg = make_registry(cfg)
    router = ScriptedRouter([json.dumps({"action": "final", "reply": "All quiet, sir."})])
    loop = ToolLoop(reg, router, KillSwitch(cfg.db_path))
    out = await loop.run(base_messages(), "test")
    assert out["reply"] == "All quiet, sir."
    assert out["tools_used"] == []


async def test_tool_call_then_final(cfg):
    reg = make_registry(cfg, [simple_tool("lookup")])
    router = ScriptedRouter([
        json.dumps({"action": "tool", "tool": "lookup", "args": {"x": "42"}}),
        json.dumps({"action": "final", "reply": "It is 42."}),
    ])
    loop = ToolLoop(reg, router, KillSwitch(cfg.db_path))
    out = await loop.run(base_messages(), "test")
    assert out["reply"] == "It is 42."
    assert out["tools_used"] == ["lookup"]
    # the observation went back fenced as untrusted data
    second_prompt = router.calls[1]
    obs = second_prompt[-1]["content"]
    assert "OBSERVATION from lookup" in obs and "untrusted data" in obs


async def test_destructive_tool_surfaces_confirmation(cfg):
    reg = make_registry(cfg, [simple_tool("reboot_node", "destructive")])
    router = ScriptedRouter([
        json.dumps({"action": "tool", "tool": "reboot_node", "args": {"x": "mini"}}),
    ])
    loop = ToolLoop(reg, router, KillSwitch(cfg.db_path))
    out = await loop.run(base_messages("reboot the mini"), "test")
    assert out["pending_confirmation"] is not None
    assert "confirm " in out["reply"]
    token = out["pending_confirmation"]["token"]

    # operator confirms → executes
    result = await reg.confirm(token, "test", KillSwitch(cfg.db_path))
    assert "ran with" in result


async def test_tool_error_fed_back_not_fatal(cfg):
    reg = make_registry(cfg)  # no tools registered
    router = ScriptedRouter([
        json.dumps({"action": "tool", "tool": "nonexistent", "args": {}}),
        json.dumps({"action": "final", "reply": "Couldn't do that."}),
    ])
    loop = ToolLoop(reg, router, KillSwitch(cfg.db_path))
    out = await loop.run(base_messages(), "test")
    assert out["reply"] == "Couldn't do that."
    assert "ERROR" in router.calls[1][-1]["content"]


async def test_non_json_reply_is_the_answer(cfg):
    reg = make_registry(cfg)
    router = ScriptedRouter(["Just a plain sentence, no JSON."])
    loop = ToolLoop(reg, router, KillSwitch(cfg.db_path))
    out = await loop.run(base_messages(), "test")
    assert out["reply"] == "Just a plain sentence, no JSON."


async def test_duplicate_call_triggers_synthesis(cfg):
    """A model that re-issues the identical call must not loop — it gets one
    forced plain-text synthesis instead."""
    reg = make_registry(cfg, [simple_tool("busy")])
    call = json.dumps({"action": "tool", "tool": "busy", "args": {}})
    # 1st call executes; 2nd identical call → synthesize; synthesize gets the last reply
    router = ScriptedRouter([call, call, "Here is the summary, sir."])
    loop = ToolLoop(reg, router, KillSwitch(cfg.db_path), max_steps=4)
    out = await loop.run(base_messages(), "test")
    assert out["reply"] == "Here is the summary, sir."
    assert out["tools_used"] == ["busy"]  # ran once, not twice


async def test_step_budget_synthesizes_from_distinct_calls(cfg):
    reg = make_registry(cfg, [simple_tool("busy")])
    # exactly max_steps distinct calls (dedup guard won't fire), then the
    # forced synthesis response
    calls = [json.dumps({"action": "tool", "tool": "busy", "args": {"n": i}})
             for i in range(3)]
    router = ScriptedRouter(calls + ["Final synthesized answer."])
    loop = ToolLoop(reg, router, KillSwitch(cfg.db_path), max_steps=3)
    out = await loop.run(base_messages(), "test")
    assert out["reply"] == "Final synthesized answer."
    assert len(out["tools_used"]) == 3  # exactly the budget
