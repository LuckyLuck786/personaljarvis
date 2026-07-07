"""Knowledge-graph tools: let the agent answer "who/what is connected to X"
and "who do I interact with most" from the entity graph."""

from __future__ import annotations

from jarvis.memory.graph import KnowledgeGraph
from jarvis.tools.registry import Tool, ToolContext, ToolError


async def graph_connections(args: dict, ctx: ToolContext) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        raise ToolError("name is required")
    graph = KnowledgeGraph(ctx.cfg.db_path)
    neighbors = graph.neighbors(name, limit=int(args.get("k", 12)))
    if not neighbors:
        return f"Nothing connected to “{name}” in the graph yet."
    lines = [f"- {n['name']} ({n['kind']}, strength {n['weight']})" for n in neighbors]
    return f"Connected to “{name}”:\n" + "\n".join(lines)


async def graph_top(args: dict, ctx: ToolContext) -> str:
    graph = KnowledgeGraph(ctx.cfg.db_path)
    kind = args.get("kind")
    top = graph.top_entities(limit=int(args.get("k", 15)), kind=kind)
    if not top:
        return "The knowledge graph is empty so far."
    return "Most-connected entities:\n" + "\n".join(
        f"- {e['name']} ({e['kind']}, degree {e['degree']})" for e in top)


TOOLS = [
    Tool(
        name="graph_connections",
        description=("Who/what is connected to a person, project, or thing in the "
                     "operator's knowledge graph."),
        params={"name": {"type": "string", "required": True},
                "k": {"type": "integer"}},
        handler=graph_connections,
        permission="read",
    ),
    Tool(
        name="graph_top",
        description=("The most-connected entities in the operator's life. "
                     "Optional kind: person | project | thing."),
        params={"kind": {"type": "string"}, "k": {"type": "integer"}},
        handler=graph_top,
        permission="read",
    ),
]
