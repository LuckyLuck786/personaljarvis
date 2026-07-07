"""Embedding backends.

Embeddings are the ONE always-on LLM job on the hub (nomic-embed-text via
local Ollama), so ingestion and retrieval never depend on the MacBook being
awake. FakeEmbedder is a deterministic, dependency-free stand-in for tests.
"""

from __future__ import annotations

import hashlib
import math

import httpx


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str = "nomic-embed-text", timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            return resp.json()["embeddings"]


class FakeEmbedder:
    """Deterministic hashed bag-of-words vectors. NOT semantically meaningful —
    exact/overlapping tokens land in the same buckets, which is enough to test
    the pipeline mechanics without a model."""

    def __init__(self, dim: int = 64):
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in text.lower().split():
                h = int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "big")
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out
