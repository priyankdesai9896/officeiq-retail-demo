"""Who a tool call is for: the customer, the session and the request's trace.

Set by the app around each request; read by the tools in
agent_catalog/tools/store_tools.py. The model never passes a customer id, so it
can't ask for another customer's cart or orders.
"""

from __future__ import annotations

import contextvars
from typing import Any

CURRENT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "store_tool_context", default={})


def who() -> tuple[str, str | None]:
    ctx = CURRENT.get()
    return ctx.get("customer_id") or "GUEST", ctx.get("session_id")


def trace():
    return CURRENT.get().get("trace")


def shown() -> list[str]:
    """The SKUs of the products the customer was just shown, in order — so
    "add the white one" means one of those."""
    return list(CURRENT.get().get("shown") or [])


def set_shown(skus: list[str]) -> None:
    ctx = CURRENT.get()
    if ctx:                                    # never the shared default
        ctx["shown"] = list(skus)


def question() -> str:
    """What the customer asked this turn."""
    return str(CURRENT.get().get("question") or "")
