"""Embedding client for the shared GPU server.

Peristyle shared GPU server is at http://127.0.0.1:8080/v1.
Model: Qwen/Qwen3-Embedding-4B. We always use ?dimensions=1024 (Matryoshka
truncate) per MEMORY.md halfvec dim-ceiling lesson.

This is a simple wrapper. No circuit breaking in v0 — caller (MemoryAPI)
raises on failure, and the agent loop surfaces the error to the model.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class EmbeddingClient:
    def __init__(self, config: Any) -> None:
        self.base_url = config.embedding.base_url.rstrip("/")
        self.model = config.embedding.model
        self.dimensions = config.embedding.dimensions
        self.api_key = config.embedding.api_key
        self.timeout_s = config.embedding.timeout_s
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.timeout_s),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns 1024-dim float vector."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = self._client.post(
            f"{self.base_url}/embeddings",
            headers=headers,
            json={
                "model": self.model,
                "input": texts,
                "dimensions": self.dimensions,
            },
        )
        r.raise_for_status()
        data = r.json()
        # OpenAI-compatible format: {"data": [{"embedding": [...]}, ...]}
        return [item["embedding"] for item in data["data"]]

    def close(self) -> None:
        self._client.close()
