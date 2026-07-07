"""LLM providers behind the router.

Thin custom layer instead of LiteLLM — deliberate: LiteLLM drags in a large
dependency tree and resident RAM we can't spare on the hub, and we need
exactly four API shapes: Ollama, two OpenAI-compatible endpoints (Groq,
Cerebras), and Gemini. ~150 lines total, fully under our control.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


@dataclass
class ChatResult:
    text: str
    model: str
    latency_ms: float


class ProviderError(Exception):
    pass


async def _post_json(url: str, payload: dict, headers: dict | None = None,
                     timeout_s: float = 120.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers or {})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        raise ProviderError(
            f"{exc.response.status_code} from {url}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"{type(exc).__name__} talking to {url}") from exc


class OllamaProvider:
    def __init__(self, base_url: str, timeout_s: float = 300.0,
                 num_predict: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.num_predict = num_predict   # cap output tokens (bounds slow gen)

    async def chat(self, messages: list[dict], model: str) -> ChatResult:
        start = time.monotonic()
        payload: dict = {"model": model, "messages": messages, "stream": False}
        if self.num_predict:
            payload["options"] = {"num_predict": self.num_predict}
        data = await _post_json(
            f"{self.base_url}/api/chat",
            payload,
            timeout_s=self.timeout_s,
        )
        return ChatResult(
            text=data["message"]["content"],
            model=model,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )


class OpenAICompatProvider:
    """Groq and Cerebras both speak the OpenAI chat-completions dialect."""

    def __init__(self, base_url: str, api_key: str, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s

    async def chat(self, messages: list[dict], model: str) -> ChatResult:
        if not self.api_key:
            raise ProviderError("no API key configured")
        start = time.monotonic()
        data = await _post_json(
            f"{self.base_url}/chat/completions",
            {"model": model, "messages": messages},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout_s=self.timeout_s,
        )
        return ChatResult(
            text=data["choices"][0]["message"]["content"],
            model=model,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )


class GeminiProvider:
    def __init__(self, api_key: str, timeout_s: float = 120.0):
        self.api_key = api_key
        self.timeout_s = timeout_s

    async def chat(self, messages: list[dict], model: str) -> ChatResult:
        if not self.api_key:
            raise ProviderError("no API key configured")
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user",
             "parts": [{"text": m["content"]}]}
            for m in messages if m["role"] != "system"
        ]
        payload: dict = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        start = time.monotonic()
        data = await _post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            payload,
            headers={"x-goog-api-key": self.api_key},
            timeout_s=self.timeout_s,
        )
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"unexpected Gemini response shape: {str(data)[:200]}") from exc
        return ChatResult(
            text=text, model=model,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )
