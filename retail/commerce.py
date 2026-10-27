"""Cart and checkout.

The cart is one document per customer (`commerce.carts`, key CART::<id>):
just SKUs and quantities. Prices and savings are computed fresh from the
catalogue and the offers every time it is shown, so a price change or a new
offer is never stale in someone's cart.

Checkout uses a fake card gateway: any Luhn-valid number is approved except
4000 0000 0000 0002, which is always declined. The full card number is never
stored — `commerce.payments` keeps a token and the last four digits only.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from retail import governance, offers, search
from retail.cb import fq, kv_get, kv_remove, kv_upsert, query
from retail.trace import Trace

DECLINE = {"4000000000000002"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def cart_key(customer_id: str) -> str:
    return f"CART::{customer_id}"


def _load(customer_id: str) -> dict[str, Any]:
    return kv_get("commerce", "carts", cart_key(customer_id)) or {
        "type": "cart", "customer_id": customer_id, "status": "open", "lines": []}


def _save(cart: dict[str, Any]) -> None:
    cart["updated_at"] = _now()
    kv_upsert("commerce", "carts", cart_key(cart["customer_id"]), cart)


def add(customer_id: str, sku: str, qty: int = 1, trace: Trace | None = None,
        source: str = "shop") -> dict[str, Any]:
    t0 = time.perf_counter()
    cart = _load(customer_id)
    for line in cart["lines"]:
        if line["sku"] == sku:
            line["qty"] = int(line["qty"]) + int(qty)
            break
    else:
        cart["lines"].append({"sku": sku, "qty": int(qty), "source": source})
    _save(cart)
    if trace:
        trace.add("Data (KV)", "Saved the item to your cart", f"+{qty} × {sku}",
                  f"KV upsert, key {cart_key(customer_id)}",
                  (time.perf_counter() - t0) * 1000, writes=["commerce.carts"])
    return cart


def add_many(customer_id: str, items: list[tuple[str, int]], trace: Trace | None = None,
             source: str = "planner") -> dict[str, Any]:
    t0 = time.perf_counter()
    cart = _load(customer_id)
    for sku, qty in items:
        for line in cart["lines"]:
            if line["sku"] == sku:
                line["qty"] = int(line["qty"]) + int(qty)
                break
        else:
            cart["lines"].append({"sku": sku, "qty": int(qty), "source": source})
    _save(cart)
    if trace:
        trace.add("Data (KV)", "Saved the items to your cart", f"{len(items)} items added",
                  f"KV upsert, key {cart_key(customer_id)}",
                  (time.perf_counter() - t0) * 1000, writes=["commerce.carts"])
    return cart


def set_qty(customer_id: str, sku: str, qty: int) -> None:
    cart = _load(customer_id)
    cart["lines"] = [dict(line, qty=qty) if line["sku"] == sku else line
                     for line in cart["lines"]]
    cart["lines"] = [line for line in cart["lines"] if int(line["qty"]) > 0]
    _save(cart)


def clear(customer_id: str) -> None:
    kv_remove("commerce", "carts", cart_key(customer_id))


_offers_cache: dict[str, Any] = {"t": 0.0, "rows": []}


def active_offers() -> list[dict[str, Any]]:
    if time.time() - _offers_cache["t"] > 30:
        _offers_cache["rows"] = query(f"SELECT o.* FROM {fq('catalog', 'offers')} AS o "
                                      f"WHERE o.active = true")
        _offers_cache["t"] = time.time()
    return _offers_cache["rows"]


def arrives_by(days: int, start: date | None = None) -> date:
    """`days` business days after `start` (today)."""
    d = start or datetime.now(UTC).date()
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def shipping_choice(customer_id: str, shipping: str | None = None
                    ) -> tuple[str, str | None]:
    """(speed, the memory it came from): the speed asked for, else the one the
    customer told us (Agent Memory), else Standard."""
    if shipping in offers.SHIPPING:
        return shipping, None
    from retail import memory
    told = memory.shipping(customer_id)
    return (told[0], told[1]) if told else ("standard", None)


def view(customer_id: str, trace: Trace | None = None,
         shipping: str | None = None) -> dict[str, Any]:
    """The cart with live prices, bundle savings, nudges and totals. shipping:
    a speed the shopper picked; None uses their remembered preference."""
    t0 = time.perf_counter()
    cart = _load(customer_id)
    docs = search.get_products([f"PROD::{line['sku']}" for line in cart["lines"]])
    lines = [{"product": docs[f"PROD::{line['sku']}"], "qty": int(line["qty"])}
             for line in cart["lines"] if f"PROD::{line['sku']}" in docs]
    t_kv = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    speed, told = shipping_choice(customer_id, shipping)
    t_mem = (time.perf_counter() - t1) * 1000
    t1 = time.perf_counter()
    priced = offers.price_cart(lines, active_offers(), speed)
    priced["shipping"].update(from_memory=bool(told), memory=told,
                              arrives_by=arrives_by(offers.SHIPPING[speed]["days"])
                              .isoformat())
    if trace:
        units = sum(int(line["qty"]) for line in lines)
        trace.add("Data (KV)", "Read your cart and the current price of each item",
                  f"{units} item{'s' if units != 1 else ''}",
                  "KV get of the cart, then a multi-get of the products", t_kv,
                  ["commerce.carts", ("catalog.products", "KV multi-get")])
        trace.add("Query (SQL++)", "Loaded today's bundle offers",
                  f"{len(active_offers())} active offers",
                  "SQL++ SELECT on catalog.offers WHERE active (cached 30 s)", 0,
                  ["catalog.offers"], indexes=["idx_offer_active"])
        saved = (", ".join(f"{s['name']} (−${s['amount']:,.2f})" for s in priced["savings"])
                 or "no bundle applies yet")
        trace.add("App logic", "Applied the best combination of offers", saved,
                  "offer engine: best non-overlapping offers first",
                  (time.perf_counter() - t1) * 1000)
        if told:
            trace.add("Agent Memory", "Applied your shipping preference",
                      f"{priced['shipping']['label']} (you told us: “{told}”)",
                      "Agent Memory: newest memory that names a shipping speed", t_mem)
    return {"lines": lines, **priced}


# --------------------------------------------------------------------------- #
# checkout
# --------------------------------------------------------------------------- #
def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number][::-1]
    total = 0
    for i, d in enumerate(digits):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def card_brand(number: str) -> str:
    if number.startswith("4"):
        return "Visa"
    if re.match(r"5[1-5]", number) or re.match(r"2[2-7]", number):
        return "Mastercard"
    if re.match(r"3[47]", number):
        return "Amex"
    if number.startswith("6"):
        return "Discover"
    return "Card"


def validate_card(number: str, exp: str, cvc: str) -> str | None:
    """None if valid, otherwise what is wrong (shown to the customer)."""
    n = re.sub(r"\D", "", number or "")
    if not 13 <= len(n) <= 19 or not luhn_ok(n):
        return "That card number isn't valid — check the digits."
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{2}|\d{4})\s*$", exp or "")
    if not m:
        return "Enter the expiry as MM/YY."
    month, year = int(m.group(1)), int(m.group(2))
    year += 2000 if year < 100 else 0
    if not 1 <= month <= 12:
        return "Enter the expiry as MM/YY."
    if (year, month) < (date.today().year, date.today().month):
        return "That card has expired."
    if not re.fullmatch(r"\d{3,4}", cvc or ""):
        return "Enter the 3- or 4-digit security code."
    return None


def checkout(customer_id: str, card: dict[str, str], *, session_id: str | None = None,
             trace: Trace | None = None, over_budget_ok: bool = False,
             by_agent: bool = False, shipping: str | None = None) -> dict[str, Any]:
    """Place the order. Returns {"status": paid|declined|handoff|denied|invalid|empty|
    over_budget}. over_budget: the cart costs more than the budget the customer told
    us (Agent Memory) — nothing is charged until they confirm (over_budget_ok).

    by_agent: the AI assistant is placing it, so Agent Catalog governance applies —
    over the assistant's spending limit it hands off (status handoff): no order, the
    cart kept, and the shopper reviews and pays in the Cart. A shopper paying in the
    Cart themselves has no such limit.

    shipping: the speed picked in the Cart; None uses the remembered preference."""
    cart = view(customer_id, trace=trace, shipping=shipping)
    if not cart["lines"]:
        return {"status": "empty", "message": "Your cart is empty."}
    problem = validate_card(card.get("number", ""), card.get("exp", ""), card.get("cvc", ""))
    if problem:
        return {"status": "invalid", "message": problem}

    from retail import memory
    t0 = time.perf_counter()
    told = memory.budget(customer_id)
    over = bool(told and cart["total"] > told[0])
    if trace and told:
        trace.add("Agent Memory", "Checked the order against the budget you told us",
                  (f"${cart['total']:,.2f} is over your ${told[0]:,.0f} budget"
                   + (" — you chose to order anyway" if over_budget_ok else
                      " — asked you to confirm") if over else
                   f"Within your ${told[0]:,.0f} budget"),
                  f"Agent Memory: “{told[1]}”", (time.perf_counter() - t0) * 1000)
    if over and not over_budget_ok:
        return {"status": "over_budget", "budget": told[0], "memory": told[1],
                "total": cart["total"],
                "message": f"${cart['total']:,.2f} is over your ${told[0]:,.0f} budget "
                           f"(you told us: “{told[1]}”). Change the cart, or confirm to "
                           f"order anyway."}

    if by_agent:
        decision = governance.authorize("checkout", {"amount_usd": cart["total"]},
                                        customer_id=customer_id, session_id=session_id,
                                        trace=trace)
        if not decision.allowed:
            return {"status": "denied", "message": decision.message,
                    "policy": decision.policy}
        if decision.hand_off:
            return {"status": "handoff", "total": cart["total"], "limit": decision.limit,
                    "message": f"Orders over ${decision.limit:,.0f} can't be "
                               f"placed by the assistant — your cart "
                               f"(${cart['total']:,.2f}) is ready; review and pay in the "
                               f"Cart."}

    number = re.sub(r"\D", "", card["number"])
    order_id = f"ORD-{datetime.now(UTC):%y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    base = {
        "type": "order", "order_id": order_id, "customer_id": customer_id,
        "created_at": _now(), "session_id": session_id,
        "lines": [{"sku": l_["product"]["sku"], "name": l_["product"]["name"],
                   "qty": l_["qty"], "unit_price": l_["product"]["price"]}
                  for l_ in cart["lines"]],
        "subtotal": cart["subtotal"], "discount": cart["discount"],
        "savings": cart["savings"], "delivery": cart["delivery"], "tax": cart["tax"],
        "total": cart["total"], "shipping": cart["shipping"],
        "arrives_by": cart["shipping"]["arrives_by"]}

    t0 = time.perf_counter()
    if number in DECLINE:
        if trace:
            trace.add("App logic", "Sent the card for payment", "Declined",
                      "demo card gateway (4000 0000 0000 0002 always declines)")
        return {"status": "declined",
                "message": "Your card was declined. Try a different card."}

    token = "tok_" + hashlib.sha256((number + order_id).encode()).hexdigest()[:20]
    payment = {"type": "payment", "payment_id": f"PAY-{uuid.uuid4().hex[:10].upper()}",
               "order_id": order_id, "customer_id": customer_id, "amount": cart["total"],
               "brand": card_brand(number), "last4": number[-4:], "token": token,
               "status": "captured", "created_at": _now()}
    order = {**base, "status": "paid", "payment_id": payment["payment_id"],
             "card": f"{payment['brand']} ending {payment['last4']}"}
    kv_upsert("commerce", "payments", f"PAY::{payment['payment_id']}", payment)
    kv_upsert("commerce", "orders", f"ORDER::{order_id}", order)
    clear(customer_id)
    if trace:
        trace.add("App logic", "Sent the card for payment",
                  f"Approved — {order['card']}", "demo card gateway (Luhn-checked)")
        trace.add("Data (KV)", "Saved the order and the payment record",
                  f"Order {order_id}; card stored as a token + last 4 digits only",
                  f"KV upserts, keys ORDER::{order_id} and PAY::{payment['payment_id']}",
                  (time.perf_counter() - t0) * 1000,
                  writes=["commerce.orders", "commerce.payments"])
    return {"status": "paid", "order": order}


def orders_for(customer_id: str, limit: int = 10) -> list[dict[str, Any]]:
    return query(f"SELECT o.* FROM {fq('commerce', 'orders')} AS o "
                 f"WHERE o.customer_id = $c ORDER BY o.created_at DESC LIMIT {int(limit)}",
                 consistent=True, c=customer_id)


def get_order(order_id: str) -> dict[str, Any] | None:
    return kv_get("commerce", "orders", f"ORDER::{order_id.upper()}")
