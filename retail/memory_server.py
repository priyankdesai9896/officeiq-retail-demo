"""Client for the self-hosted Couchbase Agent Memory server.

The server is a container you run yourself (here: `agentmemory-local` on
:8082), pointed at this demo's bucket; it stores memory blocks in
`retail.agentmemory.*` and embeds and searches them itself.

How this demo maps onto the server's Users -> Sessions -> Memory Blocks:

* **user_id = customer_id** (`RET-1003`). The `RET-` prefix keeps this demo's
  users apart from any other demo sharing the same server.
* **session_id** = the conversation. Writes land in the current session;
  reads use `session_ids="all"`, so a preference set on Monday surfaces on
  Thursday in a different session.
* `kind`, `importance` and `source` ride along as **annotations**.

Writes are asynchronous: a block is `processing` before it is `ready`, so
anything that stores and immediately searches waits with `wait_ready()`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from config import settings

log = logging.getLogger(__name__)
_client = None
_SDK_ERROR = (
    "couchbase-agent-memory is not installed. `pip install couchbase-agent-memory`"
)


def available() -> bool:
    """Is the server configured? Says nothing about whether it answers."""
    return bool(settings.agent_memory_url)


def status(probe: bool = False) -> tuple[bool, str]:
    """`(live, reason)` — why the Agent Memory server is or is not in play.

    Two failures wear the same face and are completely different mistakes, so
    they get different sentences:

    * **misconfigured** — no `AGENT_MEMORY_URL`. Nothing is wrong with the
      server; nobody told the app where it is.
    * **unreachable** — the URL is set and the server does not answer. The
      container is down, or the port is wrong.

    `probe=True` actually calls the server, which costs a round trip (~1.7s
    cold). Callers that run per-turn must leave it False and let the request
    itself fail; it is for startup checks.
    """
    if not settings.agent_memory_url:
        return False, ("AGENT_MEMORY_URL is not set — the app cannot find the "
                       "Agent Memory server, and it is the only memory store")
    if not probe:
        return True, f"configured at {settings.agent_memory_url}"

    # Fast path for the common failure. The SDK's health ping tries six
    # subsystems in turn and takes ~21s to conclude that a dead port is dead;
    # a TCP connect says the same thing in milliseconds, and this runs at
    # startup where that difference is the whole user experience.
    if not _reachable():
        return False, (f"nothing is listening on {settings.agent_memory_url} — "
                       "is the container running? "
                       f"`docker start {settings.agent_memory_container}`")
    try:
        health = ping()
    except Exception as exc:
        return False, (f"{settings.agent_memory_url} is not answering "
                       f"({type(exc).__name__}) — is the container running?")
    if not health.get("ok"):
        return False, (f"{settings.agent_memory_url} answered but reported "
                       f"{health.get('status')} with no subsystems reachable")
    note = "" if health.get("status") == "healthy" else \
        f" (reporting {health.get('status')})"
    return True, f"answering at {settings.agent_memory_url}{note}"


def _reachable(timeout: float = 1.5) -> bool:
    """Can we open a socket to the server's host and port?"""
    import socket
    from urllib.parse import urlparse
    parsed = urlparse(settings.agent_memory_url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def client():
    """One client for the process. Cheap to hold: it is an HTTP session."""
    global _client
    if _client is None:
        try:
            from agentmemory import AgentMemoryClient
        except ImportError as exc:                      # pragma: no cover
            raise RuntimeError(_SDK_ERROR) from exc
        _client = AgentMemoryClient(
            base_url=settings.agent_memory_url,
            token=None,
            verify=True,
            timeout=settings.agent_memory_timeout,
        )
    return _client


# Connection-level failures, as opposed to the server answering with an error.
_TRANSPORT_ERRORS = ("RemoteProtocolError", "ConnectError", "ReadError",
                     "ConnectTimeout", "ReadTimeout", "PoolTimeout",
                     "WriteError", "ProtocolError")


def _retrying(call, *args, **kwargs):
    """Run a server call, once more with a fresh client if the socket died.

    The client holds a pooled HTTP connection. Leave it idle long enough — a
    rehearsal spends minutes at a time inside the model — and the server (or
    Docker's NAT) closes it, and the next request fails with "Server
    disconnected without sending a response". It killed a full rehearsal run
    at the two-thirds mark.

    Retried only for transport failures — and for one server answer that is
    refused before anything is written: "503 LLM service unavailable". The
    server checks its models only every 30 minutes (agent-memory.env); a check
    that lands while Ollama is busy with the vision model fails, and every
    write is turned away until the next one. Asking for /health/models re-runs
    the check (~5 s), so the write can go through now. Any other error the
    server *sent* is a real answer and is raised as it is: retrying a rejected
    write is how a demo ends up with two of something.
    """
    global _client
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        if type(exc).__name__ == "ServiceUnavailableError" and "LLM service" in str(exc):
            _recheck_models()
            return call(*args, **kwargs)
        if type(exc).__name__ not in _TRANSPORT_ERRORS:
            raise
        _client = None                      # drop the dead pool, rebuild it
        return call(*args, **kwargs)


def _recheck_models() -> None:
    """Make the server re-check its models now instead of in up to 30 minutes."""
    import httpx
    try:
        r = httpx.get(f"{settings.agent_memory_url.rstrip('/')}/health/models", timeout=60)
        log.warning("Agent Memory said its model was unavailable; re-checked: %s",
                    r.status_code)
    except Exception as exc:                # the retry will say what's wrong
        log.warning("Agent Memory model re-check failed: %s", exc)


def ping() -> dict[str, Any]:
    """Is the server up, and does it answer? Used by the preflight script.

    `ok` is derived, not asserted. This function used to hardcode
    `"ok": True` and let the absence of an exception stand for health — but
    the SDK's `health_ping()` **does not raise** when the server is gone. Point
    it at a dead port and it logs six connection-refused warnings, returns
    `overall_status=degraded` with an empty `checked_entities`, and the caller
    concludes all is well. A health check that cannot fail is not a check.

    `checked_entities` is the test: a reachable server reports on its
    subsystems (6 of them here), an unreachable one reports on none. Judged on
    reachability rather than on `overall_status == healthy`, deliberately — a
    server that is up but whose model endpoint is briefly flaky reads as
    `degraded`, and refusing to start the demo over that would be worse than
    the problem. Degraded-but-reachable starts, and `status` says so.
    """
    t0 = time.perf_counter()
    resp = _retrying(lambda: client().health_ping())
    dump = getattr(resp, "model_dump", None)
    body = dump() if dump else resp
    checked = (body or {}).get("checked_entities") or []
    overall = (body or {}).get("overall_status")
    return {"ok": bool(checked),
            "status": getattr(overall, "value", overall),
            "checked": len(checked),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "response": body}


def _user(account_id: str):
    """This account, created on the server the first time it is seen.

    The API is hierarchical and strict: a session cannot be created under a
    user that does not exist, and the demo's accounts pre-date the server. So
    get-or-create, once per process per account.
    """
    from agentmemory.exceptions import AgentMemoryError
    cli = client()
    try:
        return cli.get_user(account_id)
    except Exception as exc:
        if not isinstance(exc, AgentMemoryError) and not _is_missing(exc):
            raise
    try:
        return cli.create_user(account_id, name=account_id)
    except Exception:
        # Lost a race with another worker; the user exists now either way.
        return cli.get_user(account_id)


def _is_missing(exc: Exception) -> bool:
    return "not found" in str(exc).lower() or "404" in str(exc)


def _session(account_id: str, session_id: str):
    """The (user, session) pair this account's memories live under."""
    user = _user(account_id)
    sid = session_id or "default"
    try:
        return user.get_session(sid)
    except Exception as exc:
        if not _is_missing(exc):
            raise
    try:
        return user.create_session(sid)
    except Exception:
        return user.get_session(sid)


def _to_row(block: Any) -> dict[str, Any]:
    """A server memory block in the shape the app already renders.

    The app reads `text`, `kind`, `source`, `importance`, `memory_id` and
    `_score` everywhere — panels, ranking, the greeting, the handling block.
    Translating here rather than at twenty call sites is what keeps the two
    backends interchangeable.
    """
    ann = dict(getattr(block, "annotations", None) or {})
    text = getattr(block, "fact", None) or ""
    if not text:
        msg = getattr(block, "message", None)
        if msg is not None:
            text = " ".join(x for x in (getattr(msg, "user_content", ""),
                                        getattr(msg, "assistant_content", "")) if x)
    row = {
        "memory_id": getattr(block, "block_id", ""),
        "account_id": getattr(block, "user_id", ""),
        "session_id": getattr(block, "session_id", ""),
        "text": text,
        "kind": ann.get("kind", "fact"),
        "source": ann.get("source", "conversation"),
        "importance": float(ann.get("importance") or 0.5),
        "confidence": float(ann.get("confidence") or 0.8),
        "created_at": getattr(block, "created_at", None)
        or getattr(block, "ingested_at", ""),
        "status": str(getattr(block, "status", "ready")),
        "structured": _structured_from(ann),
    }
    # The SDK hands back a str-Enum, and `str()` on one renders as
    # "MemoryBlockStatus.READY" — so the readiness wait compared against the
    # wrong thing and timed out on blocks that were ready in three seconds.
    row["status"] = str(row["status"]).rsplit(".", 1)[-1].lower()
    score = getattr(block, "rel_score", None)
    if score is not None:
        row["_score"] = round(float(score), 4)
    return row


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
# Structured facts that ride along as annotations (string key/values) and are
# rebuilt with their types on the way out — e.g. the last room a customer
# planned, so "plan the same room again" needs no re-typing.
_STRUCTURED_KEYS = ("room_type", "room_length_in", "room_width_in", "headcount",
                    "color", "budget_usd", "plan_id")
_STRUCTURED_INTS = ("room_length_in", "room_width_in", "headcount", "budget_usd")


def _structured_from(ann: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the structured dict from annotations, numerics coerced back.

    The planner does arithmetic on the room dimensions, so a string "144"
    must come back as a number. One place knows the mapping, both ways.
    """
    out: dict[str, Any] = {}
    for key in _STRUCTURED_KEYS:
        raw = ann.get(key)
        if raw in (None, ""):
            continue
        if key in _STRUCTURED_INTS:
            try:
                out[key] = int(str(raw))
                continue
            except ValueError:
                pass
        out[key] = str(raw)
    return out


def remember(account_id: str, text: str, kind: str = "fact",
             user_id: str | None = None, importance: float = 0.5,
             confidence: float = 0.8, source: str = "conversation",
             session_id: str = "default", ttl_days: int | None = None,
             structured: dict[str, Any] | None = None,
             **_ignored: Any) -> dict[str, Any]:
    """Store one fact. Annotations carry what this demo ranks and labels by."""
    ann = {"kind": kind, "source": source,
           "importance": f"{float(importance):.2f}",
           "confidence": f"{float(confidence):.2f}"}
    if user_id:
        ann["set_by"] = str(user_id)
    for key in _STRUCTURED_KEYS:
        val = (structured or {}).get(key)
        if val not in (None, ""):
            ann[key] = str(val)
    resp = _retrying(
        lambda: _session(account_id, session_id).add_memory(
            facts=[text], annotations=ann,
            memory_block_ttl=(int(ttl_days) * 86400 if ttl_days else None)))
    block_id = (getattr(resp, "block_ids", None) or [""])[0]
    return {"memory_id": block_id, "account_id": account_id, "text": text,
            "kind": kind, "source": source, "importance": importance,
            "accepted": getattr(resp, "accepted_count", 0)}


def forget(account_id: str, block_ids: list[str],
           session_id: str | None = None) -> int:
    """Delete blocks, each through the session that owns it.

    Deletion is a session-scoped operation: asking one session to delete
    another session's block deletes nothing and says nothing. Passing the
    wrong session is how a purge reported nine deletions and left all nine in
    place, so the blocks are grouped by the session they were written under
    unless the caller names one.
    """
    wanted = {b for b in block_ids if b}
    if not wanted:
        return 0
    if session_id:
        _retrying(lambda: _session(account_id, session_id).delete_memory(
            block_ids=sorted(wanted)))
        return len(wanted)

    by_session: dict[str, list[str]] = {}
    for row in list_for_account(account_id, limit=200):
        if row["memory_id"] in wanted:
            by_session.setdefault(row["session_id"] or "default",
                                  []).append(row["memory_id"])
    removed = 0
    for sid, ids in by_session.items():
        _retrying(lambda: _session(account_id, sid).delete_memory(
            block_ids=sorted(ids)))
        removed += len(ids)
    return removed


def update(account_id: str, block_id: str, text: str) -> dict[str, Any]:
    """Rewrite one block in place: same id, new text, re-embedded by the server.
    Like deletion, it goes through the session that owns the block."""
    row = next((r for r in list_for_account(account_id, limit=200)
                if r["memory_id"] == block_id), None)
    if not row:
        return {}
    sid = row.get("session_id") or "default"
    if row.get("status", "").lower().endswith("failed"):
        return _replace(account_id, row, text)
    for wait in (1, 2, 3, 4, 5, 5, 5, 5, 0):     # ~30 s: re-embedding takes ~12 s idle
        try:
            _retrying(lambda: _session(account_id, sid).update_memory(block_id=block_id,
                                                                      fact=text))
            break
        except Exception as exc:
            # A block just written is re-embedded in the background, and the server
            # turns away edits until that's done. Setting the text again is safe.
            if "being processed" not in str(exc):
                raise
            if not wait:
                return _replace(account_id, row, text)
            time.sleep(wait)
    return {**row, "text": text}


def _replace(account_id: str, row: dict[str, Any], text: str) -> dict[str, Any]:
    """Edit a block the server has locked: write the new text as a fresh block,
    then delete the old one.

    When the server's background step fails on a block it marks it
    `extraction_failed` and keeps answering "currently being processed" to
    every edit until the container restarts — a memory the customer can't
    change. A new block has a new id; adding before deleting means a failed
    add loses nothing.
    """
    sid = row.get("session_id") or "default"
    new = remember(account_id, text, kind=row.get("kind", "fact"),
                   importance=row.get("importance", 0.5),
                   confidence=row.get("confidence", 0.8),
                   source=row.get("source", "conversation"), session_id=sid,
                   structured=row.get("structured"))
    if not new.get("memory_id"):
        raise RuntimeError("could not save the edited memory")
    forget(account_id, [row["memory_id"]], session_id=sid)
    log.info("memory %s was locked by the server; replaced it with %s",
             row["memory_id"], new["memory_id"])
    return {**row, "memory_id": new["memory_id"], "text": text, "status": "queued"}


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def recall(account_id: str, question: str, session_id: str = "default",
           k: int | None = None,
           floor: float | None = None) -> list[dict[str, Any]]:
    """Semantic recall across every session this account has ever had.

    `session_ids="all"` is the whole point: a preference set on Monday has to
    surface on Thursday, in a different browser session, or the memory story
    is just a conversation buffer.
    """
    res = _retrying(
        lambda: _session(account_id, session_id).search_memory(
            query=question, filters={"session_ids": "all"}))
    rows = [_to_row(b) for b in (getattr(res, "memory_blocks", None) or [])]
    # The server returns its top blocks whatever they score. Without a floor, "my back hurts
    # sitting at my desk" recalled six irrelevant blocks at 0.13-0.25 — the
    # payment terms among them — and one of them ended up quoted under a list
    # of chairs. A memory that is not about the question is not a recall.
    floor = settings.memory_threshold if floor is None else floor
    kept = [r for r in rows if r.get("_score") is None
            or float(r["_score"]) >= floor]

    # A second, word-match arm.
    # Measured here: "show me pen options" came back with the payment terms
    # and not the stored kit that begins "branded pens" — the server ranks by
    # its own embedding and the kit did not make the cut. A memory whose own
    # words the customer just used belongs in the answer, whatever the vector
    # thought. The server has no keyword endpoint, so the match runs over this
    # account's blocks, which are a couple of dozen at most.
    import re as _re
    from retail.memory import MEM_STOP as _MEM_STOP
    words = {w for w in _re.findall(r"[a-z]{3,}", (question or "").lower())
             if w not in _MEM_STOP}
    probes = {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}
    if probes:
        seen = {r["memory_id"] for r in kept}
        scored = []
        for row in list_for_account(account_id, limit=200,
                                    session_id=session_id):
            if row["memory_id"] in seen:
                continue
            text = str(row.get("text") or "").lower()
            hits = sum(1 for p_ in probes
                       if _re.search(rf"\b{_re.escape(p_)}(?:s|es)?\b", text))
            if hits:
                scored.append((hits, row))
        for _hits, row in sorted(scored, key=lambda x: -x[0]):
            kept.append({**row, "_matched": "words"})

    return kept[: (k or 6)]


def list_for_account(account_id: str, limit: int = 50,
                     session_id: str = "default") -> list[dict[str, Any]]:
    res = _retrying(lambda: _user(account_id).list_memories(
        session_ids="all", limit=min(int(limit), 200)))
    return [_to_row(b) for b in (getattr(res, "memory_blocks", None) or [])]


def wait_ready(account_id: str, block_ids: list[str], timeout: float = 20.0,
               session_id: str = "default") -> bool:
    """Block until the server has embedded and indexed what we just wrote.

    Writes are accepted asynchronously, so a store followed immediately by a
    search legitimately returns nothing. Demo scripts and tests wait; the app
    does not, because a customer stating a preference should not be made to
    watch a spinner while it indexes.
    """
    deadline = time.time() + timeout
    want = set(block_ids)
    while time.time() < deadline:
        ready = {r["memory_id"] for r in list_for_account(
            account_id, limit=200, session_id=session_id)
            if str(r.get("status", "")).lower().endswith("ready")}
        if want <= ready:
            return True
        time.sleep(0.5)
    return False
