"""Retail demo storefront — OfficeIQ, an AI-powered office store on Couchbase.

    streamlit run app.py --server.port 8502
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import html
import json
import re
import time
import uuid
import warnings
from datetime import datetime

import streamlit as st

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*scope_name.*")

from config import ROOT, settings  # noqa: E402
from retail import (agentc_bridge, assistant, commerce, companions, memory,  # noqa: E402
                    moving, offers, photo_cam, planner, render, search, semantic_cache, trace, vision)
from retail.cb import query  # noqa: E402

st.set_page_config(page_title="OfficeIQ — AI-powered office store",
                   page_icon="✨", layout="wide", initial_sidebar_state="expanded")

RED = "#CC0000"
CSS = f"""
<style>
  .block-container {{padding-top: 0.6rem; max-width: 1400px;}}
  header[data-testid="stHeader"] {{display: none;}}
  #MainMenu, footer {{visibility: hidden;}}
  .promo {{background:{RED}; color:#fff; text-align:center; font-weight:600;
           padding:6px 0; border-radius:4px; margin:6px 0 10px 0; font-size:0.92rem;}}
  .card {{border:1px solid #e3e3e3; border-radius:6px; padding:10px; background:#fff;
          height: 100%;}}
  .card img {{width:100%; border-radius:4px; background:#f7f7f7;}}
  .pname {{font-size:0.88rem; color:#222; min-height:2.6em; line-height:1.3em;
           margin:6px 0 2px 0;}}
  .price {{font-size:1.15rem; font-weight:700; color:#222;}}
  .stars {{color:#f5a623; font-size:0.85rem;}}
  .muted {{color:#6b6b6b; font-size:0.78rem;}}
  .swatch {{display:inline-block; width:12px; height:12px; border-radius:50%;
            border:1px solid #999; vertical-align:middle; margin-right:4px;}}
  .save {{color:#1a7f37; font-weight:600;}}
  .bring {{color:#1d4ed8; font-size:0.82rem;}}
  .nudge {{background:#fff4e5; border-left:4px solid #f5a623; padding:6px 10px;
           margin:4px 0; border-radius:3px; font-size:0.9rem;}}
  div.stButton > button[kind="primary"],
  div.stFormSubmitButton > button[kind="primaryFormSubmit"],
  div.stFormSubmitButton > button[kind="primary"] {{background:{RED}; border-color:{RED};
      color:#fff;}}
  .hero {{background:linear-gradient(90deg,#fff 0%,#fff 55%,#fdecec 100%);
          border:1px solid #f0d0d0; border-radius:8px; padding:14px 22px; margin-bottom:12px;}}
  .hero h2 {{margin:0 0 6px 0; color:#222;}}
  .hero p {{margin:0; color:#444;}}
  div.stButton > button[kind="primary"]:hover {{background:#a30000; border-color:#a30000;}}
  .navbar div.stButton > button {{border:none; background:transparent; font-weight:600;}}
  div[data-testid="stButtonGroup"] button {{min-height:1.5rem; padding:0 8px;
      font-size:0.72rem; line-height:1.4rem;}}
  div[data-testid="stButtonGroup"] {{gap:4px;}}
  .badge {{background:{RED}; color:#fff; border-radius:10px; padding:1px 8px;
           font-size:0.8rem; margin-left:4px;}}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=300)
def customers() -> list[dict]:
    return query("SELECT c.* FROM `retail`.`commerce`.`customers` AS c "
                 "ORDER BY c.customer_id")


@st.cache_data(ttl=300)
def categories() -> list[dict]:
    return search.categories()


@st.cache_data(ttl=300)
def all_colors() -> list[str]:
    return search.colors()


ss = st.session_state
ss.setdefault("page", "Shop")
ss.setdefault("session_id", uuid.uuid4().hex)
GUEST = "GUEST"          # browsing without signing in: a cart, but nothing remembered
ss.setdefault("customer_id", GUEST)
ss.setdefault("q", "")
ss.setdefault("last_trace", None)
ss.setdefault("last_order", None)


def is_guest() -> bool:
    return ss.customer_id == GUEST


def go(page: str) -> None:
    ss.page = page


def traced(flow: str, t: trace.Trace, asked: str, answer: str = "") -> None:
    """Log this request to the Agent Tracer (in the background)."""
    agentc_bridge.log_run(flow, t, session=ss.session_id, user=ss.customer_id,
                          asked=asked, answer=answer)


def _open_run(run: str) -> None:
    ss.page, ss.ac_view = "Agent Catalog", "tracer"
    ss.tracer_session, ss.tracer_tag, ss.hub_item = ss.session_id, f"run={run}", None
    _all_types(True)


def _submit_search() -> None:
    ss.q = (ss.get("search_box") or "").strip()
    ss.page = "Shop"


def _clear_search() -> None:
    ss.q = ""
    ss.search_box = ""


def usd(x: float, places: int = 2) -> str:
    """A price for markdown: an escaped $, so two prices on a line never read as
    a LaTeX formula."""
    return f"&#36;{x:,.{places}f}"


def stars(r: float) -> str:
    full = int(round(r))
    return "★" * full + "☆" * (5 - full)


def logo_html() -> str:
    return ('<div style="margin-top:2px;line-height:1.05">'
            '<div style="display:flex;align-items:center;gap:7px">'
            '<span style="font-size:1.55rem">✨</span>'
            '<span style="font-weight:800;font-size:1.85rem;letter-spacing:-0.6px;'
            'font-family:Arial,Helvetica,sans-serif;background:linear-gradient(90deg,'
            '#6d28d9,#db2777 55%,#f97316);-webkit-background-clip:text;'
            'background-clip:text;color:transparent">OfficeIQ</span>'
            '<span style="background:linear-gradient(90deg,#6d28d9,#db2777);color:#fff;'
            'font-size:0.68rem;font-weight:700;border-radius:9px;padding:2px 7px;'
            'letter-spacing:0.5px">AI</span></div>'
            '<div style="color:#666;font-size:0.74rem;margin-left:2px">AI-powered office '
            'store · snap a photo, get a plan</div></div>')


SERVICE_COLOURS = {"Data (KV)": "#1f6feb", "Query (SQL++)": "#8250df",
                   "Full-Text Search": "#bf3989", "Vector Search": "#d1242f",
                   "Agent Memory": "#1a7f37", "Agent Catalog": "#9a6700",
                   "Semantic Cache": "#0a7ea4", "Model": "#57606a", "App logic": "#6e7781"}


def _tokens_cell(s_: trace.Step) -> str:
    """Model tokens a step spent, or saved by reusing an earlier answer."""
    out = []
    if s_.tokens_spent:
        out.append(f"<div class='tsp' title='prompt + answer tokens'>"
                   f"{s_.tokens_spent:,} spent</div>")
    if s_.tokens_saved:
        out.append(f"<div class='tsv' title='no model call needed'>"
                   f"⚡ {s_.tokens_saved:,} saved</div>")
    return "".join(out) or "<span class='tnil'>—</span>"


def _data_cell(s_: trace.Step) -> str:
    """The collections a step read and wrote, and the indexes it used."""
    import html as _h
    def chip(cls: str, label: str, name: str, how: str) -> str:
        return (f"<div class='dc {cls}'><span class='dl'>{label}</span>"
                f"<code>{_h.escape(name)}</code> <span class='dop'>{_h.escape(how)}</span>"
                f"</div>")
    chips = ([chip("dr", "READ", c, s_.op("r", c)) for c in s_.collections]
             + [chip("dw", "WRITE", c, s_.op("w", c)) for c in s_.writes]
             + [chip("di", "INDEX", i, trace.index_kind(i) + " index") for i in s_.indexes])
    return "".join(chips) or "<span class='tnil'>—</span>"


STAGE_ICON = {"model": "🧠", "tool": "🛠️", "system": "⚙️"}
TRACE_CSS = (
    "<style>.trc{width:100%;border-collapse:collapse;font-size:0.88rem}"
    ".trc td{border-bottom:1px solid #eee;padding:7px 6px;vertical-align:top}"
    ".trc .tn{color:#999;width:24px}.trc .tr{color:#1a7f37;margin-top:2px;"
    "overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}"
    ".trc .tt{color:#888;font-size:0.76rem;margin-top:2px}"
    ".trc .tag{color:#fff;border-radius:10px;padding:2px 8px;font-size:0.74rem;"
    "white-space:nowrap;display:inline-block;margin:0 3px 3px 0}"
    ".trc .tms{color:#666;white-space:nowrap;text-align:right;width:80px}"
    ".trc th{background:#f3f4f6;color:#444;font-size:0.74rem;text-transform:uppercase;"
    "letter-spacing:.04em;text-align:left;padding:6px;border-bottom:2px solid #ddd}"
    ".trc th.r{text-align:right}"
    ".trc .ttk{white-space:nowrap;text-align:right;width:110px;font-size:0.8rem}"
    ".trc .tsp{color:#b45309}.trc .tsv{color:#15803d;font-weight:600}"
    ".trc .tdat{font-size:0.74rem;width:250px}.trc .dc{margin:2px 0;line-height:1.35}"
    ".trc .dc code{font-size:0.72rem;background:none;padding:0;color:#333}"
    ".trc .dl{display:inline-block;width:44px;text-align:center;border-radius:4px;"
    "font-size:0.62rem;font-weight:700;letter-spacing:.04em;padding:1px 0;margin-right:5px;"
    "color:#fff}.trc .dop{color:#888}.trc .dr .dl{background:#2563eb}"
    ".trc .dw .dl{background:#d97706}.trc .di .dl{background:#7c3aed}"
    ".trc .tnil{color:#ccc}.trc tfoot td{border-top:2px solid #ddd;border-bottom:none;"
    "font-weight:600;font-size:0.8rem}"
    ".trc details summary{cursor:pointer;color:#64748b;font-size:.76rem;margin-top:4px}"
    ".trc table.sub{width:100%;border-collapse:collapse;margin-top:4px;background:#fafafa}"
    ".trc table.sub td{font-size:.8rem;padding:5px 6px;border-bottom:1px solid #eee}"
    ".trc table.sub .tr{-webkit-line-clamp:3}</style>")


def _step_row(i, s_: trace.Step, sub: bool = False) -> str:
    import html as _h
    colour = SERVICE_COLOURS.get(s_.service, "#6e7781")
    return (f"<tr><td class='tn'>{i}</td>"
            f"<td><b>{_h.escape(s_.what)}</b><div class='tr'>{_h.escape(s_.result)}</div>"
            f"<div class='tt'>{_h.escape(s_.how)}</div></td>"
            f"<td><span class='tag' style='background:{colour}'>{_h.escape(s_.service)}"
            f"</span></td><td class='tdat'>{_data_cell(s_)}</td>"
            f"<td class='ttk'>{_tokens_cell(s_)}</td>"
            f"<td class='tms'>{s_.ms:,.0f} ms</td></tr>")


def _stage_row(i: int, g: trace.Stage, first: int) -> str:
    """One stage: what it did and its result; the services and data it touched;
    its steps folded underneath."""
    import html as _h
    agg = trace.Step(g.services[0], g.title, collections=tuple(dict.fromkeys(
        c for s_ in g.steps for c in s_.collections)), writes=tuple(dict.fromkeys(
            c for s_ in g.steps for c in s_.writes)), indexes=tuple(dict.fromkeys(
                x for s_ in g.steps for x in s_.indexes)),
        tokens_spent=g.tokens_spent, tokens_saved=g.tokens_saved)
    agg.ops = {k: v for s_ in g.steps for k, v in s_.ops.items()}
    tags = "".join(f"<span class='tag' style='background:"
                   f"{SERVICE_COLOURS.get(sv, '#6e7781')}'>{_h.escape(sv)}</span>"
                   for sv in g.services)
    how = g.steps[0].how if g.kind == "model" else ""
    sub = ""
    if len(g.steps) > 1 or g.kind != "model":
        sub = (f"<details><summary>{len(g.steps)} step{'s' if len(g.steps) != 1 else ''}"
               f"</summary><table class='sub'>"
               + "".join(_step_row(first + k, s_, True) for k, s_ in enumerate(g.steps))
               + "</table></details>")
    return (f"<tr><td class='tn'>{i}</td><td>{STAGE_ICON[g.kind]} <b>{_h.escape(g.title)}</b>"
            f"<div class='tr'>{_h.escape(g.result[:400])}</div>"
            + (f"<div class='tt'>{_h.escape(how)}</div>" if how else "") + f"{sub}</td>"
            f"<td>{tags}</td><td class='tdat'>{_data_cell(agg)}</td>"
            f"<td class='ttk'>{_tokens_cell(agg)}</td><td class='tms'>{g.ms:,.0f} ms</td></tr>")


def show_trace(t: trace.Trace | None, title: str = "Show what happened") -> None:
    """What a request did, in a few stages — the cache check, each model call, each
    tool it ran — with every step (service, operation, collections) folded inside."""
    if not t or not t.steps:
        return
    spent, saved = t.tokens_spent(), t.tokens_saved()
    toks = "".join([f", {spent:,} tokens spent" if spent else "",
                    f", {saved:,} tokens saved" if saved else ""])
    groups = trace.stages(t.steps)
    staged = len(groups) > 1
    what = (f"{len(groups)} stages · {len(t.steps)} steps" if staged
            else f"{len(t.steps)} step{'s' if len(t.steps) != 1 else ''}")
    with st.expander(f"🔎 {title} — {what}, {t.total_ms():,.0f} ms{toks}"):
        if staged:
            rows, first = [], 1
            for i, g in enumerate(groups, 1):
                rows.append(_stage_row(i, g, first))
                first += len(g.steps)
        else:
            rows = [_step_row(i, s_) for i, s_ in enumerate(t.steps, 1)]
        st.markdown(
            TRACE_CSS + "<table class='trc'><thead><tr><th>#</th>"
            f"<th>{'Stage' if staged else 'Step'} — result</th><th>Done by</th>"
            "<th>Reads / writes / indexes</th>"
            "<th class='r'>Tokens</th><th class='r'>Time</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody>"
            + (f"<tfoot><tr><td></td><td>Total</td><td></td><td></td><td class='ttk'>"
               + (f"<div class='tsp'>{spent:,} spent</div>" if spent else "")
               + (f"<div class='tsv'>{saved:,} saved</div>" if saved else "")
               + f"</td><td class='tms'>{t.total_ms():,.0f} ms</td></tr></tfoot>"
               if spent or saved else "")
            + "</table>",
            unsafe_allow_html=True)
        if t.run:
            st.button("🛰️ Open in Agent Tracer", key=f"tracer::{t.run}", on_click=_open_run,
                      args=(t.run,), help="This request as logged by the Agent Catalog's "
                                          "tracer, in retail.agent_activity.logs")


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
def header() -> None:
    cust = {c["customer_id"]: c for c in customers()}
    c1, c2, c3, c4, c5 = st.columns([1.7, 3.7, 2.1, 1.1, 1.5])
    with c1:
        st.markdown(logo_html(), unsafe_allow_html=True)
    with c2:
        with st.form("search", border=False, clear_on_submit=False):
            s1, s2 = st.columns([4, 1.3])
            # A fixed key, not value=ss.q: a value that changes after each search
            # made Streamlit treat the box as a new widget on the next run, drop
            # what was typed, and re-run the previous search.
            s1.text_input("Search", key="search_box", label_visibility="collapsed",
                          placeholder="Search desks, chairs, printers, ink & more")
            s2.form_submit_button("Search", type="primary", width="stretch",
                                  on_click=_submit_search)
    with c3:
        ids = [GUEST] + list(cust)
        pick = st.selectbox("Signed in as", ids, index=ids.index(ss.customer_id),
                            format_func=lambda i: "👤 Guest — not signed in" if i == GUEST
                            else f"✅ {cust[i]['name']} · {cust[i]['company'] or 'Home'}",
                            label_visibility="collapsed")
        if pick != ss.customer_id:
            ss.customer_id = pick
            ss.session_id = uuid.uuid4().hex
            ss.ship_pick = ss.ship_told = None      # shipping comes from their memory
            st.rerun()
    with c4:
        n = sum(int(line["qty"]) for line in commerce._load(ss.customer_id)["lines"])
        if st.button(f"🛒 Cart ({n})", width="stretch"):
            go("Cart")
    with c5:
        with st.popover("Reset demo", icon=":material/restart_alt:", width="stretch"):
            st.caption("Empties the semantic cache, sets every count back to zero, "
                       "forgets the room photos read before and clears the Agent Tracer. "
                       "Carts, orders, memories and the Tools and Prompts Hubs are not "
                       "touched.")
            st.button("Reset", type="primary", key="demo_reset", width="stretch",
                      on_click=_reset_demo)
    st.markdown('<div class="promo">FREE DELIVERY on orders $500+ &nbsp;·&nbsp; Bundle & '
                'save: desk + chair 15% off &nbsp;·&nbsp; Plan your office free with our '
                'Room Planner</div>', unsafe_allow_html=True)
    nav = st.columns(5)
    for col, page in zip(nav, ["Shop", "Room Planner", "Ask the Assistant", "Cart",
                               "My Orders"]):
        if col.button(page, width="stretch",
                      type="primary" if ss.page == page else "secondary"):
            go(page)
            st.rerun()


# --------------------------------------------------------------------------- #
# product card + details: one card per product, with a colour picker
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=300)
def all_products() -> dict[str, dict]:
    """Every colour variant, by SKU — so switching colour needs no round trip."""
    rows = query("SELECT RAW OBJECT_REMOVE(p, 'embedding') "
                 "FROM `retail`.`catalog`.`products` AS p WHERE p.type = 'product'")
    return {r["sku"]: r for r in rows}


@st.cache_data(show_spinner=False)
def _photo_uri(path: str) -> str | None:
    f = ROOT / path
    if not f.exists():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()


def image_uri(p: dict) -> str:
    """The product's real photo for this colour; the 3D render if none."""
    return (p.get("image") and _photo_uri(p["image"])) or render.data_uri(p)


def color_key(base_id: str, where: str = "card") -> str:
    return f"color::{where}::{base_id}"


def selected_variant(p: dict, where: str = "card", preferred: str | None = None) -> dict:
    """The variant of this product in the colour the shopper picked."""
    colors = [c["color"] for c in p.get("colors_available", [])] or [p["color"]]
    key = color_key(p["base_id"], where)
    if ss.get(key) not in colors:
        # The colour asked for (search words, filter), else the product's
        # lead colour — not whichever variant the search happened to rank first.
        ss[key] = preferred if preferred in colors else colors[0]
    sku = next((c["sku"] for c in p.get("colors_available", []) if c["color"] == ss[key]),
               p["sku"])
    return all_products().get(sku, p)


def color_picker(p: dict, where: str = "card") -> None:
    colors = [c["color"] for c in p.get("colors_available", [])]
    if len(colors) < 2:
        return
    key = color_key(p["base_id"], where)
    st.pills("Colour", colors, key=key, selection_mode="single",
             label_visibility="collapsed")
    if not ss.get(key):                        # clicking the selected pill clears it
        ss[key] = colors[0]


def swatches(p: dict, selected: str) -> str:
    return "".join(
        f'<span class="swatch" title="{c["color"]}" style="background:{c["hex"]};'
        f'{"outline:2px solid #CC0000;outline-offset:1px;" if c["color"] == selected else ""}'
        f'"></span>' for c in p.get("colors_available", []))


@st.dialog("Product details", width="large")
def product_dialog(p: dict) -> None:
    ss[color_key(p["base_id"], "dlg")] = ss.get(color_key(p["base_id"]), p["color"])
    v = selected_variant(p, "dlg")
    a, b = st.columns([1.15, 1.2])
    a.markdown(f'<img src="{image_uri(v)}" '
               f'style="width:100%;border:1px solid #eee;border-radius:6px"/>',
               unsafe_allow_html=True)
    with b:
        st.markdown(f"### {v.get('base_name', v['name'])}")
        st.markdown(f'<span class="stars">{stars(v["rating"])}</span> '
                    f'<span class="muted">{v["rating"]} ({v["reviews"]:,} reviews) · '
                    f'{v["brand"]} · SKU {v["sku"]}</span>', unsafe_allow_html=True)
        st.markdown(f'<div class="price">{usd(v["price"])}</div>', unsafe_allow_html=True)
        if len(v.get("colors_available", [])) > 1:
            st.markdown(f"**Colour: {v['color']}** &nbsp;" + swatches(v, v["color"]),
                        unsafe_allow_html=True)
            color_picker(p, "dlg")
        st.write(v["description"])
        d = v.get("dims") or {}
        if d:
            st.caption(f"Dimensions: {d['w']}\" W × {d['d']}\" D × {d['h']}\" H")
        st.caption(f"{v['stock']} in stock" if v["stock"] else
                   "Out of stock in this colour — ships in 2 weeks")
        qty = st.number_input("Quantity", 1, 100, 1, key=f"q_{p['base_id']}")
        if st.button(f"Add to cart — {v['color']}", type="primary",
                     key=f"dlg_{p['base_id']}"):
            t = trace.new()
            commerce.add(ss.customer_id, v["sku"], qty, trace=t)
            traced("add_to_cart", t, f"Add {qty} × {v['name']} to the cart")
            st.toast(f"Added {qty} × {v['name'][:44]}")
            st.rerun()
    if v["kind"] == "printer":
        st.markdown("**Compatible ink & toner**")
        for c in search.compatible_cartridges(v["cartridge_family"]):
            st.write(f"- {c['base_name']} — {usd(c['price'])} ({c['page_yield']:,} pages)")


def card(p: dict, key: str, preferred: str | None = None, where: str = "card") -> None:
    v = selected_variant(p, where, preferred)
    with st.container(border=True):
        st.markdown(
            f'<img src="{image_uri(v)}" style="width:100%;aspect-ratio:4/3;'
            f'object-fit:cover;border-radius:4px"/>'
            f'<div class="pname">{v.get("base_name", v["name"])}</div>'
            f'<span class="stars">{stars(v["rating"])}</span> '
            f'<span class="muted">({v["reviews"]:,})</span>'
            f'<div class="price">{usd(v["price"])}</div>'
            f'<div class="muted" style="margin:2px 0 4px 0">{swatches(v, v["color"])} '
            f'{v["color"]} · {"In stock" if v["stock"] else "Backorder"}</div>',
            unsafe_allow_html=True)
        color_picker(p, where)
        b1, b2 = st.columns(2)
        if b1.button("Add to cart", key=f"add_{key}", type="primary", width="stretch"):
            t = trace.new()
            commerce.add(ss.customer_id, v["sku"], 1, trace=t)
            traced("add_to_cart", t, f"Add 1 × {v['name']} to the cart")
            st.toast(f"Added {v['name'][:44]}")
            st.rerun()
        if b2.button("Details", key=f"det_{key}", width="stretch"):
            product_dialog(p)


def grid(products: list[dict], prefix: str, cols: int = 4,
         preferred: str | None = None, where: str = "card") -> None:
    for i in range(0, len(products), cols):
        row = st.columns(cols)
        for col, p in zip(row, products[i:i + cols]):
            with col:
                card(p, f"{prefix}_{p['base_id']}", p["color"] if preferred == "own"
                     else preferred, where)


def _my_hints() -> dict:
    """What we remember about the signed-in customer (the Room Planner's reading)."""
    if is_guest():
        return {"used": []}
    _planner_defaults()                        # read once per customer
    return ss.get("rp_hints") or {"used": []}


def goes_with_row(items: list[dict], prefix: str) -> None:
    """What the products shown need, from what we remember — so nobody has to ask."""
    if not items:
        return
    mem = any(g.get("memory") for g in items)
    orders = any(g.get("from_order") for g in items)
    st.markdown("**🧠 Goes with it — from your orders and what we remember**" if mem and orders
                else "**🧾 Goes with what you've bought**" if orders
                else "**🧠 Goes with it — from what we remember**" if mem
                else "**Goes with it**")
    cols = st.columns(5)
    for col, g in zip(cols, items):
        with col:
            st.markdown(f"<div class='muted' style='font-size:.8rem;margin-bottom:4px'>"
                        f"{html.escape(g['why'])}"
                        + (f" — <i>“{html.escape(g['memory'])}”</i>" if g.get("memory")
                           else "") + "</div>", unsafe_allow_html=True)
            card(g["product"], f"{prefix}_{g['product']['base_id']}",
                 g["product"]["color"], where=prefix)


def one_per_base(products: list[dict]) -> list[dict]:
    """Variants -> one entry per product, in ranking order."""
    seen, out = set(), []
    for p in products:
        if p["base_id"] in seen:
            continue
        seen.add(p["base_id"])
        out.append(p)
    return out


def color_named(text: str) -> str | None:
    low = (text or "").lower()
    return next((c for c in sorted(all_colors(), key=len, reverse=True)
                 if c.lower() in low), None)


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
def page_shop() -> None:
    with st.sidebar:
        st.markdown("### Shop by")
        cats = categories()
        cat = st.radio("Category", ["All"] + [c["category"] for c in cats], index=0)
        subs = next((c["subcategories"] for c in cats if c["category"] == cat), [])
        sub = st.selectbox("Department", ["All"] + subs) if subs else "All"
        color = st.selectbox("Colour", ["Any"] + all_colors())
        max_price = st.slider("Max price ($)", 10, 2000, 2000, step=10)
        sort = st.selectbox("Sort by", ["rating", "price_low", "price_high", "name"],
                            format_func=lambda s: {"rating": "Top rated",
                                                   "price_low": "Price: low to high",
                                                   "price_high": "Price: high to low",
                                                   "name": "Name"}[s])
    t = trace.new()
    kw = dict(category=None if cat == "All" else cat,
              color=None if color == "Any" else color,
              max_price=None if max_price >= 2000 else float(max_price))
    over: list = []                    # over their budget or max price: listed last
    if ss.q:
        st.markdown(f"#### Results for “{ss.q}”")
        h = _my_hints()
        cap, why = companions.item_cap(h, kw["max_price"])
        # search past the budget too, so the options above it can be shown after
        results = search.hybrid(ss.q, limit=36 if cap else 24, trace=t,
                                **{**kw, "max_price": None})
        if not kw["category"] and not kw["color"]:   # the kinds they like, too
            results = companions.with_liked(results, h)
        results = companions.prefer(results, h, f"{ss.q} {kw['color'] or ''}",
                                    max_price=kw["max_price"], keep_over=True, trace=t)
        if sub != "All":
            results = [p for p in results if p["subcategory"] == sub]
        results, over = companions.split(one_per_base(results), cap)
        bases = {p["base_id"] for p in results}
        over = [p for p in one_per_base(over) if p["base_id"] not in bases]
        if over and not results:
            st.info(f"Since {why}, we looked for what fits it first — nothing here does. "
                    f"Below are {len(over)} option{'s' if len(over) != 1 else ''} above it, "
                    f"in case you want to go over, or try other words."
                    .replace("$", "\\$"), icon=":material/savings:")
        elif over:
            st.info(f"Since {why}, what fits it comes first. {len(over)} more option"
                    f"{'s' if len(over) != 1 else ''} above it "
                    f"{'are' if len(over) != 1 else 'is'} further down — in case you want "
                    f"to go over.".replace("$", "\\$"), icon=":material/savings:")
        elif cap and results:
            st.caption(f"Everything here fits — {why}.".replace("$", "\\$"))
        if h.get("max_desk_w"):
            st.caption(f"🧠 Showing desks up to {h['max_desk_w']}″ wide — what you told us")
        goes = companions.goes_with(results[:12], h, ss.customer_id, trace=t)
        sig = (ss.q, repr(kw), sub)
        if ss.get("search_logged") != sig:          # once per search, not per rerun
            ss.search_logged = sig
            traced("search", t, f"Search: {ss.q}", f"{len(results)} products"
                   + (f", {len(over)} more over budget" if over else ""))
        st.caption("Found by **keyword** (Full-Text Search) and by **meaning** "
                   "(vector search), merged. Hover a card's details for why.")
        st.button("✕ Clear search", on_click=_clear_search)
    else:
        if cat == "All" and sub == "All":
            h1, h2 = st.columns([3, 1])
            h1.markdown('<div class="hero"><h2>Plan your office in minutes</h2><p>Tell us '
                        'the room size — or upload a photo — and we\'ll suggest the desks, '
                        'chairs, printer, display and supplies that fit, with bundle '
                        'savings.</p></div>', unsafe_allow_html=True)
            if h2.button("Try the Room Planner →", type="primary", width="stretch"):
                go("Room Planner")
                st.rerun()
        if cat != "All" or sub != "All":
            st.markdown(f"#### {sub if sub != 'All' else cat}")
        results = search.browse(subcategory=None if sub == "All" else sub, sort=sort,
                                limit=1000, trace=t, **kw)
    preferred = kw["color"] or color_named(ss.q) or ("own" if ss.q else None)
    results = one_per_base(results)
    if not results and not over:
        st.info("No products match. Try fewer filters or different words.")
    view = (ss.q, cat, sub, sort, repr(kw))          # a new list starts at the top 24
    if ss.get("shop_view") != view:
        ss.shop_view, ss.shop_n = view, 24
    grid(results[:ss.shop_n], "shop", preferred=preferred)
    if ss.q:                                   # the results first, then what goes with them
        goes_with_row(goes, "shopgw")
    if len(results) > ss.shop_n:
        if st.button(f"Show more ({len(results) - ss.shop_n} more products)"):
            ss.shop_n += 24
            st.rerun()
    elif len(results) > 24:
        st.caption(f"All {len(results)} products shown.")
    if over:
        st.markdown("#### Over your budget — in case you want to stretch")
        grid(over[:12], "shopover", preferred=preferred)
    show_trace(t)
    if st.button("Photo credits", type="tertiary"):
        go("Photo credits")
        st.rerun()


def _add_offer(items: list[tuple[str, str, int]]) -> None:
    """A cart offer's one-click add."""
    t = trace.new()
    commerce.add_many(ss.customer_id, [(sku, q) for sku, _, q in items], trace=t,
                      source="nudge")
    traced("add_to_cart", t, "Add from the cart's offer: "
           + ", ".join(f"{q} × {name}" for _, name, q in items))
    ss.last_trace = t
    n = sum(q for _, _, q in items)
    st.toast(f"Added {n} item{'s' if n != 1 else ''} to your cart")


def page_cart() -> None:
    t = trace.new()
    cart = commerce.view(ss.customer_id, trace=t, shipping=ss.get("ship_pick"))
    st.markdown("### Your cart")
    if ss.last_order:
        o = ss.last_order
        if o["status"] == "paid":
            st.success(f"✅ Order **{o['order']['order_id']}** placed — "
                       f"{usd(o['order']['total'])} charged to {o['order']['card']}. "
                       f"{shipping_words(o['order'])}.")
        ss.last_order = None
    if not cart["lines"]:
        st.info("Your cart is empty. Browse the shop or plan a room to fill it.")
        show_trace(t)
        return
    left, right = st.columns([2.2, 1])
    with left:
        for line in cart["lines"]:
            p, q = line["product"], line["qty"]
            a, b, c, d = st.columns([0.8, 3.2, 1.2, 1])
            a.markdown(f'<img src="{image_uri(p)}" style="width:100%"/>',
                       unsafe_allow_html=True)
            b.markdown(f"**{p.get('base_name', p['name'])}**  \n<span class='muted'>"
                       f"Colour: {p['color']} · SKU {p['sku']} · {usd(p['price'])} each"
                       f"</span>", unsafe_allow_html=True)
            # keyed by the count too: a count changed elsewhere (an offer's Add) is a new box,
            # so the browser can't send the old one back
            nq = c.number_input("Qty", 0, 200, q, key=f"cq_{p['sku']}_{q}",
                                label_visibility="collapsed")
            if nq != q:
                commerce.set_qty(ss.customer_id, p["sku"], nq)
                st.rerun()
            d.markdown(f"**{usd(p['price'] * q)}**")
        if st.button(f"🗑 Clear cart ({cart['units']} items)", key="cart_clear"):
            commerce.clear(ss.customer_id)
            st.toast("Cart cleared")
            st.rerun()
        h = _my_hints() if cart["nudges"] else {}
        for k, n in enumerate(cart["nudges"]):         # each offer, with what gets it
            items = companions.for_nudge(n["add"], cart["lines"], h)
            if not items:
                st.markdown(f'<div class="nudge">💡 {n["text"]}</div>', unsafe_allow_html=True)
                continue
            x, y = st.columns([3.4, 1], vertical_alignment="center")
            x.markdown(f'<div class="nudge">💡 {n["text"]}<br><span class="muted">Adds '
                       + ", ".join(f"{q} × {html.escape(p_['name'])}" if q > 1
                                   else html.escape(p_["name"]) for p_, q in items)
                       + f' — {usd(sum(p_["price"] * q for p_, q in items))}</span></div>',
                       unsafe_allow_html=True)
            y.button("🛒 Add to cart", key=f"nudge{k}::"
                     + ",".join(f"{p_['sku']}x{q}" for p_, q in items), width="stretch",
                     on_click=_add_offer, args=([(p_["sku"], p_["name"], q)
                                                 for p_, q in items],))
    with right:
        with st.container(border=True):
            st.markdown("#### Order summary")
            st.write(f"Subtotal ({cart['units']} items): **{usd(cart['subtotal'])}**")
            for s in cart["savings"]:
                st.markdown(f'<span class="save">− {usd(s["amount"])} · {s["name"]}</span>',
                            unsafe_allow_html=True)
            shipping_picker(cart)
            delivery = cart["delivery"]
            st.write(f"Delivery ({cart['shipping']['label']}): "
                     + ("FREE" if not delivery else f"{usd(delivery)}"))
            st.write(f"Estimated tax: {usd(cart['tax'])}")
            st.markdown(f"### Total: {usd(cart['total'])}")
            if cart["discount"]:
                st.markdown(f'<span class="save">You save {usd(cart["discount"])}</span>',
                            unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown("#### Checkout")
            st.caption("Demo store — use test card **4242 4242 4242 4242** (approved) or "
                       "**4000 0000 0000 0002** (declined), any future expiry and CVC.")
            with st.form("pay"):
                name = st.text_input("Name on card", "Demo Customer")
                number = st.text_input("Card number", "4242 4242 4242 4242")
                e, c = st.columns(2)
                exp = e.text_input("Expiry (MM/YY)", "12/29")
                cvc = c.text_input("CVC", "123", type="password")
                if st.form_submit_button("Place order", type="primary", width="stretch"):
                    ss.pay_card = {"name": name, "number": number, "exp": exp, "cvc": cvc}
                    _place_order(t, False)
            ob = ss.get("over_budget")
            if ob and abs(ob.get("total", 0) - cart["total"]) > 0.005:
                ob = ss.over_budget = None          # the cart changed: ask again at checkout
            if ob:
                st.warning(f"🧠 {ob['message']}".replace("$", "\\$"))
                if st.button("Order anyway", key="pay_anyway", width="stretch"):
                    _place_order(t, True)
    # what the cart's items need, from what we remember — a mat for a chair on their
    # carpet, a dual arm for two monitors; offers above already cover their own kinds
    offered = {a["kind"] for n in cart["nudges"] for a in n["add"]}
    goes = [g for g in companions.related([ln["product"] for ln in cart["lines"]], _my_hints(),
                                          None if is_guest() else ss.customer_id, trace=t,
                                          from_orders=False)
            if g["product"].get("kind") not in offered]
    goes_with_row(goes, "cartgw")
    show_trace(t)


def shipping_picker(cart: dict) -> None:
    """Shipping speed for this order: the remembered preference unless the shopper
    picks another here."""
    sh = cart["shipping"]
    if sh.get("from_memory") and ss.get("ship_pick") is None:
        ss.ship_told = sh["speed"]
    names = {k: f"{v['label']} · " + ("free over \\$500" if v["fee"] is None
                                      else f"\\${v['fee']:,.2f}")
             for k, v in offers.SHIPPING.items()}
    pick = st.segmented_control("Shipping", list(names), format_func=names.get,
                                default=sh["speed"], key=f"ship_{sh['speed']}")
    if pick and pick != sh["speed"]:
        ss.ship_pick = None if pick == ss.get("ship_told") else pick
        st.rerun()
    if sh.get("from_memory"):
        st.caption(f"🧠 From your preference: “{sh['memory']}”".replace("$", "\\$"))
    elif ss.get("ship_told") and ss.get("ship_pick"):
        st.caption("Changed for this order — your saved preference is "
                   f"{offers.SHIPPING[ss.ship_told]['label']}.")
    st.caption(f"Arrives by {_day(sh['arrives_by'])} ({sh['eta']}).")


def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a, %b %-d")


def shipping_words(order: dict) -> str:
    """"2-Day shipping (your preference) — arrives by Wed, Oct 28"; orders from
    before shipping speeds were Standard."""
    sh = order.get("shipping") or {"label": "Standard"}
    out = f"{sh['label']} shipping" + (" (your preference)" if sh.get("from_memory") else "")
    return out + (f" — arrives by {_day(order['arrives_by'])}" if order.get("arrives_by")
                  else "")


def _place_order(t: trace.Trace, over_budget_ok: bool) -> None:
    """Checkout with the card just entered; over the remembered budget, ask first."""
    res = commerce.checkout(ss.customer_id, ss.get("pay_card") or {},
                            session_id=ss.session_id, trace=t, over_budget_ok=over_budget_ok,
                            shipping=ss.get("ship_pick"))
    traced("checkout", t, "Place the order" + (" — over my budget, order anyway"
                                               if over_budget_ok else ""),
           res.get("message") or res["status"])
    ss.over_budget = res if res["status"] == "over_budget" else None
    if res["status"] == "paid":
        ss.last_order = res
        ss.last_trace = t
        ss.ship_pick = None                  # the next order starts from the preference
        st.rerun()
    elif res["status"] != "over_budget":
        st.error(res["message"].replace("$", "\\$"))


def page_orders() -> None:
    st.markdown("### My orders")
    rows = commerce.orders_for(ss.customer_id)
    if not rows:
        st.info("No orders yet.")
        return
    for o in rows:
        with st.container(border=True):
            st.markdown(f"**{o['order_id']}** · {o['created_at'][:16].replace('T', ' ')} UTC · "
                        f"**{o['status'].replace('_', ' ')}** · {usd(o['total'])}"
                        + (f" · {o['card']}" if o.get("card") else ""))
            st.caption(", ".join(f"{line['qty']} × {line['name']}" for line in o["lines"]))
            st.caption(f"🚚 {shipping_words(o)}")
    show_trace(ss.last_trace, "Last checkout — what happened")


# --------------------------------------------------------------------------- #
# Room Planner
# --------------------------------------------------------------------------- #
QUICK_HOME: list = []      # home rooms come from Your room size… or a photo
QUICK_HQ = [
    ("Executive 14′×14′", "executive suite", 14, 14, 1),
    ("Manager 15′×12′", "private office", 15, 12, 1),
    ("Conference 20′×14′ · 10", "conference room", 20, 14, 10),
    ("Boardroom 30′×18′ · 16", "boardroom", 30, 18, 16),
    ("Cubicles 6′×8′ · 8", "open office", 28, 23, 8),
]


def _current_customer() -> dict:
    return next((c for c in customers() if c["customer_id"] == ss.customer_id), {})


def _set_room(room_type: str, length_ft: float, width_ft: float, people: int) -> None:
    ss.rp_type, ss.rp_people = room_type, people
    ss.rp_len_ft, ss.rp_len_in = int(length_ft), 0
    ss.rp_wid_ft, ss.rp_wid_in = int(width_ft), 0
    _equipment_defaults(room_type)
    _apply_hints(planner.for_room(ss.get("rp_hints") or {}, room_type))
    ss.rp_go = True
    ss.rp_owned, ss.rp_photo_trace = [], None     # a typed-in room has no photo
    ss.rp_replicate = None
    ss.rp_photo_bytes = None


def _equipment_defaults(room_type: str) -> None:
    """The room type's usual TV, whiteboard, rug and meeting table — set, not
    cleared, so the checkboxes show exactly what the plan will use."""
    d = planner.RoomSpec(room_type, 1, 1).defaults()
    ss.rp_tv, ss.rp_wb, ss.rp_decor = d.tv, d.whiteboard, d.decor
    ss.rp_meeting = bool(d.meeting_table) or room_type != "private office"
    ss.rp_video = bool(d.video_calls)


PLANNER_KEYS = ("rp_type", "rp_len_ft", "rp_len_in", "rp_wid_ft", "rp_wid_in", "rp_people",
                "rp_tier", "rp_color", "rp_chair",
                "rp_carpet", "rp_tv", "rp_wb", "rp_decor", "rp_standing", "rp_ergo",
                "rp_shape", "rp_meeting", "rp_monitors", "rp_video", "rp_display",
                "rp_table_style", "rp_desk_style", "rp_moving")


def _keep_planner_settings(save: bool) -> None:
    """Streamlit forgets a widget's value on any page that doesn't draw it, so
    the planner's settings are kept in a plain copy across visits to other pages."""
    if save:
        ss._rp_saved = {k: ss[k] for k in PLANNER_KEYS if k in ss}
    else:
        for k, v in ss.get("_rp_saved", {}).items():
            if k not in ss:
                ss[k] = v


def _planner_defaults() -> None:
    """Fresh settings for this customer: their room and what we remember."""
    if ss.get("rp_for") == ss.customer_id:
        return
    t = trace.new()
    hints = {"used": []} if is_guest() else planner.room_hints(ss.customer_id, all_colors(),
                                                                trace=t)
    ss.rp_hints, ss.rp_hint_trace, ss.rp_for = hints, t, ss.customer_id
    room = planner.persona_room(_current_customer()) or {
        "room_type": "home office", "length_ft": 11, "width_ft": 10, "people": 1}
    _set_room(room["room_type"], room["length_ft"], room["width_ft"], room["people"])
    ss.rp_go = False
    ss.rp_tier = "Recommended"
    _apply_hints(_room_hints())
    ss.rp_display, ss.rp_table_style, ss.rp_desk_style = "Narrow wall", "Standard", "U-shaped"
    ss.rp_shape = "Any"
    ss.plan = None


def _room_hints() -> dict:
    """What we remember that applies to the room being planned — for an office at
    work, the colours and what's about the workplace."""
    return planner.for_room(ss.get("rp_hints") or {"used": []}, ss.get("rp_type", "home office"))


def _apply_hints(hints: dict) -> None:
    """The planner settings that what we remember about the customer implies."""
    ss.rp_color = hints.get("color", "Any")
    ss.rp_chair = hints.get("chair_color", "Any")
    ss.rp_carpet = bool(hints.get("carpet"))
    ss.rp_monitors = int(hints.get("monitors", 0))
    ss.rp_standing = bool(hints.get("standing"))
    ss.rp_ergo = bool(hints.get("ergonomic"))
    if "video_calls" in hints and ss.get("rp_type") in planner.MEETING_ROOMS:
        ss.rp_video = bool(hints["video_calls"])   # "all our conference rooms need video calls"


def _spec() -> planner.RoomSpec:
    g = ss.get
    length = planner.to_inches(g("rp_len_ft", 11), "ft", g("rp_len_in", 0))
    width = planner.to_inches(g("rp_wid_ft", 10), "ft", g("rp_wid_in", 0))
    if g("rp_type") == "open office" and g("rp_replicate") is None:
        length, width = planner.cubicle_room(g("rp_people", 1))
    h = _room_hints()
    color, chair = g("rp_color", "Any"), g("rp_chair", "Any")
    return planner.RoomSpec(
        g("rp_type", "home office"), length, width, g("rp_people", 1),
        g("rp_tier") or "Recommended",
        color=None if color == "Any" else color, chair_color=None if chair == "Any" else chair,
        carpet=g("rp_carpet", False), monitors=g("rp_monitors", 0), tv=g("rp_tv"),
        whiteboard=g("rp_wb"), decor=g("rp_decor"), standing=g("rp_standing", False),
        ergonomic=g("rp_ergo", False), min_desk_w=h.get("min_desk_w", 0),
        max_desk_w=h.get("max_desk_w", 0), printer=h.get("printer"),
        budget=h.get("budget"), max_item=h.get("max_item"), table_shape=g("rp_shape") or "Any",
        replicate=copy.deepcopy(g("rp_replicate")),
        video_calls=g("rp_video"), display_wall=g("rp_display") or "Narrow wall",
        table_style=g("rp_table_style") or "Standard",
        desk_style=g("rp_desk_style") or "U-shaped",
        meeting_table=g("rp_meeting", True) if g("rp_type") == "private office" else None)


@st.cache_data(ttl=600, show_spinner=False)
def _max_people(spec: planner.RoomSpec) -> int:
    """Rebuilt only when the room or its options change, not the headcount."""
    return planner.max_people(spec)


@st.dialog("Your room")
def custom_room() -> None:
    """Enter your own room: what it is, how big, how many people."""
    kind = st.radio("Room", ["home office", "bedroom office"], horizontal=True,
                    format_func=lambda t: planner.ROOM_LABELS.get(t, t), key="cr_kind")
    a, b, c, d = st.columns([1.2, 1, 1.2, 1])
    lf = a.number_input("Length (ft)", 5, 100, 12, key="cr_lf")
    li = b.number_input("in", 0, 11, 0, key="cr_li")
    wf = c.number_input("Width (ft)", 5, 100, 10, key="cr_wf")
    wi = d.number_input("in ", 0, 11, 0, key="cr_wi")
    n = st.number_input("People working here", 1, 20, 1, key="cr_n")
    if st.button("Plan my room", type="primary", width="stretch", key="cr_go"):
        _set_room(kind, lf, wf, n)
        ss.rp_len_in, ss.rp_wid_in = li, wi
        st.rerun()


SAMPLE_PHOTOS = {"Bedroom with a desk": "assets/rooms/bedroom-office-1.jpg",
                 "Home office": "assets/rooms/home-office-2.jpg",
                 "Executive office": "assets/rooms/executive-suite-1.jpg",
                 "Meeting room": "assets/rooms/conference-room-1.jpg"}
OWNED_WORDS = {"desk": "desk", "chair": "chair", "storage": "shelves or cabinets",
               "lamp": "a lamp", "printer": "a printer", "monitor": "a monitor",
               "tv": "a TV", "whiteboard": "a whiteboard", "rug": "a rug",
               "conference_table": "a table", "wastebasket": "a bin"}


@st.dialog("Plan from a photo", width="large")
def photo_room() -> None:
    """Upload a room photo; the local vision model reads it; you check the reading."""
    st.caption("A photo of the room from a corner works best. The local vision model reads "
               "the room type, an estimated size, the floor, colours and what's already there.")
    up = st.file_uploader("Room photo", type=["jpg", "jpeg", "png"], key="ph_file")
    sample = st.selectbox("…or try a sample photo", ["—"] + list(SAMPLE_PHOTOS), key="ph_sample")
    data = up.getvalue() if up else (
        (ROOT / SAMPLE_PHOTOS[sample]).read_bytes() if sample != "—" else None)
    if not data:
        return
    a, b = st.columns([1, 1.3])
    a.image(data, width="stretch")
    fp = vision.fingerprint(data)
    if ss.get("ph_fp") != fp:
        ss.ph_fp, ss.ph_res = fp, None
    if ss.get("ph_res") is None:
        if b.button("Read my photo", type="primary", key="ph_go"):
            t = trace.new()
            with b, st.spinner("Reading the photo with the local vision model — about a "
                               "minute on this laptop, instant if it's been read before…"):
                ss.ph_res = vision.analyze_room_photo(data, customer_id=ss.customer_id,
                                                      session_id=ss.session_id, trace=t)
            ss.ph_trace = t
            traced("read_photo", t, "Read my room photo",
                   (ss.ph_res.get("room") or {}).get("notes") or ss.ph_res.get("message", ""))
            st.rerun(scope="fragment")
        return
    res = ss.ph_res
    if not res["ok"]:
        b.error(res["message"])
        return
    r = res["room"]
    b.success(("Read before — reused from Couchbase. " if res.get("cached") else "")
              + (r.get("notes") or "Here's what the model saw."))
    seen = ", ".join(r["existing"][:8]) or "nothing obvious"
    b.markdown(f"**Seen:** {seen}  \n**Floor:** {'carpet' if r['carpet'] else 'hard floor'}"
               f" · **Colours:** {', '.join(r['colours']) or '—'}"
               + (f" · **Style:** {r['style']}" if r.get("style") else ""))
    b.caption("Sizes from a photo are estimates — check them before planning.")
    mode = b.radio("What should we plan?", PHOTO_MODES, key="ph_mode", horizontal=True,
                   help="Suggest: our planner lays out the room afresh and adds what it "
                        "usually needs. Replicate: the same furniture as the photo, where "
                        "the photo has it, and nothing extra.")
    kind = b.selectbox("Room type", planner.ROOM_TYPES, key="ph_type",
                       index=planner.ROOM_TYPES.index(r["room_type"]),
                       format_func=lambda t: planner.ROOM_LABELS.get(t, t))
    c1, c2, c3 = b.columns(3)
    lf = c1.number_input("Length (ft)", 5, 100, int(r["length_ft"]), key="ph_lf")
    wf = c2.number_input("Width (ft)", 5, 100, int(r["width_ft"]), key="ph_wf")
    if mode == PHOTO_MODES[1]:
        _replicate_from_photo(b, data, fp, r, kind, lf, wf)
        return
    n = c3.number_input("People", 1, 60, planner.DEFAULT_PEOPLE.get(kind, 1), key="ph_n")
    owned = [k for k in r["owned_kinds"] if k in OWNED_WORDS]
    skip = owned and b.checkbox("Leave out what's already there: "
                                + ", ".join(OWNED_WORDS[k] for k in owned), True,
                                key="ph_skip")
    b.markdown(_colour_note(), unsafe_allow_html=True)
    if b.button("Plan this room", type="primary", width="stretch", key="ph_plan"):
        _set_room(kind, lf, wf, n)
        _prefs_from_memory(r["carpet"])
        ss.rp_owned = owned if skip else []
        ss.rp_photo_bytes = data                 # shown beside the new layout
        ss.rp_photo_trace = ss.get("ph_trace")
        ss.ph_res = None
        st.rerun()


def _prefs_from_memory(photo_carpet: bool) -> None:
    """A plan from a photo follows what the customer told us they like — Agent
    Memory: colours, chairs, desks, monitors, printer, budget — not the photo's
    look. The floor is carpeted if the photo shows it or they told us so."""
    _apply_hints(_room_hints())
    ss.rp_carpet = bool(photo_carpet or ss.rp_carpet)


def _pref_words(h: dict) -> list[str]:
    """What we remember, as the planner will use it."""
    out = []
    if h.get("color"):
        out.append(f"<b>{html.escape(h['color'])}</b> furniture")
    if h.get("chair_color"):
        out.append(f"<b>{html.escape(h['chair_color'])}</b> chairs")
    if h.get("ergonomic"):
        out.append("ergonomic mesh chairs")
    if h.get("standing"):
        out.append("standing desks")
    if h.get("min_desk_w"):
        out.append(f"desks at least {h['min_desk_w']}″ wide")
    if h.get("max_desk_w"):
        out.append(f"desks no wider than {h['max_desk_w']}″")
    if h.get("monitors"):
        out.append(f"{h['monitors']} monitor{'s' if h['monitors'] != 1 else ''} per desk")
    if h.get("carpet"):
        out.append("a carpeted floor (chair mats)")
    if h.get("printer"):
        out.append("an inkjet printer" if h["printer"] == "inkjet" else "a colour printer")
    if h.get("budget"):
        out.append(f"a budget of {usd(h['budget'], 0)}")
    return out


def _colour_note() -> str:
    """What the plan takes from Agent Memory, in a line."""
    h = _room_hints()
    words = _pref_words(h)
    if not words:
        return ("<div class='muted' style='font-size:.85rem;margin:4px 0'>🎨 Colours and "
                "styles: our usual picks — " + (
                    "sign in and we'll use what you've told us you like." if is_guest() else
                    "tell the assistant what you like and we'll use it.") + "</div>")
    return ("<div class='muted' style='font-size:.85rem;margin:4px 0'>🧠 From what we "
            f"remember about you: {', '.join(words)}.</div>")


PHOTO_MODES = ["Suggest a new layout", "Replicate my room as is"]
FIT_FLAGS: dict = {}       # this run's "won't fit" slots under the count boxes


def _flag_fit(p: planner.Plan | None) -> None:
    """Under each count box: how many of that thing the room can't take."""
    for k, slot in FIT_FLAGS.items():
        n = (p.unplaced if p else {}).get(k, 0)
        if n:
            slot.markdown(f"<div style='color:#b42318;font-size:0.82rem;margin-top:-6px'>"
                          f"⚠️ won't fit — {n} too many for this room</div>",
                          unsafe_allow_html=True)
    FIT_FLAGS.clear()


def replica_editor(where, counts: dict[str, int], prefix: str) -> dict[str, int]:
    """A count for each kind of thing in the room, and a way to add another kind."""
    added = ss.setdefault(f"{prefix}::added", [])
    kinds = [k for k in planner.REPLICA_KINDS if k in counts or k in added]
    out = {}
    cols = where.columns(3)
    for i, k in enumerate(kinds):
        key = f"{prefix}::{k}"
        if key not in ss:
            ss[key] = int(counts.get(k, 1))
        many = planner.REPLICA_NAMES[k][1]
        out[k] = cols[i % 3].number_input(many[0].upper() + many[1:], 0, 60, key=key)
        FIT_FLAGS[k] = cols[i % 3].empty()      # filled once the plan says what fits
    missing = [k for k in planner.REPLICA_KINDS if k not in kinds]
    if missing:
        a, b = where.columns([2, 1], vertical_alignment="bottom")
        a.selectbox("Add an item", missing, key=f"{prefix}::pick",
                    format_func=lambda k: planner.REPLICA_NAMES[k][0][0].upper()
                    + planner.REPLICA_NAMES[k][0][1:])
        b.button("➕ Add", key=f"{prefix}::add", width="stretch", on_click=_add_kind,
                 args=(prefix,))
    return out


def _add_kind(prefix: str) -> None:
    """Kept by name in session state: the counts dict is rebuilt on every run."""
    kind = ss[f"{prefix}::pick"]
    ss[f"{prefix}::added"] = [*ss.get(f"{prefix}::added", []), kind]
    ss[f"{prefix}::{kind}"] = 1


def _replicate_from_photo(b, data: bytes, fp: str, r: dict, kind: str, lf: int,
                          wf: int) -> None:
    """Photo dialog, replicate mode: find each piece of furniture, let the
    shopper fix the counts, then plan exactly that."""
    if ss.get("ph_lay_fp") != fp + kind:      # the room type decides what a "table" is
        if not vision.layout_saved(data) and not ss.get("ph_find_go"):
            b.caption("To copy the room as it is, the vision model first finds each piece "
                      "of furniture and where it sits — about a minute and a half for a new "
                      "photo. You'll check the counts before anything is planned.")
            if b.button("Find my furniture", type="primary", width="stretch",
                        key="ph_find"):
                ss.ph_find_go = True
                st.rerun(scope="fragment")
            return
        ss.ph_find_go = False
        t = trace.new()
        with b, st.spinner("Finding each piece of furniture and where it is — about a "
                           "minute and a half for a new photo, instant if it's been read "
                           "before…"):
            res = vision.locate_objects(data, room_type=kind, customer_id=ss.customer_id,
                                        session_id=ss.session_id, trace=t)
        ss.ph_lay_fp, ss.ph_lay_res, ss.ph_lay_trace = fp + kind, res, t
        traced("replicate_photo", t, "Find each piece of furniture in my photo",
               ", ".join(f"{n} × {k.replace('_', ' ')}" for k, n in
                         res["layout"]["counts"].items()) if res["ok"] else res["message"])
        ss.ph_lay_id = ss.get("ph_lay_id", 0) + 1        # fresh count boxes
        ss.ph_counts = dict(res["layout"]["counts"]) if res["ok"] else {}
    res = ss.ph_lay_res
    if not res["ok"]:
        b.error(res["message"])
        return
    lay = res["layout"]
    b.markdown("**What's in your room**")
    b.caption("Counted from your photo — the model can miss things half-hidden behind a "
              "table. Fix any count (0 removes it) or add an item. You can change these "
              "again after planning.")
    ss.ph_counts = replica_editor(b, ss.ph_counts, f"phc{ss.ph_lay_id}")
    if lay["not_sold"]:
        b.caption("Also seen, not sold here: " + ", ".join(lay["not_sold"][:6]))
    # a size read from a photo is a guess; the furniture in it is not
    want = (fp, kind, lf, wf, repr(sorted(ss.ph_counts.items())))
    if ss.get("ph_size_for") != want:
        spec = planner.RoomSpec(kind, lf * 12, wf * 12, replicate={
            "items": lay["items"], "counts": dict(ss.ph_counts), "extras": {},
            "aspect": lay.get("aspect")})
        ss.ph_size_for, ss.ph_size = want, planner.replica_size(spec)
    if ss.ph_size:
        lf, wf = (max(lf, -(-int(ss.ph_size[0]) // 12)), max(wf, -(-int(ss.ph_size[1]) // 12)))
        b.warning(f"📏 The table and chairs in your photo need a room of about **{lf}′ × "
                  f"{wf}′** — larger than the size guessed from the photo, so we'll plan "
                  f"it at that size. You can change it on the next page.")
    b.markdown(_colour_note(), unsafe_allow_html=True)
    if b.button("Plan this room", type="primary", width="stretch", key="ph_plan_rep",
                disabled=not any(ss.ph_counts.values())):
        _set_room(kind, lf, wf, max(1, ss.ph_counts.get("chair", 0)))
        _prefs_from_memory(r["carpet"])
        ss.rp_replicate = {"items": lay["items"], "counts": dict(ss.ph_counts), "extras": {},
                           "aspect": lay.get("aspect")}
        ss.rp_photo_bytes = data                 # shown beside the 3D view to compare
        ss.rp_rep_id = ss.get("rp_rep_id", 0) + 1
        t = trace.new()
        for part in (ss.get("ph_trace"), ss.get("ph_lay_trace")):
            if part:
                t.steps.extend(part.steps)
        ss.rp_photo_trace = t
        ss.ph_res, ss.ph_lay_fp = None, None
        st.rerun()


def _replica_banner(where) -> None:
    """Replicate mode: what's being copied, and a place to change it."""
    rep = ss.rp_replicate
    a, b = where.columns([3, 1], vertical_alignment="center")
    b.button("Back to a suggested layout", key="rp_unrep", width="stretch",
             on_click=_unreplicate)
    with where.expander("✏️ Change what's in the room"):
        counts = replica_editor(st, rep["counts"], f"rpc{ss.get('rp_rep_id', 0)}")
    if counts != rep["counts"]:          # the planner seats as many as there are chairs
        ss.rp_replicate = rep = {**rep, "counts": counts}
    a.info(f"📷 **Replicating your photo:** {planner.replica_words(rep)}. Nothing extra is "
           f"added — change anything below, or pick from the ideas under the list.")
    a.markdown(_colour_note(), unsafe_allow_html=True)


def _unreplicate() -> None:
    ss.rp_people = max(1, ss.rp_replicate["counts"].get("chair", 0),
                       planner.DEFAULT_PEOPLE.get(ss.rp_type, 1))
    ss.rp_replicate = None                      # same room: the photo stays beside it


BRING_LABELS = {"storage": "Cabinets & shelves", "conference_table": "Tables",
                "tv": "TVs", "video_bar": "Video bars", "side_table": "Side tables"}


def _bring() -> dict:
    """What they're bringing from their current room — when the Moving panel is on."""
    return (ss.get("rp_bring") or {}) if ss.get("rp_moving") else {}


def _fill_bring(counts: dict) -> None:
    """New counts from a photo, a memory or the chat: fresh boxes, filled with them."""
    ss.rp_bring = {k: int(n) for k, n in counts.items() if k in moving.NAMES and int(n) > 0}
    ss.rp_bring_gen = ss.get("rp_bring_gen", 0) + 1


def moving_panel() -> None:
    """Moving to a bigger room: what's coming from the current one — typed, counted
    from a photo of it, or remembered — so the plan lists only what to buy."""
    if ss.get("rp_bring_for") != ss.customer_id:  # first look for this customer:
        ss.rp_bring_for = ss.customer_id          # what we remember, the panel open
        _fill_bring((ss.get("rp_hints") or {}).get("bring") or {})
        ss.rp_moving = bool(ss.rp_bring)
    if not st.toggle("🚚 Moving from another room? Bring what you have — we'll work out "
                     "what else to buy", key="rp_moving"):
        return
    with st.container(border=True):
        typed, photo, remembered = st.tabs(["⌨️ Type it", "📷 From a photo of it",
                                            "🧠 Remembered"])
        gen, cur = ss.get("rp_bring_gen", 0), ss.get("rp_bring") or {}
        got = {}
        with typed:
            st.caption("How many of each are you bringing from your current room?")
            for row in (moving.COMMON, None):
                where = st if row else st.expander("More: lamps, printers, rugs, bins…")
                kinds = row or moving.MORE
                for col, k in zip(where.columns(len(kinds)), kinds):
                    name = BRING_LABELS.get(k) or moving.NAMES[k][1].capitalize()
                    got[k] = col.number_input(name, 0, 60, int(cur.get(k, 0)),
                                              key=f"mv{gen}::{k}")
        new = {k: int(n) for k, n in got.items() if n}
        if new != cur:
            ss.rp_bring = new
        with photo:
            _bring_from_photo()
        with remembered:
            h = ss.get("rp_hints") or {}
            if is_guest():
                st.caption("Sign in and we'll remember what's in your current room.")
            elif h.get("bring"):
                st.markdown(f"🧠 “{html.escape(h['from']['bring'])}”")
                st.button("Use these", key="mv_use", on_click=_fill_bring, args=(h["bring"],),
                          disabled=h["bring"] == ss.rp_bring)
            else:
                st.caption("Nothing remembered yet — type your counts or count them from a "
                           "photo, then press Remember this.")
        a, b = st.columns([3, 1], vertical_alignment="center")
        a.markdown(f"**Bringing:** {moving.words(ss.rp_bring)}. Each line below says how "
                   f"many you bring and how many to buy — matched by kind, so check your "
                   f"pieces suit the new room.")
        b.button("🧠 Remember this", key="mv_remember", width="stretch",
                 on_click=_remember_bring,
                 disabled=is_guest() or not ss.rp_bring
                 or ss.rp_bring == (ss.get("rp_hints") or {}).get("bring"),
                 help="Save what's in your current room to Agent Memory for next time.")


def _bring_from_photo() -> None:
    st.caption("A photo of your current room: the local vision model counts the furniture "
               "and fills in Type it, where you can fix any count.")
    up = st.file_uploader("Photo of your current room", type=["jpg", "jpeg", "png"],
                          key="mv_file")
    sample = st.selectbox("…or try a sample photo", ["—"] + list(SAMPLE_PHOTOS),
                          key="mv_sample")
    data = up.getvalue() if up else (
        (ROOT / SAMPLE_PHOTOS[sample]).read_bytes() if sample != "—" else None)
    if not data:
        return
    a, b = st.columns([1, 1.4])
    a.image(data, width="stretch")
    kind = b.selectbox("That room is a…", planner.ROOM_TYPES,
                       index=planner.ROOM_TYPES.index("private office"), key="mv_kind",
                       format_func=lambda t: planner.ROOM_LABELS.get(t, t),
                       help="It decides whether a table is a desk or a meeting table.")
    fp = vision.fingerprint(data) + kind
    if ss.get("mv_photo_fp") == fp:
        if ss.get("mv_photo_error"):
            b.error(ss.mv_photo_error)
        else:
            b.success(f"Counted: {moving.words(ss.mv_photo_counts)} — now in Type it.")
        return
    # counted in the button's callback, before the page is drawn: a rerun part-way
    # through it would lose the room's size boxes further down
    b.button("Count my furniture", type="primary", key="mv_count", on_click=_count_photo,
             args=(data, fp, kind), help="About a minute and a half for a new photo; "
                                         "instant if it's been read before.")


def _count_photo(data: bytes, fp: str, kind: str) -> None:
    t = trace.new()
    with st.spinner("Counting the furniture in the photo of your current room — about a "
                    "minute and a half for a new photo…"):
        res = vision.locate_objects(data, room_type=kind, customer_id=ss.customer_id,
                                    session_id=ss.session_id, trace=t)
    traced("count_current_room", t, "Count the furniture in my current room",
           ", ".join(f"{n} × {k.replace('_', ' ')}" for k, n in
                     res["layout"]["counts"].items()) if res["ok"] else res["message"])
    ss.mv_photo_fp = fp
    ss.mv_photo_error = None if res["ok"] else res["message"]
    if res["ok"]:
        _fill_bring(res["layout"]["counts"])
        ss.mv_photo_counts = dict(ss.rp_bring)


def _remember_bring() -> None:
    """Agent Memory keeps one 'current room' sentence: the new one replaces the old."""
    text = moving.memory_text(ss.get("rp_bring") or {})
    t = trace.new()
    t0 = time.perf_counter()
    try:
        old = [r["memory_id"] for r in memory.list_for(ss.customer_id, 50)
               if moving.CURRENT.search(str(r.get("text") or ""))]
        row = memory.remember(ss.customer_id, text, kind="fact", source="ui",
                              session_id=ss.session_id)
        old = [m for m in old if m != row.get("memory_id")]
        if old:
            memory.forget(ss.customer_id, old)
    except Exception as exc:
        st.toast(f"Agent Memory couldn't save that just now ({type(exc).__name__}) — "
                 "try again in a moment.", icon="⚠️")
        return
    t.add("Agent Memory", "Remembered your current room", text,
          "remember() on the Agent Memory server" + (", replacing the old one" if old else ""),
          (time.perf_counter() - t0) * 1000, writes=["agentmemory.memory"],
          indexes=["AgentMemory_BlockEmbeddingIndex"])
    traced("save_preference", t, "Remember: " + text)
    ss.last_trace = t
    keep = dict(ss.rp_bring)
    _refresh_hints()
    ss.rp_bring = keep
    st.toast("🧠 Remembered what's in your current room.")


def page_room_planner() -> None:
    _keep_planner_settings(save=False)
    _planner_defaults()
    st.markdown("### Room Planner")
    st.caption("Tell us the room's size and who will use it. We lay it out to scale — with "
               "space to sit, walk and open the door — and list everything it needs.")
    for row_label, quick in (("Home", QUICK_HOME), ("HQ", QUICK_HQ)):
        cols = st.columns([0.45] + [1] * 6)
        cols[0].markdown(f"<div style='padding-top:8px;font-weight:600'>{row_label}</div>",
                         unsafe_allow_html=True)
        for col, (label, rt, lf, wf, n) in zip(cols[1:], quick):
            col.button(label, key=f"qr::{label}", width="stretch", on_click=_set_room,
                       args=(rt, lf, wf, n))
        if row_label == "Home" and cols[1].button("Your room size…", key="qr::custom",
                                                  width="stretch"):
            custom_room()
        if row_label == "Home" and cols[2].button("📷 From a photo…", key="qr::photo",
                                                  width="stretch"):
            photo_room()

    if ss.get("plan") is None and not ss.get("rp_go"):
        # nothing until a room is chosen: a quick-start, your own size or a photo
        st.info("Pick a room above — or enter your own size, or start from a photo — and "
                "we'll plan it.")
        return
    if ss.get("rp_replicate") is None:
        moving_panel()
    left, right = st.columns([1, 2.3], gap="large")
    with left, st.container(border=True):
        # the room type comes from the buttons above (or a photo): named, not picked
        st.markdown(f"**{planner.ROOM_LABELS.get(ss.rp_type, ss.rp_type)}**")
        st.caption(planner.ROOM_HELP.get(ss.rp_type, ""))
        rep = ss.get("rp_replicate") is not None
        off = "Replicating your photo: change what's in the room above the plan."
        if ss.rp_type == "open office" and not rep:
            # cubicles: say how many; the floor is sized to hold them
            st.number_input("Cubicles (6′ × 8′ each, one per person)", 1, 60,
                            key="rp_people")
            L, W = planner.cubicle_room(ss.rp_people)
            st.caption(f"📐 Floor needed: about **{L / 12:.0f}′ × {W / 12:.0f}′** — rows "
                       f"of cubicles off 3′ walkways, an aisle to the door and a strip "
                       f"for shared files, printer and bins.")
        else:
            a, b, c, d = st.columns([1.2, 1, 1.2, 1])
            a.number_input("Length (ft)", 5, 100, key="rp_len_ft")
            b.number_input("in", 0, 11, key="rp_len_in")
            c.number_input("Width (ft)", 5, 100, key="rp_wid_ft")
            d.number_input("in ", 0, 11, key="rp_wid_in")
            # the People box stops at what this room can seat: past that the plan
            # has nowhere to put another chair
            most = 60 if rep else _max_people(dataclasses.replace(_spec(), people=1))
            if not rep and ss.get("rp_people", 1) > most:
                ss.rp_people = most
            size = f"{ss.rp_len_ft}′ × {ss.rp_wid_ft}′"
            name = planner.ROOM_LABELS.get(ss.rp_type, ss.rp_type).split(" (")[0].lower()
            st.number_input("People" + (" at the table" if ss.rp_type in planner.MEETING_ROOMS
                                        else " working here"), 1, max(most, 1),
                            key="rp_people", disabled=rep or most == 1,
                            help=off if rep else (
                                f"An {name} is for one person; the guest chairs come with "
                                f"it." if ss.rp_type in planner.ONE_PERSON else
                                f"A {size} {name} seats up to {most}. Make the room bigger "
                                f"for more."))
            if not rep and most > 1 and ss.rp_people >= most:
                st.caption(f"⛔ {most} is the most a {size} {name} seats — make the room "
                           f"bigger for more.")
        if not ss.get("rp_tier"):                 # clicking the chosen tier clears it
            ss.rp_tier = "Recommended"
        st.segmented_control("Budget", planner.TIERS, key="rp_tier")
        a, b = st.columns(2)
        a.selectbox("Furniture colour", ["Any"] + all_colors(), key="rp_color")
        b.selectbox("Chair colour", ["Any"] + all_colors(), key="rp_chair")
        a, b = st.columns(2)
        a.checkbox("Carpeted floor", key="rp_carpet")
        b.checkbox("TV / display", key="rp_tv", disabled=rep, help=off if rep else None)
        a.checkbox("Whiteboard", key="rp_wb", disabled=rep, help=off if rep else None)
        b.checkbox("Rug & floor lamp", key="rp_decor",
                   disabled=rep or ss.rp_type not in ("home office", "bedroom office"),
                   help=off if rep else "For home rooms only.")
        # Controls that don't apply are greyed out, not hidden: a hidden widget
        # loses its value in Streamlit.
        meeting = ss.rp_type in planner.MEETING_ROOMS
        tables = ss.rp_type in ("conference room", "boardroom", "private office")
        a.checkbox("Standing desks", key="rp_standing", disabled=meeting)
        b.checkbox("Ergonomic chairs", key="rp_ergo", disabled=meeting)
        stock = planner.stocked()          # only offer what the catalogue can fill
        for key, opts in (("rp_shape", stock["shapes"]),
                          ("rp_table_style", stock["table_styles"]),
                          ("rp_desk_style", stock["desk_styles"])):
            if ss.get(key) not in opts:
                ss[key] = opts[0]
        a.selectbox("Table shape", stock["shapes"], key="rp_shape",
                    disabled=not (tables or rep))
        b.checkbox("Meeting table", key="rp_meeting",
                   disabled=rep or ss.rp_type != "private office",
                   help="A small table for 4 in a manager's office, when the floor has room.")
        a.checkbox("Video calls", key="rp_video", disabled=rep,
                   help=off if rep else "A camera at the display and audio sized to the room: a "
                        "speakerphone per 10 seats, and a soundbar in a boardroom.")
        b.selectbox("Display wall", planner.DISPLAY_WALLS, key="rp_display",
                    disabled=rep or ss.rp_type not in ("conference room", "boardroom"))
        a.selectbox("Table style", stock["table_styles"], key="rp_table_style",
                    disabled=rep or ss.rp_type != "huddle room"
                    or len(stock["table_styles"]) < 2)
        b.selectbox("Desk style", stock["desk_styles"], key="rp_desk_style",
                    disabled=rep or ss.rp_type != "executive suite"
                    or len(stock["desk_styles"]) < 2)
        st.select_slider("Monitors per desk", [0, 1, 2, 3], key="rp_monitors",
                         disabled=meeting or rep, help=off if rep else None)
        used = _room_hints().get("used", [])
        if used and not is_guest():
            # the memories themselves are listed once, in the sidebar
            st.markdown(f"<div class='muted'>🧠 Set up from {len(used)} thing"
                        f"{'s' if len(used) != 1 else ''} we remember about you — ticked "
                        f"above.</div>", unsafe_allow_html=True)

    # Plan when a room is chosen, and re-plan whenever an option changes,
    # so the floor plan, 3D view and list always match the settings on the left.
    # Only the chosen room's first plan is saved; updates are saved when added to the cart.
    if ss.get("rp_replicate") is not None:
        _replica_banner(right)
    spec = _spec()
    sig = repr(spec) + repr(ss.get("rp_owned", []))     # what the photo showed counts too
    if ss.get("rp_go") or (ss.get("plan") is not None and sig != ss.get("plan_sig")):
        save = bool(ss.get("rp_go"))
        ss.rp_go = False
        t = trace.new()
        if ss.get("rp_photo_trace"):
            t.steps.extend(ss.rp_photo_trace.steps)
        t.steps.extend(ss.rp_hint_trace.steps)
        try:
            ss.plan = planner.plan(spec, customer_id=ss.customer_id,
                                   session_id=ss.session_id, trace=t, save=save)
        except PermissionError as exc:
            ss.plan = None
            st.error(f"The planner isn't allowed to run: {exc}")
        ss.plan_trace = t
        if ss.plan is not None:
            traced("plan_room", t, f"Plan a {planner.ROOM_LABELS.get(spec.room_type, spec.room_type)}"
                   f", {ss.plan.length / 12:.0f}′ × {ss.plan.width / 12:.0f}′, for "
                   f"{spec.people}", ss.plan.headline("ft"))
        ss.plan_sig = sig
        ss.plan_key = hashlib.md5(sig.encode()).hexdigest()[:8]

    _keep_planner_settings(save=True)
    _flag_fit(ss.get("plan") if ss.get("rp_replicate") is not None else None)
    with right:
        p = ss.get("plan")
        if not p:
            st.info("Pick a room above, or enter your own.")
            return
        show_plan(p)
    show_trace(ss.get("plan_trace"))


def room_view(p: planner.Plan) -> str:
    """The 3D view — for a copied photo, through the camera the photo was taken
    with, so it lines up with the photo."""
    cam = photo_cam.from_params(p.cam) if p.spec.replicate is not None else None
    return render.room_svg(p.items, p.length, p.width, carpet=p.spec.carpet,
                           fixed=p.spec.replicate is not None, cam=cam)


@st.cache_data
def room_photos() -> dict[str, list[dict]]:
    """Credits for the sample room photos."""
    f = ROOT / "data" / "room_credits.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _step_qty(key: str, d: int) -> None:
    ss[key] = min(99, max(1, int(ss.get(key, 1)) + d))


def qty_stepper(where, key: str, start: int) -> int:
    """− count + : a quantity changed one at a time, no typing. The count is plain
    session state (not a widget), so it stays put however the page reruns."""
    if key not in ss:
        ss[key] = int(start)
    n = int(ss[key])
    with where.container(horizontal=True, vertical_alignment="center", gap="xsmall", wrap=False):
        st.button(":material/remove:", key=f"{key}::minus", help="One fewer",
                  disabled=n <= 1, on_click=_step_qty, args=(key, -1))
        st.markdown(f"**{n}**", width="content")
        st.button(":material/add:", key=f"{key}::plus", help="One more",
                  disabled=n >= 99, on_click=_step_qty, args=(key, 1))
    return n


def show_plan(p: planner.Plan) -> None:
    s, units = p.spec, "ft"
    if p.fits:
        st.success(f"**{p.headline(units)}**")
    g = p.guideline()
    if g:
        (st.caption if g[1] else st.info)("📐 " + g[0])
    for w in p.warnings:
        st.warning(w)
    fit = p.fit_note(units)
    if fit:
        (st.error if p.unplaced else st.warning)("⚠️ " + fit if p.unplaced else "📏 " + fit)
    if p.crowded:
        st.warning("🚧 **This room is getting crowded** — " + "; ".join(p.crowded) + ". "
                   + ("Take some furniture out, or make the room bigger."
                      if s.replicate is not None else
                      "Plan for fewer people, or make the room bigger."))
    view = (f"<div style='border:1px solid #eee;border-radius:8px;overflow:hidden'>"
            f"{room_view(p)}</div>")
    photo = ss.get("rp_photo_bytes")
    if photo and s.replicate is None:
        src = f"data:image/jpeg;base64,{base64.b64encode(photo).decode()}"
        a, b = st.columns(2, gap="small")
        a.markdown("**Your photo**")
        a.markdown(f"<img src='{src}' style='width:100%;border-radius:8px;"
                   f"border:1px solid #eee;display:block'/>", unsafe_allow_html=True)
        b.markdown("**Suggested layout**")
        b.markdown(view, unsafe_allow_html=True)
        st.caption("Your room as it is, and our suggestion for it — the products on your "
                   "list, in the colours you picked, seen from the doorway with the near "
                   "wall cut away.")
    elif photo:
        src = f"data:image/jpeg;base64,{base64.b64encode(photo).decode()}"
        over = p.cam and st.toggle("Lay your photo over the replica", key="rp_overlay",
                                   help="Your photo, see-through, on top of the replica — "
                                        "anything that doesn't line up shows at a glance.")
        a, b = st.columns(2, gap="small")
        a.markdown("**Your photo**")
        a.markdown(f"<img src='{src}' style='width:100%;border-radius:8px;"
                   f"border:1px solid #eee;display:block'/>", unsafe_allow_html=True)
        b.markdown("**Our replica**" + (" · your photo laid over it" if over else ""))
        if over:
            view = (f"<div style='position:relative;border:1px solid #eee;border-radius:8px;"
                    f"overflow:hidden'>{room_view(p)}<img src='{src}' style='position:"
                    f"absolute;inset:0;width:100%;height:100%;opacity:.45'/></div>")
        b.markdown(view, unsafe_allow_html=True)
        st.caption("Seen from where your photo was taken, with the whole table in view: the "
                   "table turned as in your photo, its chairs spaced evenly round it, the "
                   "screens on the walls they're on. If something's off, change it in "
                   "✏️ Change what's in the room." if p.cam else
                   "Seen from the doorway, as in your photo: the far wall at the back, the "
                   "same things on the left and right. Compare them — if something's off, "
                   "change it in ✏️ Change what's in the room.")
    else:
        st.markdown(view, unsafe_allow_html=True)
        st.caption("Drawn from your plan — the products on your list, in the colours you "
                   "picked, seen from the doorway. The near wall is cut away so you can see "
                   "in.")
    if p.notes:
        with st.expander(f"Notes ({len(p.notes)})"):
            for n in p.notes:
                st.write("• " + n)

    rep = s.replicate is not None
    st.markdown("#### " + ("Everything in your photo" if rep else "Everything this room needs"))
    st.caption("Matched to our products, in the colours you picked. Untick anything you "
               "don't need, or change how many." if rep else "Untick anything you already have, "
               "or change how many. Quantities and choices come from the room size — each "
               "line says why.")
    include = []
    have = {} if rep else _bring()
    if have:
        st.caption("🚚 Lines marked “bring” use what's coming from your current room — the "
                   "prices and the cart are only what you buy.")
    mv = moving.delta(p.lines, have) if have else None
    bsig = hashlib.md5(repr(sorted(have.items())).encode()).hexdigest()[:6] if have else ""
    groups: dict[str, list] = {}
    for line in p.lines:
        groups.setdefault(line.group, []).append(line)
    for group, lines in groups.items():
        st.markdown(f"**{group}**")
        for line in lines:
            v = line.product
            a, b, q, c, d = st.columns([0.55, 3.1, 1.3, 1.1, 0.5], vertical_alignment="center")
            a.markdown(f'<img src="{image_uri(v)}" style="width:100%;border-radius:4px"/>',
                       unsafe_allow_html=True)
            colour = f" · {v['color']}" if len(v.get("colors_available", [])) > 1 else ""
            qty = qty_stepper(q, f"qty::{ss.get('plan_key')}::{v['sku']}", line.qty)
            b.markdown(f"**{v.get('base_name', v['name'])}**{colour}  \n"
                       f"<span class='muted'>{line.why.replace('$', chr(92) + '$')}"
                       + (f" · we suggested {line.qty}" if qty != line.qty else "")
                       + "</span>", unsafe_allow_html=True)
            bring = min(mv.bring.get(v["sku"], 0), qty) if mv else 0
            buy = qty - bring
            c.markdown(f"**{usd(v['price'] * buy)}**  \n<span class='muted'>"
                       f"{usd(v['price'])} each</span>", unsafe_allow_html=True)
            owned = not mv and v["kind"] in ss.get("rp_owned", [])
            if owned:
                b.caption("📷 Your photo shows you already have one — unticked.")
            if bring:
                b.markdown(f"<span class='bring'>🚚 Need {qty} · <b>bring {bring} of yours</b>"
                           + (f" · buy {buy}" if buy else " — nothing to buy") + "</span>",
                           unsafe_allow_html=True)
            if d.checkbox("Include", not owned and buy > 0, disabled=mv is not None and not buy,
                          key=f"inc::{ss.get('plan_key')}{bsig}::{v['sku']}",
                          label_visibility="collapsed") and buy:
                include.append(dataclasses.replace(line, qty=buy))

    priced = planner.reprice(p, lines=include)
    if mv:
        spare = moving.words(mv.spare)
        st.info(f"🚚 **Bringing {mv.pieces} piece{'s' if mv.pieces != 1 else ''} from your "
                f"current room** — about {usd(mv.value, 0)} you don't spend. The total below "
                f"is only what to buy."
                + (f"  \nNot needed in the new room: {spare}." if mv.spare else ""))
    with st.container(border=True):
        a, b = st.columns([1.4, 1])
        with a:
            st.write(f"Subtotal ({priced['units']} items): **{usd(priced['subtotal'])}**")
            for sv in priced["savings"]:
                st.markdown(f'<span class="save">− {usd(sv["amount"])} · {sv["name"]}</span>',
                            unsafe_allow_html=True)
            st.write("Delivery: " + ("FREE" if not priced["delivery"]
                                     else f"{usd(priced['delivery'])}")
                     + f" · Estimated tax: {usd(priced['tax'])}")
        with b:
            st.markdown(f"### {usd(priced['total'])}")
            if priced["discount"]:
                st.markdown(f'<span class="save">Bundles save you {usd(priced["discount"])}'
                            f'</span>', unsafe_allow_html=True)
            if s.budget:
                diff = s.budget - priced["total"]
                st.caption(f"Your budget: {usd(s.budget, 0)} — " +
                           (f"{usd(diff, 0)} to spare" if diff >= 0 else
                            f"{usd(-diff, 0)} over; untick extras to get closer"
                            if p.budget_fit else
                            f"{usd(-diff, 0)} over; try the Value budget or untick extras"))
                if fitted := planner.budget_words(p):
                    st.caption((f"🧠 {fitted}" + (" Choose a tier above to see others."
                                                  if p.to_budget else "")).replace("$", "\\$"))
        units = sum(line.qty for line in include)
        if st.button(f"Add {units} item{'s' if units != 1 else ''} to cart", type="primary",
                     width="stretch",
                     disabled=not include, key=f"addall::{ss.get('plan_key')}"):
            t = trace.new()
            if not p.plan_id:                         # an updated plan: keep it now
                planner.save_plan(p, ss.customer_id, t)
            commerce.add_many(ss.customer_id, [(line.product["sku"], line.qty)
                                               for line in include], trace=t)
            traced("add_to_cart", t, f"Add the plan's {units} items to the cart")
            ss.last_trace = t
            st.toast(f"Added {sum(line.qty for line in include)} items from plan {p.plan_id}")
            go("Cart")
            st.rerun()
    st.caption(f"Plan {p.plan_id} saved — the assistant can pick it up later."
               if p.plan_id else "Updated as you change the options. It's saved when you "
                                 "add it to the cart.")
    if rep and p.suggestions:
        more_for_room(p)


def more_for_room(p: planner.Plan) -> None:
    """Replica: what the photo didn't show but a room like it usually has — each
    straight to the cart, not onto the copy of the room."""
    name = planner.ROOM_LABELS.get(p.spec.room_type, p.spec.room_type).split(" (")[0].lower()
    st.markdown(f"#### Not in your photo — often in a {name}")
    st.caption("Not in your photo, so not on the plan or the list above. Add any you want "
               "straight to your cart.")
    for line in p.suggestions:
        v = line.product
        a, b, c, d = st.columns([0.55, 4, 1.1, 1.3], vertical_alignment="center")
        a.markdown(f'<img src="{image_uri(v)}" style="width:100%;border-radius:4px"/>',
                   unsafe_allow_html=True)
        b.markdown(f"**{line.qty} × {v.get('base_name', v['name'])}**  \n"
                   f"<span class='muted'>usually: {line.why.replace('$', chr(92) + '$')}</span>", unsafe_allow_html=True)
        c.markdown(f"**{usd(v['price'] * line.qty)}**")
        if d.button("🛒 Add to cart", key=f"more::{ss.get('plan_key')}::{v['sku']}",
                    width="stretch"):
            t = trace.new()
            commerce.add_many(ss.customer_id, [(v["sku"], line.qty)], trace=t)
            traced("add_to_cart", t, f"Add {line.qty} × {v['name']} to the cart")
            ss.last_trace = t
            st.toast(f"Added {line.qty} × {v.get('base_name', v['name'])} to your cart")


# --------------------------------------------------------------------------- #
# Agent Catalog: Tools Hub, Prompts Hub, Agent Tracer — laid out like the AI Data
# Plane in Capella, read straight from Couchbase
# --------------------------------------------------------------------------- #
AC_CSS = """<style>
.cap-title{font-size:1.45rem;font-weight:700;color:#1f2937;margin:4px 0 18px}
.cap-blurb{color:#1f2937;font-size:.95rem;margin-bottom:16px}
.cap-blurb a,.cap-start a,.cap-empty a,.cap-info a{color:#0b6bcb;text-decoration:none}
.cap-start div{margin:8px 0;color:#1f2937;font-size:.92rem}
.cap-start span{color:#f08a24;font-weight:700;margin-right:10px}
.cap-start i{color:#6b7280}
.cap-empty{border:1px solid #e5e7eb;border-radius:6px;text-align:center;padding:48px 16px;
  margin-top:10px}
.cap-empty b{font-size:1.05rem;color:#111827}.cap-empty div{margin-top:10px;color:#374151}
.cap-info{border:1px solid #e5e7eb;border-left:4px solid #0b6bcb;border-radius:4px;
  padding:12px 16px;margin-top:22px;font-size:.92rem;color:#1f2937;display:flex;gap:14px}
.cap-info .ic{color:#0b6bcb;font-size:1.3rem;line-height:1.2}
.cap-info details summary{cursor:pointer;color:#0b6bcb;list-style:none}
.cap-info pre{font-size:.75rem;background:#f8fafc;padding:6px 8px;border-radius:4px;
  white-space:pre-wrap;margin:4px 0 0}
.cap-count{color:#6b7280;font-size:.92rem;margin:14px 0 0 20px}
.cap-th{color:#374151;font-size:.92rem;font-weight:500;white-space:nowrap}
.cap-th span{color:#4b5563;font-size:.85rem;margin-left:4px;cursor:help}
.cap-td{color:#1f2937;font-size:.92rem}
.cap-mut{color:#6b7280;font-size:.92rem}
.cap-desc{color:#6b7280;font-size:.88rem;line-height:1.35;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin:-12px 0 6px;
  cursor:default}
.cap-chip{display:inline-block;background:#e5e7eb;color:#1f2937;border-radius:999px;
  padding:3px 11px;font-size:.74rem;font-weight:500;text-transform:uppercase;
  margin:2px 4px 2px 0;letter-spacing:.01em}
.cap-ver{font-size:.92rem;color:#1f2937}.cap-ver b{color:#0b6bcb;font-weight:500}
.chip{display:inline-block;border-radius:10px;padding:1px 8px;font-size:.72rem;font-weight:600;
  white-space:nowrap;margin:1px 2px 1px 0}
.c-ok{background:#dcfce7;color:#166534}.c-no{background:#fee2e2;color:#991b1b}
.c-old{background:#fef3c7;color:#92400e}.c-lim{background:#dbeafe;color:#1e40af}
.c-g{background:#f1f5f9;color:#475569}.c-w{background:#fff1dc;color:#8a4b00}
.c-r{background:#e0efff;color:#0b4a8b}.c-p{background:#f1e8ff;color:#5b2a9a}
.sub{color:#6b7280;font-size:.8rem;margin-top:2px}
/* the table: a bordered box, a grey heading band, a rule between rows */
[class*='st-key-captbl']{border:1px solid #e5e7eb!important;border-radius:4px;gap:0!important}
[class*='st-key-caphead']{background:#f3f4f6!important;padding:14px 20px!important;
  border-bottom:1px solid #e5e7eb!important}
[class*='st-key-caprow']{padding:16px 20px!important;border-bottom:1px solid #e5e7eb!important}
[class*='st-key-captbl'] > div:last-child [class*='st-key-caprow']{border-bottom:none!important}
[class*='st-key-capcopy'] [data-testid='stPopoverButton'] > div > div[aria-hidden='true']{
  display:none}
.cap-gi{font-family:'Material Symbols Rounded';font-size:17px;vertical-align:-3px;
  margin-right:1px}
/* names are links */
[class*='st-key-lnk_'] button{color:#0b6bcb!important;padding:0!important;min-height:0;
  justify-content:flex-start;text-align:left}
[class*='st-key-lnk_'] button p{font-size:.95rem!important;word-break:break-all}
[class*='st-key-lnk_'] button:hover p{text-decoration:underline}
/* the filter row: a labelled cluster box, Search by joined to the search box, pills */
[class*='st-key-capclu'],[class*='st-key-capmode']{border:1px solid #d1d5db;
  border-radius:6px;padding:6px 12px 0;background:#fff;height:74px}
[class*='st-key-capmode']{background:#f3f4f6;border-radius:6px 0 0 6px;border-right:none}
[class*='st-key-capclu'] label p,[class*='st-key-capmode'] label p{font-size:.78rem!important;
  color:#4b5563!important}
[class*='st-key-capclu'] label,[class*='st-key-capmode'] label{margin-bottom:-6px}
[class*='st-key-capclu'] [data-baseweb='select'] > div,
[class*='st-key-capmode'] [data-baseweb='select'] > div{border:none!important;
  background:transparent!important;padding-left:0}
[class*='st-key-capsearch'] [data-testid='stTextInputRootElement'],
[class*='st-key-capsearch'] [data-baseweb='input']{height:74px!important;
  border-radius:0 6px 6px 0!important;background:#f3f4f6!important;border-color:#d1d5db!important}
[class*='st-key-capsearch'] [data-baseweb='base-input']{background:#f3f4f6!important}
[class*='st-key-capsearch'] input{background:#f3f4f6;font-size:1rem}
[class*='st-key-cappills']{flex-wrap:nowrap!important}
[class*='st-key-cappills'] [data-testid='stPopover']{width:auto!important;flex:0 0 auto}
[class*='st-key-cappills'] [data-testid='stPopover'] button{border-radius:999px;
  background:#e8f0fe;border:none;color:#1f3a8a;font-size:.88rem;min-height:0;
  padding:4px 12px;white-space:nowrap}
[class*='st-key-cappills'] [data-testid='stPopover'] button p{color:#1f3a8a}
[class*='st-key-capclear'] button p{font-size:.95rem!important;color:#1f2937!important}
/* Capella's drawers: the dialog slides in from the right, full height */
[data-testid='stDialog']{justify-content:flex-end!important;align-items:stretch!important;
  padding:0!important}
[data-testid='stDialog'] > div{margin:0!important;width:min(64vw,1150px)!important;
  max-width:none!important;height:100vh!important;max-height:100vh!important}
section[role='dialog']{width:100%!important;max-width:none!important;height:100vh!important;
  max-height:100vh!important;border-radius:0!important;overflow-y:auto!important;margin:0!important}
section[role='dialog'] h2{font-size:1.1rem!important}
[class*='st-key-drwdone'] button{background:#0b6bcb!important;border-color:#0b6bcb!important;
  color:#fff!important;border-radius:4px;padding:10px 22px}
.drw-tbl{width:100%;font-size:.9rem;margin:12px 0;border-collapse:collapse}
.drw-tbl td{border:none!important;padding:5px 14px 5px 0!important;vertical-align:top}
.drw-tbl td:first-child{color:#6b7280;white-space:nowrap;width:1%}
.drw-tbl tr{border:none!important;background:transparent!important}
.drw-id{font-weight:600;color:#111827;font-size:.98rem}
.drw-meta{color:#4b5563;font-size:.92rem;margin-top:6px}
.drw-meta span{margin:0 8px;color:#9ca3af}
.drw-start{color:#6b7280;font-size:1rem;text-align:right;padding-top:14px}
.drw-start b{color:#374151;font-weight:400;margin-left:6px}
.drw-rule{border-bottom:1px solid #e5e7eb;margin:12px 0 16px}
.drw-filter{font-weight:600;color:#1f2937;font-size:.92rem;margin:4px 0 10px}
section[role='dialog'] [data-testid='stCheckbox'] label[data-selected='true'] > div:first-of-type{
  background-color:#2f6fd6!important;border-color:#2f6fd6!important}
section[role='dialog'] [data-testid='stCheckbox'] p{font-size:.95rem!important;color:#1f2937}
section[role='dialog'] [data-testid='stCheckbox'] .stMarkdownColoredBackground{color:#fff;
  border-radius:4px;padding:1px 2px;margin-right:4px}
.lgc details[open]{display:flex;flex-direction:column}
.lgc details[open] summary{order:9}
[class*='st-key-drwlog']{background:#f3f4f6;padding:18px 22px!important;border-radius:2px}
.lgc{background:#fff;border:1px solid #d1d5db;border-radius:6px;padding:12px 18px;
  margin:4px 0 14px;font-size:.92rem;color:#1f2937;line-height:1.45}
.lgw{color:#6b7280;font-size:.88rem;text-align:right;margin:6px 0 4px}
.lgi{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;
  border-radius:4px;color:#fff;font-family:'Material Symbols Rounded';font-size:15px;
  vertical-align:middle;margin-left:8px;line-height:1}
.lgx{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.lgc:has(details[open]) .lgx{display:none}
.lgc details summary{list-style:none;color:#0b6bcb;cursor:pointer;margin-top:6px}
.lgc details summary::-webkit-details-marker{display:none}
.lgc details summary::before{content:'Read more'}
.lgc details[open] summary::before{content:'Show less'}
.lgc details[open] summary{margin:8px 0 0}
.lgc details > summary + *{margin-top:0}
.lgc pre{font-size:.72rem;background:#f8fafc;padding:6px;border-radius:6px;
  white-space:pre-wrap;max-height:320px;overflow:auto;margin:6px 0 0}
.lgc .sub code{font-size:.78rem}
.lgspan{color:#475569;font-size:.8rem;margin:10px 0 2px}
.lgspan code{font-size:.78rem;background:transparent;color:#334155}
</style>"""


def _when(ts: str | None, fmt: str = "%H:%M:%S") -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime(fmt)
    except (TypeError, ValueError):
        return "—"


def _chips(items, cls: str) -> str:
    return "".join(f"<span class='chip {cls}'>{html.escape(str(i))}</span>" for i in items)


DOCS = "https://docs.couchbase.com/ai/build/"
CAP_DATE = "%b %d, %Y %H:%M:%S %Z"
CLUSTER = "Local cluster"
AC_VIEWS = {"tracer": "Agent Tracer", "tools": "Tools Hub", "prompts": "Prompts Hub"}
AC_ICONS = {"tracer": ":material/assignment:", "tools": ":material/home_repair_service:",
            "prompts": ":material/terminal:", "offerings": ":material/apps:"}
AC_TITLES = {"offerings": "Offerings", **AC_VIEWS}
AC_BLURB = {
    "offerings": ("The AI Data Plane's capabilities, and how each one runs in this demo: for "
                  "real on this laptop, by a local stand-in, or only in Capella.",
                  "https://docs.couchbase.com/ai/get-started/intro.html"),
    "tracer": ("View various types of trace signals generated during an Agent-user session "
               "for seamless debugging.", DOCS + "agent-tracer/agent-tracer.html"),
    "tools": ("Tools hub allows centralization and visualization of tools metadata to help "
              "with tool governance while simplifying searchability and reusability of tools.",
              DOCS + "tools-prompts-hub.html"),
    "prompts": ("Prompt hub allows centralization and visualization of prompts to help with "
                "prompt governance while simplifying searchability and reusability of prompts.",
                DOCS + "tools-prompts-hub.html")}
AC_START = {
    "tracer": [
        "Use a single or multi-node multi-AZ cluster to capture traces. For custom analysis "
        "of traces using pre-built Analytics views, enable the Analytics service. "
        "<i>Here: the local cluster, with Analytics on.</i>",
        "Create a bucket to store agent traces. <i>Here: <code>{bucket}</code>, scope "
        "<code>agent_activity</code>.</i>",
        f"Use <a href='{DOCS}integrate-agent-with-catalog.html' target='_blank'>init CLI "
        "command ↗</a> and log session traces to the bucket using the <a href='https://"
        "couchbaselabs.github.io/agent-catalog/' target='_blank'>SDK ↗</a> inside your app. "
        "<i>Here: <code>agentc init</code>, and <code>retail/agentc_bridge.py</code>.</i>"],
    "tools": [
        "Create a bucket to store the catalog. <i>Here: <code>{bucket}</code>, scope "
        "<code>agent_catalog</code>.</i>",
        f"Use <a href='{DOCS}integrate-agent-with-catalog.html' target='_blank'>init CLI "
        "command ↗</a> to set up the catalog. <i>Here: <code>agentc init</code>.</i>",
        "Write tools as decorated Python functions, then index and publish them. <i>Here: "
        "<code>agent_catalog/tools/store_tools.py</code>; the post-commit hook runs "
        "<code>agentc index agent_catalog && agentc publish</code>.</i>"],
    "prompts": [
        "Create a bucket to store the catalog. <i>Here: <code>{bucket}</code>, scope "
        "<code>agent_catalog</code>.</i>",
        f"Use <a href='{DOCS}integrate-agent-with-catalog.html' target='_blank'>init CLI "
        "command ↗</a> to set up the catalog. <i>Here: <code>agentc init</code>.</i>",
        "Write prompts as YAML files, then index and publish them. <i>Here: "
        "<code>agent_catalog/prompts/</code>; the post-commit hook publishes on every "
        "commit, and the app reads each prompt from the hub when it calls a model.</i>"]}
AC_KEY = {"tracer": "tr", "tools": "th", "prompts": "ph"}
DATE_RANGES = {"All Time": 0, "Last 15 Minutes": 15, "Last Hour": 60, "Last 24 Hours": 1440,
               "Last 7 Days": 10080}
KIND_NAMES = {"python_function": "Python Function", "sqlpp_query": "SQL++ Query",
              "semantic_search": "Semantic Search", "http_request": "HTTP Request",
              "prompt": "Prompt"}


def page_agent_catalog() -> None:
    st.markdown(AC_CSS, unsafe_allow_html=True)
    view = ss.get("ac_view") or "tracer"
    blurb, link = AC_BLURB[view]
    st.markdown(f"<div class='cap-title'>{AC_TITLES[view]}</div><div class='cap-blurb'>"
                f"{blurb} <a href='{link}' target='_blank'>Learn More ↗</a></div>",
                unsafe_allow_html=True)
    if view == "offerings":
        _offerings()
        return
    ok, err = agentc_bridge.status()
    if not ok:
        st.error(f"The Agent Catalog can't be reached — {err}")
        return
    {"tools": _tools_hub, "prompts": _prompts_hub}.get(view, _agent_tracer)()


def _open_catalog(view: str) -> None:
    ss.page, ss.ac_view = "Agent Catalog", view
    ss.tracer_session = ss.hub_item = None


def catalog_panel() -> None:
    """AGENT CATALOG in the sidebar, under the AI models: Capella's three views."""
    here = ss.page == "Agent Catalog"
    active = ss.get("ac_view") if here else None
    css = ("<style>.acnav-h{color:#6b7280;font-size:.74rem;letter-spacing:.06em;"
           "margin:18px 0 4px 2px}"
           "[class*='st-key-acnav_'] button{justify-content:flex-start;border-radius:0;"
           "border-left:3px solid transparent;padding:6px 12px;color:#1f2937;width:100%}"
           "[class*='st-key-acnav_'] button > div{justify-content:flex-start}"
           "[class*='st-key-acnav_'] button:hover{background:#f3f4f6;color:#1f2937}"
           + (f".st-key-acnav_{active} button{{background:#eef4ff;border-left-color:#0b6bcb;"
              f"color:#0b6bcb;font-weight:600}}" if active else "") + "</style>")
    st.sidebar.markdown(css + "<div class='acnav-h'>AI DATA PLANE</div>",
                        unsafe_allow_html=True)
    st.sidebar.container(key="acnav_top", gap=None).button(
        "Offerings", key="acnav_offerings", icon=AC_ICONS["offerings"], type="tertiary",
        width="stretch", on_click=_open_catalog, args=("offerings",))
    st.sidebar.markdown("<div class='acnav-h'>AGENT CATALOG</div>", unsafe_allow_html=True)
    nav = st.sidebar.container(key="acnav", gap=None)
    for view, label in AC_VIEWS.items():
        nav.button(label, key=f"acnav_{view}", icon=AC_ICONS[view], type="tertiary",
                          width="stretch", on_click=_open_catalog, args=(view,))


# --- the parts every page shares --------------------------------------------- #
def _cap_start(view: str, expanded: bool) -> None:
    with st.expander("**How to get started**", expanded=expanded):
        st.markdown("<div class='cap-start'>" + "".join(
            f"<div><span>→</span>{s.format(bucket=settings.bucket)}</div>"
            for s in AC_START[view]) + "</div>", unsafe_allow_html=True)


FILTER_DEFAULTS = {"mode": None, "search": "", "range": "All Time", "bucket": "All",
                   "pick": "All"}
SEARCH_MODES = {"tr": ["Session Id", "Tags"], "th": ["Name", "Description"],
                "ph": ["Name", "Description"]}


def _fkey(p: str, name: str) -> str:
    return f"{p}_{name}"


def _clear_filters(p: str) -> None:
    for name, v in FILTER_DEFAULTS.items():
        ss[_fkey(p, name)] = v if v is not None else SEARCH_MODES[p][0]


def _cap_filters(p: str, pick_label: str | None = None,
                 pick_options: list[str] | None = None) -> dict:
    """Capella's filter row: Operational Cluster, Search by + search box, then the
    Bucket, Date Range and Type/App pills and Clear Filters. Returns the choices."""
    for name, v in FILTER_DEFAULTS.items():
        if ss.get(_fkey(p, name)) is None:
            ss[_fkey(p, name)] = v if v is not None else SEARCH_MODES[p][0]
    mode = ss[_fkey(p, "mode")]
    widths = [1.3, 1.05, 2.0, 3.5, 0.85] if pick_label else [1.3, 1.05, 2.5, 2.6, 0.85]
    a, b, c, d, e = st.columns(widths, vertical_alignment="center", gap=None)
    with a.container(key=f"capclu_{p}"):
        st.selectbox("Operational Cluster", [CLUSTER], key=f"{p}_cluster")
    with b.container(key=f"capmode_{p}"):
        st.selectbox("Search by", SEARCH_MODES[p], key=_fkey(p, "mode"))
    with c.container(key=f"capsearch_{p}"):
        st.text_input("Search", key=_fkey(p, "search"), label_visibility="collapsed",
                      icon=":material/search:",
                      placeholder=("Search by Session ID" if mode == "Session Id" else
                                   f"Search by {mode.lower()}..."))
    with d.container(key=f"cappills_{p}", horizontal=True, gap="small",
                     horizontal_alignment="right"):
        with st.popover(f"Bucket : {ss[_fkey(p, 'bucket')]}"):
            st.radio("Bucket", ["All", settings.bucket], key=_fkey(p, "bucket"),
                     label_visibility="collapsed")
        with st.popover(f"Date Range : {ss[_fkey(p, 'range')]}",
                        icon=":material/calendar_today:"):
            st.radio("Date Range", list(DATE_RANGES), key=_fkey(p, "range"),
                     label_visibility="collapsed")
        if pick_label:
            with st.popover(f"{pick_label} : {ss[_fkey(p, 'pick')]}"):
                st.radio(pick_label, ["All"] + (pick_options or []), key=_fkey(p, "pick"),
                         label_visibility="collapsed")
    with e.container(key=f"capclear_{p}"):
        st.button("Clear Filters", type="tertiary", on_click=_clear_filters, args=(p,),
                  key=f"{p}_clear")
    rng = DATE_RANGES.get(ss[_fkey(p, "range")])
    return {"mode": mode, "search": (ss[_fkey(p, "search")] or "").strip().lower(),
            "since_ms": int((time.time() - rng * 60) * 1000) if rng else None,
            "bucket": ss[_fkey(p, "bucket")], "pick": ss[_fkey(p, "pick")]}


def _th(label: str, tip: str = "") -> str:
    return (f"<div class='cap-th'>{label}" + (f"<span title='{html.escape(tip)}'>ⓘ</span>"
                                             if tip else "") + "</div>")


def _cap_head(widths: list[float], heads: list[tuple[str, str]], key: str):
    """The table box with its grey heading band; rows go in the returned box."""
    box = st.container(key=f"captbl_{key}", gap=None)
    hd = box.container(key=f"caphead_{key}").columns(widths, vertical_alignment="center")
    for col, (h, tip) in zip(hd, heads):
        col.markdown(_th(h, tip), unsafe_allow_html=True)
    return box


def _cap_row(box, widths: list[float], key: str):
    return box.container(key=f"caprow_{key}").columns(widths, vertical_alignment="center")


def _cap_date(ts=None, ms=None) -> str:
    try:
        d = (datetime.fromtimestamp(ms / 1000) if ms is not None
             else datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
        return d.astimezone().strftime(CAP_DATE)
    except (TypeError, ValueError, OverflowError):
        return "—"


def _cap_ms(ts) -> int | None:
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _copy_cell(col, item: dict, key: str) -> None:
    """Capella's copy icon: the item's catalog identifier and source, ready to copy."""
    with col.container(key=f"capcopy_{key}"), \
            st.popover("", icon=":material/content_copy:", type="tertiary"):
        st.caption("Source")
        st.code(item.get("source") or "", None)
        st.caption("Catalog record")
        st.code(f"{item.get('source')}:{item['name']}:git_{item.get('commit') or ''}", None)


def _hub_chips(item: dict) -> str:
    a = item.get("annotations") or {}
    return "".join(f"<span class='cap-chip'>{html.escape(str(v))}</span>"
                   for v in a.values() if v not in (None, ""))


def _hub_filter(items: list[dict], f: dict, kind: bool = False) -> list[dict]:
    out = []
    for it in items:
        text = it["name"] if f["mode"] == "Name" else (it.get("description") or "")
        if f["search"] and f["search"] not in text.lower():
            continue
        if f["since_ms"] and (_cap_ms(it.get("ts")) or 0) < f["since_ms"]:
            continue
        if kind and f["pick"] != "All" and \
                KIND_NAMES.get(it.get("record_kind"), it.get("record_kind")) != f["pick"]:
            continue
        out.append(it)
    return out


def _hub_name_cell(col, item: dict, key: str, opener) -> None:
    col.button(item["name"], key=f"lnk_{key}", type="tertiary", on_click=opener,
               args=(item["name"],))
    desc = (item.get("description") or "").strip()
    col.markdown(f"<div class='cap-desc' title='{html.escape(desc)}'>{html.escape(desc)}</div>"
                 f"<div>{_hub_chips(item)}</div>", unsafe_allow_html=True)


def _hub_version(item: dict) -> str:
    n = item.get("versions") or 1
    return (f"<div class='cap-ver'>{(item.get('commit') or '')[:7]}<br><b title='Published "
            f"versions of this item'><span class='cap-gi'>commit</span>{n}</b></div>")


def _open_item(name: str) -> None:
    ss.hub_item = name


def _close_item() -> None:
    ss.hub_item = None


def _done_item() -> None:
    ss.hub_item = None
    st.rerun()


def _empty(what: str, how: str) -> None:
    st.markdown(f"<div class='cap-empty'><b>No {what} found</b><div>{how}</div></div>",
                unsafe_allow_html=True)


# --- Offerings: the AI Data Plane's capabilities, as they run here ------------ #
OFFER_STATE = {"real": ("Running here", "#dcfce7", "#166534"),
               "stand-in": ("Local stand-in", "#fef3c7", "#92400e"),
               "capella": ("Capella only", "#e5e7eb", "#374151"),
               "unused": ("Not in this demo", "#e5e7eb", "#374151")}
DOCS_AI = "https://docs.couchbase.com/ai/"


def _memory_llm() -> str:
    """The model the Agent Memory server summarises with (its env file)."""
    try:
        for line in (ROOT / "agent-memory.env").read_text().splitlines():
            if line.startswith("AGENTMEMORY_LLM_MODEL="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "—"


def _offerings() -> None:
    """Capella's Offerings page for this demo: each capability, what it is (in the
    docs' words), how it runs here, and where to see it."""
    from urllib.parse import urlparse
    from retail import memory_server
    pulled = _models_pulled()
    cat_ok, _ = agentc_bridge.status()
    mem_ok, _ = memory_server.status()
    code = lambda t: f"<code>{html.escape(t)}</code>"  # noqa: E731
    b = settings.bucket
    rows = [
        ("Model Service", "model-service", "stand-in",
         "Deploys and manages LLMs and embedding models in Capella, or brings your own "
         "through the OpenAI and Bedrock integrations.",
         f"Ollama serves every model on this laptop through an OpenAI-compatible endpoint "
         f"at {code(urlparse(settings.model_endpoint).netloc)} — standing in for Capella's "
         f"bring-your-own-model option. Chat and tool calling {code(settings.chat_model)}, "
         f"vision {code(settings.vision_model)}, embeddings {code(settings.embedding_model)}, "
         f"memory summaries {code(_memory_llm())}. The semantic cache is the app's own, in "
         f"{code(b + '.aidp.semantic_cache')} — not the Model Service's caching."
         + ("" if pulled is not None else " <b>Ollama isn't answering right now.</b>"),
         DOCS_AI + "build/model-service/model-service.html", "Ask the Assistant"),
        ("Agent Catalog", "agent-catalog", "real" if cat_ok else "unused",
         "Governs agentic app development by managing the tools and prompts of custom AI "
         "agents, with any LLM and agent framework.",
         f"Real {code('agentc')}: the tools in {code('agent_catalog/tools/')} and prompts in "
         f"{code('agent_catalog/prompts/')} are published to {code(b + '.agent_catalog')} on "
         f"every git commit. The store assistant gets its tools and prompt from the catalog, "
         f"and a policy check runs before every tool call, recorded in "
         f"{code(b + '.aidp.audit_log')}.",
         DOCS_AI + "build/integrate-agent-with-catalog.html", "tools"),
        ("Agent Memory", "agent-memory", "real" if mem_ok else "unused",
         "A persistent memory layer for agentic apps, so agents recall context across "
         "sessions. Works with any agent framework.",
         f"Real Agent Memory server, the {code(settings.agent_memory_container)} container at "
         f"{code(urlparse(settings.agent_memory_url).netloc)}, storing in "
         f"{code(b + '.agentmemory')}. Sign in as a customer (top right): their preferences "
         f"are recalled, and the Room Planner and the assistant apply them."
         + ("" if mem_ok else " <b>The server isn't answering right now.</b>"),
         "https://docs.couchbase.com/agent-mem/current/about-agent-mem.html", None),
        ("AI Functions", "ai-functions", "capella",
         "Summarises text, classifies content, detects sentiment and explains patterns, "
         "inside SQL++ queries.",
         "Not used here. They're set up in Capella and call a Model Service, OpenAI or "
         "Bedrock model. This cluster runs Server 8.0 (the version they need), but the demo "
         "calls its models from the app instead.",
         DOCS_AI + "build/ai-functions.html", None),
        ("MCP Server", "mcp-server", "unused",
         "A Model Context Protocol server that lets LLMs work with the data in Couchbase "
         "clusters through a set of tools.",
         "It runs with Capella or a self-managed cluster like this one, but this app doesn't "
         "use it: the store assistant's tools come from the Agent Catalog instead.",
         "https://docs.couchbase.com/mcp-server/get-started/overview.html", None),
        ("Data Processing Service", "data-processing", "stand-in",
         "Vectorises structured and unstructured data for the rest of the AI Data Plane.",
         f"Done by the app's own loader here: {code('scripts/load_data.py')} embeds each "
         f"product and help article with {code(settings.embedding_model)} "
         f"({settings.embedding_dims}-d) into an {code('embedding')} field, searched through "
         f"the vector indexes {code('products_vec_idx')} and {code('kb_vec_idx')}.",
         DOCS_AI + "build/vectorization-service/data-processing.html", None),
        ("Agent Tracer", "agent-tracer", "real" if cat_ok else "unused",
         "Monitors and observes agent activity.",
         f"Real {code('agentc')} activity logs in {code(b + '.agent_activity.logs')}: one "
         f"session per browser, a span per request, messages in the tracer's trace types.",
         DOCS_AI + "build/agent-tracer/agent-tracer.html", "tracer"),
    ]
    counts = {k: sum(1 for r in rows if r[2] == k) for k in OFFER_STATE}
    st.markdown(
        "<style>.ofr{border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px;"
        "margin-bottom:4px;background:#fff}.ofr h4{margin:0 0 4px;font-size:1.02rem;"
        "color:#111827}.ofr .os{display:inline-block;border-radius:999px;padding:2px 10px;"
        "font-size:.74rem;font-weight:600;margin-left:8px;vertical-align:2px}"
        ".ofr .od{color:#4b5563;font-size:.88rem;margin:2px 0 10px}"
        ".ofr .oh{color:#6b7280;font-size:.72rem;letter-spacing:.05em;font-weight:600}"
        ".ofr .ot{color:#1f2937;font-size:.9rem;margin-top:2px}"
        ".ofr a{color:#0b6bcb;text-decoration:none;font-size:.85rem}"
        ".ofr code{font-size:.78rem}.ofsum{color:#374151;font-size:.92rem;margin:0 0 14px}"
        "</style><div class='ofsum'>"
        + " · ".join(f"<b>{counts[k]}</b> {OFFER_STATE[k][0]}" for k in OFFER_STATE
                     if counts[k]) + "</div>", unsafe_allow_html=True)
    left, right = st.columns(2, gap="medium")
    for i, (name, key, state, what, here, doc, go_to) in enumerate(rows):
        label, bg, fg = OFFER_STATE[state]
        col = left if i % 2 == 0 else right
        col.markdown(
            f"<div class='ofr'><h4>{name}<span class='os' style='background:{bg};"
            f"color:{fg}'>{label}</span></h4><div class='od'>{what}</div>"
            f"<div class='oh'>IN THIS DEMO</div><div class='ot'>{here}</div>"
            f"<div style='margin-top:8px'><a href='{doc}' target='_blank'>Docs ↗</a></div>"
            "</div>", unsafe_allow_html=True)
        if go_to in AC_VIEWS:
            col.button(f"Open {AC_VIEWS[go_to]}", key=f"ofr_{key}", type="tertiary",
                       on_click=_open_catalog, args=(go_to,), icon=AC_ICONS[go_to])
        elif go_to:
            col.button(f"Open {go_to}", key=f"ofr_{key}", type="tertiary", on_click=go,
                       args=(go_to,))
    st.caption("Search, vector search, SQL++ and key-value reads and writes underneath it all "
               "are Couchbase Server's own services, on the local cluster.")


# --- Tools Hub --------------------------------------------------------------- #
TOOL_W = [3.3, 0.9, 1.4, 1.1, 1.0, 1.6, 2.1, 0.45]


def _tools_hub() -> None:
    tools = agentc_bridge.tools()
    _cap_start("tools", expanded=not tools)
    kinds = sorted({KIND_NAMES.get(t.get("record_kind"), t.get("record_kind") or "")
                    for t in tools} - {""})
    f = _cap_filters("th", "Type", kinds)
    if not tools:
        _empty("Tools", "Commit, and the post-commit hook publishes <code>agent_catalog/</code> "
                        "(or run <code>agentc index agent_catalog && agentc publish</code>).")
        return
    shown = _hub_filter(tools, f, kind=True)
    box = _cap_head(TOOL_W, [("Name", ""), ("Bucket", ""), ("Type", ""),
                             ("Catalog ID", "The catalog version this tool was published in"),
                             ("Version", "The git commit of this version, and how many "
                                         "versions have been published"),
                             ("Date", ""), ("Source", "The file the tool is defined in"),
                             ("", "")], "th")
    for t in shown:
        c = _cap_row(box, TOOL_W, f"th_{t['name']}")
        _hub_name_cell(c[0], t, f"th_{t['name']}", _open_item)
        c[1].markdown(f"<div class='cap-td'>{settings.bucket}</div>", unsafe_allow_html=True)
        c[2].markdown(f"<div class='cap-td'>{KIND_NAMES.get(t.get('record_kind'), '—')}</div>",
                      unsafe_allow_html=True)
        c[3].markdown(f"<div class='cap-td'>{(t.get('catalog_identifier') or '')[:7]}</div>",
                      unsafe_allow_html=True)
        c[4].markdown(_hub_version(t), unsafe_allow_html=True)
        c[5].markdown(f"<div class='cap-mut'>{_cap_date(t.get('ts'))}</div>",
                      unsafe_allow_html=True)
        c[6].markdown(f"<div class='cap-td'>{html.escape(t.get('source') or '')}</div>",
                      unsafe_allow_html=True)
        _copy_cell(c[7], t, f"th_{t['name']}")
    st.markdown(f"<div class='cap-count'>Showing {len(shown)} of {len(tools)} results</div>",
                unsafe_allow_html=True)
    if ss.get("hub_item") and ss.hub_item in {t["name"] for t in tools}:
        _tool_drawer(next(t for t in tools if t["name"] == ss.hub_item))


def _tool_policy(name: str) -> tuple[str, str]:
    """(the store assistant's grant for this tool, its policy checks so far)."""
    from retail import governance
    registry = {(t["tool_name"], t["version"]): t for t in governance.TOOLS}
    pinned = governance.GRANTS.get(name)
    reg = registry.get((name, pinned or "")) or {}
    if not pinned:
        pol = "<span class='chip c-no'>Not granted</span>"
    elif reg.get("status") == "deprecated":
        pol = f"<span class='chip c-old'>Pinned to retired v{pinned}</span>"
    elif (reg.get("limits") or {}).get("max_amount_usd"):
        pol = (f"<span class='chip c-lim'>Granted · over "
               f"${reg['limits']['max_amount_usd']:,.0f} the shopper pays in the Cart</span>")
    else:
        pol = "<span class='chip c-ok'>Granted</span>"
    try:
        c = (query("SELECT COUNT(1) AS n, SUM(CASE WHEN a.decision = 'allow' THEN 1 ELSE 0 "
                   "END) AS allowed, MAX(a.ts) AS last_ts FROM `retail`.`aidp`.`audit_log` "
                   "AS a WHERE a.session_id IS NOT MISSING AND a.tool = $t", t=name)
             or [{}])[0]
    except Exception:
        c = {}
    used = (f"{c['n']:,} checked · {c['n'] - (c.get('allowed') or 0):,} blocked · last "
            f"{_when(c['last_ts'], '%b %d %H:%M')}" if c.get("n") else "none yet")
    return pol, used


@st.dialog("Tool Details", width="large", on_dismiss=_close_item)
def _tool_drawer(t: dict) -> None:
    a = t.get("annotations") or {}
    l, r = st.columns([3, 1.4])
    l.markdown(f"<div class='drw-id'>{html.escape(t['name'])}</div><div class='drw-meta'>"
               f"{KIND_NAMES.get(t.get('record_kind'), '')}<span>|</span>{settings.bucket}"
               f"<span>|</span>{html.escape(t.get('source') or '')}</div>",
               unsafe_allow_html=True)
    r.markdown(f"<div class='drw-start'>Published:<b>{_cap_date(t.get('ts'))}</b></div>",
               unsafe_allow_html=True)
    st.markdown("<div class='drw-rule'></div>", unsafe_allow_html=True)
    st.markdown(html.escape((t.get("description") or "").strip()).replace("\n", "<br>"),
                unsafe_allow_html=True)
    pol, used = _tool_policy(t["name"])
    rows = [("Policy for the store assistant", pol), ("Policy checks", used)] + [
        (k.replace("_", " ").capitalize(), html.escape(str(v))) for k, v in a.items()] + [
        ("Catalog ID", f"<code>{t.get('catalog_identifier') or ''}</code>"),
        ("Git commit", f"<code>{t.get('commit') or ''}</code> · {t.get('versions') or 1} "
                    f"published")]
    st.markdown("<table class='drw-tbl'>" + "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table>",
        unsafe_allow_html=True)
    st.caption("Being listed here doesn't let the assistant use a tool — the policy is what "
               "it's granted, checked before every call and written to `aidp.audit_log`.")
    st.code(t.get("code") or "", "python")
    if st.container(key="drwdone_item").button("Done", type="primary"):
        _done_item()


# --- Prompts Hub ------------------------------------------------------------- #
PROMPT_W = [3.3, 0.9, 1.1, 1.0, 1.9, 2.4, 0.45]


def _prompts_hub() -> None:
    prompts = agentc_bridge.prompts()
    _cap_start("prompts", expanded=not prompts)
    f = _cap_filters("ph")
    if not prompts:
        _empty("Prompts", "Commit, and the post-commit hook publishes them.")
        return
    shown = _hub_filter(prompts, f)
    box = _cap_head(PROMPT_W, [("Name", ""), ("Bucket", ""),
                               ("Catalog ID", "The catalog version this prompt was published in"),
                               ("Version", "The git commit of this version, and how many "
                                           "versions have been published"),
                               ("Date", ""), ("Source", "The file the prompt is defined in"),
                               ("", "")], "ph")
    for p in shown:
        c = _cap_row(box, PROMPT_W, f"ph_{p['name']}")
        _hub_name_cell(c[0], p, f"ph_{p['name']}", _open_item)
        c[1].markdown(f"<div class='cap-td'>{settings.bucket}</div>", unsafe_allow_html=True)
        c[2].markdown(f"<div class='cap-td'>{(p.get('catalog_identifier') or '')[:7]}</div>",
                      unsafe_allow_html=True)
        c[3].markdown(_hub_version(p), unsafe_allow_html=True)
        c[4].markdown(f"<div class='cap-mut'>{_cap_date(p.get('ts'))}</div>",
                      unsafe_allow_html=True)
        c[5].markdown(f"<div class='cap-td'>{html.escape(p.get('source') or '')}</div>",
                      unsafe_allow_html=True)
        _copy_cell(c[6], p, f"ph_{p['name']}")
    st.markdown(f"<div class='cap-count'>Showing {len(shown)} of {len(prompts)} results</div>",
                unsafe_allow_html=True)
    if ss.get("hub_item") and ss.hub_item in {p["name"] for p in prompts}:
        _prompt_drawer(next(p for p in prompts if p["name"] == ss.hub_item))


@st.dialog("Prompt Details", width="large", on_dismiss=_close_item)
def _prompt_drawer(p: dict) -> None:
    a = p.get("annotations") or {}
    model = settings.chat_model if a.get("model") == "chat" else settings.vision_model
    _, where = agentc_bridge.prompt(p["name"], "")
    uses = agentc_bridge.prompt_uses().get(p["name"], 0)
    l, r = st.columns([3, 1.4])
    l.markdown(f"<div class='drw-id'>{html.escape(p['name'])}</div><div class='drw-meta'>"
               f"{settings.bucket}<span>|</span>{html.escape(p.get('source') or '')}</div>",
               unsafe_allow_html=True)
    r.markdown(f"<div class='drw-start'>Published:<b>{_cap_date(p.get('ts'))}</b></div>",
               unsafe_allow_html=True)
    st.markdown("<div class='drw-rule'></div>", unsafe_allow_html=True)
    st.markdown(html.escape((p.get("description") or "").strip()), unsafe_allow_html=True)
    tools = ", ".join(f"<code>{html.escape(t.get('name', ''))}</code>"
                      for t in p.get("tools") or [] if isinstance(t, dict)) or "—"
    rows = [("Sent to", f"<code>{model}</code>"), ("Loaded by the app from", html.escape(where)),
            ("Used in the Agent Tracer", f"{uses}×"), ("Tools", tools)] + [
        (k.replace("_", " ").capitalize(), html.escape(str(v))) for k, v in a.items()] + [
        ("Catalog ID", f"<code>{p.get('catalog_identifier') or ''}</code>"),
        ("Git commit", f"<code>{p.get('commit') or ''}</code> · {p.get('versions') or 1} "
                    f"published")]
    st.markdown("<table class='drw-tbl'>" + "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table>",
        unsafe_allow_html=True)
    st.caption("The app reads this prompt from the hub when it calls the model — change it, "
               "commit, and the next call uses the new version.")
    st.code(str(p.get("content") or "").strip(), None, wrap_lines=True)
    if st.container(key="drwdone_item").button("Done", type="primary"):
        _done_item()


# --- Agent Tracer ------------------------------------------------------------ #
TYPE_STYLE = {"User": ("#4f7cf7", "person", "blue"),
              "Internal": ("#2f5bd3", "forum", "blue"),
              "LLM": ("#4caf50", "memory", "green"),
              "Tool Call": ("#f59e0b", "build", "orange"),
              "Tool Results": ("#1e3a8a", "keyboard_return", "blue"),
              "Hand-off": ("#2e7d32", "swap_horiz", "green"),
              "System": ("#e08a1e", "computer", "orange"),
              "Assistant": ("#8b5cf6", "chat", "violet")}
TYPE_HELP = {"User": "What the customer typed",
             "Internal": "What the app sent a model: the prompt from the Prompts Hub, and "
                         "the tools offered",
             "LLM": "A model's answer",
             "Tool Call": "A tool the model chose to call, and its arguments",
             "Tool Results": "What a tool returned",
             "Hand-off": "One agent handing over to another",
             "System": "Steps the app ran itself: searches, reads and writes in Couchbase",
             "Assistant": "The answer the customer saw"}
SESSION_W = [2.6, 1.6, 1.6, 0.9, 1.9]


def _agent_tracer() -> None:
    for name, v in FILTER_DEFAULTS.items():
        if ss.get(_fkey("tr", name)) is None:
            ss[_fkey("tr", name)] = v if v is not None else SEARCH_MODES["tr"][0]
    rng = DATE_RANGES.get(ss.tr_range)
    rows = agentc_bridge.sessions(
        app=None if ss.tr_pick == "All" else ss.tr_pick,
        since_ms=int((time.time() - rng * 60) * 1000) if rng else None,
        session=ss.tr_search if ss.tr_mode == "Session Id" else "",
        tag=ss.tr_search if ss.tr_mode == "Tags" else "")
    _cap_start("tracer", expanded=not rows)
    _cap_filters("tr", "App", agentc_bridge.apps())
    if not rows:
        _empty("Agent Sessions", "Make sure to initialize and log session traces to the "
               f"bucket. <a href='{DOCS}agent-tracer/add-spans-callbacks.html' "
               "target='_blank'>Learn More ↗</a>")
    else:
        _session_rows(rows)
    sql = (f"SELECT l.* FROM {settings.bucket}.agent_activity.logs AS l\n"
           f"WHERE l.span.session = \"<session id>\" ORDER BY l.timestamp")
    st.markdown(
        "<div class='cap-info'><div class='ic'>ⓘ</div><div>"
        f"<div><a href='{DOCS}agent-tracer/agent-tracer.html' target='_blank'>Learn More ↗</a>"
        " about how to delete traces. <i style='color:#6b7280'>Here: Reset demo clears "
        f"them.</i></div><details><summary>Learn More ↗ <span style='color:#1f2937'>about "
        f"how to analyze traces using SQL++.</span></summary><pre>{html.escape(sql)}</pre>"
        "</details></div></div>", unsafe_allow_html=True)
    if ss.get("tracer_session"):
        _session_drawer(ss.tracer_session)


def _session_rows(rows: list[dict]) -> None:
    names = {c["customer_id"]: c["name"] for c in customers()}
    box = _cap_head(SESSION_W, [("Session", "The session id the app logs under — one per "
                                            "browser session and signed-in customer"),
                                ("Tags", "Who the session was for"),
                                ("App", "The root span: the agent that logged it"),
                                ("Bucket", "Where the traces are stored"),
                                ("Date", "When the session started")], "tr")
    for r in rows:
        c = _cap_row(box, SESSION_W, f"tr_{r['sid']}")
        mine = r["sid"] == ss.session_id
        c[0].button(r["sid"] + (" ●" if mine else ""), key=f"lnk_trs_{r['sid']}",
                    type="tertiary", on_click=_open_session, args=(r["sid"],),
                    help="This browser session" if mine else "Open the session's trace logs")
        who = sorted({names.get(u, "Guest" if u == "GUEST" else u)
                      for u in r.get("users") or [] if u})
        c[1].markdown("".join(f"<span class='cap-chip'>{html.escape(w)}</span>" for w in who)
                      or "<div class='cap-td'>-</div>", unsafe_allow_html=True)
        c[2].markdown(f"<div class='cap-td'>{html.escape(r.get('root') or '')}</div>",
                      unsafe_allow_html=True)
        c[3].markdown(f"<div class='cap-td'>{settings.bucket}</div>", unsafe_allow_html=True)
        c[4].markdown(f"<div class='cap-mut'>{_cap_date(ms=r.get('start_ms'))}</div>",
                      unsafe_allow_html=True)
    st.markdown(f"<div class='cap-count'>Showing {len(rows)} of {len(rows)} results</div>",
                unsafe_allow_html=True)


def _all_types(on: bool = True) -> None:
    for t in agentc_bridge.TRACE_TYPE_ORDER:
        ss[f"trf_{t}"] = on
    ss.trf_All = on


def _open_session(sid: str) -> None:
    ss.tracer_session, ss.tracer_tag = sid, ""
    _all_types(True)


def _close_session() -> None:
    ss.tracer_session = None


def _toggle_all() -> None:
    _all_types(bool(ss.get("trf_All")))


def _sync_all() -> None:
    ss.trf_All = all(ss.get(f"trf_{t}", True) for t in agentc_bridge.TRACE_TYPE_ORDER)


def _show_all_runs() -> None:
    ss.tracer_tag = ""


def _type_icon(t: str) -> str:
    colour, icon, _ = TYPE_STYLE.get(t, TYPE_STYLE["System"])
    return f"<span class='lgi' style='background:{colour}'>{icon}</span>"


@st.dialog("Session Trace Logs", width="large", on_dismiss=_close_session)
def _session_drawer(sid: str) -> None:
    """One session's messages in order, filtered by trace type — Capella's drawer."""
    logs = agentc_bridge.session_logs(sid)
    if not logs:
        st.info("This session has no trace logs (the Agent Tracer may have been reset).")
        if st.container(key="drwdone_empty").button("Done", type="primary"):
            _close_session()
            st.rerun()
        return
    root = (logs[0].get("span") or ["?"])[0]
    names = {c["customer_id"]: c["name"] for c in customers()}
    for lg in logs:
        lg["type"] = agentc_bridge.TRACE_TYPES.get((lg.get("content") or {}).get("kind"),
                                                   "System")
    h1, h2 = st.columns([3, 1.4])
    h1.markdown(
        f"<div class='drw-id'>{html.escape(sid)}"
        + (" <span class='chip c-ok'>this browser</span>" if sid == ss.session_id else "")
        + f"</div><div class='drw-meta'>{html.escape(root)}<span>|</span>{len(logs)} "
        f"messages<span>|</span>{settings.bucket}</div>", unsafe_allow_html=True)
    h2.markdown(f"<div class='drw-start'>Started:<b>"
                f"{_when(logs[0].get('timestamp'), '%b %d, %Y, %H:%M %Z')}</b></div>",
                unsafe_allow_html=True)
    st.markdown("<div class='drw-rule'></div>", unsafe_allow_html=True)
    counts = {t: sum(1 for lg in logs if lg["type"] == t)
              for t in agentc_bridge.TRACE_TYPE_ORDER}
    if ss.get("trf_All") is None:
        _all_types(True)
    st.markdown("<style>" + "".join(
        f"[class*='st-key-trf_{t.replace(' ', '-')}'] .stMarkdownColoredBackground"
        f"{{background:{c}!important}}"
        for t, (c, _, _) in TYPE_STYLE.items()) + "</style>", unsafe_allow_html=True)
    left, right = st.columns([3.2, 1], gap="medium")
    with right:
        st.markdown("<div class='drw-filter'>⚲ Filter By</div>", unsafe_allow_html=True)
        st.checkbox("All", key="trf_All", on_change=_toggle_all)
        for t in agentc_bridge.TRACE_TYPE_ORDER:
            _, icon, colour = TYPE_STYLE[t]
            st.checkbox(f":{colour}-background[:material/{icon}:] {t} ({counts[t]})",
                        key=f"trf_{t}", on_change=_sync_all, help=TYPE_HELP[t])
    tag = (ss.get("tracer_tag") or "").strip().lower()
    types = {t for t in agentc_bridge.TRACE_TYPE_ORDER if ss.get(f"trf_{t}", True)}

    def tagged(lg) -> bool:
        return not tag or any(tag in f"{k}={v}".lower()
                              for k, v in (lg.get("annotations") or {}).items())
    shown = [lg for lg in logs if lg["type"] in types and tagged(lg)]
    with left:
        if tag:
            a, b = st.columns([3, 1], vertical_alignment="center")
            a.markdown(f"<div class='sub'>Showing one request: <code>{html.escape(tag)}"
                       f"</code></div>", unsafe_allow_html=True)
            b.button("Show all", icon=":material/close:", type="tertiary",
                     on_click=_show_all_runs, key="trl_all")
        with st.container(key="drwlog"):
            if not shown:
                st.markdown("<div class='sub'>No messages match these filters.</div>",
                            unsafe_allow_html=True)
            out, last_run = [], None
            for lg in shown:
                ann = lg.get("annotations") or {}
                if ann.get("run") != last_run:          # a line for each child span
                    last_run = ann.get("run")
                    who = names.get(ann.get("user"), "Guest") if ann.get("user") else ""
                    out.append(
                        f"<div class='lgspan'>▸ <code>"
                        f"{html.escape(' › '.join(lg.get('span') or []))}</code>"
                        + (f" · {html.escape(who)}" if who else "")
                        + (f" · {ann['ms']:,.0f} ms" if ann.get("ms") is not None else "")
                        + (f" · {ann['tokens_spent']:,} tokens spent"
                           if ann.get("tokens_spent") else "")
                        + (f" · ⚡ {ann['tokens_saved']:,} tokens saved"
                           if ann.get("tokens_saved") else "") + "</div>")
                summary, detail = _trace_message(lg.get("content") or {})
                tags = _chips([f"{k}={v}" for k, v in ann.items()], "c-g")
                raw = html.escape(json.dumps(lg.get("content") or {}, indent=1,
                                             default=str)[:6000])
                colour = TYPE_STYLE.get(lg["type"], TYPE_STYLE["System"])[0]
                out.append(
                    f"<div class='lgw'>{_when(lg.get('timestamp'), '%b %d, %Y %H:%M:%S %Z')}"
                    f"{_type_icon(lg['type'])}</div>"
                    f"<div class='lgc' style='border-color:{colour}'><div class='lgx'>"
                    f"{summary}</div><details><summary></summary>{summary}{detail}"
                    f"<div style='margin-top:6px'>{tags}</div><pre>{raw}</pre></details></div>")
            st.markdown("".join(out), unsafe_allow_html=True)
        st.caption(f"{len(shown)} of {len(logs)} messages")
    if st.container(key="drwdone_tr").button("Done", type="primary", key="trl_done"):
        _close_session()
        st.rerun()


def _ts(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _trace_message(c: dict) -> tuple[str, str]:
    """A log's content -> (one-line message, details: reads, writes, indexes, tokens)."""
    kind = c.get("kind")
    esc = lambda v, n=400: html.escape(str(v if v is not None else "")[:n])  # noqa: E731
    if kind in ("user", "assistant", "system"):
        return esc(c.get("value"), 4000), ""
    if kind == "request-header":
        m = c.get("meta") or {}
        ps = m.get("prompts") or []
        n_tools = len(c.get("tools") or [])
        head = (f"<b>Sent the model its prompt" + (f" and {n_tools} tools" if n_tools else "")
                + f"</b><div class='sub'>for: {esc(m.get('what', 'a model call'))} · to "
                f"<code>{esc(m.get('model'))}</code>"
                + "".join(f" · prompt <code>{esc(p.get('name'))}</code> from "
                          f"{esc(p.get('source'))}" for p in ps) + "</div>")
        body = "".join(f"<div class='sub' style='margin-top:6px'>Prompt <code>"
                       f"{esc(p.get('name'))}</code></div><pre>{esc(p.get('content'), 4000)}"
                       f"</pre>" for p in ps)
        if c.get("tools"):
            body = ("<div class='sub'>tools offered: " + ", ".join(
                f"<code>{esc(t.get('name'))}</code>" for t in c["tools"]) + "</div>" + body)
        return head, body
    if kind == "chat-completion":
        m = c.get("meta") or {}
        return (f"<b>{esc(m.get('what', 'Model answer'))}</b><div class='sub'><code>"
                f"{esc(m.get('model'))}</code> — {esc(c.get('output'), 2000)}</div>",
                _log_detail(m))
    if kind == "tool-call":
        args = ", ".join(f"{k}={json.dumps(v, default=str)[:120]}"
                         for k, v in (c.get("tool_args") or {}).items()
                         if k in ("request", "what") or not isinstance(v, (dict, list)))
        st_ = c.get("status")
        return (f"<code>{esc(c.get('tool_name'))}({html.escape(args[:300])})</code>"
                + (f" <span class='chip {'c-no' if st_ == 'error' else 'c-ok'}'>{st_}</span>"
                   if st_ else ""), "")
    if kind == "tool-result":
        st_ = c.get("status")
        return ((f"<span class='chip {'c-no' if st_ == 'error' else 'c-ok'}'>{st_}</span> "
                 if st_ else "") + esc(c.get("tool_result"), 2000), "")
    if kind == "key-value":
        v = c.get("value") if isinstance(c.get("value"), dict) else {}
        steps = v.get("steps") or []
        head = (f"<b>{esc(c.get('key'))}</b>"
                + (f"<div class='sub'>{esc(v.get('result', ''))}</div>" if v.get("result")
                   else "")
                + (f"<div class='sub'>{len(steps)} step{'s' if len(steps) != 1 else ''} · "
                   f"{esc(', '.join(v.get('services') or []))}</div>" if steps else ""))
        body = _log_detail(v) + "".join(
            f"<div style='margin-top:8px'><b style='font-weight:600'>{esc(r.get('what'))}</b>"
            f"<div class='sub'>{esc(r.get('result'), 300)}</div><div class='sub'><i>"
            f"{esc(r.get('how'), 300)}</i></div>{_log_detail(r)}</div>" for r in steps)
        return head, body
    if kind == "edge":
        return f"{esc(c.get('source'))} → {esc(c.get('dest'))}", ""
    if kind in ("begin", "end"):
        return f"<span class='sub'>span {'entered' if kind == 'begin' else 'exited'}</span>", ""
    return esc(c), ""


def _log_detail(v: dict) -> str:
    out = (_chips([f"READ {r}" for r in v.get("reads") or []], "c-r")
           + _chips([f"WRITE {w}" for w in v.get("writes") or []], "c-w")
           + _chips([f"INDEX {i}" for i in v.get("indexes") or []], "c-p"))
    if v.get("tokens_spent"):
        out += _chips([f"{v['tokens_spent']:,} tokens spent"], "c-old")
    if v.get("tokens_saved"):
        out += _chips([f"⚡ {v['tokens_saved']:,} tokens saved"], "c-ok")
    if v.get("service"):
        out = _chips([v["service"]], "c-g") + out
    if v.get("ms") is not None:
        out += f"<span class='sub' style='margin-left:4px'>{v['ms']:,.0f} ms</span>"
    return f"<div style='margin-top:6px'>{out}</div>" if out else ""


ROOT_SPAN = agentc_bridge.ROOT


def page_placeholder(name: str) -> None:
    st.info(f"**{name}** is coming in the next build phase.")


def page_credits() -> None:
    st.markdown("### Photo credits")
    st.write("Product images are 3D renders drawn by the demo from each product's "
             "dimensions and colour. The room photos are from **Pixabay** (Pixabay Content "
             "License), credited to the photographers below. Brands, prices, names and "
             "specifications in this demo store are fictional.")
    rows = {}
    for p in all_products().values():
        c = p.get("image_credit")
        if c:
            key = (c["photographer"], c.get("source", "Pixabay"),
                   c.get("licence", "Pixabay Content License"), c.get("licence_url", ""))
            rows.setdefault(key, (c["photographer_url"], []))[1].append(p["name"])
    for room, photos in room_photos().items():
        for ph in photos:
            key = (ph["photographer"], ph.get("source", "Pixabay"),
                   ph.get("licence", "Pixabay Content License"), ph.get("licence_url", ""))
            rows.setdefault(key, (ph["photographer_url"], []))[1].append(
                f"{planner.ROOM_LABELS.get(room, room)} photo")
    for (who, source, lic, lic_url), (url, items) in sorted(rows.items(),
                                                             key=lambda kv: kv[0][0].lower()):
        licence = f"[{lic}]({lic_url})" if lic_url else lic
        st.markdown(f"- [{who}]({url}) · {source} · {licence} — "
                    f"{', '.join(sorted(set(items)))}")

@st.cache_data(ttl=60, show_spinner=False)
def _models_pulled() -> list[str] | None:
    """The models the Ollama server has pulled; None when it can't be reached."""
    import httpx
    import re
    try:
        r = httpx.get(re.sub(r"/v1/?$", "", settings.model_endpoint) + "/api/tags", timeout=1.0)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Ask the Assistant: general store questions, answered once, then from the cache
# --------------------------------------------------------------------------- #
def _history(n: int = 3) -> list[dict]:
    """The last few turns, oldest first, with the products each one showed."""
    prods = all_products()
    out = []
    mine = [r for r in ss.get("ask_log", []) if r.get("cid") == ss.customer_id]
    for r in reversed(mine[:n]):
        if r.get("error"):
            continue
        shown = [prods[s_] for s_ in r.get("products", []) if s_ in prods]
        out.append({"q": r["q"], "answer": r["answer"],
                    "shown": [{"sku": p["sku"], "name": p["name"],
                               "colours": [c["color"] for c in p.get("colors_available", [])]}
                              for p in one_per_base(shown)],
                    "suggested": [{"name": prods[g["sku"]]["name"], "why": g["why"],
                                   "memory": g.get("memory")}
                                  for g in r.get("goes_with", []) if g["sku"] in prods],
                    "room_ask": r.get("room_ask"), "room_plan": r.get("room_plan")})
    return out


def _open_room(room_type: str, length_ft: float, width_ft: float, people: int,
               bring: dict | None = None, video: bool | None = None) -> None:
    """From the chat to the Room Planner, set up for the room just talked about —
    and, when they're moving, with what they said they're bringing."""
    _planner_defaults()                       # this customer's settings first
    _set_room(room_type, length_ft, width_ft, people)
    if video is not None:                     # "no video conference needed" in the chat
        ss.rp_video = video
    if bring:
        _fill_bring(bring)
        ss.rp_moving, ss.rp_bring_for = True, ss.customer_id
    go("Room Planner")


def _ask(q: str) -> None:
    q = (q or "").strip()
    if not q:
        return
    t = trace.new()
    try:
        r = assistant.ask(q, trace=t, customer_id=ss.customer_id, session_id=ss.session_id,
                          history=_history())
    except Exception as exc:
        r = {"answer": f"Sorry — the assistant couldn't answer just now ({type(exc).__name__}).",
             "cached": False, "tokens": 0, "ms": 0, "saved_ms": 0, "sources": [],
             "error": True}
    ss.ask_log = [{"q": q, "cid": ss.customer_id, **r}] + ss.get("ask_log", [])[:19]
    if any(c.get("saved") for c in r.get("tools", [])):
        _refresh_hints()                       # what was just remembered applies now
    ss.ask_trace = t
    traced("ask", t, q, r["answer"])
    ss.ask_box = ""


def _ask_size(key: str) -> None:
    """The room's size, typed into the "how big is it?" card."""
    _ask(ss.get(key))


def _reset_demo() -> None:
    """A fresh demo: empty the semantic cache and its counts, and forget every
    photo read before — so each one is read by the vision model again."""
    semantic_cache.reset()
    n = vision.forget_photos()
    runs = agentc_bridge.clear_activity()
    st.toast(f"Demo reset — semantic cache emptied, counts zeroed, {n} saved photo "
             f"reading{'s' if n != 1 else ''} removed, Agent Tracer cleared "
             f"({runs} log entries).", icon="🔄")
    ss.ask_log = []
    ss.pop("ask_trace", None)
    for k in [k for k in ss if str(k).startswith("ph_")]:
        del ss[k]                                   # the upload box and its readings
    if ss.get("rp_replicate") is not None:          # a room copied from a photo
        ss.plan = None
    ss.rp_replicate = ss.rp_photo_bytes = ss.rp_photo_trace = None
    ss.rp_owned = []


def _without_listed(answer: str, found: list[dict]) -> str:
    """The answer without the lines that list products the cards already show — a
    small model lists them even when told not to."""
    names = [n.lower() for p in found for n in (p["name"], p.get("base_name") or "",
                                                re.split(r"\s\d", p.get("base_name") or "")[0])
             if len(n) >= 8]                   # "Trestle Desk", however the size is written
    keep, dropped = [], False
    for ln in answer.splitlines():
        low = ln.lower()
        # a list line naming a product — not a sentence that mentions one
        listed = any(n in low and (re.match(r"\s*(?:[-*•]|\d+[.)])\s", ln)
                                   or len(low) <= len(n) + 60) for n in names)
        if listed or (dropped and low.strip(" -*•").startswith(("rating", "price", "$"))):
            dropped = True
            continue
        keep.append(ln)
    text = "\n".join(keep).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text if dropped and text else answer


def page_assistant() -> None:
    st.markdown("### Ask the Assistant")
    st.caption(f"The chat model (`{settings.chat_model}`) is given "
               f"{len(assistant.AGENT_TOOLS)} tools from the Agent Catalog — help articles, "
               "product search, suggestions, add to cart, your cart, your orders, refunds and "
               "remembering a preference — and **decides which to call**. Every call passes the policy check "
               "first. Store questions are answered once, then any question that **means the "
               "same** comes straight from Couchbase's semantic cache: no model call, no "
               "tokens, in milliseconds.")
    tot = semantic_cache.totals()          # one tally for every shopper and session
    st.markdown("**All shoppers, since the last reset**")
    a, b, c, d = st.columns(4)
    a.metric("Questions asked", tot["questions"])
    rate = tot["hits"] / tot["questions"] if tot["questions"] else 0
    b.metric("Answered from cache", f"{tot['hits']}", f"{rate:.0%} of questions",
             delta_color="off")
    bill = tot["tokens_spent"] + tot["tokens_saved"]
    misses = tot["questions"] - tot["hits"]
    c.metric("Tokens spent", f"{tot['tokens_spent']:,}",
             f"{misses} answer{'s' if misses != 1 else ''} from the model", delta_color="off")
    d.metric("Tokens saved", f"{tot['tokens_saved']:,}",
             f"{tot['tokens_saved'] / bill:.0%} of the bill" if bill else None,
             delta_color="off")

    st.caption("Store questions — try one, then ask it again in other words (semantic cache):")
    cols = st.columns(len(assistant.EXAMPLES))
    for col, ex in zip(cols, assistant.EXAMPLES):
        col.button(ex, key=f"ex::{ex}", width="stretch", on_click=_ask, args=(ex,))
    st.caption("Ask it to do something — the model picks the tools"
               + (" (sign in, top right, for your orders)" if is_guest() else "") + ":")
    cols = st.columns(len(assistant.TOOL_EXAMPLES))
    for col, ex in zip(cols, assistant.TOOL_EXAMPLES):
        col.button(ex, key=f"tx::{ex}", width="stretch", on_click=_ask, args=(ex,))
    with st.form("ask", border=False):
        x, y = st.columns([5, 1])
        x.text_input("Your question", key="ask_box", label_visibility="collapsed",
                     placeholder="e.g. how soon will my order get here?")
        y.form_submit_button("Ask", type="primary", width="stretch",
                             on_click=lambda: _ask(ss.get("ask_box")))

    for i, r in enumerate(ss.get("ask_log", [])):
        if r.get("cid", ss.customer_id) != ss.customer_id:
            continue                           # another customer's conversation
        with st.container(border=True):
            st.markdown(f"**🙋 {r['q']}**")
            if r.get("tools"):
                st.markdown(
                    "<div style='font-size:.8rem;color:#555;margin:-4px 0 4px'>"
                    + (("📐 Planned with the Room Planner — no model call: "
                        if any(c["name"] == "plan_room" for c in r["tools"])
                        else "⚙️ Done by the app — no model call: ")
                       if all(c.get("by_app") for c in r["tools"]) else "🛠️ The model called: ")
                    + " → ".join(
                        f"<code style='color:{'#b42318' if c['status'] == 'error' else '#1a7f37'}'>"
                        f"{html.escape(c['name'])}({html.escape(', '.join(f'{k}={v!r}' for k, v in (c.get('args') or {}).items()))})"
                        f"</code>{' 🚫 refused by policy' if c.get('refused') else ' ⚠️ error' if c['status'] == 'error' else  ' 🧠 updated in Agent Memory' if c.get('updated') else ' 🧠 saved to Agent Memory' if c.get('saved') else ' ⏭️ skipped' if c.get('skipped') else ''}"
                        for c in r["tools"]) + "</div>", unsafe_allow_html=True)
            elif r.get("cached"):
                st.markdown("<div style='font-size:.8rem;color:#555;margin:-4px 0 4px'>⚡ From "
                            "the semantic cache — no model call</div>", unsafe_allow_html=True)
            found = one_per_base([all_products()[s_] for s_ in r.get("products", [])
                                  if s_ in all_products()])
            goes = [{"product": all_products()[g["sku"]], **g}
                    for g in r.get("goes_with", []) if g["sku"] in all_products()]
            shown = found + [g["product"] for g in goes]
            text = _without_listed(r["answer"], shown) if shown else r["answer"]
            st.markdown(text.replace("$", "\\$"))   # two prices aren't LaTeX
            n = len(ss.ask_log) - i                # stable while newer questions arrive
            if found:                              # what search_products found, as cards
                grid(found, f"ask{n}", cols=5, preferred="own",
                     where=f"ask{n}")
            goes_with_row(goes, f"askgw{n}")
            if r.get("orders"):                    # what list_orders found, every one
                st.dataframe(
                    [{"Order": o["order_id"], "Placed": o["placed_on"],
                      "Status": o["status"].replace("_", " "), "Total": o.get("total_usd"),
                      "Shipping": o.get("shipping", "Standard")
                      + (f" — arrives {o['arrives_by']}" if o.get("arrives_by") else ""),
                      "Items": ", ".join(o["items"])} for o in r["orders"]],
                    hide_index=True, key=f"askord{n}",
                    column_config={"Total": st.column_config.NumberColumn(format="dollar")})
            if (r.get("room_ask") or {}).get("stage") == "bring":   # moving: what's coming?
                ra = r["room_ask"]
                with st.container(horizontal=True):
                    if ra.get("remembered"):
                        st.button(f"🚚 Bring what you told us: {ra['remembered']}",
                                  key=f"rbr{n}::mem", on_click=_ask,
                                  args=(f"Bring {ra['remembered']}",))
                    st.button("Nothing — all new", key=f"rbr{n}::none", on_click=_ask,
                              args=("Nothing — all new",))
                if i == 0:
                    with st.form(f"rbr_form{n}", border=False):
                        x, y = st.columns([5, 1], vertical_alignment="bottom")
                        x.text_input("What are you bringing?", key=f"rbr_box{n}",
                                     placeholder="e.g. 2 chairs, a desk and a cabinet")
                        y.form_submit_button("Next", type="primary", width="stretch",
                                             on_click=_ask_size, args=(f"rbr_box{n}",))
            elif (r.get("room_ask") or {}).get("stage") == "resize":   # planned at a size
                ra = r["room_ask"]
                cur = tuple(ra.get("current") or ())
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown("Other sizes:", width="content")
                    for lf, wf in ra["sizes"]:
                        st.button(f"✓ {lf} × {wf} ft" if (lf, wf) == cur else f"{lf} × {wf} ft",
                                  key=f"rsz{n}::{lf}x{wf}", disabled=(lf, wf) == cur,
                                  on_click=_ask, args=(f"{lf} x {wf} ft",))
                if i == 0:                         # or type it, right here
                    with st.form(f"rsz_form{n}", border=False):
                        x, y = st.columns([5, 1], vertical_alignment="bottom")
                        x.text_input("Or type your room's size", key=f"rsz_box{n}",
                                     placeholder=f"e.g. {cur[0]} x {cur[1]} ft, or 300 sq ft"
                                     if cur else "e.g. 20 x 15 ft, or 300 sq ft")
                        y.form_submit_button("Re-plan", type="primary", width="stretch",
                                             on_click=_ask_size, args=(f"rsz_box{n}",))
            elif r.get("room_ask"):                # "how big is it?" — answer in one click
                ra = r["room_ask"]
                cols = st.columns(len(ra["sizes"]) + 1)
                for col, (lf, wf) in zip(cols, ra["sizes"]):
                    col.button(f"{lf} × {wf} ft", key=f"rsz{n}::{lf}x{wf}", width="stretch",
                               on_click=_ask, args=(f"{lf} x {wf} ft",))
                lf, wf = ra["sizes"][1]
                cols[-1].button("📐 Plan it in the Room Planner", key=f"rpl{n}", type="primary",
                                width="stretch", on_click=_open_room,
                                args=(ra["room_type"], lf, wf, ra["people"],
                                      ra.get("bring")))
                if i == 0:                         # or type it, right here
                    with st.form(f"rsz_form{n}", border=False):
                        x, y = st.columns([5, 1], vertical_alignment="bottom")
                        x.text_input("Or type your room's size", key=f"rsz_box{n}",
                                     placeholder=f"e.g. {lf} x {wf} ft, or 300 sq ft")
                        y.form_submit_button("Plan it", type="primary", width="stretch",
                                             on_click=_ask_size, args=(f"rsz_box{n}",))
            if r.get("room_plan"):
                rp = r["room_plan"]
                with st.container(horizontal=True):
                    st.button(f"📐 Open this {rp['length_ft']} × {rp['width_ft']} ft room in "
                              "the Room Planner", key=f"rop{n}", type="primary",
                              on_click=_open_room, args=(rp["room_type"], rp["length_ft"],
                                                         rp["width_ft"], rp["people"],
                                                         rp.get("bring"),
                                                         rp.get("video_calls")))
                    if i == 0 and rp.get("units"):     # the whole plan, from the chat
                        st.button(f"🛒 Add all {rp['units']} items to cart", key=f"rac{n}",
                                  on_click=_ask,
                                  args=(f"Add all {rp['units']} items to my cart",))
            if r.get("error") or r.get("off_topic"):
                st.caption("Not a store question we have an answer for — nothing cached.")
            if i == 0:                         # the trace is the newest question's
                show_trace(ss.get("ask_trace"))


# --------------------------------------------------------------------------- #
# sidebar: who you are, and what Agent Memory remembers about you
# --------------------------------------------------------------------------- #
PREF_KEYS = ("pf_color", "pf_chair", "pf_monitors", "pf_standing", "pf_carpet", "pf_ergo",
             "pf_note")


def _remember_prefs() -> None:
    g = ss.get
    said = []
    if g("pf_color", "Any") != "Any":
        said.append(f"Prefers {g('pf_color')} furniture.")
    if g("pf_chair", "Any") != "Any":
        said.append(f"Likes {g('pf_chair')} chairs.")
    if g("pf_monitors"):
        said.append(f"Uses {['', 'one', 'two', 'three'][g('pf_monitors')]} "
                    f"monitor{'s' if g('pf_monitors') > 1 else ''}.")
    if g("pf_standing"):
        said.append("Wants a standing desk.")
    if g("pf_ergo"):
        said.append("Needs ergonomic chairs.")
    if g("pf_carpet"):
        said.append("The office floor is carpet.")
    if (g("pf_note") or "").strip():
        said.append(g("pf_note").strip())
    t = trace.new()
    done = []
    for text in said:
        t0 = time.perf_counter()
        try:
            memory.remember(ss.customer_id, text, kind="preference", source="ui",
                            session_id=ss.session_id)
        except Exception as exc:            # keep the form filled so they can try again
            st.toast(f"Agent Memory couldn't save that just now ({type(exc).__name__}) — "
                     "try again in a moment.", icon="⚠️")
            break
        done.append(text)
        t.add("Agent Memory", "Remembered a preference", text,
              "remember() on the Agent Memory server", (time.perf_counter() - t0) * 1000,
              writes=["agentmemory.memory"], indexes=["AgentMemory_BlockEmbeddingIndex"])
    if len(done) == len(said):
        for k in PREF_KEYS:
            ss.pop(k, None)
    _refresh_hints()
    ss.pref_saved = len(done)
    ss.last_trace = t
    if done:
        traced("save_preference", t, "Remember: " + " ".join(done))


def _forget(memory_id: str) -> None:
    try:
        memory.forget(ss.customer_id, [memory_id])
    except Exception as exc:
        st.toast(f"Agent Memory couldn't forget that just now ({type(exc).__name__}) — "
                 "try again in a moment.", icon="⚠️")
        return
    _refresh_hints()


def _update(memory_id: str, old: str, key: str) -> None:
    """Change a memory in place — Agent Memory keeps the id and re-embeds the text."""
    new = (ss.get(key) or "").strip()
    if not new or new == old:
        return
    t = trace.new()
    t0 = time.perf_counter()
    try:
        out = memory.update(ss.customer_id, memory_id, new)
    except Exception as exc:
        st.toast(f"Agent Memory couldn't save that just now ({type(exc).__name__}) — "
                 "try again in a moment.", icon="⚠️")
        return
    how = ("update_memory() on the Agent Memory server — same id, text re-embedded"
           if out.get("memory_id", memory_id) == memory_id else
           "the server had locked this memory, so it was saved as a new one and the "
           "old one deleted — add_memory() then delete_memory()")
    t.add("Agent Memory", "Updated a memory", f"“{old}” → “{new}”", how,
          (time.perf_counter() - t0) * 1000, writes=["agentmemory.memory"],
          indexes=["AgentMemory_BlockEmbeddingIndex"])
    _refresh_hints()
    ss.last_trace = t
    traced("save_preference", t, f"Update: {new}")
    ss.pref_updated = new


def _refresh_hints() -> None:
    """Memory in action: the planner picks up what was just remembered or forgotten."""
    t = trace.new()
    ss.rp_hints = planner.room_hints(ss.customer_id, all_colors(), trace=t)
    ss.rp_hint_trace, ss.rp_for = t, ss.customer_id
    _apply_hints(_room_hints())


MEM_CSS = """<style>
.membox{border:1px solid #ddd6fe;background:#faf8ff;border-radius:10px;padding:7px 12px 6px;
  margin:-2px 0 2px}
.membox .who{font-weight:700;color:#6d28d9;font-size:.85rem}
.membox ul{margin:3px 0 0;padding-left:18px;font-size:.78rem;line-height:1.45;color:#3b2a6b;
  columns:2;column-gap:28px}
.membox ul.one{columns:1}
.membox li{margin:0;break-inside:avoid}
.membox li.used{color:#15803d}
.membox li.used::marker{content:"✓  ";font-weight:700}
.membox .guest{color:#666;font-size:.78rem}
</style>"""


def memory_panel(box) -> None:
    """A slim strip under the tabs, on every page: who's shopping and what Agent
    Memory keeps about them, as a small bulleted box. Drawn into `box` after the page, so the
    Room Planner's ticks reflect the plan just made."""
    with box:
        if is_guest():
            st.markdown(MEM_CSS + "<div class='membox'><span class='who'>👤 Guest</span> "
                        "<span class='guest'>Sign in (top right) and Agent Memory remembers "
                        "your preferences — the Room Planner sets itself up for you.</span>"
                        "</div>", unsafe_allow_html=True)
            return
        name = _current_customer().get("name", ss.customer_id).split()[0]
        rows = [r for r in memory.list_for(ss.customer_id, 50) if r.get("text")]
        used = (set(_room_hints().get("used", []))
                if ss.page == "Room Planner" else set())
        a, b = st.columns([8, 1], vertical_alignment="center")
        items = "".join(
            ("<li class='used' title='The Room Planner uses this'>" if r["text"] in used
             else "<li>") + f"{html.escape(r['text'])}</li>" for r in rows)
        if memory.unavailable():
            body = "<div class='guest'>Agent Memory isn't answering right now.</div>"
        elif not rows:
            body = "<div class='guest'>Nothing yet — add a preference.</div>"
        else:
            body = f"<ul class='{'one' if len(rows) < 4 else ''}'>{items}</ul>"
        a.markdown(MEM_CSS + f"<div class='membox'><div class='who'>🧠 {name} · Agent "
                   f"Memory</div>{body}</div>", unsafe_allow_html=True)
        with b.popover("Edit", icon=":material/edit:", width="stretch"):
            st.markdown(f"**What we remember about {name}**")
            st.caption("Change one and press Save — or Forget to remove it.")
            for r in rows:
                # Streamlit drops a box's value on some reruns, and the browser can keep a
                # blank copy under the same key. So a refill gets a new key — a new box,
                # filled with the memory — and the key stays put while someone types.
                cur = f"memcur::{r['memory_id']}"
                key = ss.get(cur)
                if not key or key not in ss or ss.get(f"{key}::was") != r["text"]:
                    ss.memgen = ss.get("memgen", 0) + 1
                    key = ss[cur] = f"memedit::{r['memory_id']}::{ss.memgen}"
                    ss[f"{key}::was"] = r["text"]
                with st.container(border=True, gap="small"):
                    st.text_area("Memory", value=r["text"], key=key,
                                 label_visibility="collapsed", height=68)
                    x, y, _ = st.columns([1, 1, 1.4], gap="small")
                    x.button("Save", key=f"save::{r['memory_id']}", icon=":material/save:",
                             help="Save the change", width="stretch",
                             disabled=(ss.get(key) or "").strip() in ("", r["text"]),
                             on_click=_update, args=(r["memory_id"], r["text"], key))
                    y.button("Forget", key=f"forget::{r['memory_id']}",
                             icon=":material/delete:", help="Forget this", width="stretch",
                             on_click=_forget, args=(r["memory_id"],))
            st.markdown("**Add preferences**")
            colours = ["Any"] + all_colors()
            c1, c2 = st.columns(2)
            c1.selectbox("Furniture colour", colours, key="pf_color")
            c2.selectbox("Chair colour", colours, key="pf_chair")
            st.select_slider("Monitors", [0, 1, 2, 3], key="pf_monitors")
            c1, c2, c3 = st.columns(3)
            c1.checkbox("Standing desk", key="pf_standing")
            c2.checkbox("Ergonomic", key="pf_ergo")
            c3.checkbox("Carpet", key="pf_carpet")
            st.text_input("Anything else", key="pf_note",
                          placeholder="e.g. Budget around $2,000")
            st.button("Remember", type="primary", width="stretch", on_click=_remember_prefs)
        if ss.get("pref_updated"):
            st.toast(f"🧠 Updated — “{ss.pref_updated[:60]}”. Search, suggestions and the "
                     f"Room Planner use it now.")
            ss.pref_updated = ""
        if ss.get("pref_saved"):
            st.toast(f"🧠 Remembered {ss.pref_saved} preference"
                     f"{'s' if ss.pref_saved != 1 else ''} — the Room Planner now uses "
                     f"{'them' if ss.pref_saved != 1 else 'it'}.")
            ss.pref_saved = 0


def models_panel() -> None:
    """Which AI models the demo uses, at the bottom of the sidebar on every page."""
    from urllib.parse import urlparse
    pulled = _models_pulled()

    def dot(name: str) -> str:
        if pulled is None:
            return "<span title='model server unreachable' style='color:#d1242f'>●</span>"
        ok = any(m == name or m.split(":")[0] == name or m == name + ":latest" for m in pulled)
        return (f"<span title='{'ready' if ok else 'not pulled'}' "
                f"style='color:{'#1a7f37' if ok else '#d1242f'}'>●</span>")
    rows = [("Vision", settings.vision_model, "reads room photos — what's in the room and where",
             False),
            ("Embeddings", settings.embedding_model,
             f"{settings.embedding_dims}-d vectors — search by meaning", False),
            ("Chat", settings.chat_model, "answers store questions — cached by meaning", False)]
    html = ["<div class='mdl'><div class='mh'>🤖 AI models in use</div>"]
    for role, name, what, idle in rows:
        html.append(f"<div class='mr{' idle' if idle else ''}'><div class='mt'>{dot(name)} "
                    f"<span class='mrole'>{role}</span></div><div class='mn'><code>{name}"
                    f"</code></div><div class='mw'>{what}</div></div>")
    html.append(f"<div class='mf'>Served by Ollama at "
                f"{urlparse(settings.model_endpoint).netloc}</div></div>")
    st.sidebar.markdown(
        "<style>.mdl{border:1px solid #e3e3e3;border-radius:10px;padding:10px 12px;"
        "margin-top:14px;background:#fafafa}.mdl .mh{font-weight:700;margin-bottom:6px}"
        ".mdl .mr{margin:6px 0}.mdl .mr.idle{opacity:.55}.mdl .mrole{font-weight:600}"
        ".mdl code{font-size:.8rem;white-space:nowrap}.mdl .mn{margin-left:14px}"
        ".mdl .mw{color:#666;font-size:.78rem;margin-left:14px}"
        ".mdl .mf{color:#888;font-size:.74rem;margin-top:6px}</style>" + "".join(html),
        unsafe_allow_html=True)


header()
memory_box = st.container()      # filled after the page: see memory_panel
{"Photo credits": page_credits, "Shop": page_shop, "Cart": page_cart, "My Orders": page_orders,
 "Room Planner": page_room_planner, "Ask the Assistant": page_assistant,
 "Agent Catalog": page_agent_catalog}.get(ss.page, page_shop)()
memory_panel(memory_box)
models_panel()
catalog_panel()
