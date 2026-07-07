"""Notes + memory search as tools, so the agent can deliberately store and
look things up mid-conversation."""

from __future__ import annotations

import time

from jarvis.tools.registry import Tool, ToolContext, ToolError


async def note_save(args: dict, ctx: ToolContext) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("text is required")
    doc_id = await ctx.store.ingest(text, kind="note", source="tool",
                                    tags=("personal",))
    return f"Noted (doc {doc_id})."


async def memory_search(args: dict, ctx: ToolContext) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    results = await ctx.store.search(query, k=int(args.get("k", 5)))
    if not results:
        return "Nothing in memory for that."
    return "\n".join(
        f"- [{r.kind}/{r.source} "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r.ts))}] {r.text[:300]}"
        for r in results
    )


TOOLS = [
    Tool(
        name="note_save",
        description="Store a durable note in the operator's memory archive.",
        params={"text": {"type": "string", "required": True}},
        handler=note_save,
        permission="act",
    ),
    Tool(
        name="memory_search",
        description="Search the operator's personal memory archive (notes, chats, captures).",
        params={"query": {"type": "string", "required": True},
                "k": {"type": "integer"}},
        handler=memory_search,
        permission="read",
    ),
]
