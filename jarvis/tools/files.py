"""File tools, jailed to tools.files_allowed_roots from config/jarvis.yaml.
Path traversal is blocked by resolving before the containment check.
Read-only in Phase 3 — writes/moves/deletes arrive with a destructive
permission when there's a use case, not before."""

from __future__ import annotations

from pathlib import Path

from jarvis.tools.registry import Tool, ToolContext, ToolError

MAX_READ_CHARS = 20_000


def _resolve_jailed(raw: str, roots: list[str]) -> Path:
    if not roots:
        raise ToolError("no files_allowed_roots configured in config/jarvis.yaml")
    path = Path(raw).expanduser().resolve()
    for root in roots:
        root_resolved = Path(root).expanduser().resolve()
        if path == root_resolved or root_resolved in path.parents:
            return path
    raise ToolError(f"{raw} is outside the allowed roots {roots}")


async def file_read(args: dict, ctx: ToolContext) -> str:
    path = _resolve_jailed(args.get("path", ""), ctx.cfg.tools.files_allowed_roots)
    if not path.is_file():
        raise ToolError(f"not a file: {path}")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise ToolError(f"read failed: {exc}")
    truncated = " [truncated]" if len(text) > MAX_READ_CHARS else ""
    return f"{path}:{truncated}\n{text[:MAX_READ_CHARS]}"


async def file_list(args: dict, ctx: ToolContext) -> str:
    path = _resolve_jailed(args.get("path", ""), ctx.cfg.tools.files_allowed_roots)
    if not path.is_dir():
        raise ToolError(f"not a directory: {path}")
    entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))[:200]
    return "\n".join(f"{'d' if e.is_dir() else 'f'} {e.name}" for e in entries) or "(empty)"


TOOLS = [
    Tool(
        name="file_read",
        description="Read a text file (within the operator's allowed roots only).",
        params={"path": {"type": "string", "required": True}},
        handler=file_read,
        permission="read",
    ),
    Tool(
        name="file_list",
        description="List a directory (within the operator's allowed roots only).",
        params={"path": {"type": "string", "required": True}},
        handler=file_list,
        permission="read",
    ),
]
