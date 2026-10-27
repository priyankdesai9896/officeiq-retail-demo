"""Agent Catalog governance: which tools the store assistant may use, within
which limits — decided before anything runs, and every decision audited.

The registry lives in `aidp.agent_catalog`: one document per tool version
(owner, status, side effect, limits) and one agent profile listing its grants.
`authorize()` is the gate every tool call and every checkout passes through;
`audit()` writes the decision to `aidp.audit_log`.

Three deliberate moments for the demo:
* `issue_refund` is registered, owned by Payments, and NOT granted.
* `apply_manual_discount` is granted only at a deprecated version, so it is
  refused as `deprecated_version`.
* `checkout` is granted with an amount limit: above it the assistant can't place the
  order and hands off to the shopper, who reviews and pays in the Cart.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config import settings
from retail.cb import fq, kv_get, kv_upsert, query
from retail.trace import Trace

AGENT = "store_assistant"
AGENT_VERSION = "1.0.0"
SCOPE, COLL = "aidp", "agent_catalog"

TOOLS: list[dict[str, Any]] = [
    {"tool_name": "search_products", "version": "1.2.0", "owner": "Merchandising",
     "side_effect": "read", "description": "Find products by keyword and by meaning, "
     "with filters for kind, colour, category and price."},
    {"tool_name": "suggest_for_me", "version": "1.0.0", "owner": "Merchandising",
     "side_effect": "read", "description": "Suggest what goes with the customer's past "
     "orders and with what the conversation is about."},
    {"tool_name": "get_product", "version": "1.0.0", "owner": "Merchandising",
     "side_effect": "read", "description": "Look up one product's details, price and stock."},
    {"tool_name": "plan_room", "version": "1.1.0", "owner": "Design Services",
     "side_effect": "read", "status": "deprecated", "description": "Plan a room from its "
     "dimensions, type and headcount: layout, quantities and matching products."},
    {"tool_name": "plan_room", "version": "1.2.0", "owner": "Design Services",
     "side_effect": "read", "description": "Plan a room from its dimensions, type and "
     "headcount: layout, quantities and matching products — and, when moving rooms, "
     "what to bring and what to buy."},
    {"tool_name": "analyze_room_photo", "version": "1.0.0", "owner": "Design Services",
     "side_effect": "read", "description": "Read a room photo: room type, rough size, "
     "existing furniture, colours and style."},
    {"tool_name": "add_to_cart", "version": "1.0.0", "owner": "Commerce",
     "side_effect": "write", "description": "Add products to the customer's cart."},
    {"tool_name": "view_cart", "version": "1.0.0", "owner": "Commerce",
     "side_effect": "read", "description": "Show the cart with bundle savings applied."},
    {"tool_name": "checkout", "version": "1.0.0", "owner": "Commerce",
     "side_effect": "write", "limits": {"max_amount_usd": settings.approval_limit_usd},
     "description": "Place the order and charge the card on file."},
    {"tool_name": "search_kb", "version": "1.0.0", "owner": "Customer Care",
     "side_effect": "read", "description": "Answer store policy questions: delivery, "
     "returns, assembly, warranty, bundles."},
    {"tool_name": "save_preference", "version": "1.0.0", "owner": "Customer Care",
     "side_effect": "write", "description": "Remember a customer's preference for next time."},
    {"tool_name": "forget_preference", "version": "1.0.0", "owner": "Customer Care",
     "side_effect": "write", "description": "Forget a stored preference."},
    {"tool_name": "track_order", "version": "1.0.0", "owner": "Commerce",
     "side_effect": "read", "description": "Look up an order's status."},
    {"tool_name": "list_orders", "version": "1.0.0", "owner": "Commerce",
     "side_effect": "read", "description": "List the customer's orders, newest first."},
    {"tool_name": "apply_manual_discount", "version": "0.9.0", "owner": "Pricing",
     "side_effect": "write", "status": "deprecated",
     "description": "Apply an ad-hoc percentage discount to the cart."},
    {"tool_name": "apply_manual_discount", "version": "2.0.0", "owner": "Pricing",
     "side_effect": "write", "description": "Apply an approved promotion code to the cart."},
    {"tool_name": "issue_refund", "version": "1.0.0", "owner": "Payments",
     "side_effect": "write", "description": "Refund money to the customer's card."},
]

GRANTS = {  # tool -> version the assistant is pinned to
    "search_products": "1.2.0", "suggest_for_me": "1.0.0", "get_product": "1.0.0", "plan_room": "1.2.0",
    "analyze_room_photo": "1.0.0", "add_to_cart": "1.0.0", "view_cart": "1.0.0",
    "checkout": "1.0.0", "search_kb": "1.0.0", "save_preference": "1.0.0",
    "forget_preference": "1.0.0", "track_order": "1.0.0", "list_orders": "1.0.0",
    "apply_manual_discount": "0.9.0",   # pinned to the deprecated version
}


def tool_key(name: str, version: str) -> str:
    return f"TOOL::{name}::{version}"


def seed(with_embeddings: bool = True) -> int:
    """Write the registry and the agent profile."""
    docs = []
    for t in TOOLS:
        d = {"type": "catalog_tool", "status": "active", "limits": {}, **t,
             "registered_at": datetime.now(UTC).isoformat()}
        docs.append(d)
    if with_embeddings:
        from retail import embeddings
        vecs = embeddings.embed_documents([f"{d['tool_name']}: {d['description']}"
                                           for d in docs])
        for d, v in zip(docs, vecs, strict=True):
            d["embedding"] = v
    for d in docs:
        kv_upsert(SCOPE, COLL, tool_key(d["tool_name"], d["version"]), d)
    kv_upsert(SCOPE, COLL, f"AGENT::{AGENT}::{AGENT_VERSION}", {
        "type": "agent", "name": AGENT, "version": AGENT_VERSION,
        "description": "The store's shopping and room-planning assistant.",
        "grants": [{"tool_name": k, "version": v} for k, v in GRANTS.items()]})
    return len(docs)


POLICY_WORDS = {
    "tool_not_granted": "Blocked — this action belongs to another team",
    "deprecated_version": "Blocked — the approved version of this tool is retired",
    "tool_not_registered": "Blocked — not a registered tool",
    "version_missing": "Blocked — the approved version is missing",
}


@dataclass
class Decision:
    allowed: bool
    policy: str
    tool: str
    version: str | None = None
    owner: str | None = None
    hand_off: bool = False            # over the assistant's limit: the shopper pays
    message: str = ""
    limit: float | None = None        # the amount limit that applied


def get_agent() -> dict[str, Any]:
    return kv_get(SCOPE, COLL, f"AGENT::{AGENT}::{AGENT_VERSION}") or {}


def authorize(tool: str, args: dict[str, Any] | None = None, *,
              customer_id: str | None = None, session_id: str | None = None,
              trace: Trace | None = None) -> Decision:
    t0 = time.perf_counter()
    args = args or {}
    agent = get_agent()
    grants = {g["tool_name"]: g["version"] for g in agent.get("grants", [])}
    registered = query(f"SELECT t.tool_name, t.version, t.owner, t.status, t.limits "
                       f"FROM {fq(SCOPE, COLL)} AS t WHERE t.type = 'catalog_tool' "
                       f"AND t.tool_name = $n", n=tool)
    if not registered:
        d = Decision(False, "tool_not_registered", tool,
                     message=f"{tool} is not a registered tool.")
    elif tool not in grants:
        owner = registered[0].get("owner")
        d = Decision(False, "tool_not_granted", tool, registered[0]["version"], owner,
                     message=f"{tool} belongs to {owner} and is not granted to this "
                             f"assistant.")
    else:
        pinned = next((r for r in registered if r["version"] == grants[tool]), None)
        if not pinned:
            d = Decision(False, "version_missing", tool, grants[tool])
        elif pinned.get("status") == "deprecated":
            newest = max(r["version"] for r in registered)
            d = Decision(False, "deprecated_version", tool, pinned["version"],
                         pinned.get("owner"),
                         message=f"{tool} v{pinned['version']} is deprecated "
                                 f"(v{newest} is current); the assistant must be "
                                 f"re-granted before using it.")
        else:
            limit = (pinned.get("limits") or {}).get("max_amount_usd")
            amount = float(args.get("amount_usd") or 0)
            if limit and amount > limit:
                d = Decision(True, "amount_limit", tool, pinned["version"],
                             pinned.get("owner"), hand_off=True, limit=float(limit),
                             message=f"${amount:,.2f} is over the assistant's "
                                     f"${limit:,.0f} limit; the shopper places it in "
                                     f"the Cart.")
            else:
                d = Decision(True, "allowed", tool, pinned["version"], pinned.get("owner"))
    audit(d, args, customer_id=customer_id, session_id=session_id)
    if trace:
        verdict = (POLICY_WORDS.get(d.policy, d.policy) if not d.allowed else
                   "Over the assistant's limit — handed to the shopper" if d.hand_off else
                   "Allowed")
        what = ("Checked the order against the store's spending limit, and recorded "
                "the decision" if tool == "checkout" else
                f"Checked the assistant is allowed to "
                f"\u201c{tool.replace('_', ' ')}\u201d, and recorded the decision")
        if tool == "checkout" and d.allowed and not d.hand_off:
            verdict = "Allowed — within the limit"
        trace.add("Agent Catalog", what, verdict,
                  f"{tool}" + (f" v{d.version}" if d.version else "") + f" · policy {d.policy}"
                  + "; audit entry written", (time.perf_counter() - t0) * 1000,
                  ["aidp.agent_catalog"], writes=["aidp.audit_log"])
    return d


def audit(d: Decision, args: dict[str, Any], *, customer_id: str | None,
          session_id: str | None) -> None:
    safe = {k: v for k, v in args.items() if k not in ("card_number", "cvc")}
    kv_upsert("aidp", "audit_log", f"AUD::{uuid.uuid4().hex}", {
        "type": "audit_entry", "ts": datetime.now(UTC).isoformat(), "agent": AGENT,
        "agent_version": AGENT_VERSION, "tool": d.tool, "tool_version": d.version,
        "decision": "allow" if d.allowed else "deny", "policy": d.policy,
        "hand_off": d.hand_off, "customer_id": customer_id,
        "session_id": session_id, "args": safe})


def recent_audit(limit: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE a.session_id = $s" if session_id else ""
    params = {"s": session_id} if session_id else {}
    return query(f"SELECT a.* FROM {fq('aidp', 'audit_log')} AS a {where} "
                 f"ORDER BY a.ts DESC LIMIT {int(limit)}", consistent=True, **params)
