"""Allow-listed shell execution — locally on the hub, or on a configured
home-lab node over ssh.

Security posture: the allow-list (config/permissions.yaml → shell.allowlist)
is the ONLY path to execution. A command runs iff it exactly matches an
allow-list entry or is an entry-prefix plus simple safe arguments (no shell
metacharacters ever). There is deliberately no confirmation flow that
bypasses the list — extend the list in config or it does not run.
"""

from __future__ import annotations

import asyncio
import re
import shlex

from jarvis.tools.registry import Tool, ToolContext, ToolError

TIMEOUT_S = 30
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._/=:@%+-]+$")
_METACHARS = re.compile(r"[;&|<>`$(){}\[\]*?~\n\\]")


def command_allowed(cmd: str, allowlist: list[str]) -> bool:
    cmd = cmd.strip()
    if not cmd or _METACHARS.search(cmd):
        return False
    for entry in allowlist:
        entry = entry.strip()
        if cmd == entry:
            return True
        if cmd.startswith(entry + " "):
            extra = cmd[len(entry):].strip()
            if all(_SAFE_ARG.match(tok) for tok in extra.split()):
                return True
    return False


async def _run(argv: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError(f"command timed out after {TIMEOUT_S}s")
    text = out.decode(errors="replace")[:6000]
    return f"exit={proc.returncode}\n{text}"


async def shell_exec(args: dict, ctx: ToolContext) -> str:
    cmd = (args.get("command") or "").strip()
    node = (args.get("node") or "local").strip()
    allowlist = (ctx.permissions.get("shell") or {}).get("allowlist", [])
    if not command_allowed(cmd, allowlist):
        raise ToolError(
            f"command not on the allow-list: {cmd!r}. "
            "Extend shell.allowlist in config/permissions.yaml to permit it."
        )
    if node == "local":
        return await _run(shlex.split(cmd))
    ssh_target = ctx.cfg.tools.homelab.get(node)
    if not ssh_target:
        known = ", ".join(sorted(ctx.cfg.tools.homelab)) or "none configured"
        raise ToolError(f"unknown node {node!r} (known: {known})")
    return await _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                       ssh_target, cmd])


TOOLS = [
    Tool(
        name="shell_exec",
        description=("Run an allow-listed shell command. node: 'local' (default) "
                     "or a configured home-lab node name (runs over ssh). Only "
                     "commands on the operator's allow-list will execute."),
        params={"command": {"type": "string", "required": True},
                "node": {"type": "string"}},
        handler=shell_exec,
        permission="shell",
    ),
]
