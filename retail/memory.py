"""Agent Memory — what the store remembers about each customer.

A thin door onto the Agent Memory server (`memory_server`). Every read fails
soft: a server that stops answering costs the turn its memory, not the turn.
Every write also clears the customer's cached answers, because an answer built
on yesterday's preferences is wrong today.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from retail import memory_server

log = logging.getLogger(__name__)

KINDS = ("preference", "fact", "room", "issue")

# Words that carry no signal about which stored memory a question means.
MEM_STOP = {
    "the", "and", "for", "our", "your", "you", "with", "what", "when", "how", "can",
    "could", "would", "should", "need", "want", "have", "has", "are", "was", "were",
    "this", "that", "these", "those", "some", "any", "all", "from", "into", "out",
    "about", "please", "show", "give", "tell", "get", "buy", "new", "one", "two",
    "more", "most", "just", "like", "also", "there", "here", "does", "did", "me", "my",
    "we", "us", "it", "its", "is", "in", "on", "of", "to", "a", "an", "room", "office",
}

_last_error: str | None = None


def unavailable() -> str | None:
    """Why the last read from the Agent Memory server failed, if it did."""
    return _last_error


def _read(call, *args, **kwargs):
    global _last_error
    try:
        out = call(*args, **kwargs)
    except Exception as exc:
        _last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        log.error("Agent Memory read failed: %s", _last_error)
        return []
    _last_error = None
    return out


def _normalised(text: str) -> str:
    out = re.sub(r"^\s*(?:please\s+)?(?:remember(?:\s+that)?|note(?:\s+that)?)\b[\s,:-]*",
                 "", (text or "").strip(), flags=re.I)
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", out.lower()).split())


def _answers_changed(customer_id: str) -> None:
    try:
        from retail import semantic_cache
        semantic_cache.invalidate_customer(customer_id)
    except Exception as exc:
        log.warning("could not invalidate cached answers for %s: %s", customer_id, exc)


def remember(customer_id: str, text: str, kind: str = "preference",
             importance: float = 0.8, source: str = "conversation",
             structured: dict[str, Any] | None = None,
             session_id: str = "default") -> dict[str, Any]:
    want = _normalised(text)
    for row in list_for(customer_id):
        if _normalised(str(row.get("text") or "")) == want:
            return row                      # same thing said twice is one memory
    out = memory_server.remember(customer_id, text, kind=kind if kind in KINDS else "fact",
                                 importance=importance, source=source,
                                 structured=structured, session_id=session_id)
    _answers_changed(customer_id)
    return out


def update(customer_id: str, memory_id: str, text: str) -> dict[str, Any]:
    """Change what one memory says, keeping its id — unless the server has locked it,
    when it is saved as a new memory and the old one deleted. Empty text forgets it."""
    text = (text or "").strip()
    if not text:
        forget(customer_id, [memory_id])
        return {}
    row = next((r for r in list_for(customer_id, 200) if r.get("memory_id") == memory_id),
               None)
    if not row or _normalised(str(row.get("text") or "")) == _normalised(text):
        return row or {}
    out = memory_server.update(customer_id, memory_id, text)
    _answers_changed(customer_id)
    return out


def forget(customer_id: str, memory_ids: list[str]) -> int:
    ids = [m for m in memory_ids if m]
    if not ids:
        return 0
    n = memory_server.forget(customer_id, ids)
    _answers_changed(customer_id)
    return n


def list_for(customer_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return _read(memory_server.list_for_account, customer_id, limit=limit)


def recall(customer_id: str, question: str, k: int = 6) -> list[dict[str, Any]]:
    return _read(memory_server.recall, customer_id, question, k=k)


_BUDGET = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")
_PER_ITEM = re.compile(r"\b(?:one|single|each|per|any one|a single)\s+(?:item|thing|piece|"
                       r"product)\b")


def budget(customer_id: str | None) -> tuple[float, str] | None:
    """The spending budget the customer told us — (dollars, what they said) —
    newest first; None for a guest or when nothing says one."""
    if not customer_id or customer_id == "GUEST":
        return None
    try:
        rows = list_for(customer_id, 50)
    except Exception:
        return None
    rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)
    for r in rows:
        text = str(r.get("text") or "")
        low = text.lower()
        m = _BUDGET.search(text)
        if _PER_ITEM.search(low):
            continue                        # a limit on one item, not on the total
        if m and any(w in low for w in ("budget", "under", "keep the total", "spend")):
            return float(m.group(1).replace(",", "")), text
    return None


_SHIP_WORD = re.compile(r"\b(ship|shipping|shipped|deliver|delivery|delivered)\b", re.I)
_SPEEDS = [("nextday", re.compile(r"\b(next[\s-]?day|overnight|1[\s-]?day|one[\s-]?day)\b", re.I)),
           ("2day", re.compile(r"\b(2[\s-]?day|two[\s-]?day|2[\s-]?business[\s-]?day)", re.I)),
           ("standard", re.compile(r"\b(standard|regular|cheapest|slowest)\b", re.I))]


def shipping(customer_id: str | None) -> tuple[str, str] | None:
    """The shipping speed the customer told us — (offers.SHIPPING key, what they
    said) — newest first; None for a guest or when nothing says one."""
    if not customer_id or customer_id == "GUEST":
        return None
    try:
        rows = list_for(customer_id, 50)
    except Exception:
        return None
    rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)
    for r in rows:
        text = str(r.get("text") or "")
        if _SHIP_WORD.search(text):
            for speed, rx in _SPEEDS:
                if rx.search(text):
                    return speed, text
    return None


def preferences_text(customer_id: str, question: str = "") -> str:
    """The customer's remembered preferences as a short prompt block."""
    rows = recall(customer_id, question) if question else list_for(customer_id, 20)
    return "\n".join(f"- {r['text']}" for r in rows if r.get("text"))


def purge_conversation(customer_id: str) -> int:
    """Forget what this customer said in chat, keeping the seeded profile."""
    rows = [r for r in list_for(customer_id, 200)
            if r.get("source") in ("conversation", "ui")]
    return forget(customer_id, [r["memory_id"] for r in rows])
