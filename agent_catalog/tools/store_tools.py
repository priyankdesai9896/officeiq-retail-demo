"""The store assistant's tools, published to the Couchbase Agent Catalog (Tools Hub).

These are thin wrappers. The work is done in `retail/`; what lives here is what
the catalog holds: the name, the description that is embedded for search by
meaning, the typed signature, and annotations (owner, version, side effect,
status) taken from the policy registry in `retail/governance.py`.

The customer and session are not parameters. They come from
`retail.agent_context.CURRENT`, set by the app for each request, so a model
can't ask for another customer's cart or orders.

The store assistant (prompt `store_assistant`) is given six of these and the
chat model chooses which to call. Each of those six passes the policy check
first: registering a tool doesn't grant it. `issue_refund` is registered and
owned by Payments, but not granted, so `retail.governance.authorize()` refuses
it — and writes every decision to `aidp.audit_log`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentc.catalog import tool  # noqa: E402

from retail.agent_context import (question as _question, set_shown as _set_shown,  # noqa: E402
                                  shown as _shown, trace as _trace, who as _who)


def _refused(name: str, args: dict) -> dict | None:
    """The policy check every assistant tool passes first. -> an error, or None."""
    from retail import governance
    cid, sid = _who()
    d = governance.authorize(name, args, customer_id=cid, session_id=sid, trace=_trace())
    return None if d.allowed else {"error": "not allowed", "policy": d.policy,
                                   "message": d.message or "This assistant may not do that."}


@tool(annotations={"owner": "Merchandising", "version": "1.7.0", "side_effect": "read",
                   "status": "active"})
def search_products(text: str, max_price: float = 0.0) -> dict:
    """Find products in the store — desks, chairs, printers, ink, whiteboards and more —
    by what they are, colour or style. Optionally only those under max_price dollars.
    Returns up to 5 different products with their sku, name, price and rating, in the
    colours and styles the customer told us they like, and also_suggest: what they may
    need with them. The customer sees them as product cards, so the answer shouldn't list
    them again."""
    if (no := _refused("search_products", {"text": text, "max_price": max_price})):
        return no
    if not set(re.findall(r"[a-z0-9]+", (text or "").lower())) - _VAGUE:
        return {"error": "no search words",            # never a dump of the catalogue
                "message": "Nothing to search for. If the customer told you a preference "
                           "(a budget, a limit, a colour), call save_preference with their "
                           "words. Otherwise ask what they're looking for, or call "
                           "suggest_for_me for ideas from their orders."}
    from retail import companions, memory, search
    cid = _who()[0]
    h = companions.hints(cid, trace=_trace())
    cap = min(filter(None, [max_price or None, h.get("max_item")]), default=None)
    found = search.hybrid(text, limit=20, max_price=cap, trace=_trace())
    found = companions.prefer(companions.with_liked(found, h, max_price=cap),
                              h, text, max_price=cap, trace=_trace())[:5]
    _set_shown([p["sku"] for p in found])
    out = {"products": [{"sku": p["sku"], "name": p["name"], "price": p["price"],
                         "rating": p.get("rating"),
                         "why": f"matches “{text}”" + (", the kind you told us you prefer"
                                                        if companions._liked(p, h) else "")}
                        for p in found]}
    if found:
        out["shown_to_customer"] = ("These products are shown to the customer as cards "
                                    "with photos, prices and an Add to cart button. Don't "
                                    "list them: in one or two sentences, say what you found "
                                    "and the price range, then ask which they'd like. Don't mention "
                                    "the cards or field names. Don't add "
                                    "anything to the cart until they choose.")
    gw = companions.related(found, h, cid, trace=_trace())
    if gw:
        out["also_suggest"] = [_goes(g) for g in gw]
        out["shown_to_customer"] += (" also_suggest is shown as cards too: suggest it in one "
                                     "short sentence, with its reason. It is not included "
                                     "and not in the cart.")
    if h.get("max_item") and cap == h["max_item"]:
        out["per_item_limit"] = (f"Kept under the ${h['max_item']:,.0f} per item the customer "
                                 f"told us — say so in a few words.")
    if h.get("max_desk_w") and any(p.get("kind") == "desk" for p in found):
        out["desk_width_limit"] = (f"Only desks up to {h['max_desk_w']}″ wide, as the "
                                   f"customer told us — say so in a few words.")
    told = memory.budget(cid)
    if told:
        over = [p["name"] for p in found if p["price"] > told[0]]
        out["customer_budget"] = told[0]
        if over:
            out["note"] = (f"The customer told us their budget is ${told[0]:,.0f}; "
                           f"{len(over)} of these cost more — say so.")
    return out


def _goes(g: dict) -> dict:
    p = g["product"]
    return {"sku": p["sku"], "name": p["name"], "price": p["price"], "why": g["why"],
            **({"because_you_told_us": g["memory"]} if g.get("memory") else {}),
            **({"from_order": g["from_order"]} if g.get("from_order") else {})}


# Words that don't say what to look for: a search for them is a catalogue dump.
_VAGUE = {"item", "items", "product", "products", "something", "anything", "stuff",
          "things", "thing", "some", "any", "more", "options", "other", "else", "the", "a",
          "an", "for", "me", "my", "show", "find", "get", "all", "everything", "good",
          "best", "nice", "new", "you", "have", "what", "do", "i", "need", "want", "to"}


@tool(annotations={"owner": "Merchandising", "version": "1.0.1", "side_effect": "read",
                   "status": "active"})
def suggest_for_me(about: str = "") -> dict:
    """Suggest products for this customer: what goes with what they've bought before
    (their past orders) and with what the conversation is about. about is the topic,
    like "chair" or "printer"; leave it empty to use the products just discussed.
    Each suggestion says why — which past order or preference it comes from."""
    if (no := _refused("suggest_for_me", {"about": about})):
        return no
    from retail import companions, search
    cid = _who()[0]
    if cid == "GUEST":
        return {"products": [], "message": "Sign in and we can suggest from your orders."}
    h = companions.hints(cid, trace=_trace())
    focus = [p for p in (search.get_product(s) for s in _shown()) if p]
    picks = companions.for_me(cid, h, focus, about, trace=_trace())
    if not picks:
        return {"products": [], "message": "No suggestions yet — nothing to go with your "
                                           "orders or this conversation."}
    _set_shown([g["product"]["sku"] for g in picks])
    return {"products": [_goes(g) for g in picks],
            "shown_to_customer": "These are shown to the customer as cards, each with its "
                                 "reason. Don't list them or mention the cards: in one or two "
                                 "sentences, say they go with what they bought or are "
                                 "looking at."}


# A small model sometimes adds the top search result on its own.
_ASKED_TO_ADD = re.compile(r"\b(add|buy|cart|basket|order|take|purchase|"
                           r"yes|yeah|sure|ok|okay|please|one|it|that|this)\b", re.I)

_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
             "fourth": 3, "4th": 3, "fifth": 4, "5th": 4}
_GENERIC = {"with", "and", "the", "for", "of", "in", "x", "w", "d", "chair", "desk",
            "one", "inch", "pack"}


def _from_shown(want: str) -> dict | None:
    """'the white one', 'the second one', 'the mesh one' — one of the products
    just shown, in the colour named. None when the words name something else."""
    from retail import companions, search
    shown = [p for p in (search.get_product(s) for s in _shown()) if p]
    if not shown:
        return None
    low = want.lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    pick = None
    m = re.search(r"#\s*(\d)|\bnumber\s+(\d)\b", low)
    idx = (int(m.group(1) or m.group(2)) - 1 if m else
           next((i for w, i in _ORDINALS.items() if w in words), None))
    if "last" in words and idx is None:
        idx = len(shown) - 1
    if idx is not None and 0 <= idx < len(shown):
        pick = shown[idx]
    if pick is None:                           # by its name: "the mesh task one"
        def score(p):
            name = set(re.findall(r"[a-z0-9]+", p.get("base_name", p["name"]).lower()))
            return len((name - _GENERIC) & words)
        best = max(shown, key=score)
        if score(best) and sum(score(p) == score(best) for p in shown) == 1:
            pick = best
    kinds = search.kinds_named(want)
    if pick is not None and kinds and pick.get("kind") not in kinds:
        pick = None                            # "whiteboard eraser" isn't the whiteboard
    colour = companions.colour_named(want)
    if pick is None:
        refers = bool(words & {"one", "ones", "it", "that", "this", "those", "them"})
        same = [p for p in shown if not kinds or p.get("kind") in kinds]
        if not same or not (refers or kinds or colour):
            return None
        pick = next((p for p in same if colour in [c["color"] for c in
                                                   p.get("colors_available", [])]),
                    same[0]) if colour else same[0]
    sku = next((c["sku"] for c in pick.get("colors_available", []) if c["color"] == colour),
               None) if colour else None
    return (search.get_product(sku) if sku and sku != pick["sku"] else None) or pick


@tool(annotations={"owner": "Merchandising", "version": "1.0.0", "side_effect": "read",
                   "status": "active"})
def get_product(sku: str) -> dict:
    """Look up one product's details, price, colours and stock by its SKU."""
    from retail import search
    return search.get_product(sku) or {}


@tool(annotations={"owner": "Design Services", "version": "1.2.0", "side_effect": "read",
                   "status": "active"})
def plan_room(room_type: str, length_ft: float, width_ft: float, people: int,
              bring: str = "") -> dict:
    """Plan an office room from its type, size and headcount: the layout, how many
    of each item, and the matching products with bundle prices. When the customer
    is moving from another room, pass what they're bringing (e.g. "2 chairs, a desk,
    a TV"): each item then says how many they bring and how many to buy."""
    from retail import moving, planner
    cid, sid = _who()
    p = planner.plan(planner.RoomSpec(room_type, length_ft * 12, width_ft * 12, people),
                     customer_id=cid, session_id=sid)
    d = moving.delta(p.lines, moving.owned_in(bring)) if bring else None
    out = {"headline": p.headline("ft"), "seats": p.seats,
           "items": [{"sku": line.product["sku"], "qty": line.qty} for line in p.lines]}
    if d:
        for it in out["items"]:
            it["bring"] = d.bring.get(it["sku"], 0)
            it["buy"] = it["qty"] - it["bring"]
        out["not_needed"] = moving.words(d.spare) if d.spare else ""
    return out


@tool(annotations={"owner": "Design Services", "version": "1.0.0", "side_effect": "read",
                   "status": "active"})
def analyze_room_photo(image_path: str) -> dict:
    """Read a photo of a room: the room type, its estimated size, the floor, the
    colours and the furniture already there."""
    from retail import vision
    cid, sid = _who()
    return vision.analyze_room_photo(Path(image_path).read_bytes(), customer_id=cid,
                                     session_id=sid)


@tool(annotations={"owner": "Commerce", "version": "1.4.0", "side_effect": "write",
                   "status": "active"})
def add_to_cart(product: str, qty: int = 1) -> dict:
    """Add a product to the customer's cart. product is what the customer asked for —
    a name or description like "whiteboard" or "black mesh chair", a sku, or a
    reference to a product just shown, by its colour or position. The
    best match is added. Returns also_suggest: what it may need, shown to the customer."""
    if (no := _refused("add_to_cart", {"product": product, "qty": qty})):
        return no
    if not _ASKED_TO_ADD.search(_question() or "add"):
        return {"not_added": True, "message": "The customer hasn't asked to add anything "
                "yet, so nothing was added. Ask which one they'd like."}
    from retail import commerce, companions, memory, search
    want = product.strip()
    p = (search.get_product(want.upper(), trace=_trace())
         if re.fullmatch(r"[A-Za-z]{3}-\d{4}-[A-Za-z]{4}", want) else
         # the customer's own words first: a small model rewrites "white one"
         _from_shown(_question()) or _from_shown(want)
         or next(iter(search.hybrid(want, limit=1, trace=_trace())), None))
    if not p:
        return {"error": f"no product matches {product!r}"}
    cid = _who()[0]
    qty = max(1, int(qty or 1))
    commerce.add(cid, p["sku"], qty, trace=_trace(), source="assistant")
    out = {"added": qty, "sku": p["sku"], "name": p["name"], "price": p["price"],
           "say": f"Added {qty} × {p['name']} (${p['price']:,.2f}). Name exactly this "
                  f"product — it may not be the one in your call."}
    gw = companions.related([p], companions.hints(cid), cid, trace=_trace())
    if gw:
        out["also_suggest"] = [_goes(g) for g in gw]
        out["shown_to_customer"] = ("also_suggest is shown to the customer as cards: suggest "
                                    "it in one short sentence, with its reason. It is not "
                                    "included and not in the cart.")
    told = memory.budget(cid)
    if told:
        total = commerce.view(cid)["total"]
        if total > told[0]:
            out["note"] = (f"The cart is now ${total:,.2f}, over the ${told[0]:,.0f} budget "
                           f"the customer told us — tell them.")
    return out


@tool(annotations={"owner": "Commerce", "version": "1.0.0", "side_effect": "read",
                   "status": "active"})
def view_cart() -> dict:
    """Show what is in the customer's cart, with bundle savings and the total."""
    if (no := _refused("view_cart", {})):
        return no
    from retail import commerce
    c = commerce.view(_who()[0], trace=_trace())
    return {"items": [{"name": line["product"]["name"], "qty": line["qty"],
                       "price": line["product"]["price"]} for line in c["lines"]],
            "savings": round(c["discount"], 2), "total": round(c["total"], 2)}


@tool(annotations={"owner": "Commerce", "version": "1.0.0", "side_effect": "write",
                   "status": "active", "limit": "max_amount_usd"})
def checkout(card_number: str, exp: str, cvc: str, confirm_over_budget: bool = False) -> dict:
    """Place the order and charge the card. The assistant can't place an order over
    its spending limit: then tell the customer their cart is ready to review and pay
    in the Cart. An order over the budget the customer told us is not placed until
    they confirm: set confirm_over_budget only when they say to order anyway."""
    from retail import commerce
    cid, sid = _who()
    res = commerce.checkout(cid, {"number": card_number, "exp": exp, "cvc": cvc},
                            session_id=sid, over_budget_ok=bool(confirm_over_budget),
                            by_agent=True)
    if res["status"] == "handoff":
        res["tell_customer"] = res["message"]
    return res


@tool(annotations={"owner": "Customer Care", "version": "1.0.0", "side_effect": "read",
                   "status": "active"})
def search_kb(question: str) -> dict:
    """Look up the store's help articles — delivery, returns, warranty, payment,
    assembly, bundles — for a policy question. Returns the closest articles."""
    if (no := _refused("search_kb", {"question": question})):
        return no
    from retail import assistant
    return {"articles": assistant.kb_articles(question, trace=_trace())}


# "I can't spend more than $400 on one item", "we prefer walnut", "I have back pain":
# the customer telling us something to keep — not a question or a request.
_STATES_PREFERENCE = re.compile(
    r"\b(?:preference|prefer|prefers|like|likes|love|hate|don't like|can't|cannot|won't|"
    r"never|always|budget|no more than|at most|under \$|i have|i've got|we have|our "
    r"(?:office|room|floor|team)|my (?:room|floor|office|back|budget)|remember)\b", re.I)


@tool(annotations={"owner": "Customer Care", "version": "1.2.1", "side_effect": "write",
                   "status": "active"})
def save_preference(text: str) -> dict:
    """Remember something the customer tells us about themselves — a budget or price
    limit, colours, style, their room, their health (a bad back), equipment — in Agent
    Memory, so search, suggestions and the room planner use it from now on. Also when
    they change one ("my budget is now $2,000"): the old memory is updated. text is the
    preference in the customer's own words."""
    if (no := _refused("save_preference", {"text": text})):
        return no
    cid, sid = _who()
    if cid == "GUEST":
        return {"not_saved": True, "message": "Only signed-in customers have a memory — "
                                              "ask them to sign in (top right)."}
    said = _question() or text
    if not _STATES_PREFERENCE.search(said):
        return {"not_saved": True, "message": "The customer didn't state a preference, so "
                                              "nothing was saved."}
    from retail import memory, planner, search
    words = re.sub(r"^\s*(?:preference|remember( that)?)\s*[-:–]?\s*", "", said,
                   flags=re.I).strip() or text
    words = words[0].upper() + words[1:]
    palette = search.colors()

    def sets(t: str) -> set[str]:
        return set(planner.hints_from_texts([t], palette)) - {"used"}
    new = sets(words)
    # A memory that says only what this one now says differently is replaced, in
    # place; one that also says other things ("walnut and a black chair") is kept.
    old = next((r for r in memory.list_for(cid, 50) if r.get("text") and new
                and sets(r["text"]) and sets(r["text"]) <= new), None) if new else None
    if old:
        out = memory.update(cid, old["memory_id"], words)
        return {"updated": words, "was": old["text"],
                "memory_id": out.get("memory_id") or old["memory_id"],
                "tell_customer": f"Done — I've updated what I remember: “{old['text']}” is "
                                 f"now “{words}”. I'll use it from now on."}
    row = memory.remember(cid, words, kind="preference",
                          source="conversation", session_id=sid or "default")
    return {"saved": words, "memory_id": row.get("memory_id"),
            "tell_customer": f"Got it — I'll remember “{words}” and use it from now on."}


@tool(annotations={"owner": "Customer Care", "version": "1.0.0", "side_effect": "write",
                   "status": "active"})
def forget_preference(memory_id: str) -> int:
    """Forget one of the customer's stored preferences."""
    from retail import memory
    return memory.forget(_who()[0], [memory_id])


@tool(annotations={"owner": "Commerce", "version": "1.0.0", "side_effect": "read",
                   "status": "active"})
def track_order(order_id: str = "") -> dict:
    """Look up one of the customer's orders: its status, items, total, shipping and
    when it arrives. Leave order_id empty for their latest order."""
    if (no := _refused("track_order", {"order_id": order_id})):
        return no
    from retail import commerce
    cid = _who()[0]
    if cid == "GUEST":
        return {"error": "the customer is not signed in, so there are no orders to show"}
    o = (commerce.get_order(order_id.strip()) if order_id.strip()
         else next(iter(commerce.orders_for(cid, 1)), None))
    if not o or o.get("customer_id") != cid:
        return {"error": "no such order for this customer"}
    if (t := _trace()):
        t.add("Data (KV)" if order_id.strip() else "Query (SQL++)",
              "Read the customer's order", f"{o['order_id']} · {o['status']}",
              "KV get" if order_id.strip() else "SQL++ SELECT … ORDER BY created_at DESC "
              "LIMIT 1", 0, ["commerce.orders"])
    return _order_summary(o)


def _order_summary(o: dict) -> dict:
    """One order as the assistant tells it: when, what, how much, how it ships."""
    from datetime import date, datetime
    try:
        placed = datetime.fromisoformat(str(o["created_at"])).strftime("%B %-d, %Y")
    except (KeyError, ValueError):
        placed = "unknown"
    sh = o.get("shipping") or {"label": "Standard"}
    out = {"order_id": o["order_id"], "status": o["status"], "placed_on": placed,
           "total_usd": o.get("total"),
           "items": [f"{x['qty']} × {x['name']}" for x in o.get("lines", [])],
           "shipping": sh["label"] + (" (the customer's saved preference)"
                                      if sh.get("from_memory") else "")}
    if o.get("arrives_by"):
        out["arrives_by"] = date.fromisoformat(o["arrives_by"]).strftime("%A, %B %-d")
    return out


@tool(annotations={"owner": "Commerce", "version": "1.0.0", "side_effect": "read",
                   "status": "active"})
def list_orders(limit: int = 10) -> dict:
    """List the customer's orders, newest first: each one's id, date, status, items,
    total and shipping. Use it for "my orders", "order history" or "what have I
    ordered"; use track_order for one order."""
    if (no := _refused("list_orders", {"limit": limit})):
        return no
    from retail import commerce
    cid = _who()[0]
    if cid == "GUEST":
        return {"error": "the customer is not signed in, so there are no orders to show"}
    limit = max(1, min(int(limit or 10), 20))
    rows = commerce.orders_for(cid, limit)
    if (t := _trace()):
        t.add("Query (SQL++)", "Read the customer's orders",
              f"{len(rows)} order{'s' if len(rows) != 1 else ''}",
              f"SQL++ SELECT … WHERE customer_id ORDER BY created_at DESC LIMIT {limit}",
              0, ["commerce.orders"])
    return {"count": len(rows), "orders": [_order_summary(o) for o in rows]}


@tool(annotations={"owner": "Pricing", "version": "2.0.0", "side_effect": "write",
                   "status": "active"})
def apply_manual_discount(promo_code: str) -> dict:
    """Apply an approved promotion code to the cart. (The assistant is pinned to the
    retired 0.9.0, so the policy check refuses it.)"""
    return {"applied": False, "reason": "refused by policy before it runs"}


@tool(annotations={"owner": "Payments", "version": "1.0.0", "side_effect": "write",
                   "status": "active"})
def issue_refund(order_id: str = "", amount_usd: float = 0.0) -> dict:
    """Refund money for an order to the customer's card. Leave order_id empty for
    their latest order, and amount_usd 0 to refund all of it."""
    if (no := _refused("issue_refund", {"order_id": order_id, "amount_usd": amount_usd})):
        return no
    return {"refunded": False, "reason": "refunds are handled by Payments"}