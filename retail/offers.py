"""Bundle discounts and special offers — deterministic and explainable.

The cart is expanded into units (2 chairs = 2 units). Each offer is priced on
the units still free; the offer that saves the most is applied and its units
are used up; repeat until nothing else applies. No unit is discounted twice,
and every saving names the items it came from.

Also produces *nudges*: offers the cart nearly qualifies for ("add one more
compatible cartridge to save 10%"), which is where bundles sell themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import settings


@dataclass
class Unit:
    sku: str
    name: str
    price: float
    kind: str
    category: str
    product: dict[str, Any]
    used: bool = False


@dataclass
class Saving:
    offer_id: str
    name: str
    amount: float
    items: list[str] = field(default_factory=list)


def _units(lines: list[dict[str, Any]]) -> list[Unit]:
    out = []
    for line in lines:
        p = line["product"]
        for _ in range(int(line["qty"])):
            out.append(Unit(p["sku"], p["name"], float(p["price"]), p["kind"],
                            p["category"], p))
    return out


def _free(units: list[Unit], **match) -> list[Unit]:
    out = []
    for u in units:
        if u.used:
            continue
        if "kind" in match and u.kind != match["kind"]:
            continue
        if "category" in match and u.category != match["category"]:
            continue
        if "min_reams" in match and int(u.product.get("reams", 0)) < match["min_reams"]:
            continue
        out.append(u)
    return sorted(out, key=lambda u: -u.price)


def _seats(u: Unit) -> int:
    return int(u.product.get("pack_qty") or 1)


# --------------------------------------------------------------------------- #
# One function per offer kind: returns (saving, units it would use) or None.
# --------------------------------------------------------------------------- #
def _pair(o, units):
    a, b = _free(units, **o["a"]), _free(units, **o["b"])
    n = min(len(a), len(b))
    if not n:
        return None
    used = a[:n] + b[:n]
    return round(sum(u.price for u in used) * o["discount_pct"] / 100, 2), used


def _printer_ink(o, units):
    used, saving = [], 0.0
    carts = _free(units, kind="cartridge")
    for printer in _free(units, kind="printer"):
        fam = printer.product.get("cartridge_family")
        fits = [c for c in carts if fam in c.product.get("compatible_printers", [])
                and c not in used]
        if len(fits) >= o["min_cartridges"]:
            group = [printer] + fits[:o["min_cartridges"]]
            used += group
            saving += sum(u.price for u in group) * o["discount_pct"] / 100
    return (round(saving, 2), used) if used else None


def _qty_tier(o, units):
    pool = _free(units, **o["match"])
    count = sum(_seats(u) for u in pool) if o["match"].get("kind") == "chair" else len(pool)
    for min_qty, pct in o["tiers"]:
        if count >= min_qty:
            return round(sum(u.price for u in pool) * pct / 100, 2), pool
    return None


def _kit(o, units):
    anchors = _free(units, **o["anchor"])
    used, saving = [], 0.0
    pools = [_free(units, **m) for m in o["items"]]
    for anchor in anchors:
        picks = []
        for pool in pools:
            avail = [u for u in pool if u not in used and u not in picks]
            if not avail:
                picks = []
                break
            picks.append(avail[-1])          # the cheapest matching item
        if not picks:
            break
        used += [anchor] + picks
        saving += sum(u.price for u in picks) * o["discount_pct"] / 100
    return (round(saving, 2), used) if used else None


def _free_item(o, units):
    anchors, items = _free(units, **o["anchor"]), _free(units, **o["item"])
    n = min(len(anchors), len(items))
    if not n:
        return None
    gifts = items[-n:]                       # the cheapest cables go free
    return (round(sum(min(u.price, o["max_value"]) for u in gifts), 2),
            anchors[:n] + gifts)


def _set(o, units):
    picks = []
    for need in o["set"]:
        pool = [u for u in _free(units, kind=need["kind"]) if u not in picks]
        qty = need.get("qty", 1)
        if need["kind"] == "chair":
            got, seats = [], 0
            for u in pool:
                if seats >= qty:
                    break
                got.append(u)
                seats += _seats(u)
            if seats < qty:
                return None
            picks += got
        else:
            if len(pool) < qty:
                return None
            picks += pool[:qty]
    return picks


def _set_amount(o, units):
    picks = _set(o, units)
    return (o["amount_off"], picks) if picks else None


def _set_pct(o, units):
    picks = _set(o, units)
    return (round(sum(u.price for u in picks) * o["discount_pct"] / 100, 2), picks) \
        if picks else None


def _spend_category(o, units):
    pool = _free(units, category=o["category"])
    spend = sum(u.price for u in pool)
    if spend >= o["min_spend"]:
        return round(spend * o["discount_pct"] / 100, 2), pool
    return None


# Shipping speeds: Standard keeps the free-over-$500 rule; faster ones always cost.
SHIPPING = {
    "standard": {"label": "Standard", "fee": None, "days": 5, "eta": "3–5 business days"},
    "2day": {"label": "2-Day", "fee": 39.99, "days": 2, "eta": "2 business days"},
    "nextday": {"label": "Next Day", "fee": 79.99, "days": 1, "eta": "next business day"},
}


PRICERS = {"pair": _pair, "printer_ink": _printer_ink, "qty_tier": _qty_tier,
           "kit": _kit, "free_item": _free_item, "set_amount": _set_amount,
           "set_pct": _set_pct, "spend_category": _spend_category}


def price_cart(lines: list[dict[str, Any]], offers: list[dict[str, Any]],
               shipping: str = "standard") -> dict[str, Any]:
    """lines: [{"product": {...}, "qty": n}] -> totals, savings and nudges.
    shipping: a key of SHIPPING; only Standard can be free."""
    units = _units(lines)
    subtotal = round(sum(u.price for u in units), 2)
    savings: list[Saving] = []
    active = [o for o in offers if o.get("active", True) and o["kind"] in PRICERS]
    while True:
        best = None
        for o in active:
            if any(s.offer_id == o["offer_id"] for s in savings) and o["kind"] in (
                    "qty_tier", "spend_category", "set_amount", "set_pct"):
                continue                     # those apply once per cart
            r = PRICERS[o["kind"]](o, units)
            if r and r[0] > 0 and (best is None or r[0] > best[1]):
                best = (o, r[0], r[1])
        if not best:
            break
        o, amount, used = best
        for u in used:
            u.used = True
        names = sorted({u.name.split(",")[0] for u in used})
        savings.append(Saving(o["offer_id"], o["name"], amount, names))

    discount = round(sum(s.amount for s in savings), 2)
    after = round(subtotal - discount, 2)
    free_delivery = next((o for o in offers if o["kind"] == "free_delivery"), None)
    speed = SHIPPING.get(shipping) or SHIPPING["standard"]
    shipping = shipping if shipping in SHIPPING else "standard"
    delivery = 0.0 if not units else speed["fee"] if speed["fee"] is not None else (
        0.0 if free_delivery and after >= free_delivery["min_subtotal"]
        else settings.delivery_fee)
    tax = round(after * settings.tax_rate, 2)
    return {
        "subtotal": subtotal, "discount": discount, "after_discount": after,
        "delivery": delivery, "tax": tax, "total": round(after + delivery + tax, 2),
        "savings": [s.__dict__ for s in savings],
        "nudges": nudges(lines, offers, after, shipping),
        "units": len(units),
        "shipping": {"speed": shipping, "label": speed["label"], "fee": delivery,
                     "eta": speed["eta"]},
    }


def nudges(lines: list[dict[str, Any]], offers: list[dict[str, Any]],
           after_discount: float, shipping: str = "standard") -> list[dict[str, Any]]:
    """Offers the cart is one step away from -> [{text, add}]. add: what would
    qualify — [{kind, qty, like?: sku already in the cart, for?: printer sku}];
    empty when nothing can be added in one click (spend more for free delivery)."""
    kinds: dict[str, int] = {}
    first: dict[str, dict[str, Any]] = {}          # the line of each kind with most units
    for line in lines:
        k = line["product"]["kind"]
        units = int(line["qty"]) * (int(line["product"].get("pack_qty") or 1)
                                    if k == "chair" else 1)
        kinds[k] = kinds.get(k, 0) + units
        if units > first.get(k, {}).get("units", 0):
            first[k] = {"units": units, "line": line}

    def more(kind: str, n: int) -> dict[str, Any]:  # n more units of what's in the cart
        p = first[kind]["line"]["product"]
        pack = int(p.get("pack_qty") or 1) if kind == "chair" else 1
        return {"kind": kind, "qty": -(-n // pack), "like": p["sku"]}

    out: list[dict[str, Any]] = []
    if kinds.get("desk") and not kinds.get("chair"):
        out.append({"text": "Add an office chair — desk + chair saves 15% on both.",
                    "add": [{"kind": "chair", "qty": 1}]})
    if kinds.get("chair") and not kinds.get("desk") and not kinds.get("conference_table"):
        out.append({"text": "Add a desk — desk + chair saves 15% on both.",
                    "add": [{"kind": "desk", "qty": 1}]})
    if kinds.get("chair") in (2,):
        out.append({"text": "Add one more chair — 3 or more chairs save 10%.",
                    "add": [more("chair", 1)]})
    if 6 <= kinds.get("chair", 0) < 10:
        out.append({"text": f"Add {10 - kinds['chair']} more chair(s) — 10 or more save 15%.",
                    "add": [more("chair", 10 - kinds["chair"])]})
    if kinds.get("printer") and kinds.get("cartridge", 0) < 2 * kinds["printer"]:
        out.append({"text": "Add compatible cartridges — printer + 2 cartridges saves 10%.",
                    "add": [{"kind": "cartridge",
                             "qty": 2 * kinds["printer"] - kinds.get("cartridge", 0),
                             "for": first["printer"]["line"]["product"]["sku"]}]})
    if kinds.get("whiteboard") and not (kinds.get("dry_erase_marker")
                                        and kinds.get("whiteboard_eraser")):
        out.append({"text": "Add dry-erase markers and an eraser — 20% off with a whiteboard.",
                    "add": [{"kind": k, "qty": 1} for k in ("dry_erase_marker",
                                                            "whiteboard_eraser")
                            if not kinds.get(k)]})
    if kinds.get("tv") and not kinds.get("hdmi_cable"):
        out.append({"text": "Add an HDMI cable — one is free with any display.",
                    "add": [{"kind": "hdmi_cable", "qty": 1}]})
    if kinds.get("desk") and kinds.get("chair") and not kinds.get("lamp"):
        out.append({"text": "Add a desk lamp — desk + chair + lamp takes $50 off.",
                    "add": [{"kind": "lamp", "qty": 1}]})
    fd = next((o for o in offers if o["kind"] == "free_delivery"), None)
    if fd and shipping == "standard" and 0 < after_discount < fd["min_subtotal"]:
        gap = fd["min_subtotal"] - after_discount
        if gap <= 150:
            out.append({"text": f"Spend ${gap:,.2f} more for free delivery.", "add": []})
    return out
