"""Semantic cache — answers kept by what the question means, not its words.

"How long does delivery take?" and "when will my desk arrive" are the same
question: the second is answered from the cache, with no call to the chat
model, so it costs no tokens and comes back in milliseconds.

Each entry is a document in `aidp.semantic_cache` with the question's vector;
`cache_vec_idx` finds the closest one. Entries live in a namespace: "public" for
general store questions anyone may get the same answer to, "private::<customer>"
for answers built on one customer's memories (cleared when those change).
A running tally of what the cache saved is kept in `aidp.usage`.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from couchbase.options import SearchOptions
from couchbase.search import SearchRequest, TermQuery
from couchbase.vector_search import VectorQuery, VectorSearch

from config import settings
from retail import embeddings
from retail.cb import fq, get_scope, kv_get, kv_upsert, query
from retail.trace import Trace

PUBLIC = "public"
COLLECTION = "aidp.semantic_cache"
TALLY = "USAGE::semantic_cache"
TALLY_COLLECTION = "aidp.usage"


def _key(namespace: str, question: str) -> str:
    q = " ".join(question.lower().split())
    return "CACHE::" + hashlib.sha1(f"{namespace}|{q}".encode()).hexdigest()[:20]


def lookup(question: str, namespace: str = PUBLIC, trace: Trace | None = None
           ) -> tuple[dict[str, Any] | None, list[float]]:
    """The cached answer whose question is closest in meaning, however close
    -> (entry with its "score", the question's vector — reused when storing).
    Whether it's close enough is the caller's call."""
    t0 = time.perf_counter()
    vec = embeddings.embed_query(question)
    t_embed = (time.perf_counter() - t0) * 1000
    if trace:
        trace.add("Model", "Turned the question into a meaning vector",
                  "768 numbers that capture what it asks", "nomic-embed-text via Ollama",
                  t_embed, model=settings.embedding_model)
    # The very same question first, by key: the vector index takes a moment to
    # take in a new answer, so asking again straight away would miss it there.
    t1 = time.perf_counter()
    key = _key(namespace, question)
    same = kv_get("aidp", "semantic_cache", key)
    if same:
        if trace:
            trace.add("Semantic Cache", "Looked for the same question in the semantic cache",
                      f"closest: “{same['canonical_question']}”, 100% similar (asked word "
                      f"for word before)", f"KV get, key {key}",
                      (time.perf_counter() - t1) * 1000, [(COLLECTION, "KV get")])
        return {**same, "score": 1.0, "id": key}, vec
    vq = VectorQuery("embedding", vec, num_candidates=20,
                     prefilter=TermQuery(namespace, field="namespace"))
    try:
        res = get_scope("aidp").search(
            "cache_vec_idx", SearchRequest.create(VectorSearch.from_vector_query(vq)),
            SearchOptions(limit=3))
        hits = [(r.id, float(r.score)) for r in res.rows()]
    except Exception:                       # an empty or still-building index
        hits = []
    best = None
    if hits:
        best = kv_get("aidp", "semantic_cache", hits[0][0])
        if best:
            best = {**best, "score": hits[0][1], "id": hits[0][0]}
    if trace:
        trace.add("Semantic Cache",
                  "Looked for the same question in the semantic cache",
                  f"closest: “{best['canonical_question']}”, {best['score']:.0%} similar"
                  if best else "Cache MISS",
                  "KNN on cache_vec_idx, namespace " + namespace,
                  (time.perf_counter() - t1) * 1000, [(COLLECTION, "vector search + KV get")],
                  indexes=["cache_vec_idx"])
    return best, vec


def store(question: str, vec: list[float], answer: str, *, tokens: dict[str, int],
          ms: float, sources: list[str], namespace: str = PUBLIC,
          trace: Trace | None = None) -> str:
    t0 = time.perf_counter()
    key = _key(namespace, question)
    kv_upsert("aidp", "semantic_cache", key, {
        "type": "cache_entry", "cache_id": key, "namespace": namespace,
        "canonical_question": question, "answer": answer, "sources": sources,
        "model": settings.chat_model, "tokens": tokens, "generate_ms": round(ms),
        "hits": 0, "created_at": datetime.now(timezone.utc).isoformat(),
        "embedding": vec})
    if trace:
        trace.add("Semantic Cache", "Kept the answer in the semantic cache",
                  "the next question that means the same is answered from here",
                  f"KV upsert, key {key}", (time.perf_counter() - t0) * 1000,
                  writes=[COLLECTION])
    return key


def count_hit(entry: dict[str, Any]) -> None:
    try:
        query(f"UPDATE {fq('aidp', 'semantic_cache')} AS c USE KEYS $k "
              f"SET c.hits = IFMISSINGORNULL(c.hits, 0) + 1, c.last_hit = NOW_STR()",
              k=entry["id"])
    except Exception:
        pass


def tally(*, hit: bool, tokens: int, ms: float, saved_ms: float = 0.0) -> dict[str, Any]:
    """Add one question to the running tally: tokens spent on a miss, saved on a hit."""
    t = kv_get("aidp", "usage", TALLY) or {
        "type": "usage", "what": "semantic_cache", "questions": 0, "hits": 0,
        "tokens_spent": 0, "tokens_saved": 0, "ms_saved": 0}
    t["questions"] += 1
    if hit:
        t["hits"] += 1
        t["tokens_saved"] += tokens
        t["ms_saved"] += round(saved_ms)
    else:
        t["tokens_spent"] += tokens
    kv_upsert("aidp", "usage", TALLY, t)
    return t


def totals() -> dict[str, Any]:
    return kv_get("aidp", "usage", TALLY) or {"questions": 0, "hits": 0, "tokens_spent": 0,
                                               "tokens_saved": 0, "ms_saved": 0}


def entries(namespace: str = PUBLIC, limit: int = 50) -> list[dict[str, Any]]:
    return query(f"SELECT c.canonical_question, c.hits, c.tokens, c.generate_ms "
                 f"FROM {fq('aidp', 'semantic_cache')} AS c WHERE c.`namespace` = $ns "
                 f"ORDER BY c.hits DESC, c.created_at LIMIT {int(limit)}", ns=namespace)


def clear(namespace: str = PUBLIC) -> None:
    query(f"DELETE FROM {fq('aidp', 'semantic_cache')} AS c WHERE c.`namespace` = $ns",
          ns=namespace)
    if namespace == PUBLIC:
        query(f"DELETE FROM {fq('aidp', 'usage')} USE KEYS $k", k=TALLY)


def reset() -> None:
    """Empty the whole cache, every namespace, and zero the tally — for a fresh demo."""
    query(f"DELETE FROM {fq('aidp', 'semantic_cache')}")
    query(f"DELETE FROM {fq('aidp', 'usage')} USE KEYS $k", k=TALLY)


def invalidate_customer(customer_id: str) -> None:
    """Answers built on a customer's memories are stale once those change."""
    clear(f"private::{customer_id}")
