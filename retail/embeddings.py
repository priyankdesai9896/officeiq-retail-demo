"""Text embeddings from the local model server (nomic-embed-text, 768 dims).

Vectors are L2-normalised, so the dot_product similarity the vector indexes
use *is* cosine similarity — safe to show and to threshold against.

nomic-embed-text marks the side of a search in the text itself:
"search_query: " for what a person types, "search_document: " for what is
stored. Mixing them up costs real recall, so callers say which side they are.
"""

from __future__ import annotations

import math
import time
from functools import lru_cache

import httpx

from config import settings


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _embed_batch(texts: list[str], side: str) -> list[list[float]]:
    prefix = (settings.embedding_query_prefix if side == "query"
              else settings.embedding_document_prefix)
    payload = {"model": settings.embedding_model,
               "input": [prefix + (t or " ") for t in texts]}
    headers = {"Authorization": f"Bearer {settings.model_api_key}"}
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = httpx.post(f"{settings.model_endpoint}/embeddings", json=payload,
                           headers=headers, timeout=settings.model_timeout)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [_normalise(d["embedding"]) for d in data]
        except Exception as exc:  # model loading, a dropped connection
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{settings.model_service_label} embeddings failed: {last}") from last


def embed_documents(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), settings.embedding_batch):
        out += _embed_batch(texts[i:i + settings.embedding_batch], "document")
    return out


def embed_document(text: str) -> list[float]:
    return embed_documents([text])[0]


@lru_cache(maxsize=512)
def _embed_query_cached(text: str) -> tuple[float, ...]:
    return tuple(_embed_batch([text], "query")[0])


def embed_query(text: str) -> list[float]:
    return list(_embed_query_cached(text.strip()))


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def describe() -> str:
    return (f"{settings.embedding_model} ({settings.embedding_dims}d, "
            f"{settings.model_service_label})")
