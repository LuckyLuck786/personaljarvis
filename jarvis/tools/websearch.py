"""Web SEARCH (not just fetch) — the piece that makes JARVIS research like
ChatGPT/Gemini: it can look things up, then web_fetch a specific result for
detail.

Providers, in preference order:
  1. Brave Search API   (BRAVE_API_KEY — free tier ~2k/mo, clean + reliable)
  2. Tavily             (TAVILY_API_KEY — free tier, LLM-oriented)
  3. DuckDuckGo HTML    (no key — works out of the box, best-effort scrape)

So it works with zero setup (DuckDuckGo) and gets better if you add a free
Brave key. Results are untrusted data — the agent loop fences them.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from jarvis.tools.registry import Tool, ToolContext, ToolError

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 jarvis/0.1"
MAX_SNIPPET = 300


class _DDGParser(HTMLParser):
    """Extracts result links + snippets from html.duckduckgo.com/html/."""

    def __init__(self):
        super().__init__()
        self._links: list[dict] = []
        self._snippets: list[str] = []
        self._mode = None          # 'title' | 'snippet' | None
        self._buf = ""
        self._href = ""

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        cls = dict(attrs).get("class", "")
        if "result__a" in cls:
            self._mode, self._buf = "title", ""
            self._href = dict(attrs).get("href", "")
        elif "result__snippet" in cls:
            self._mode, self._buf = "snippet", ""

    def handle_endtag(self, tag):
        if tag == "a" and self._mode == "title":
            self._links.append({"title": self._buf.strip(),
                                "url": _decode_uddg(self._href)})
            self._mode = None
        elif tag == "a" and self._mode == "snippet":
            self._snippets.append(self._buf.strip())
            self._mode = None

    def handle_data(self, data):
        if self._mode:
            self._buf += data

    def results(self) -> list[dict]:
        out = []
        for i, link in enumerate(self._links):
            if not link["url"]:
                continue
            out.append({**link, "snippet": self._snippets[i] if i < len(self._snippets) else ""})
        return out


def _decode_uddg(href: str) -> str:
    """DDG wraps result URLs as //duckduckgo.com/l/?uddg=<encoded>. Unwrap it."""
    if "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    if href.startswith("//"):
        return "https:" + href
    return href


async def _brave(query: str, n: int, key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    return [{"title": w.get("title", ""), "url": w.get("url", ""),
             "snippet": w.get("description", "")}
            for w in data.get("web", {}).get("results", [])]


async def _tavily(query: str, n: int, key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": query, "max_results": n})
        r.raise_for_status()
        data = r.json()
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": x.get("content", "")}
            for x in data.get("results", [])]


async def _ddg(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA},
                                 follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        r.raise_for_status()
        html = r.text
    parser = _DDGParser()
    parser.feed(html)
    return parser.results()[:n]


async def web_search(args: dict, ctx: ToolContext) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    n = min(int(args.get("num_results", 5)), 10)
    s = ctx.cfg.secrets
    try:
        if getattr(s, "brave_api_key", ""):
            results = await _brave(query, n, s.brave_api_key)
        elif getattr(s, "tavily_api_key", ""):
            results = await _tavily(query, n, s.tavily_api_key)
        else:
            results = await _ddg(query, n)
    except httpx.HTTPError as exc:
        raise ToolError(f"search failed: {type(exc).__name__}")
    if not results:
        return ("No results (the keyless search may be rate-limited). Add a free "
                "BRAVE_API_KEY to .env for reliable search.")
    lines = [f"[{i+1}] {r['title']}\n{r['url']}\n{r['snippet'][:MAX_SNIPPET]}"
             for i, r in enumerate(results[:n])]
    return f"Search results for “{query}”:\n\n" + "\n\n".join(lines)


TOOLS = [
    Tool(
        name="web_search",
        description=("Search the web and return top results (title, URL, snippet). "
                     "Use this to look things up, then web_fetch a specific URL "
                     "for full detail."),
        params={"query": {"type": "string", "required": True},
                "num_results": {"type": "integer"}},
        handler=web_search,
        permission="read",
    ),
]
