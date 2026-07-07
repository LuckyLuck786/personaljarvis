"""Home-lab control. Non-destructive status is `read`; the reboot action is
`destructive` and therefore always parks for operator confirmation — the
canonical example of the confirmation gate."""

from __future__ import annotations

import asyncio

from jarvis.tools.registry import Tool, ToolContext, ToolError


async def _ssh(target: str, cmd: str, timeout: int = 15) -> str:
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError("ssh timed out")
    return out.decode(errors="replace")[:4000]


def _target(ctx: ToolContext, node: str) -> str:
    target = ctx.cfg.tools.homelab.get(node)
    if not target:
        known = ", ".join(sorted(ctx.cfg.tools.homelab)) or "none configured"
        raise ToolError(f"unknown node {node!r} (known: {known})")
    return target


async def homelab_status(args: dict, ctx: ToolContext) -> str:
    node = (args.get("node") or "").strip()
    return await _ssh(_target(ctx, node), "uptime; df -h / | tail -1")


async def homelab_reboot(args: dict, ctx: ToolContext) -> str:
    node = (args.get("node") or "").strip()
    target = _target(ctx, node)
    out = await _ssh(target, "sudo reboot", timeout=10)
    return f"Reboot issued to {node}. {out}".strip()


TOOLS = [
    Tool(
        name="homelab_status",
        description="Uptime + disk of a configured home-lab node (over ssh).",
        params={"node": {"type": "string", "required": True}},
        handler=homelab_status,
        permission="read",
    ),
    Tool(
        name="homelab_reboot",
        description="Reboot a home-lab node. Destructive — requires confirmation.",
        params={"node": {"type": "string", "required": True}},
        handler=homelab_reboot,
        permission="destructive",
        destructive=True,
    ),
]
