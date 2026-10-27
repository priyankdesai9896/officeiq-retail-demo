"""What we remember shapes what the store shows.

`prefer` puts the products a customer told us they like first, in the colours
they like. `goes_with` adds what those products need given what we remember —
a chair mat with a rolling chair on a carpeted floor, a dual arm with a desk for
someone on two monitors, the ink that fits a printer — so nobody has to ask.

Used by the assistant's search_products and add_to_cart tools and by the Shop
search. The hints come from `planner.room_hints`, the same reading of Agent
Memory the Room Planner uses.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from retail import search
from retail.trace import Trace


def hints(customer_id: str | None, trace: Trace | None = None) -> dict[str, Any]:
    """What we remember about the customer, as planner settings; nothing for a guest."""
    if not customer_id or customer_id == "GUEST":
        return {"used": []}
    from retail import planner
    return planner.room_hints(customer_id, search.colors(), trace=trace)


def colour_named(text: str, palette: list[str] | None = None) -> str | None:
    low = f" {(text or '').lower()} "
    for c in sorted(palette or search.colors(), key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(c.lower())}(?![a-z])", low):
            return c
    return None


def rolling(p: dict[str, Any]) -> bool:
    """A chair on casters — the planner's test: not a guest or meeting chair."""
    return p.get("kind") == "chair" and p.get("use") not in ("guest", "meeting")


def said(h: dict[str, Any], *words: str) -> str | None:
    """The memory a hint came from, found by its words."""
    return next((t for t in h.get("used", []) if any(w in t.lower() for w in words)), None)


def _liked(p: dict[str, Any], h: dict[str, Any]) -> bool:
    name = p.get("name", "").lower()
    return bool((h.get("ergonomic") and p.get("kind") == "chair"
                 and ("ergonomic" in name or "mesh" in name or "lumbar" in name))
                or (h.get("standing") and p.get("kind") == "desk" and "standing" in name)
                or (h.get("style") and p.get("style") == h["style"]
                    and p.get("kind") in FURNITURE))


FURNITURE = {"desk", "chair", "storage", "conference_table", "side_table", "lamp"}


def _fits(p: dict[str, Any], h: dict[str, Any], max_price: float | None,
          price: bool = True) -> bool:
    """What the customer told us rules out: desks too wide for the room, and —
    unless price is False — prices over their per-item limit."""
    if h.get("max_desk_w") and p.get("kind") == "desk" and \
            (p.get("dims") or {}).get("w", 0) > h["max_desk_w"]:
        return False
    cap = min(filter(None, [max_price, h.get("max_item")]), default=None)
    return not price or not cap or p.get("price", 0) <= cap


def item_cap(h: dict[str, Any], max_price: float | None = None
             ) -> tuple[float | None, str | None]:
    """The most one product should cost, and why: a price they typed, the per-item
    limit we remember, or else the whole budget we remember."""
    remembered = h.get("max_item") or h.get("budget")
    if max_price and (not remembered or max_price <= remembered):
        return max_price, f"your max price is ${max_price:,.0f}"
    if not remembered:
        return None, None
    words = ("single", "item", "each", "per") if h.get("max_item") else ("budget", "total")
    told = said(h, *words)
    what = "per-item limit" if h.get("max_item") else "budget"
    return remembered, (f"your {what} is ${remembered:,.0f}"
                        + (f" (you told us: “{told}”)" if told else ""))


def split(products: list[dict[str, Any]], cap: float | None
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Within the cap first, then the rest — each in the order given. Prices vary
    by colour, so a product over the cap in one colour moves up in the cheapest
    colour that fits; it's only over budget when no colour fits."""
    if not cap:
        return products, []
    over = [p for p in products if p.get("price", 0) > cap]
    others = [c["sku"] for p in over for c in p.get("colors_available", [])
              if c["sku"] != p["sku"]]
    docs = search.get_products([f"PROD::{s}" for s in others]) if others else {}
    within, still = [], []
    for p in products:
        if p.get("price", 0) <= cap:
            within.append(p)
            continue
        fits = [v for c in p.get("colors_available", [])
                if (v := docs.get(f"PROD::{c['sku']}")) and v["price"] <= cap]
        if fits:
            within.append(min(fits, key=lambda v: v["price"]))
        else:
            still.append(p)
    return within, still


def _rank(p: dict[str, Any], h: dict[str, Any]) -> int:
    """0 first: lumbar support for a bad back, then the rest of what they like."""
    if h.get("ergonomic") and p.get("kind") == "chair" and "lumbar" in p.get("name", "").lower():
        return 0
    return 1 if _liked(p, h) else 2


LIKED_SEARCH = [("ergonomic", "chair", "ergonomic mesh chair lumbar support"),
                ("standing", "desk", "electric standing desk")]


def with_liked(products: list[dict[str, Any]], h: dict[str, Any], *,
               max_price: float | None = None) -> list[dict[str, Any]]:
    """A search for chairs, for someone who told us they want ergonomic ones,
    also brings those — they may not rank in the top results for the words."""
    extra: list[dict[str, Any]] = []
    for key, kind, words in LIKED_SEARCH:
        if h.get(key) and any(p.get("kind") == kind for p in products):
            cap = min(filter(None, [max_price, h.get("max_item")]), default=None)
            extra += search.hybrid(words, kind=kind, limit=6, max_price=cap)
    return extra + products if extra else products


def prefer(products: list[dict[str, Any]], h: dict[str, Any], text: str = "", *,
           max_price: float | None = None, keep_over: bool = False,
           trace: Trace | None = None) -> list[dict[str, Any]]:
    """One entry per product, each in the colour asked for — else the colour we
    remember — and what the customer told us they like first. keep_over keeps
    what's over their price limit (the Shop lists it after the rest)."""
    t0 = time.perf_counter()
    named = colour_named(text)
    out, seen, want = [], set(), {}
    for p in products:
        if p["base_id"] in seen:
            continue
        seen.add(p["base_id"])
        colour = named or (h.get("chair_color") if p.get("kind") == "chair" else None) \
            or h.get("color")
        sku = next((c["sku"] for c in p.get("colors_available", [])
                    if c["color"] == colour), None) if colour else None
        if sku and sku != p["sku"]:
            want[len(out)] = sku
        out.append(p)
    docs = search.get_products([f"PROD::{s}" for s in want.values()])
    recoloured = 0
    for i, sku in want.items():
        v = docs.get(f"PROD::{sku}")
        if v and (not max_price or v["price"] <= max_price):
            out[i] = v
            recoloured += 1
    dropped = len(out)
    out = [p for p in out if _fits(p, h, max_price, price=not keep_over)]
    dropped -= len(out)
    cap = item_cap(h, max_price)[0] if keep_over else None
    over = sum(1 for p in out if cap and p.get("price", 0) > cap)
    liked = [p for p in out if _liked(p, h)]
    out = sorted(out, key=lambda p: _rank(p, h))         # stable: search order kept
    if trace and h.get("used") and (recoloured or liked or dropped or over):
        trace.add("App logic", "Put what you like first",
                  ", ".join(filter(None, [
                      f"{recoloured} shown in the colour we remember" if recoloured else "",
                      f"{len(liked)} you said you prefer, first" if liked else "",
                      f"{dropped} left out — too wide or over your per-item limit"
                      if dropped else "",
                      f"{over} above your budget, listed after the rest" if over else ""])),
                  "Agent Memory hints applied to the ranking",
                  (time.perf_counter() - t0) * 1000)
    return out


# What goes with what. (does it apply — always, or because of what we remember;
# which products need it; the kind to suggest; the memory's words; why, about
# the product that needs it.) A new rule is one line.
Rule = tuple[Callable[[dict], bool], Callable[[dict], bool], str, tuple[str, ...], str]
_always: Callable[[dict], bool] = lambda h: True
_kind = lambda *kinds: (lambda p: p.get("kind") in kinds)  # noqa: E731
RULES: list[Rule] = [
    (lambda h: bool(h.get("carpet")), rolling, "chair_mat", ("carpet",),
     "your floor is carpeted — a mat under the {name} protects it"),
    (lambda h: bool(h.get("video_calls")), _kind("conference_table"), "camera", ("video",),
     "every conference room needs video calls, you told us — a camera for the {name}'s room"),
    (lambda h: h.get("monitors", 0) >= 2, _kind("desk", "monitor"), "monitor_arm",
     ("monitor",), "you use two monitors — a dual arm holds both and frees the desk"),
    (_always, _kind("printer"), "cartridge", (), "ink that fits the {name}"),
    (_always, _kind("printer"), "paper", (), "paper for the {name}"),
    (_always, _kind("whiteboard"), "dry_erase_marker", (), "markers for the {name}"),
    (_always, _kind("whiteboard"), "whiteboard_eraser", (), "an eraser for the {name}"),
    (_always, _kind("tv", "monitor"), "hdmi_cable", (), "a cable to connect the {name}"),
    (_always, _kind("desk"), "lamp", (), "task light for the {name}"),
    (_always, _kind("desk"), "organizer", (), "an organizer for the {name}"),
]

# One per product that needs it — a new chair needs its own mat, a new conference
# room its own camera, a printer its ink — even when they've bought one before
EACH = {"cartridge", "chair_mat", "camera"}

# Kinds that belong together, so a conversation about one brings the others —
# paper reminds us of the printer they bought, and its ink.
TOPICS = [{"printer", "cartridge", "paper"},
          {"whiteboard", "dry_erase_marker", "whiteboard_eraser", "whiteboard_cleaner"},
          {"chair", "chair_mat"},
          {"desk", "lamp", "organizer", "monitor_arm", "monitor_riser", "monitor"},
          {"tv", "monitor", "hdmi_cable", "video_bar", "speaker"}]


def topic_of(kinds: set[str]) -> set[str]:
    out = set(kinds)
    for t in TOPICS:
        if t & kinds:
            out |= t
    return out


def _pick(kind: str, h: dict[str, Any], for_product: dict[str, Any] | None = None
          ) -> dict[str, Any] | None:
    if kind == "cartridge":                    # only the ink that fits that printer
        fam = (for_product or {}).get("cartridge_family")
        inks = search.compatible_cartridges(fam) if fam else []
        return next((c for c in inks if "combo" in c["name"].lower()),
                    inks[0] if inks else None)
    rows = search.browse(kind=kind, sort="rating", limit=20)
    if not rows:
        return None
    best = rows[0]
    colour = h.get("color")
    return next((r for r in rows if r["base_id"] == best["base_id"]
                 and r["color"] == colour), best)


# A nudge's one-click pick: an everyday office chair, desk or desk lamp — not a stool,
# a benching workstation or a floor lamp
NUDGE_NAME = {"chair": re.compile(r"\b(?:task|mesh|ergonomic)\b", re.I),
              "lamp": re.compile(r"\bdesk\b|task light", re.I)}
NUDGE_SUB = {"desk": "Desks"}


def for_nudge(add: list[dict[str, Any]], lines: list[dict[str, Any]], h: dict[str, Any]
              ) -> list[tuple[dict[str, Any], int]]:
    """What a cart nudge adds in one click -> [(product, qty)]: more of the chair
    already in the cart, the ink that fits its printer, else the best-rated of
    the kind in a colour they told us they like."""
    in_cart = {ln["product"]["sku"]: ln["product"] for ln in lines}
    out = []
    for a in add:
        kind, qty = a["kind"], int(a["qty"])
        if a.get("like") in in_cart:
            p = in_cart[a["like"]]
        elif kind == "cartridge":
            p = _pick("cartridge", h, in_cart.get(a.get("for")))
        else:
            rows = search.browse(kind=kind, subcategory=NUDGE_SUB.get(kind), sort="rating",
                                 limit=60)
            rows = [r for r in rows if NUDGE_NAME.get(kind, re.compile("")).search(r["name"])]
            if not rows:
                continue
            colour = h.get("chair_color" if kind == "chair" else "color")
            p = next((r for r in rows if r["base_id"] == rows[0]["base_id"]
                      and r["color"] == colour), rows[0])
        if p and qty > 0:
            out.append((p, qty))
    return out


def _day(ts: str) -> str:
    try:
        from datetime import datetime
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return f"{d:%b} {d.day}"
    except Exception:
        return ts[:10]


def bought(customer_id: str | None, limit: int = 10) -> list[dict[str, Any]]:
    """-> [{product, date, order_id}] — what the customer has ordered, newest first."""
    if not customer_id or customer_id == "GUEST":
        return []
    from retail import commerce
    rows = []
    for o in commerce.orders_for(customer_id, limit):
        for ln in o.get("lines", []):
            rows.append((ln["sku"], o.get("created_at", ""), o.get("order_id")))
    docs = search.get_products([f"PROD::{s}" for s, _, _ in rows])
    out, seen = [], set()
    for sku, ts, oid in rows:
        p = docs.get(f"PROD::{sku}")
        if p and sku not in seen:
            seen.add(sku)
            out.append({"product": p, "date": _day(ts), "order_id": oid})
    return out


def related(focus: list[dict[str, Any]], h: dict[str, Any],
            customer_id: str | None = None, *, limit: int = 4,
            trace: Trace | None = None, from_orders: bool = True) -> list[dict[str, Any]]:
    """What goes with the products in the conversation (focus): first what fits
    something they bought on the same topic (unless from_orders is False), then
    what the focus needs. Not what is on the list, in the cart or already bought.
    -> [{product, why, memory, from_order, for}]"""
    t0 = time.perf_counter()
    have_kinds = {p.get("kind") for p in focus}
    have_skus = {p["sku"] for p in focus}
    if customer_id and customer_id != "GUEST":
        from retail import commerce
        lines = commerce.view(customer_id)["lines"]
        have_kinds |= {ln["product"].get("kind") for ln in lines}
        have_skus |= {ln["product"]["sku"] for ln in lines}
    past = bought(customer_id)
    owned_kinds = {b["product"].get("kind") for b in past}
    owned_bases = {b["product"]["base_id"] for b in past}
    topic = topic_of({p.get("kind") for p in focus})
    # (product that needs it, when bought or None, order id)
    triggers = [(b["product"], b["date"], b["order_id"]) for b in past
                if from_orders and b["product"].get("kind") in topic] + [(p, None, None)
                                                                         for p in focus]
    out: list[dict[str, Any]] = []
    for p, when, oid in triggers:
        for wanted, needs, kind, words, why in RULES:
            if len(out) >= limit:
                break
            if kind in have_kinds or not wanted(h) or not needs(p):
                continue
            if kind not in EACH and kind in owned_kinds:
                continue                       # they've bought one already
            add = _pick(kind, h, p)
            if not add or add["sku"] in have_skus or (add["base_id"] in owned_bases
                                                      and kind == "cartridge"):
                continue
            have_kinds.add(kind)
            have_skus.add(add["sku"])
            name = p.get("base_name", p["name"])
            text = why.format(name=name)
            if when:
                text += f" you bought on {when}"
            out.append({"product": add, "why": text[0].upper() + text[1:],
                        "memory": said(h, *words) if words else None,
                        "from_order": oid, "for": name})
    if trace and out:
        trace.add("App logic", "Added what goes with it",
                  "; ".join(f"{g['product']['base_name']} — {g['why']}" for g in out),
                  "goes-with rules over the products in the conversation, your past "
                  "orders and Agent Memory (retail/companions.py)",
                  (time.perf_counter() - t0) * 1000,
                  ["commerce.orders"] if past else [])
    return out


def goes_with(products: list[dict[str, Any]], h: dict[str, Any],
              customer_id: str | None = None, trace: Trace | None = None
              ) -> list[dict[str, Any]]:
    return related(products, h, customer_id, trace=trace)


def for_me(customer_id: str | None, h: dict[str, Any], focus: list[dict[str, Any]],
           about: str = "", trace: Trace | None = None) -> list[dict[str, Any]]:
    """Suggestions from past orders on the conversation's topic first, then the rest.
    Nothing when there's neither a topic nor an order."""
    kinds = set(search.kinds_named(about)) | {p.get("kind") for p in focus}
    past = bought(customer_id)
    on_topic = [b["product"] for b in past if b["product"].get("kind") in topic_of(kinds)]
    picks = related(focus + on_topic, h, customer_id, limit=5, trace=trace) if kinds else []
    if len(picks) < 5 and past:
        rest = related([b["product"] for b in past], h, customer_id, limit=5, trace=trace)
        seen = {g["product"]["sku"] for g in picks}
        picks += [g for g in rest if g["product"]["sku"] not in seen][:5 - len(picks)]
    return picks
