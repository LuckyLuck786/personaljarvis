"""Web fetch tool. Fetched content is UNTRUSTED — the agent loop fences
tool output as data, and this tool additionally strips markup and caps
size so a hostile page can't flood the context."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from jarvis.tools.registry import Tool, ToolContext, ToolError

MAX_CHARS = 8000
SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parser.parts))


async def web_fetch(args: dict, ctx: ToolContext) -> str:
    url = (args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http(s)://")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "jarvis/0.1"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolError(f"fetch failed: {type(exc).__name__}")
    content_type = resp.headers.get("content-type", "")
    body = resp.text[:500_000]
    text = html_to_text(body) if "html" in content_type else body
    text = text[:MAX_CHARS]
    return f"Fetched {url} ({content_type.split(';')[0] or 'unknown type'}):\n{text}"


TOOLS = [
    Tool(
        name="web_fetch",
        description=("Fetch a URL and return its readable text (capped). "
                     "Use for looking things up or summarizing pages."),
        params={"url": {"type": "string", "required": True}},
        handler=web_fetch,
        permission="read",
    ),
]
