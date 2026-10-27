"""Room Planner: from a room's size, type and headcount to a to-scale layout and
a shopping list that fits.

Rule-based and deterministic, so the same room always gets the same plan, at
once. The rules (clearances, walkways, how many people share a printer, ...)
live in `planning.room_rules`; the products come from `catalog.products`; what
we remember about the customer (colour, carpet, budget, ...) comes from Agent
Memory; every plan is saved to `planning.room_plans`.

Inside, everything is in inches: x runs along the room's long wall, y across.
Desks sit against the top wall facing it (chair behind, then a walkway); the
door is on the bottom wall at the right.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from retail import commerce, governance, memory, moving, offers, photo_cam
from retail.cb import fq, kv_get, kv_upsert, query
from retail.trace import Trace

ROOM_TYPES = ["home office", "bedroom office", "private office", "executive suite",
              "open office", "huddle room", "conference room", "boardroom"]
MEETING_ROOMS = ("huddle room", "conference room", "boardroom")
ROOM_LABELS = {"home office": "Home office", "bedroom office": "Bedroom turned office",
               "private office": "Manager's office (HQ)",
               "executive suite": "Executive suite (HQ)",
               "open office": "Cubicle area (HQ)",
               "huddle room": "Huddle room (HQ)",
               "conference room": "Conference room (HQ)",
               "boardroom": "Boardroom (HQ)"}
ROOM_HELP = {
    "home office": "A desk setup in a dedicated room at home.",
    "bedroom office": "A work corner in a bedroom or spare room.",
    "private office": "An enclosed office for one manager, with visitor chairs and a small "
                      "meeting table.",
    "executive suite": "150–200 sq ft: an L- or U-shaped desk facing the door, two guest "
                       "chairs and a side table.",
    "open office": "6′ × 8′ cubicles, one per person, off walkways — tell us how many; we size the floor.",
    "huddle room": "100–150 sq ft for 2–5: a table pushed against a wall display, set up "
                   "for quick video calls.",
    "conference room": "250–350 sq ft for 8–14: a 3–4′ × 8–12′ table, a wall display and "
                       "36″ behind every seat.",
    "boardroom": "400–600+ sq ft for 12–24: a long table with 48″ to walk around it, a "
                 "display, and a credenza at the back.",
}
DEFAULT_PEOPLE = {"home office": 1, "bedroom office": 1, "private office": 1,
                  "executive suite": 1, "open office": 8, "huddle room": 4,
                  "conference room": 10, "boardroom": 16}
TABLE_SHAPES = ["Any", "Round", "Square", "Rectangular", "Boat-shaped"]
DISPLAY_WALLS = ["Narrow wall", "Wide wall"]
TABLE_STYLES = ["Standard", "High-top"]
DESK_STYLES = ["L-shaped", "U-shaped"]
TIERS = ["Value", "Recommended", "Premium"]
TIER_WORDS = {"Value": "lowest price", "Recommended": "best rated",
              "Premium": "top of the range"}
IN_PER_M = 39.3701
CATEGORY_ORDER = ["Furniture", "Technology", "Ink & Toner", "Office Supplies",
                  "Breakroom & Facilities"]

DEFAULT_RULES = {
    "chair_clearance_in": 36, "walkway_in": 36, "min_walkway_in": 30, "wall_margin_in": 6,
    "table_to_wall_in": 48, "guest_chair_space_in": 30, "door_in": 36,
    "people_per_printer": 10, "people_per_bin": 4, "reams_per_person_month": 1.5,
    "paper_months": 2, "tv_viewing_ratio": 2.6, "whiteboard_wall_fraction": 0.6,
}
KINDS = ["desk", "chair", "conference_table", "storage", "printer", "cartridge", "paper",
         "tv", "hdmi_cable", "power_strip", "whiteboard", "dry_erase_marker",
         "whiteboard_eraser", "whiteboard_cleaner", "permanent_marker", "pens", "sticky_notes", "stapler",
         "organizer", "wastebasket", "lamp", "rug", "chair_mat", "monitor", "camera",
         "video_bar", "speaker", "side_table", "monitor_arm", "monitor_riser"]


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #
@dataclass
class RoomSpec:
    room_type: str
    length_in: float
    width_in: float
    people: int = 1
    tier: str = "Recommended"
    color: str | None = None          # furniture colour
    chair_color: str | None = None
    carpet: bool = False
    monitors: int = 0                 # per desk
    tv: bool | None = None            # None = the room type's default
    whiteboard: bool | None = None
    decor: bool | None = None         # rug (bare floor) and floor lamp at home
    standing: bool = False
    ergonomic: bool = False
    min_desk_w: int = 0
    max_desk_w: int = 0               # "no wider than 40 inches": a small room
    printer: str | None = None        # "inkjet" | "color" | None = by headcount
    budget: float | None = None
    table_shape: str = "Any"          # conference / meeting table
    meeting_table: bool | None = None  # a small table in a manager's office
    video_calls: bool | None = None   # camera, speakerphone / soundbar / video bar
    display_wall: str = "Narrow wall"
    table_style: str = "Standard"     # huddle room: Standard | High-top
    desk_style: str = "U-shaped"      # executive suite: L-shaped | U-shaped
    # Replicate a photo: {"items": what the photo shows and where, "counts": how
    # many of each kind to buy, "extras": {sku: qty} added from the suggestions}
    replicate: dict[str, Any] | None = None
    # fitting a budget: a tier for one kind ({"chair": "Value"}) over `tier`
    tiers: dict[str, str] = field(default_factory=dict)
    max_item: float | None = None     # a per-item limit we remember: no line costs more each

    def defaults(self) -> "RoomSpec":
        home = self.room_type in ("home office", "bedroom office")
        meeting = self.room_type in MEETING_ROOMS
        if self.tv is None:
            self.tv = self.room_type == "open office" or meeting
        if self.whiteboard is None:
            self.whiteboard = self.room_type not in ("bedroom office", "executive suite")
        if self.decor is None or not home:   # a rug and floor lamp are for home rooms
            self.decor = home and self.decor is not False
        if self.meeting_table is None:
            self.meeting_table = self.room_type == "private office"
        if self.video_calls is None:
            self.video_calls = meeting
        self.people = max(2 if meeting else 1, int(self.people))
        if self.replicate is not None:
            self.people = max(1, int((self.replicate.get("counts") or {}).get("chair", 0)))
        return self


def to_inches(value: float, unit: str, extra_in: float = 0.0) -> float:
    """ft (+ inches) or metres -> inches."""
    return value * IN_PER_M if unit == "m" else value * 12 + extra_in


def fmt_len(inches: float, units: str = "ft") -> str:
    if units == "m":
        return f"{inches / IN_PER_M:.2f} m"
    total = int(round(inches))
    ft, inch = divmod(total, 12)
    return f"{ft}′ {inch}″" if inch else f"{ft}′"


# --------------------------------------------------------------------------- #
# what we remember about the customer
# --------------------------------------------------------------------------- #
_NUM = {"one": 1, "two": 2, "three": 3, "1": 1, "2": 2, "3": 3}


def hints_from_texts(texts: list[str], palette: list[str]) -> dict[str, Any]:
    """Planner settings these sentences imply, each with the sentence it came from;
    the first sentence that sets a setting wins."""
    h: dict[str, Any] = {"used": [], "from": {}}
    colours = "|".join(re.escape(c) for c in sorted(palette, key=len, reverse=True))

    def use(key: str, value: Any, text: str) -> None:
        if key not in h:
            h[key] = value
            h["from"][key] = text
            if text not in h["used"]:
                h["used"].append(text)

    for text in texts:
        if moving.CURRENT.search(text):
            # what's in their current room: for the Moving panel, not a setting —
            # "1 monitor" there isn't how many monitors they want
            if "bring" not in h and (got := moving.from_memories([text])):
                h["bring"], h["from"]["bring"] = got
            continue
        low = text.lower()
        m = re.search(rf"\b({colours})\s+(?:\w+\s+)?chairs?\b", text, re.I)
        chair = m.group(1).title() if m else None
        if chair:
            use("chair_color", chair, text)
        others = [c.title() for c in re.findall(rf"\b({colours})\b", text, re.I)
                  if c.title() != chair]
        if others and any(w in low for w in ("furniture", "finish", "look", "colour",
                                              "color", "prefers", "likes")):
            use("color", others[0], text)
        if "carpet" in low:
            use("carpet", True, text)
        if "standing desk" in low:
            use("standing", True, text)
        if "ergonomic" in low or re.search(r"\bback (?:pain|hurts|problems?)\b|"
                                            r"\blumbar\b|\bsciatica\b", low):
            use("ergonomic", True, text)
        m = re.search(r"\b(one|two|three|[123])\s+monitors?\b", low)
        if m:
            use("monitors", _NUM[m.group(1)], text)
        m = re.search(r"at least (\d+)\s*(?:inches|inch|in\b|\")", low)
        if m and "desk" in low:
            use("min_desk_w", int(m.group(1)), text)
        m = re.search(r"(?:no|not) (?:wider|bigger|larger) than (\d+)\s*(?:inches|inch|in\b|\")"
                      r"|at most (\d+)\s*(?:inches|inch|in\b|\") wide", low)
        if m and "desk" in low:
            use("max_desk_w", int(m.group(1) or m.group(2)), text)
        m = re.search(r"\b(industrial|modern|classic)\b", low)
        if m and any(w in low for w in ("look", "style", "feel", "aesthetic", "likes",
                                        "prefers")):
            use("style", m.group(1), text)
        m = re.search(r"\$\s?([\d,]+)[^.]*?\b(?:one|single|any one|each|per|a single)\s+"
                      r"(?:item|thing|piece|product)\b|\b(?:per|each|single|any one) (?:item|"
                      r"piece|product)[^.$]*\$\s?([\d,]+)", low)
        if m:
            use("max_item", float((m.group(1) or m.group(2)).replace(",", "")), text)
        if re.search(r"\bvideo\b", low):        # "all our conference rooms need video calls"
            use("video_calls", not re.search(r"\b(?:no|without|don'?t|do not|never)\b[^.]{0,25}"
                                             r"\bvideo\b", low), text)
        if "inkjet" in low:
            use("printer", "inkjet", text)
        elif ("color" in low or "colour" in low) and "print" in low:
            use("printer", "color", text)
        m = re.search(r"\$\s?([\d,]+)", text)
        if m and any(w in low for w in ("budget", "under", "keep the total")):
            use("budget", float(m.group(1).replace(",", "")), text)
    return h


HOME_ROOMS = ("home office", "bedroom office")
# "Budget for the new HQ floor", "ergonomic chairs for all staff": about the workplace
AT_WORK = re.compile(r"\b(hq|headquarters|staff|team|employees|corporate|company|workplace|"
                     r"conference|meeting|boardroom|cubicles?)\b", re.I)
AT_HOME = re.compile(r"\b(home|bedroom|house|apartment)\b", re.I)


def for_room(h: dict[str, Any], room_type: str) -> dict[str, Any]:
    """The hints that apply to this room. At home, everything we remember; in an
    office at work, the colours the customer likes and what they told us about
    their workplace — not their home budget, their back or their spare room's carpet."""
    if not h or room_type in HOME_ROOMS:
        return h
    src = h.get("from", {})
    out: dict[str, Any] = {
        k: v for k, v in h.items() if k not in ("used", "from") and (
            k in ("color", "chair_color", "bring") or (AT_WORK.search(src.get(k, ""))
                                                       and not AT_HOME.search(src.get(k, ""))))}
    keep = {src.get(k) for k in out}           # "bring" is never in "used": it sets nothing
    out["used"] = [t for t in h.get("used", []) if t in keep]
    out["from"] = {k: src[k] for k in out if k in src}
    return out


def room_hints(customer_id: str, palette: list[str], trace: Trace | None = None
               ) -> dict[str, Any]:
    """Planner settings implied by the customer's memories, each with the memory
    it came from, so the page can say "using what we remember"."""
    t0 = time.perf_counter()
    rows = sorted(memory.list_for(customer_id, 50),       # newest first: it wins
                  key=lambda r: str(r.get("created_at") or ""), reverse=True)
    texts = [str(r.get("text") or "") for r in rows if r.get("text")]
    h = hints_from_texts(texts, palette)
    if trace:
        trace.add("Agent Memory", "Read what we remember about you",
                  f"{len(h['used'])} of {len(texts)} memories shape this plan"
                  if texts else "nothing remembered yet",
                  "Agent Memory server: list the customer's memories, then match "
                  "colour, floor, budget and equipment words",
                  (time.perf_counter() - t0) * 1000, ["agentmemory.memory"])
    return h


def persona_room(customer: dict[str, Any]) -> dict[str, Any] | None:
    """'Turning a 12x10 ft spare bedroom into a home office' -> a starting room."""
    text = customer.get("persona") or ""
    m = re.search(r"(\d+)\s*x\s*(\d+)\s*ft", text)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    p = re.search(r"(\d+)\s+people", text)
    people = int(p.group(1)) if p else 1
    low = text.lower()
    room = ("bedroom office" if "bedroom" in low else
            "open office" if people > 2 else
            "home office" if customer.get("segment") == "home" else "private office")
    return {"room_type": room, "length_ft": max(a, b), "width_ft": min(a, b),
            "people": people, "label": text}


# --------------------------------------------------------------------------- #
# the room: rectangles placed in inches
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    kind: str
    label: str
    x: float
    y: float
    w: float
    d: float
    shape: str = "rect"     # rect | circle | wall | zone | under | top (on a table)
    color: str = "#bbbbbb"
    product: dict[str, Any] | None = None
    face: float = math.pi / 2   # where the item's front points: +y (down the plan)
    z: float = 0.0              # height above the floor (wall-mounted, on a table)


DOWN, UP, RIGHT, LEFT = math.pi / 2, -math.pi / 2, 0.0, math.pi


def _toward(x: float, y: float, tx: float, ty: float) -> float:
    return math.atan2(ty - y, tx - x)


def _overlap(a: Item, b: Item) -> bool:
    return (a.x < b.x + b.w - 0.01 and b.x < a.x + a.w - 0.01 and
            a.y < b.y + b.d - 0.01 and b.y < a.y + a.d - 0.01)


class Room:
    def __init__(self, length: float, width: float, rules: dict[str, Any]):
        self.L, self.W, self.r = length, width, rules
        self.m = rules["wall_margin_in"]
        self.items: list[Item] = []
        door = rules.get("door_in", 36)
        self.door = (self.L - self.m - door - 6, door)          # x, width on the bottom wall
        self.zone("door", "door swing", self.door[0], self.W - door, door, door)

    @property
    def solids(self) -> list[Item]:
        return [i for i in self.items if i.shape in ("rect", "circle")]

    @property
    def zones(self) -> list[Item]:
        return [i for i in self.items if i.shape == "zone"]

    def zone(self, kind: str, label: str, x: float, y: float, w: float, d: float) -> Item:
        z = Item(kind, label, x, y, w, d, "zone")
        self.items.append(z)
        return z

    def put(self, item: Item) -> Item:
        self.items.append(item)
        return item

    def inside(self, i: Item) -> bool:
        return i.x >= -0.01 and i.y >= -0.01 and i.x + i.w <= self.L + 0.01 and \
            i.y + i.d <= self.W + 0.01

    def free(self, i: Item, *, zones: bool = True) -> bool:
        if not self.inside(i):
            return False
        others = self.solids + (self.zones if zones else [])
        return not any(_overlap(i, o) for o in others)

    def along_walls(self, kind: str, label: str, w: float, d: float, color: str,
                    access: float = 24, walls: tuple[str, ...] = ("left", "right",
                                                                "bottom", "top"),
                    near: float | None = None) -> Item | None:
        """The first spot against a wall where the item and the space in front of
        it (to open a drawer, reach a shelf) are both free — the one closest to
        `near` (0-1 along the wall) when given."""
        m, L, W = self.m, self.L, self.W
        for wall in walls:
            run = (L if wall in ("top", "bottom") else W) - 2 * m - w
            spots = [m + 3 * k for k in range(int(max(run, -1) // 3) + 1)]
            if near is not None:
                want = m + run * min(max(near, 0.0), 1.0)
                spots.sort(key=lambda q: abs(q - want))
            for pos in spots:
                if wall == "top":
                    it, ac = Item(kind, label, pos, m, w, d), Item("", "", pos, m + d, w, access)
                elif wall == "bottom":
                    it = Item(kind, label, pos, W - m - d, w, d)
                    ac = Item("", "", pos, W - m - d - access, w, access)
                elif wall == "left":
                    it, ac = Item(kind, label, m, pos, d, w), Item("", "", m + d, pos, access, w)
                else:
                    it = Item(kind, label, L - m - d, pos, d, w)
                    ac = Item("", "", L - m - d - access, pos, access, w)
                if self.free(it) and self.free(ac, zones=False):
                    it.color = color
                    it.face = {"top": DOWN, "bottom": UP, "left": RIGHT, "right": LEFT}[wall]
                    self.items.append(it)
                    if access:
                        self.zone("access", "", ac.x, ac.y, ac.w, ac.d)
                    return it
        return None

    def corner(self, kind: str, label: str, w: float, d: float, color: str) -> Item | None:
        """A small thing (a lamp, a bin, a side table) tucked into a free corner:
        clear of furniture and the door's swing, though inside a walkway zone —
        nobody walks into a corner."""
        m, L, W = self.m, self.L, self.W
        doors = [z for z in self.zones if z.kind == "door"]
        for x, y in ((m, m), (L - m - w, m), (m, W - m - d), (L - m - w, W - m - d)):
            it = Item(kind, label, x, y, w, d)
            if self.free(it, zones=False) and not any(_overlap(it, z) for z in doors):
                it.color = color
                self.items.append(it)
                return it
        return None

    def open_floor(self, w: float, d: float, ring: float) -> tuple[float, float] | None:
        """Top-left corner for a w x d item that has `ring` inches free all round
        (for chairs), anywhere on the floor — scanned from the far side."""
        x = self.L - ring - w
        while x >= ring - 0.01:
            y = ring
            while y + d + ring <= self.W + 0.01:
                if self.free(Item("", "", x - ring, y - ring, w + 2 * ring, d + 2 * ring)):
                    return x, y
                y += 6
            x -= 6
        return None


# --------------------------------------------------------------------------- #
# products
# --------------------------------------------------------------------------- #
@dataclass
class Base:
    base_id: str
    lead: dict[str, Any]
    variants: dict[str, dict[str, Any]]

    @property
    def dims(self) -> dict[str, float]:
        return self.lead.get("dims") or {"w": 0, "d": 0, "h": 0}

    def variant(self, *colours: str | None) -> dict[str, Any]:
        if self.lead["category"] != "Furniture":        # small things: neutral first
            colours = colours + ("Black", "Gray", "White", "Ivory")
        for c in colours:
            if c and c in self.variants:
                return self.variants[c]
        lead = (self.lead.get("colors_available") or [{}])[0].get("color")
        return self.variants.get(lead) or self.lead


def _candidates(trace: Trace | None) -> dict[str, list[Base]]:
    t0 = time.perf_counter()
    rows = query(f"SELECT RAW OBJECT_REMOVE(p, 'embedding') FROM "
                 f"{fq('catalog', 'products')} AS p WHERE p.type = 'product' "
                 f"AND p.kind IN $kinds", kinds=KINDS)
    bases: dict[str, Base] = {}
    for r in rows:
        b = bases.setdefault(r["base_id"], Base(r["base_id"], r, {}))
        b.variants[r["color"]] = r
    out: dict[str, list[Base]] = {}
    for b in bases.values():
        out.setdefault(b.lead["kind"], []).append(b)
    for v in out.values():
        v.sort(key=lambda b: b.lead["price"])
    if trace:
        trace.add("Query (SQL++)", "Gathered the products a room can need",
                  f"{len(bases)} products ({len(rows)} colour options) across "
                  f"{len(out)} kinds",
                  "SQL++ SELECT on catalog.products WHERE kind IN [...] (idx_prod_kind)",
                  (time.perf_counter() - t0) * 1000, ["catalog.products"],
                  indexes=["idx_prod_kind"])
    return out


def _visitor(b: Base) -> bool:
    """Seats for visitors: a guest chair, else a meeting-room chair."""
    return b.lead.get("use") in ("guest", "meeting")


def _tier(options: list[Base], tier: str, colour: str | None = None) -> Base | None:
    """Value = lowest price, Premium = top price, Recommended = best rated —
    among the products that come in the colour asked for, when any do."""
    if not options:
        return None
    if colour:
        options = [b for b in options if colour in b.variants] or options
    if tier == "Value":
        return min(options, key=lambda b: b.lead["price"])
    if tier == "Premium":
        return max(options, key=lambda b: b.lead["price"])
    return max(options, key=lambda b: (b.lead["rating"], -b.lead["price"]))


_stock_cache: dict[str, Any] = {"t": 0.0, "v": None}


def stocked() -> dict[str, Any]:
    """Which table shapes and desk/table styles the catalogue has right now, so
    the page only offers options that can be filled (cached for a minute)."""
    if _stock_cache["v"] is None or time.time() - _stock_cache["t"] > 60:
        rows = query(f"SELECT p.kind, p.shape, p.high_top, p.u_shaped FROM "
                     f"{fq('catalog', 'products')} AS p WHERE p.type = 'product' "
                     f"AND p.kind IN ['conference_table', 'desk']")
        shapes = {r.get("shape") for r in rows if r["kind"] == "conference_table"
                  and not r.get("high_top")}
        names = {"round": "Round", "square": "Square", "rectangular": "Rectangular",
                 "boat": "Boat-shaped"}
        _stock_cache["v"] = {
            "shapes": ["Any"] + [names[k] for k in ("round", "square", "rectangular", "boat")
                                 if k in shapes],
            "table_styles": ["Standard"] + (["High-top"] if any(r.get("high_top")
                                                                 for r in rows) else []),
            "desk_styles": ["L-shaped"] + (["U-shaped"] if any(r.get("u_shaped")
                                                               for r in rows) else [])}
        _stock_cache["t"] = time.time()
    return _stock_cache["v"]


def _rules(trace: Trace | None) -> dict[str, Any]:
    t0 = time.perf_counter()
    doc = kv_get("planning", "room_rules", "RULE::DEFAULT") or {}
    rules = {**DEFAULT_RULES, **doc}
    if trace:
        trace.add("Data (KV)", "Read the store's room-planning rules",
                  f"{rules['chair_clearance_in']}″ behind each chair, "
                  f"{rules['min_walkway_in']}–{rules['walkway_in']}″ walkways",
                  "KV get, key RULE::DEFAULT", (time.perf_counter() - t0) * 1000,
                  ["planning.room_rules"])
    return rules


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #
@dataclass
class Line:
    product: dict[str, Any]
    qty: int
    why: str

    @property
    def group(self) -> str:
        return self.product["category"]


@dataclass
class Plan:
    spec: RoomSpec
    length: float
    width: float
    items: list[Item]
    lines: list[Line]
    seats: int
    capacity: int
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pricing: dict[str, Any] = field(default_factory=dict)
    plan_id: str = ""
    table: dict[str, Any] | None = None      # the conference / meeting table chosen
    visitors: int = 0
    guide: dict[str, Any] = field(default_factory=dict)   # this room type's guidelines
    desk: dict[str, Any] | None = None
    suggestions: list[Line] = field(default_factory=list)   # replicate: not added
    unplaced: dict[str, int] = field(default_factory=dict)  # on the list, no room to draw
    tight: list[str] = field(default_factory=list)          # drawn, but squeezed
    roomier: tuple[float, float] | None = None              # a size that takes it all
    cam: dict[str, float] | None = None                     # replicate: the photo's camera
    crowded: list[str] = field(default_factory=list)        # why the room feels congested
    to_budget: bool = False      # lower-priced picks, to keep to the budget we remember
    budget_fit: dict[str, Any] | None = None   # what _fit_budget changed, and what's left over

    def fit_note(self, units: str = "ft") -> str | None:
        """Plain words when what's asked for won't fit, or only just fits."""
        if self.unplaced:
            parts = []
            for k, n in self.unplaced.items():
                one, many = REPLICA_NAMES.get(k, (k.replace("_", " "), k.replace("_", " ")))
                want = int(((self.spec.replicate or {}).get("counts") or {}).get(k, 0))
                parts.append(f"{n} of the {want} {many}" if want > n else
                             f"{n} {one if n == 1 else many}")
            size = f"{fmt_len(self.length, units)} × {fmt_len(self.width, units)}"
            text = (f"{' and '.join(parts)} won't fit in this {size} room — "
                    f"{'it is' if sum(self.unplaced.values()) == 1 else 'they are'} on the "
                    f"list but not drawn.")
            if self.roomier:
                text += (f" A room about {fmt_len(self.roomier[0], units)} × "
                         f"{fmt_len(self.roomier[1], units)} would take everything.")
            else:
                text += " Try fewer items or a larger room."
            return text
        if self.tight:
            return "It fits, but only just: " + "; ".join(self.tight) + "."
        return None

    def guideline(self) -> tuple[str, bool] | None:
        """How this room compares with the usual size and headcount for its type."""
        g, s = self.guide, self.spec
        if s.replicate is not None:
            return None
        sqft = self.length * self.width / 144
        label = ROOM_LABELS.get(s.room_type, s.room_type).split(" (")[0]
        if g.get("sqft_per_person"):
            lo, hi = g["sqft_per_person"]
            per = sqft / max(1, s.people)
            ok = lo <= per <= hi * 1.5
            return (f"Typical open-plan space: {lo}–{hi} sq ft per person — yours is "
                    f"{per:.0f} sq ft each ({sqft:.0f} sq ft for {s.people})"
                    + (" ✓" if ok else ""), ok)
        if not g.get("sqft"):
            return None
        lo, hi = g["sqft"]
        clo, chi = g.get("capacity", [1, 99])
        ok_size = lo * 0.9 <= sqft <= hi * 1.25
        ok_people = clo <= s.people <= chi
        text = (f"Typical {label.lower()}: {lo}–{hi} sq ft"
                + (f" for {clo}–{chi} people" if s.room_type in MEETING_ROOMS else "")
                + f" — yours is {sqft:.0f} sq ft"
                + (f" for {s.people}" if s.room_type in MEETING_ROOMS else ""))
        if ok_size and ok_people:
            return text + " ✓", True
        why = []
        if not ok_size:
            why.append("smaller than usual" if sqft < lo * 0.9 else "larger than usual")
        if not ok_people:
            why.append(f"{'fewer' if s.people < clo else 'more'} people than usual")
        text += ": " + " and ".join(why) + "."
        if s.room_type in MEETING_ROOMS and not ok_people:
            better = {"huddle room": (2, 5), "conference room": (6, 14),
                      "boardroom": (12, 24)}
            alt = next((t for t, (a, b) in better.items() if a <= s.people <= b
                        and t != s.room_type), None)
            if alt:
                text += f" For {s.people} people, a {ROOM_LABELS[alt].split(' (')[0].lower()} fits."
        return text, False

    @property
    def fits(self) -> bool:
        return self.seats >= self.spec.people

    def headline(self, units: str = "ft") -> str:
        s = self.spec
        size = f"{fmt_len(self.length, units)} × {fmt_len(self.width, units)}"
        if s.replicate is not None:
            return f"Your {size} room, as in your photo: {replica_words(s.replicate)}."
        name = ROOM_LABELS.get(s.room_type, s.room_type).split(" (")[0].lower()
        if s.room_type in ("conference room", "boardroom"):
            t = self.table
            at = min(self.seats, (t or {}).get("seats", self.seats))
            what = (f"seats {at} around {_table_words(t)}" if t else f"seats {self.seats}")
            if self.seats > at:
                what += f", plus {self.seats - at} along the wall"
            return f"Your {size} {name} {what}."
        if s.room_type == "huddle room":
            t = self.table or {}
            kind = "high-top table" if t.get("high_top") else "table"
            return (f"Your {size} huddle room seats {self.seats} at a "
                    f"{fmt_len((t.get('dims') or {}).get('w', 72))} {kind} pushed against "
                    f"the display.")
        if s.room_type == "executive suite":
            desk = "U-shaped" if (self.desk or {}).get("u_shaped") else "L-shaped"
            return (f"Your {size} executive suite fits {'a' if desk[0] == 'U' else 'an'} {desk} "
                    f"desk facing the door, "
                    f"{self.visitors} guest chairs and a side table.")
        if s.room_type == "private office":
            extra = [f"{self.visitors} visitor chairs"] if self.visitors else []
            if self.table:
                extra.append(f"a {self.table.get('seats', 4)}-seat meeting table")
            return (f"Your {size} manager's office fits the desk and executive chair"
                    + (", " + " and ".join(extra) if extra else "") + ".")
        if s.room_type == "open office":
            room_for = f" (room for up to {self.capacity})" if self.capacity > self.seats else ""
            return (f"{self.seats} cubicle{'s' if self.seats != 1 else ''} of 6′ × 8′ on a "
                    f"{size} floor{room_for}.")
        room_for = f" There's room for up to {self.capacity}." if self.capacity > s.people else ""
        return (f"Your {size} {name} works for {s.people} "
                f"{'person' if s.people == 1 else 'people'}.{room_for}")

    def summary(self) -> str:
        n = sum(line.qty for line in self.lines)
        return (f"{self.seats} seat{'s' if self.seats != 1 else ''} · {n} items · "
                f"${self.pricing.get('total', 0):,.2f}")


REPLICA_KINDS = ["chair", "conference_table", "desk", "tv", "whiteboard", "storage", "lamp",
                 "monitor", "printer", "rug", "wastebasket", "video_bar", "side_table"]
REPLICA_NAMES = {"chair": ("chair", "chairs"), "conference_table": ("table", "tables"),
                 "desk": ("desk", "desks"), "tv": ("TV", "TVs"),
                 "whiteboard": ("whiteboard", "whiteboards"),
                 "storage": ("cabinet or shelf", "cabinets and shelves"),
                 "lamp": ("lamp", "lamps"), "monitor": ("monitor", "monitors"),
                 "printer": ("printer", "printers"), "rug": ("rug", "rugs"),
                 "wastebasket": ("bin", "bins"), "video_bar": ("video bar", "video bars"),
                 "side_table": ("side table", "side tables")}


def replica_words(rep: dict[str, Any]) -> str:
    """{"counts": {"chair": 8, "tv": 1}} -> "8 chairs, 1 TV"."""
    out = []
    for k in REPLICA_KINDS:
        n = int((rep.get("counts") or {}).get(k, 0))
        if n > 0:
            one, many = REPLICA_NAMES[k]
            out.append(f"{n} {one if n == 1 else many}")
    return ", ".join(out) or "nothing yet"


def _table_words(t: dict[str, Any]) -> str:
    d = t.get("dims") or {}
    shape = t.get("shape", "")
    if shape == "round":
        return f"a {d.get('w')}″ round table"
    if shape == "square":
        return f"a {d.get('w')}″ square table"
    if shape == "boat":
        return f"a {fmt_len(d.get('w', 0))} boat-shaped table"
    return f"a {fmt_len(d.get('w', 0))} rectangular table"


def _shape_ok(b: "Base", shape: str) -> bool:
    return shape in ("Any", "", None) or b.lead.get("shape", "") == shape.split("-")[0].lower()


class _Builder:
    def __init__(self, spec: RoomSpec, rules: dict[str, Any], cands: dict[str, list[Base]]):
        self.s, self.r, self.c = spec, rules, cands
        across, deep = (max(spec.length_in, spec.width_in), min(spec.length_in, spec.width_in))
        if any(i.get("axis") == "deep" for i in (spec.replicate or {}).get("items") or []):
            across, deep = deep, across  # the photo's table runs away from it: so does the room
        self.room = Room(across, deep, rules)
        if spec.room_type == "open office" and not spec.replicate:
            # cubicle rows run across: keep whichever way round fits more of them
            fits = self._cube_grid()[2]
            self.room = Room(deep, across, rules)
            if self._cube_grid()[2] <= fits:
                self.room = Room(across, deep, rules)
        self.lines: list[Line] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.seat_xy: list[tuple[float, float]] = []
        self.capacity = 0
        self.desks = 0
        self.table: dict[str, Any] | None = None
        self.visitors = 0
        self.table_item: Item | None = None
        self.tv_item: Item | None = None
        self.desk_item: Item | None = None
        self.desk: dict[str, Any] | None = None
        self.unplaced: dict[str, int] = {}     # on the list, but no room to draw them
        self.at_table = 0                       # chairs wanted round the table
        self.reserve_missed = False             # no room behind the table for its credenza
        self.tight: list[str] = []             # drawn, but squeezed
        self.cam: photo_cam.PhotoCam | None = None      # replicate: the photo's camera
        self.capped: dict[int, bool] = {}      # picks a per-item limit changed: under it?

    def miss(self, kind: str, n: int = 1) -> None:
        if n > 0:
            self.unplaced[kind] = self.unplaced.get(kind, 0) + n

    # ---- picking -------------------------------------------------------------
    def options(self, kind: str, *, tagged: bool = True, where=None) -> list[Base]:
        opts = self.c.get(kind, [])
        if tagged:
            rooms = {self.s.room_type} | ({"home office"} if self.s.room_type ==
                                          "bedroom office" else set())
            opts = [b for b in opts if rooms & set(b.lead.get("room_tags", []))] or opts
        if where:
            opts = [b for b in opts if where(b)]
        return opts

    def tier_of(self, base: Base | None) -> str:
        return self.s.tiers.get(base.lead["kind"], self.s.tier) if base else self.s.tier

    def choose(self, options: list[Base], colour: str | None = None) -> Base | None:
        """The product for one line: its kind's tier, and under a per-item limit
        when we remember one and anything fits it."""
        if not options:
            return None
        tier, cap = self.tier_of(options[0]), self.s.max_item
        usual = _tier(options, tier, colour)
        if cap and usual and usual.variant(colour)["price"] > cap:
            under = [b for b in options if b.variant(colour)["price"] <= cap]
            # none under it: the lowest-priced of what fits, the nearest to it
            choice = _tier(under, tier, colour) if under else _tier(options, "Value", colour)
            if choice is not usual:
                self.capped[id(choice)] = bool(under)
            return choice
        return usual

    def same_size(self, chosen: Base, pool: list[Base], colour: str | None = None) -> Base:
        """A line picked by size — the table that seats 12, the 75″ display — in
        whichever product of exactly that size the tier picks (a Basics or
        Signature twin differs only in price and rating)."""
        def fit(b: Base) -> tuple:
            return (b.dims, b.lead.get("seats"), b.lead.get("screen_in"),
                    bool(b.lead.get("high_top")), b.lead.get("shape"))
        twins = [b for b in pool if fit(b) == fit(chosen)]
        return self.choose(twins, colour) if len(twins) > 1 else chosen

    def add(self, base: Base | None, qty: int, why: str, *, chair: bool = False
            ) -> dict[str, Any] | None:
        if not base or qty <= 0:
            return None
        if id(base) in self.capped:
            why += (f" · under your ${self.s.max_item:,.0f} per-item limit" if self.capped[id(base)]
                    else f" · the lowest-priced that fits — none is under your "
                         f"${self.s.max_item:,.0f} per-item limit")
        v = base.variant(self.s.chair_color if chair else None, self.s.color)
        for line in self.lines:
            if line.product["sku"] == v["sku"]:
                line.qty += qty
                line.why += f"; {why}"
                return v
        self.lines.append(Line(v, int(qty), why))
        return v

    # ---- desks ---------------------------------------------------------------
    def _grid(self, base: Base, bench: bool, facing: bool) -> tuple[int, int, int, float]:
        r, m = self.r, self.room.m
        w, d = base.dims["w"], base.dims["d"]
        cl, walk = r["chair_clearance_in"], r["min_walkway_in"]
        if bench:
            pitch = cl + d + cl + walk
        elif facing:
            pitch = cl + d + walk
        else:
            pitch = d + cl + walk
        rows = int((self.room.W - m) // pitch)
        aisle = r["walkway_in"] if (rows > 1 or bench) else 0
        per_row = int((self.room.L - 2 * m - aisle) // w) if rows else 0
        return rows, per_row, rows * per_row * (4 if bench else 1), pitch

    def workstations(self) -> None:
        s = self.s
        if s.room_type == "open office":
            self.cubicles()
            return
        private = s.room_type == "private office"
        desks = self.options("desk", where=lambda b: (b.lead.get("seats") or 1) == 1)
        bench = [b for b in self.options("desk") if (b.lead.get("seats") or 1) > 1]
        prefs = []
        if s.standing:
            prefs.append(("electric standing, as you prefer",
                          lambda b: "Standing" in b.lead["base_name"]))
        if s.min_desk_w:
            prefs.append((f"at least {s.min_desk_w}″ wide, as you need",
                          lambda b: b.dims["w"] >= s.min_desk_w))
        if s.max_desk_w:
            prefs.append((f"no wider than {s.max_desk_w}″, as you need",
                          lambda b: b.dims["w"] <= s.max_desk_w))
        if s.people > 1:            # an L-shaped corner desk is a one-person desk
            desks = [b for b in desks if b.dims["w"] != b.dims["d"]] or desks

        choice, is_bench, reason = None, False, []
        if bench and s.room_type == "open office" and s.people >= 4 and not s.standing:
            b = bench[0]
            if self._grid(b, True, False)[2] >= s.people:
                choice, is_bench = b, True
        if not choice:
            pool = desks
            for why, ok in prefs:
                narrowed = [b for b in pool if ok(b)]
                if narrowed:
                    pool, reason = narrowed, reason + [why]
                else:
                    self.notes.append(f"No desk in the catalogue is {why.split(',')[0]}; "
                                      f"showing the closest.")
            fitting = [b for b in pool if self._grid(b, False, private)[2] >= s.people]
            if not fitting and pool is not desks:
                fitting = [b for b in desks if self._grid(b, False, private)[2] >= s.people]
                if fitting:
                    self.notes.append("Your preferred desk doesn't fit this room; "
                                      "chose one that does.")
                    reason = []
            if fitting:
                choice = self.choose(fitting, s.color)
            else:
                choice = max(desks, key=lambda b: (self._grid(b, False, private)[2],
                                                   -b.dims["d"]))
        rows, per_row, cap, pitch = self._grid(choice, is_bench, private)
        self.capacity = cap
        seats_wanted = min(s.people, cap)
        if cap < s.people:
            self.warnings.append(
                f"This room fits {cap} {'person' if cap == 1 else 'people'} comfortably — "
                f"you asked for {s.people}. The plan seats {cap}; "
                + self._bigger_room(choice, is_bench, private))
        if not cap:
            return
        per_desk = 4 if is_bench else 1
        n_desks = math.ceil(seats_wanted / per_desk)
        self.desks = n_desks
        rows_used = math.ceil(n_desks / per_row)
        per = math.ceil(n_desks / rows_used)
        desk_v = self.add(choice, n_desks,
                          (f"{per_desk} people per bench, {n_desks} bench"
                           f"{'es' if n_desks > 1 else ''} for {seats_wanted}" if is_bench
                           else "1 per person") + "".join(f" · {w}" for w in reason)
                          + f" · {TIER_WORDS[self.tier_of(choice)]} that fits")
        chair_v = self.pick_chairs(seats_wanted)
        self.place_desks(choice, desk_v, chair_v, is_bench, private, rows_used, per,
                         n_desks, seats_wanted, pitch)

    # ---- cubicles --------------------------------------------------------------
    def _cube_grid(self) -> tuple[int, int, int]:
        """Rows of 6′ × 8′ cubicles: the first backs onto the far wall, then pairs
        back to back, each row opening onto a walkway; a cross aisle runs down
        the door side. -> (rows, per_row, cubicles)."""
        m, walk = self.room.m, self.r["walkway_in"]
        rows = 0
        while (rows + 1) * CUBE_D + math.ceil((rows + 1) / 2) * walk <= self.room.W - 2 * m:
            rows += 1
        per_row = max(0, int((self.room.L - 2 * m - walk) // CUBE_W)) if rows else 0
        return rows, per_row, rows * per_row

    def cubicles(self) -> None:
        """A team area as cubicles: 6′ × 8′ each, panels round three sides, a
        workstation against the back panel and the chair in the opening."""
        s, room, r = self.s, self.room, self.r
        m, walk = room.m, r["walkway_in"]
        rows, per_row, cap = self._cube_grid()
        self.capacity = cap
        if cap < s.people:
            self.warnings.append(
                f"This room fits {cap} cubicle{'s' if cap != 1 else ''} of 6′ × 8′ — you "
                f"asked for {s.people}. The plan seats {cap}; "
                + self._bigger_room(None, False, False, size=lambda: self._cube_grid()[2]))
        if not cap:
            return
        n = min(s.people, cap)
        inner = CUBE_W - 2 * PANEL_T - 4
        fits = lambda b: (b.lead.get("seats") or 1) == 1 and b.dims["w"] <= inner \
            and b.dims["d"] <= 32 and not b.lead.get("l_shaped")          # noqa: E731
        pool = self.options("desk", tagged=False, where=fits)
        cube = [b for b in pool if "Cubicle" in b.lead["base_name"]]
        why = "1 per cubicle"
        if s.standing:
            stand = [b for b in pool if "Standing" in b.lead["base_name"]]
            if stand:
                cube, why = stand, why + " · electric standing, as you prefer"
        choice = self.choose(cube or pool, s.color)
        if not choice:
            return
        self.desks = n
        desk_v = self.add(choice, n, f"{why} · {TIER_WORDS[self.tier_of(choice)]} that fits")
        chair_v = self.pick_chairs(n)
        w, d = choice.dims["w"], choice.dims["d"]
        desk_c = (desk_v or {}).get("color_hex", "#b08a5a")
        chair_c = (chair_v or {}).get("color_hex", "#333333")
        look = dict(desk_v, panel=False) if desk_v else None   # the cubicle has the panels
        x0 = m + (room.L - 2 * m - walk - per_row * CUBE_W) / 2
        y, left = m, n

        def panel(x: float, y: float, pw: float, pd: float) -> None:
            room.put(Item("panel", "", x, y, pw, pd, color=PANEL_HEX,
                          product={"kind": "panel", "name": "Cubicle panel",
                                   "color_hex": PANEL_HEX,
                                   "dims": {"w": pw, "d": pd, "h": PANEL_H}}))

        for row in range(rows):
            if left <= 0:
                break
            k = min(per_row, left)
            opens_down = row % 2 == 0           # the walkway is below the row
            back_y = y if opens_down else y + CUBE_D - PANEL_T
            panel(x0, back_y, k * CUBE_W, PANEL_T)
            for i in range(k + 1):              # side panels, shared between neighbours
                panel(x0 + i * CUBE_W - (PANEL_T if i == k else 0), y + (PANEL_T if opens_down else 0),
                      PANEL_T, CUBE_D - PANEL_T)
            for i in range(k):
                cx = x0 + i * CUBE_W + CUBE_W / 2
                if opens_down:
                    dy, cy, f = y + PANEL_T, y + PANEL_T + d + 16, UP
                    my = dy + 3
                else:
                    dy = y + CUBE_D - PANEL_T - d
                    cy, f, my = dy - 16, DOWN, dy + d - 9
                room.put(Item("desk", "Desk", cx - w / 2, dy, w, d, color=desk_c,
                              product=look, face=DOWN if opens_down else UP))
                room.put(Item("chair", "", cx - 12, cy - 12, 24, 24, "circle", chair_c,
                              product=chair_v, face=f))
                self.seat_xy.append((cx, cy))
                for j in range(s.monitors):
                    mx = cx - 11 * s.monitors + 22 * j
                    room.put(Item("monitor", "", mx, my, 22, 6, "under", "#222222",
                                  face=f + math.pi))
                if s.carpet:
                    room.put(Item("chair_mat", "", cx - 18, cy - 22, 36, 44, "under",
                                  "#cfe3f0"))
            left -= k
            y += CUBE_D
            if opens_down:                      # the walkway this row (and the next) opens on
                room.zone("walkway", "", m, y, room.L - 2 * m, walk)
                y += walk
        room.zone("walkway", "", room.L - m - walk, m, walk, room.W - 2 * m)
        self.notes.append("Each person gets a 6′ × 8′ cubicle — panels on three sides, "
                          "open to a walkway. The panels are drawn on the plan; they aren't "
                          "on the list.")

    def _bigger_room(self, base: Base | None, bench: bool, facing: bool,
                     size=None) -> str:
        """The smallest room of the same proportions that seats everyone."""
        L, W = self.room.L, self.room.W
        for grow in range(6, 600, 6):
            room = Room(L + grow, W + grow * W / L, self.r)
            saved, self.room = self.room, room
            cap = size() if size else self._grid(base, bench, facing)[2]
            self.room = saved
            if cap >= self.s.people:
                return (f"a room of about {fmt_len(L + grow)} × "
                        f"{fmt_len(W + grow * W / L)} would seat everyone.")
        return "consider splitting the team across rooms."

    def visitor_chairs(self) -> list[Base]:
        """Guest chairs for visitors; meeting-room chairs if none are stocked."""
        for use in ("guest", "meeting"):
            opts = (self.options("chair", where=lambda b: b.lead.get("use") == use)
                    or self.options("chair", tagged=False,
                                    where=lambda b: b.lead.get("use") == use))
            if opts:
                return opts
        return self.options("chair", tagged=False, where=_visitor)

    def pick_chairs(self, n: int) -> dict[str, Any] | None:
        s = self.s
        colour = s.chair_color or s.color

        def use(*uses: str):
            return lambda b: b.lead.get("use", "task") in uses

        def seats(*uses: str, tagged: bool = False) -> list[Base]:
            # the catalogue may not stock every kind of seat; any chair beats none
            return (self.options("chair", tagged=tagged, where=use(*uses))
                    or self.options("chair", tagged=False, where=use(*uses))
                    or self.options("chair", tagged=False))

        ergo_why = " · ergonomic mesh, as you prefer"
        if s.room_type == "boardroom":
            opts = seats("executive", tagged=True)      # boardroom chairs, not Big & Tall
            mesh = [b for b in opts if "Mesh" in b.lead["base_name"]] if s.ergonomic else []
            return self.add(self.choose(mesh or opts, colour), n,
                            "1 per seat · executive seating for the board"
                            + (ergo_why if mesh else ""), chair=True)
        ergo = ([b for b in seats("task") if "Ergonomic" in b.lead["base_name"]]
                if s.ergonomic else [])
        if s.room_type == "huddle room":
            if s.table_style == "High-top":
                opts = seats("stool")
                return self.add(self.choose(opts, colour), n,
                                "1 per seat · counter height for the high-top", chair=True)
            opts = seats("guest", "meeting")
            return self.add(self.choose(ergo or opts, colour), n,
                            "1 per seat" + (ergo_why if ergo else ""), chair=True)
        if s.room_type == "conference room":
            opts = seats("guest", "meeting", tagged=True)
            return self.add(self.choose(ergo or opts, colour), n,
                            "1 per seat at the table" + (ergo_why if ergo else ""),
                            chair=True)
        if s.room_type == "executive suite":
            opts = seats("executive")
            return self.add(self.choose(opts, colour), n, "the executive's chair",
                            chair=True)
        opts = seats("task", "executive", tagged=True)
        why = "1 per desk"
        if s.ergonomic:
            ergo = [b for b in opts if "Ergonomic" in b.lead["base_name"]]
            if ergo:
                opts, why = ergo, why + " · ergonomic mesh, as you prefer"
        if s.room_type == "private office" and not s.ergonomic:
            execs = [b for b in opts if b.lead.get("use") == "executive"]
            opts = execs or opts
        else:
            opts = [b for b in opts if b.lead.get("use") != "executive"] or opts
        return self.add(self.choose(opts, s.chair_color or s.color), n, why, chair=True)

    def place_desks(self, base: Base, desk_v, chair_v, bench: bool, facing: bool,
                    rows: int, per: int, n_desks: int, seats: int, pitch: float) -> None:
        room, r = self.room, self.r
        m, cl = room.m, r["chair_clearance_in"]
        w, d = base.dims["w"], base.dims["d"]
        aisle = r["walkway_in"] if (rows > 1 or bench) else 0
        span = room.L - 2 * m - aisle
        desk_c = (desk_v or {}).get("color_hex", "#b08a5a")
        chair_c = (chair_v or {}).get("color_hex", "#333333")
        label = "Bench" if bench else "Desk"
        manager = facing and self.s.room_type == "private office"
        # A manager's desk moves toward one wall when that leaves floor for a
        # meeting table; otherwise desks are centred on their wall.
        side = manager and self.s.meeting_table and room.L - 2 * m - w >= 100
        placed, left = 0, seats
        for row in range(rows):
            k = min(per, n_desks - placed)
            if k <= 0:
                break
            x0 = m + 12 if side else m + (span - k * w) / 2
            y0 = m + row * pitch
            for i in range(k):
                x = x0 + i * w
                if bench:
                    room.zone("chair", "", x, y0, w, cl)
                    room.put(Item("desk", label, x, y0 + cl, w, d, color=desk_c,
                                  product=desk_v, face=DOWN))
                    room.zone("chair", "", x, y0 + cl + d, w, cl)
                    spots = [(x + w * 0.25, y0 + cl - 16, DOWN), (x + w * 0.75, y0 + cl - 16, DOWN),
                             (x + w * 0.25, y0 + cl + d + 16, UP),
                             (x + w * 0.75, y0 + cl + d + 16, UP)]
                elif facing:
                    room.zone("chair", "", x, y0, w, cl)
                    room.put(Item("desk", label, x, y0 + cl, w, d, color=desk_c,
                                  product=desk_v, face=UP))
                    spots = [(x + w / 2, y0 + cl - 16, DOWN)]
                else:
                    room.put(Item("desk", label, x, y0, w, d, color=desk_c, product=desk_v,
                                  face=DOWN))
                    room.zone("chair", "", x, y0 + d, w, cl)
                    spots = [(x + w / 2, y0 + d + 16, UP)]
                for cx, cy, f in spots[:left]:
                    room.put(Item("chair", "", cx - 12, cy - 12, 24, 24, "circle", chair_c,
                                  product=chair_v, face=f))
                    self.seat_xy.append((cx, cy))
                    left -= 1
                if self.s.monitors:
                    n_mon = self.s.monitors
                    for cx, cy, f in spots[:4 if bench else 1]:
                        if bench:
                            my = y0 + cl + (3 if cy < y0 + cl else d - 9)
                        else:
                            my = y0 + cl + d - 9 if facing else y0 + 3
                        for j in range(n_mon):          # side by side, facing the seat
                            mx = cx - 11 * n_mon + 22 * j
                            room.put(Item("monitor", "", mx, my, 22, 6, "under", "#222222",
                                          face=f + math.pi))
                if self.s.carpet:
                    for cx, cy, _f in spots[:4 if bench else 1]:
                        room.put(Item("chair_mat", "", cx - 18, cy - 22, 36, 44, "under",
                                      "#cfe3f0"))
            walk_x, walk_w = ((x0, k * w) if manager else (m, room.L - 2 * m))
            room.zone("walkway", "", walk_x, y0 + pitch - r["min_walkway_in"], walk_w,
                      r["min_walkway_in"])
            placed += k
        if aisle:
            room.zone("walkway", "", room.L - m - aisle, m, aisle, room.W - 2 * m)
        if manager:
            g = self.visitor_chairs()
            gv = self.add(self.choose(g, self.s.chair_color or self.s.color), 2,
                          "2 visitor chairs", chair=True) if g else None
            colour = (gv or {}).get("color_hex", "#555555")
            gy = m + cl + d + r["guest_chair_space_in"] / 2
            if gy + 12 + r["min_walkway_in"] <= room.W - m:     # facing the desk
                for gx in (x0 + w / 2 - 16, x0 + w / 2 + 16):
                    room.put(Item("chair", "", gx - 12, gy - 12, 24, 24, "circle", colour,
                                  product=gv, face=UP))
            else:                                                # against a side wall
                for _ in range(2):
                    it = room.along_walls("chair", "", 24, 24, colour, access=0,
                                          walls=("right", "left", "bottom"))
                    if it:
                        it.shape, it.product = "circle", gv
                self.notes.append("The visitor chairs sit against the wall — there isn't "
                                  "room for them in front of the desk.")
            self.visitors = 2
            if self.s.meeting_table:
                self.meeting_table()

    def meeting_table(self) -> None:
        """A small round or square table for 4 in a manager's office, if the floor
        has room for it and its chairs."""
        s, room = self.s, self.room
        ring = 30
        tables = [b for b in self.options("conference_table")
                  if b.lead.get("seats", 0) <= 4 and _shape_ok(b, s.table_shape)]
        if not tables:
            tables = [b for b in self.options("conference_table")
                      if b.lead.get("seats", 0) <= 4]
        for t in sorted(tables, key=lambda b: b.dims["w"]):
            spot = room.open_floor(t.dims["w"], t.dims["d"], ring)
            if spot:
                break
        else:
            self.notes.append("There isn't open floor for a meeting table with chairs — "
                              "try a larger room or untick Meeting table.")
            return
        tv = self.add(t, 1, "a small meeting table for 4, clear of the desk and door")
        g = self.visitor_chairs()
        gv = self.add(self.choose(g, s.chair_color or s.color), 4,
                      "4 chairs at the meeting table", chair=True) if g else None
        self.table = tv
        self._seat_table(t, tv, gv, spot[0], spot[1], 4, count_seats=False)

    def _seat_table(self, t: Base, tv, chair_v, x0: float, y0: float, n: int,
                    count_seats: bool = True) -> None:
        """Put a table at (x0, y0) and n chairs around it, each facing the table."""
        room = self.room
        w, d = t.dims["w"], t.dims["d"]
        shape = t.lead.get("shape", "rectangular")
        room.zone("chair", "", x0 - 30, y0 - 30, w + 60, d + 60)
        self.table_item = room.put(Item("table", f"Table · {t.lead.get('seats')}", x0, y0, w, d,
                                        "circle" if shape == "round" else "rect",
                                        (tv or {}).get("color_hex", "#8a6a4a"), product=tv,
                                        face=DOWN))
        cx, cy = x0 + w / 2, y0 + d / 2
        seats = t.lead.get("seats", n)
        if shape == "round":
            rr = w / 2 + 16
            spots = [(cx + rr * math.cos(a), cy + rr * math.sin(a))
                     for a in [math.pi + i * 2 * math.pi / seats for i in range(seats)]]
        elif shape == "square":
            per = max(1, seats // 4)
            spots = []
            for i in range(per):
                f = (i + 0.5) / per
                spots += [(x0 + w * f, y0 - 16), (x0 + w * f, y0 + d + 16),
                          (x0 - 16, y0 + d * f), (x0 + w + 16, y0 + d * f)]
        else:
            per_side = max(1, min(seats // 2, int(w // 30)))
            if seats - 2 * per_side > 2:          # long boardroom tables: closer spacing
                per_side = min(math.ceil((seats - 2) / 2), int(w // 24))
            xs = [x0 + w * (i + 0.5) / per_side for i in range(per_side)]
            spots = [p for pair in zip([(x, y0 - 16) for x in xs],
                                       [(x, y0 + d + 16) for x in xs]) for p in pair]
            if seats - 2 * per_side >= 1:
                spots += [(x0 + w + 16, cy), (x0 - 16, cy)][:seats - 2 * per_side]
        chair_c = (chair_v or {}).get("color_hex", "#333333")
        for px, py in spots[:n]:
            room.put(Item("chair", "", px - 12, py - 12, 24, 24, "circle", chair_c,
                          product=chair_v, face=_toward(px, py, cx, cy)))
            if count_seats:
                self.seat_xy.append((px, py))

    # ---- conference ------------------------------------------------------------
    def conference(self) -> None:
        """Conference room or boardroom: one table in the middle, chairs round it."""
        s, room, r = self.s, self.room, self.r
        board = s.room_type == "boardroom"
        g = self.r.get("room_types", {}).get(s.room_type, {})
        every = self.options("conference_table")
        if not board:              # boardroom tables are for boardrooms; high-tops for huddles
            every = [b for b in every if not b.lead.get("high_top")
                     and "Boardroom" not in b.lead["base_name"]] or every
        tables = [b for b in every if _shape_ok(b, s.table_shape)] or every
        gaps = (48, 36) if board else (r["table_to_wall_in"], g.get("table_to_wall_in", 36))
        # the comfortable gap round the table — or the snug one, when only that
        # gets a table seating everyone into the room
        def fitting(tables: list[Base]) -> tuple[float, list[Base]]:
            by_gap = [(gap, [b for b in tables if b.dims["w"] + 2 * gap <= room.L
                             and b.dims["d"] + 2 * gap <= room.W]) for gap in gaps]
            most = lambda f: max((b.lead.get("seats", 0) for b in f), default=-1)  # noqa: E731
            return next(((g_, f) for g_, f in by_gap if most(f) >= s.people),
                        max(by_gap, key=lambda gf: most(gf[1])))
        gap, fits = fitting(tables)
        if not fits and board:        # no boardroom table fits: a smaller meeting table
            small = [b for b in self.options("conference_table", tagged=False,
                                             where=lambda b: not b.lead.get("high_top"))
                     if _shape_ok(b, s.table_shape)]
            gap, fits = fitting(small)
            every = tables = small or tables
            self.warnings.append("No boardroom table fits this room with space to walk "
                                 "round it — planned a smaller meeting table instead.")
            board_fits = False
        else:
            board_fits = True
        if fits and gap < gaps[0]:
            if board and board_fits:
                self.warnings.append(f"Boardrooms want {gaps[0]}″ of walking space round the "
                                     f"table; this room allows {gap}″.")
            else:
                self.notes.append(f"A snug room: {gap}″ behind each seat — the guideline "
                                  f"minimum ({gaps[0]}″ is comfortable).")
        good = [b for b in fits if b.lead.get("seats", 0) >= s.people]
        if not board and s.table_shape in ("Any", ""):
            # the guideline conference table: 3-4 ft wide, 8-12 ft long
            ideal = [b for b in good if 36 <= b.dims["d"] <= 48 and 96 <= b.dims["w"] <= 144]
            good = ideal or good
        if good:
            t = min(good, key=lambda b: (b.lead["seats"], b.dims["w"] * b.dims["d"],
                                         b.lead["price"]))
        elif fits:
            t = max(fits, key=lambda b: b.lead["seats"])
            better = [b for b in every if b.dims["w"] + 2 * gap <= room.L
                      and b.dims["d"] + 2 * gap <= room.W
                      and b.lead.get("seats", 0) > t.lead["seats"]]
            if s.table_shape not in ("Any", "") and better:
                alt = min(better, key=lambda b: b.lead["seats"])
                self.notes.append(
                    f"A {s.table_shape.lower()} table here seats at most "
                    f"{t.lead['seats']} — {_table_words(alt.lead)} would seat "
                    f"{alt.lead['seats']}.")
        else:
            t = min(tables, key=lambda b: b.dims["w"] * b.dims["d"])
            gap = gaps[-1]
            self.warnings.append(f"Even the smallest table needs {gap}″ of space on every "
                                 f"side for chairs; this room is tight.")
        t = self.same_size(t, every, s.color)
        seats = t.lead["seats"]
        at_table = min(s.people, seats)
        tv_ = self.add(t, 1, f"seats {seats}"
                       + (f" — the smallest that seats {s.people}" if seats >= s.people
                          else " — the largest that fits")
                       + (f" · {gap}″ walking space all round" if board else ""))
        self.table = tv_
        chair_v = self.pick_chairs(s.people)
        w, d = t.dims["w"], t.dims["d"]
        self._seat_table(t, tv_, chair_v, (room.L - w) / 2, (room.W - d) / 2, at_table)
        self._overflow(s.people - at_table, chair_v, seats)

    def _overflow(self, extra: int, chair_v, seats: int) -> None:
        room = self.room
        chair_c = (chair_v or {}).get("color_hex", "#333333")
        drawn = 0
        for _ in range(extra):
            it = room.along_walls("chair", "", 24, 24, chair_c, access=0,
                                  walls=("top", "bottom"))
            if not it:
                break
            it.shape, it.product = "circle", chair_v
            self.seat_xy.append((it.x + 12, it.y + 12))
            drawn += 1
        if drawn:
            self.notes.append(f"{drawn} extra chair{'s' if drawn > 1 else ''} along the "
                              f"wall for the people beyond the table's {seats} seats.")
        self.miss("chair", extra - drawn)       # bought, but nowhere to put them
        self.capacity = len(self.seat_xy)

    def huddle(self) -> None:
        """A table with one end pushed against the display wall; chairs on the
        other three sides."""
        s, room = self.s, self.room
        high = s.table_style == "High-top"
        tables = []
        if high:
            tables = self.options("conference_table", tagged=False,
                                  where=lambda b: b.lead.get("high_top"))
            if not tables:
                self.notes.append("No high-top table is in stock right now; planned a "
                                  "standard-height table instead.")
                s.table_style = "Standard"
        if not tables:
            tables = (self.options("conference_table", tagged=False,
                                   where=lambda b: not b.lead.get("high_top")
                                   and b.lead.get("shape") == "rectangular"
                                   and b.dims["w"] <= 84)
                      or self.options("conference_table", tagged=False,
                                      where=lambda b: not b.lead.get("high_top")))
        t = self.same_size(min(tables, key=lambda b: b.dims["w"]), tables, s.color)
        w, d = t.dims["w"], t.dims["d"]
        per_side = max(1, int(w // 30))
        cap = 2 * per_side + 1
        at = min(s.people, cap)
        if room.L < w + 30 + 30 or room.W < d + 60 + 12:
            self.warnings.append(f"A {fmt_len(w)} huddle table with chairs needs about "
                                 f"{fmt_len(w + 60)} × {fmt_len(d + 72)}; this room is tight.")
        tv_ = self.add(t, 1, ("counter height, for quick stand-up meetings" if high
                              else "a compact table") + f" · seats up to {cap} with its end "
                                                        f"against the display")
        self.table = tv_
        chair_v = self.pick_chairs(at)
        x0, y0 = 2.0, (room.W - d) / 2
        room.zone("chair", "", x0, y0 - 30, w + 30, d + 60)
        self.table_item = room.put(Item("table", "Table", x0, y0, w, d, "rect",
                                        (tv_ or {}).get("color_hex", "#8a6a4a"), product=tv_,
                                        face=DOWN))
        xs = [x0 + 12 + (w - 12) * (i + 0.5) / per_side for i in range(per_side)]
        spots = [p for pair in zip([(x, y0 - 16) for x in xs],
                                   [(x, y0 + d + 16) for x in xs]) for p in pair]
        spots.append((x0 + w + 16, y0 + d / 2))
        cx, cy = x0 + w / 2, y0 + d / 2
        colour = (chair_v or {}).get("color_hex", "#333333")
        for px, py in spots[:at]:
            room.put(Item("chair", "", px - 12, py - 12, 24, 24, "circle", colour,
                          product=chair_v, face=_toward(px, py, cx, cy)))
            self.seat_xy.append((px, py))
        if s.people > cap:
            self.notes.append(f"A huddle room seats 2–5; for {s.people} people a conference "
                              f"room works better.")
        self.capacity = len(self.seat_xy)

    def executive(self) -> None:
        """An L- or U-shaped desk facing the door, the executive's chair behind it,
        two guest chairs opposite and a small side table."""
        s, room, r = self.s, self.room, self.r
        m = room.m
        styles = ["U-shaped", "L-shaped"] if s.desk_style == "U-shaped" else ["L-shaped"]
        for style in styles:
            key = "u_shaped" if style == "U-shaped" else "l_shaped"
            desks = self.options("desk", tagged=False, where=lambda b: b.lead.get(key))
            if not desks:
                continue
            desk = self.choose(desks, s.color)
            w, d = desk.dims["w"], desk.dims["d"]
            y0 = m if key == "u_shaped" else m + 18
            guests_y = y0 + d + 22
            if guests_y + 12 + r["min_walkway_in"] <= room.W - m and w + 2 * m <= room.L:
                break
            if style == "U-shaped":
                self.notes.append("A U-shaped desk and its guest chairs need about "
                                  f"{fmt_len(d + 70)} of depth; this plan uses an L-shaped "
                                  f"desk.")
        x0 = max(m, (room.L - w) / 2 - 18)
        dv = self.add(desk, 1, f"{style} desk facing the door"
                      + (" — a credenza return behind you" if key == "u_shaped" else ""))
        self.desk = dv
        self.desk_item = room.put(Item("desk", "Desk", x0, y0, w, d, color=(dv or {}).get(
            "color_hex", "#6b4a2f"), product=dv, face=DOWN))
        self.desks = 1
        if key == "u_shaped":       # the chair sits inside the U, between desk and return
            cx, cy = x0 + w / 3, y0 + d / 2 - 9
        else:                       # inside the L's corner
            cx, cy = x0 + (w - 22) / 2, y0 + 19
        chair_v = self.pick_chairs(1)
        room.put(Item("chair", "", cx - 12, cy - 12, 24, 24, "circle",
                      (chair_v or {}).get("color_hex", "#333333"), product=chair_v, face=DOWN))
        self.seat_xy.append((cx, cy))
        g = self.visitor_chairs()
        gv = self.add(self.choose(g, s.chair_color or s.color), 2,
                      "2 guest chairs opposite the desk", chair=True)
        colour = (gv or {}).get("color_hex", "#555555")
        mid = x0 + w / 2
        for gx in (mid - 18, mid + 18):
            room.put(Item("chair", "", gx - 12, guests_y - 12, 24, 24, "circle", colour,
                          product=gv, face=UP))
        self.visitors = 2
        room.zone("chair", "", x0, y0 + d, w, guests_y + 12 - (y0 + d))
        side = self.options("side_table", tagged=False)
        if side:
            st_ = self.choose(side, s.color)
            sv = self.add(st_, 1, "a small side table beside the guest chairs")
            sx = mid + 18 + 12 + 6
            item = Item("side_table", "", sx, guests_y - 12, 24, 24, "circle",
                        (sv or {}).get("color_hex", "#8a6a4a"), product=sv, face=UP)
            if not room.free(item):
                spot = room.open_floor(24, 24, 6)
                if spot:
                    item.x, item.y = spot
            room.put(item)
        self.capacity = 1

    # ---- the rest of the room ------------------------------------------------------
    def wall_display(self) -> None:
        s, room, r = self.s, self.room, self.r
        if not self.seat_xy:
            return
        wide = s.display_wall == "Wide wall" and s.room_type in ("conference room",
                                                                 "boardroom")
        if s.tv:
            far = (max(y for _, y in self.seat_xy) if wide else
                   max(x for x, _ in self.seat_xy))
            want = far / r["tv_viewing_ratio"]
            tvs = sorted(self.options("tv", tagged=False), key=lambda b: b.lead["screen_in"])
            tv = self.same_size(next((b for b in tvs if b.lead["screen_in"] >= want - 3),
                                     tvs[-1]), tvs)
            v = self.add(tv, 1, f"farthest seat is {fmt_len(far)} away → "
                         + (f"a {tv.lead['screen_in']}″ screen reads well"
                            if tv.lead["screen_in"] >= want - 3 else "our largest screen"))
            tw = tv.dims["w"]
            high = (self.table or {}).get("high_top")
            z = 52 if high else 44
            if wide:
                self.tv_item = room.put(Item("tv", f"TV {tv.lead['screen_in']}″",
                                             room.L / 2 - tw / 2, 0, tw, 4, "wall",
                                             (v or {}).get("color_hex", "#111111"), product=v,
                                             face=DOWN, z=z))
                room.zone("view", "", room.L / 2 - tw / 2 - 6, 0, tw + 12, 30)
            else:
                self.tv_item = room.put(Item("tv", f"TV {tv.lead['screen_in']}″", 0,
                                             room.W / 2 - tw / 2, 4, tw, "wall",
                                             (v or {}).get("color_hex", "#111111"), product=v,
                                             face=RIGHT, z=z))
                room.zone("view", "", 0, room.W / 2 - tw / 2 - 6, 30, tw + 12)
            near = (min(y for _, y in self.seat_xy) if wide else
                    min(x for x, _ in self.seat_xy))
            run = near + 36
            cables = sorted(self.options("hdmi_cable", tagged=False),
                            key=lambda b: b.lead["price"])
            if cables:
                cable = cables[0] if run <= 72 or len(cables) == 1 else cables[-1]
                self.add(cable, 1, f"TV to the nearest seat is about {fmt_len(run)}")
        if s.whiteboard:
            if s.room_type in MEETING_ROOMS and wide:
                self.whiteboard_side()
                return
            if s.room_type in MEETING_ROOMS:
                wall_len, x_start, y = room.L, 0.0, 0.0
            else:
                wall_len, x_start, y = room.door[0], 0.0, room.W - 3
            room_for = wall_len * r["whiteboard_wall_fraction"]
            boards = [b for b in self.options("whiteboard", tagged=False)
                      if b.dims["d"] <= 2 and b.dims["w"] <= room_for]
            if boards:
                wb = self.same_size(max(boards, key=lambda b: b.dims["w"]), boards)
                wbv = self.add(wb, 1, f"the largest board that fits {int(r['whiteboard_wall_fraction'] * 100)}% "
                         f"of a {fmt_len(wall_len)} wall")
                ww = wb.dims["w"]
                wx = x_start + (wall_len - ww) / 2
                room.put(Item("whiteboard", f"Whiteboard {fmt_len(ww)}", wx, y, ww, 3, "wall",
                              "#f4f4f4", product=wbv, face=DOWN if y < 1 else UP, z=34))
                room.zone("view", "", wx - 6, 0 if y < 1 else room.W - 36, ww + 12, 36)
                self.board_supplies()
            else:
                self.notes.append("No free wall wide enough for a whiteboard.")

    def whiteboard_side(self) -> None:
        """Display on the wide wall: the whiteboard goes on the narrow (left) wall."""
        room, r = self.room, self.r
        room_for = room.W * r["whiteboard_wall_fraction"]
        boards = [b for b in self.options("whiteboard", tagged=False)
                  if b.dims["d"] <= 2 and b.dims["w"] <= room_for]
        if not boards:
            self.notes.append("No free wall wide enough for a whiteboard.")
            return
        wb = self.same_size(max(boards, key=lambda b: b.dims["w"]), boards)
        ww = wb.dims["w"]
        wbv = self.add(wb, 1, "on the narrow wall — the display has the wide wall")
        room.put(Item("whiteboard", f"Whiteboard {fmt_len(ww)}", 0, (room.W - ww) / 2, 3, ww,
                      "wall", "#f4f4f4", product=wbv, face=RIGHT, z=34))
        self.board_supplies()

    def board_supplies(self) -> None:
        """Markers sized to the room, an eraser, and a cleaner for big meeting rooms."""
        big = self.s.room_type in ("boardroom", "conference room")
        pack = (lambda b: (b.lead.get("pack_qty") or 0) >= 8) if big else \
            (lambda b: (b.lead.get("pack_qty") or 0) < 8)
        markers = (self.pick("dry_erase_marker", tagged=False, where=pack)
                   or self.pick("dry_erase_marker", tagged=False))
        self.add(markers, 1, "a full set for the whiteboard" if big else "for the whiteboard")
        self.add(self.pick("whiteboard_eraser", tagged=False), 1, "for the whiteboard")
        if big:
            self.add(self.pick("whiteboard_cleaner", tagged=False), 1,
                     "keeps the board clean between meetings")

    def av_kit(self) -> None:
        """Video calls: a camera at the display and audio sized to the room."""
        s, room = self.s, self.room
        if not s.video_calls:
            return
        n = len(self.seat_xy) or s.people
        tv = self.tv_item
        tv_h = ((tv.product or {}).get("dims") or {}).get("h", 30) if tv else 0

        def at_display(kind: str, v, width: float, above: bool) -> None:
            if not tv:
                return
            z = tv.z + tv_h + 3 if above else tv.z - 6
            if tv.w > tv.d:                       # display on the top (wide) wall
                it = Item(kind, "", tv.x + tv.w / 2 - width / 2, 0, width, 4, "wall",
                          "#1b1c1e", product=v, face=DOWN, z=z)
            else:
                it = Item(kind, "", 0, tv.y + tv.d / 2 - width / 2, 4, width, "wall",
                          "#1b1c1e", product=v, face=RIGHT, z=z)
            room.put(it)

        phone = self.choose(self.options("speaker", tagged=False,
                                         where=lambda b: "Speakerphone" in b.lead["base_name"]))
        bar = self.choose(self.options("video_bar", tagged=False))
        if s.room_type == "huddle room" and bar:
            v = self.add(bar, 1, "camera, speakers and mics in one bar above the display — "
                                 "made for small rooms")
            at_display("video_bar", v, 30, above=True)
            return
        cam = self.choose(self.options("camera", tagged=False))
        v = self.add(cam, 1, "centred above the display so the whole table is in frame"
                     if tv else "for video calls; mounts on a monitor or display")
        at_display("camera", v, 10, above=True)
        if s.room_type == "boardroom":
            sb = self.choose(self.options("speaker", tagged=False,
                                          where=lambda b: "Soundbar" in b.lead["base_name"]))
            v = self.add(sb, 1, "under the display: clear voice for a large room")
            at_display("speaker", v, 36, above=False)
        k = max(1, math.ceil(n / 10)) if s.room_type in MEETING_ROOMS else 1
        pv = self.add(phone, k, ("1 per 10 seats on the table" if k > 1 else
                                 "on the table — picks up everyone" if self.table_item else
                                 "on the desk for calls"))
        host = self.table_item or self.desk_item
        if host and pv:
            h = ((host.product or {}).get("dims") or {}).get("h", 30)
            for i in range(k):
                if host is self.table_item:
                    px = host.x + host.w * (i + 0.5) / k
                    py = host.y + host.d / 2
                else:
                    px, py = host.x + host.w - 14, host.y + host.d - 18
                room.put(Item("speaker", "", px - 4.5, py - 4.5, 9, 9, "top", "#1b1c1e",
                              product=pv, face=DOWN, z=h))

    # ---- replicate the room in a photo ----------------------------------------
    def replicate(self) -> None:
        """Copy the room in the photo: what it has and roughly where — nothing more.
        Counts the shopper raised go round the table, at the desks, then along
        a wall; lowered counts drop the loose chairs first."""
        s, room = self.s, self.room
        rep = s.replicate or {}
        want = {k: max(0, int(n)) for k, n in (rep.get("counts") or {}).items()}
        seen: dict[str, list[dict[str, Any]]] = {}
        for it in rep.get("items") or []:
            seen.setdefault(it["kind"], []).append(it)
        self.cam = photo_cam.estimate(rep.get("items") or [], rep.get("aspect"),
                                      room.L, room.W)

        def why(kind: str, n: int) -> str:
            k = len(seen.get(kind, []))
            if k == 0:
                return "you added this"
            if n <= k:
                return "as in your photo"
            return f"{k} as in your photo, {n - k} you added"

        def wall_for(it: dict[str, Any] | None) -> tuple[str, ...]:
            if it is None and room.W > room.L:     # added, not in the photo: a long wall
                return ("left", "right", "top", "bottom")
            u = (it or {}).get("u", 0.5)
            if (it or {}).get("wall"):
                first = it["wall"]
            elif u < 0.2:
                first = "left"
            elif u > 0.8:
                first = "right"
            else:           # the far wall: the near one is behind the camera, with the door
                first = "top"
            return (first,) + tuple(w for w in ("top", "left", "right", "bottom") if w != first)

        def along(it: dict[str, Any] | None) -> float:
            """Where along its wall: across the photo for the far wall, depth for a side."""
            if not it:
                return 0.5
            w = wall_for(it)[0]
            at = self._floor(it)            # where it stands, through the photo's camera
            if at:
                return at[0] / room.L if w in ("top", "bottom") else at[1] / room.W
            return it.get("u", 0.5) if w in ("top", "bottom") else it.get("v", 0.5)

        # chairs: at the table, at a desk, loose — the loose ones go first when fewer
        n_chairs = want.get("chair", 0)
        photo_chairs = seen.get("chair", [])
        rank = {"near": 0, "far": 0, "left": 0, "right": 0, "desk": 1}
        kept = sorted(photo_chairs, key=lambda c: rank.get(c.get("side"), 2))[:n_chairs]
        extra_chairs = max(0, n_chairs - len(kept))

        # desks first (against a wall, a chair in front)
        desk_items: list[Item] = []
        n_desks = want.get("desk", 0)
        if n_desks:
            office = s.room_type != "open office"         # cubicles are for team areas
            single = lambda b: (b.lead.get("seats", 1) == 1 and not b.lead.get("u_shaped")  # noqa: E731
                                and "L-Shaped" not in b.lead["base_name"]
                                and not (office and "Cubicle" in b.lead["base_name"]))
            if s.room_type in ("executive suite", "private office"):
                execs = [b for b in self.options("desk", tagged=False, where=single)
                         if "Executive" in b.lead["base_name"]]
                single = (lambda b, ok=execs: b in ok) if execs else single
            opts = self.options("desk", where=single) or self.options("desk", tagged=False,
                                                                       where=single)
            if s.standing:
                opts = [b for b in opts if "Standing" in b.lead["base_name"]] or opts
            if s.min_desk_w:
                opts = [b for b in opts if b.dims["w"] >= s.min_desk_w] or opts
            if s.max_desk_w:
                opts = [b for b in opts if b.dims["w"] <= s.max_desk_w] or opts
            base = self.choose(opts, s.color)
            v = self.add(base, n_desks, why("desk", n_desks))

            def draw_desks() -> None:
                spots = seen.get("desk", [])
                for i in range(n_desks):
                    it = spots[i] if i < len(spots) else None
                    d = room.along_walls("desk", "Desk", base.dims["w"], base.dims["d"],
                                         (v or {}).get("color_hex", "#8a6a4a"), 36,
                                         wall_for(it), near=along(it))
                    if not d:
                        self.notes.append("No free wall left to draw a desk — it's still "
                                          "on the list.")
                        self.miss("desk", n_desks - i)
                        break
                    d.product = v
                    desk_items.append(d)
            # a desk the photo doesn't have makes room for its table, not the other way
            later = draw_desks if not seen.get("desk") and want.get("conference_table") \
                else None
            if not later:
                draw_desks()
        else:
            later = None
        self.desk_item = desk_items[0] if desk_items else None

        at_desks = [c for c in kept if c.get("side") == "desk"]
        desk_chairs = min(len(at_desks), len(desk_items))
        spare = at_desks[desk_chairs:]          # more chairs than desks: along the wall
        free_desks = max(0, len(desk_items) - desk_chairs)
        to_desks = min(extra_chairs, free_desks)
        extra_chairs -= to_desks
        desk_chairs = min(len(desk_items), desk_chairs + to_desks)
        if desk_chairs:
            boss = s.room_type in ("executive suite", "private office")
            opts = self.options("chair", tagged=False,
                                where=lambda b: b.lead.get("use", "task") ==
                                ("executive" if boss else "task"))
            if s.ergonomic:
                opts = [b for b in opts if "Ergonomic" in b.lead["base_name"]] or opts
            cv = self.add(self.choose(opts, s.chair_color or s.color), desk_chairs,
                          "at the desk · " + why("chair", n_chairs), chair=True)
            mat = self.pick("chair_mat", tagged=False) if s.carpet else None
            mv = self.add(mat, desk_chairs, "carpeted floor: 1 under each rolling chair")
            for d in desk_items[:desk_chairs]:
                fx, fy = math.cos(d.face), math.sin(d.face)
                cx = d.x + d.w / 2 + fx * (min(d.w, d.d) / 2 + 14)
                cy = d.y + d.d / 2 + fy * (min(d.w, d.d) / 2 + 14)
                if mv:
                    room.put(Item("chair_mat", "", cx - 18, cy - 22, 36, 44, "under",
                                  (mv or {}).get("color_hex", "#dfe6ea"), product=mv))
                room.put(Item("chair", "", cx - 12, cy - 12, 24, 24, "circle",
                              (cv or {}).get("color_hex", "#333333"), product=cv,
                              face=d.face + math.pi))
                self.seat_xy.append((cx, cy))

        # the table and its chairs, on the sides they're on in the photo
        loose = [c for c in kept if c.get("side") not in rank] + spare
        n_tables = want.get("conference_table", 0)
        if n_tables:
            # a chair the photo didn't place at the table goes round it too: the
            # replica is tidy, not a copy of where each one was left
            extra_chairs += len(loose)
            loose = []
            t_seen = (seen.get("conference_table") or [None])[0]
            sides = {"near": 0, "far": 0, "left": 0, "right": 0}
            by_side: dict[str, list[dict[str, Any]]] = {k: [] for k in sides}
            for c in kept:
                if c.get("side") in sides and (t_seen is None or
                                               c.get("at") == t_seen.get("host")):
                    sides[c["side"]] += 1
                    by_side[c["side"]].append(c)
                elif c.get("side") in sides:
                    loose.append(c)
            a, b = (("left", "right") if (t_seen or {}).get("axis") == "deep"
                    else ("far", "near"))        # the table's long sides
            while extra_chairs > 0:      # more chairs than the photo showed: round the table
                side = a if sides[a] < sides[b] else b
                sides[side] += 1
                extra_chairs -= 1
            # a credenza or cabinet along the far wall in the photo: leave it room
            t_top = ((t_seen or {}).get("box") or [0, 1, 0, 1])[1]
            far_storage = any(0.2 < c.get("u", 0.5) < 0.8 and
                              (c.get("v", 1) < 0.6 or (c.get("box") or [0, 1])[1] < t_top)
                              for c in seen.get("storage", []))
            self._replica_table(t_seen, sides, n_chairs, why,
                                reserve=26 if want.get("storage") and far_storage else 0,
                                by_side=by_side)
            for i in range(1, n_tables):
                more = self.pick("conference_table", tagged=False,
                                 where=lambda b: b.lead.get("seats", 0) <= 4)
                spot = more and room.open_floor(more.dims["w"], more.dims["d"], 30)
                if spot:
                    v = self.add(more, 1, why("conference_table", n_tables))
                    room.put(Item("table", "Table", spot[0], spot[1], more.dims["w"],
                                  more.dims["d"], "circle" if more.lead.get("shape") ==
                                  "round" else "rect", (v or {}).get("color_hex", "#8a6a4a"),
                                  product=v, face=DOWN))
                else:
                    self.notes.append("No open floor for another table.")
                    self.miss("conference_table", n_tables - i)
                    break
        else:
            loose += [c for c in kept if c.get("side") in ("near", "far", "left", "right")]
        if later:
            later()
            self.desk_item = desk_items[0] if desk_items else None

        # loose chairs and any still unplaced: along a wall, near where the photo has them
        n_loose = len(loose) + extra_chairs
        if n_loose:
            gv = self.add(self.choose(self.visitor_chairs(), s.chair_color or s.color),
                          n_loose, "along the wall · " + why("chair", n_chairs), chair=True)
            for i in range(n_loose):
                c = loose[i] if i < len(loose) else None
                it = room.along_walls("chair", "", 24, 24, (gv or {}).get("color_hex",
                                                                          "#333333"),
                                      0, wall_for(c), near=along(c))
                if not it:
                    self.notes.append("No free wall left for every chair — they're all on "
                                      "the list.")
                    self.miss("chair", n_loose - i)
                    break
                it.shape, it.product = "circle", gv
                self.seat_xy.append((it.x + 12, it.y + 12))
        self.capacity = len(self.seat_xy)

        # on the walls: TVs and whiteboards
        used: dict[str, list[tuple[float, float]]] = {}
        for kind in ("tv", "whiteboard"):
            n = want.get(kind, 0)
            if not n:
                continue
            spots = seen.get(kind, [])
            for i in range(n):
                it = spots[i] if i < len(spots) else None
                self._replica_wall(kind, it, used, why(kind, n), first=i == 0)

        # monitors on the desks, lamps on a desk or the floor, the rest along the walls
        n_mon = want.get("monitor", 0)
        mon_why = why("monitor", n_mon)
        if s.monitors and desk_items and n_mon < s.monitors * len(desk_items):
            n_mon = s.monitors * len(desk_items)
            mon_why = f"{s.monitors} per desk, as you use"
        if n_mon:
            two = desk_items and n_mon >= 2 * len(desk_items)     # side by side: not ultrawide
            v = self.add(self.pick("monitor", tagged=False,
                                   where=lambda b: not (two and b.lead.get("ultrawide")))
                         or self.pick("monitor", tagged=False), n_mon, mon_why)
            for i in range(n_mon):
                if not desk_items:
                    self.notes.append("Monitors are on the list; there's no desk to draw "
                                      "them on.")
                    self.miss("monitor", n_mon - i)
                    break
                d = desk_items[i % len(desk_items)]
                k = i // len(desk_items)
                off = (k - (n_mon // len(desk_items) - 1) / 2) * 22
                if d.face in (DOWN, UP):
                    mx = d.x + d.w / 2 - 11 + off
                    my = d.y + 3 if d.face == DOWN else d.y + d.d - 9
                    room.put(Item("monitor", "", mx, my, 22, 6, "under", "#222222",
                                  product=v, face=d.face))
                else:
                    my = d.y + d.d / 2 - 11 + off
                    mx = d.x + 3 if d.face == RIGHT else d.x + d.w - 9
                    room.put(Item("monitor", "", mx, my, 6, 22, "under", "#222222",
                                  product=v, face=d.face))
        n_lamp = want.get("lamp", 0)
        if n_lamp:
            lamps = self.options("lamp", tagged=False)
            desk_lamp = self.choose([b for b in lamps if "Floor" not in b.lead["base_name"]], s.color)
            floor_lamp = self.choose([b for b in lamps if "Floor" in b.lead["base_name"]], s.color)
            on_desks = min(n_lamp, len(desk_items)) if desk_lamp else 0
            if on_desks:
                v = self.add(desk_lamp, on_desks, why("lamp", n_lamp))
                for d in desk_items[:on_desks]:
                    h = ((d.product or {}).get("dims") or {}).get("h", 30)
                    room.put(Item("lamp", "", d.x + d.w - 12, d.y + 3, 8, 8, "top",
                                  (v or {}).get("color_hex", "#333333"), product=v,
                                  face=d.face, z=h))
            rest = n_lamp - on_desks
            if rest:
                self.place(floor_lamp or desk_lamp, rest, why("lamp", n_lamp), "Lamp",
                           access=0, walls=wall_for((seen.get("lamp") or [None])[-1]))
        for kind, label, access in (("storage", "Storage", 24), ("printer", "Printer", 24),
                                    ("wastebasket", "Bin", 0)):
            n = want.get(kind, 0)
            if not n:
                continue
            spots = seen.get(kind, [])
            for i in range(n):
                it = spots[i] if i < len(spots) else None
                # each one matched to what the photo calls it: shelves, a file cabinet…
                ref = it or (spots[0] if spots else {})
                label = ref.get("label", "")
                bx = ref.get("box") or [0, 0, 0, 1]
                if kind == "storage" and (bx[2] - bx[0]) > 1.3 * (bx[3] - bx[1]):
                    label = "credenza"          # wide and low in the photo, whatever it's called
                base = self._replica_pick(kind, label)
                if not base:
                    break
                v = self.add(base, 1, why(kind, n))
                dims = base.dims
                drawn = room.along_walls(kind, label, max(dims["w"], 12), max(dims["d"], 12),
                                         (v or {}).get("color_hex", "#999999"), access,
                                         wall_for(it), near=along(it))
                if not drawn:
                    self.notes.append(f"No free wall left to draw every "
                                      f"{REPLICA_NAMES[kind][0]} — they're on the list.")
                    self.miss(kind, n - i)
                    break
                drawn.product = v
        n_bar = want.get("video_bar", 0)
        bar = next(iter(self.options("video_bar", tagged=False)), None) if n_bar else None
        if bar:
            v = self.add(bar, n_bar, why("video_bar", n_bar))
            tvs = [i for i in room.items if i.kind == "tv" and i.shape == "wall"]
            for i in range(n_bar):
                tv = tvs[i] if i < len(tvs) else None
                if not tv:
                    self.notes.append("A video bar mounts at a display — add a TV to draw it.")
                    self.miss("video_bar", n_bar - i)
                    break
                z = max(4, tv.z - 8)                    # just under the screen
                if tv.w > tv.d:
                    room.put(Item("video_bar", "", tv.x + tv.w / 2 - 15, tv.y, 30, 4, "wall",
                                  "#1b1c1e", product=v, face=tv.face, z=z))
                else:
                    room.put(Item("video_bar", "", tv.x, tv.y + tv.d / 2 - 15, 4, 30, "wall",
                                  "#1b1c1e", product=v, face=tv.face, z=z))
        n_side = want.get("side_table", 0)
        side = self.pick("side_table", tagged=False) if n_side else None
        if side:
            spots = seen.get("side_table", [])
            v = self.add(side, n_side, why("side_table", n_side))
            for i in range(n_side):
                it = spots[i] if i < len(spots) else None
                d = side.dims
                drawn = (room.along_walls("side_table", "", max(d["w"], 12), max(d["d"], 12),
                                          (v or {}).get("color_hex", "#999999"), 0,
                                          wall_for(it), near=along(it))
                         or room.corner("side_table", "", max(d["w"], 12), max(d["d"], 12),
                                        (v or {}).get("color_hex", "#999999")))
                if not drawn:
                    self.miss("side_table", n_side - i)
                    break
                drawn.product = v
        if want.get("rug"):
            rug = self.pick("rug", tagged=False)
            if rug:
                self.add(rug, want["rug"], why("rug", want["rug"]))
                if not self.place_under(rug, "Rug"):
                    self.miss("rug", want["rug"])

        # the camera: from the photo's side, far enough back to show it all
        t_seen = (seen.get("conference_table") or seen.get("desk") or [None])[0]
        self.cam = photo_cam.frame(self.cam, room.items, room.L, room.W, rep.get("aspect"),
                                   lean=(t_seen or {}).get("u", 0.5))

        # extras ticked from the suggestions: on the list as chosen
        if rep.get("extras"):
            by_sku = {v["sku"]: v for bases in self.c.values() for b in bases
                      for v in b.variants.values()}
            for sku, qty in rep["extras"].items():
                v = by_sku.get(sku)
                if v and int(qty) > 0:
                    if not any(line.product["sku"] == sku for line in self.lines):
                        self.lines.append(Line(v, int(qty), "you added this"))

    def _replica_pick(self, kind: str, label: str) -> Base | None:
        """The catalogue product closest to what the photo calls the thing."""
        if kind == "storage":
            for words, name in (("book|shel", "Bookcase"), ("credenza", "Credenza"),
                                ("drawer|file", "File"), ("cabinet", "Cabinet")):
                if re.search(words, label):
                    hit = self.pick("storage", tagged=False,
                                    where=lambda b, n=name: n in b.lead["base_name"])
                    if hit:
                        return hit
        if kind == "wastebasket":
            return self.pick("wastebasket", tagged=False)
        if kind == "printer" and self.s.printer:     # the kind they told us they need
            word = "Inkjet" if self.s.printer == "inkjet" else "Color"
            hit = self.pick("printer", tagged=False,
                            where=lambda b: word in b.lead["base_name"])
            if hit:
                return hit
        return self.choose(kind) or self.choose(kind, tagged=False)

    def _floor(self, it: dict[str, Any] | None) -> tuple[float, float] | None:
        """Where a thing in the photo stands on the plan, through the photo's
        camera: the bottom middle of its box, on the floor."""
        cam, box = self.cam, (it or {}).get("box")
        if not cam or not box:
            return None
        at = photo_cam.stand(cam, it, (self.s.replicate or {}).get("items") or [])
        if not at:
            return None
        room = self.room
        return (min(max(at[0], 0.0), room.L), min(max(room.W - at[1], 0.0), room.W))

    def _footprint(self, t_seen: dict[str, Any] | None
                   ) -> tuple[float, float, float, float] | None:
        """The table in the photo, measured through its camera: (centre x, near edge
        y, size across, size deep) on the plan. The near edge (where the box meets
        the floor) and the width across are sure; the depth isn't — near the
        horizon a pixel is a foot — so it's only a hint, kept short of the chairs."""
        cam, box = self.cam, (t_seen or {}).get("box")
        if not cam or not box:
            return None
        x1, y1, x2, y2 = box
        top = photo_cam.TABLE_TOP
        near = cam.at_height((x1 + x2) / 2, y2, 0.0)
        far = cam.at_height((x1 + x2) / 2, y1, top)
        if not near or not far or far[1] <= near[1] + 12:
            return None
        _, v_top = cam.project((near[0], near[1], top))     # the near edge of the top
        a, b = cam.at_height(x1, v_top, top), cam.at_height(x2, v_top, top)
        if not a or not b:
            return None
        items = (self.s.replicate or {}).get("items") or []
        at = {id(c): photo_cam.stand(cam, c, items) for c in items
              if c.get("kind") == "chair" and c.get("box")}
        backs = [p[1] for p in at.values() if p]
        far_y = min(far[1], max(backs) + 18) if backs else far[1]
        if far_y <= near[1] + 12:
            far_y = far[1]
        lo, hi = min(a[0], b[0]), max(a[0], b[0])
        if t_seen.get("axis") == "deep":        # the chairs down its sides bound its width
            rows = {s_: [at[id(c)][0] for c in items if c.get("side") == s_ and at.get(id(c))]
                    for s_ in ("left", "right")}
            if rows["left"] and rows["right"]:
                lo = max(lo, sum(rows["left"]) / len(rows["left"]) + 14)
                hi = min(hi, sum(rows["right"]) / len(rows["right"]) - 14)
                if hi - lo < 30:
                    lo, hi = min(a[0], b[0]), max(a[0], b[0])
        return (lo + hi) / 2, self.room.W - near[1], hi - lo, far_y - near[1]

    def _spread(self, chairs: list[dict[str, Any]], k: int, lo: float, length: float,
                axis: int) -> list[float]:
        """k chairs along one side of a table, lo .. lo + length on the plan: where
        the photo has them when it shows that many, else evenly spaced. axis: which
        plan coordinate runs along the side (0 = x, across the photo)."""
        even = [lo + length * (j + 0.5) / k for j in range(k)]
        if self.s.replicate is not None:        # a replica's chairs: evenly spaced
            return even
        pts = [self._floor(c) for c in chairs]
        if not self.cam or len(chairs) != k or not all(pts):
            return even
        a, b = lo + 12, lo + length - 12
        if b < a:
            return even
        vals = sorted(min(max(p[axis], a), b) for p in pts)
        gap = min(24.0, (b - a) / max(k - 1, 1))
        for j in range(1, k):                       # at least a chair's width apart
            vals[j] = max(vals[j], vals[j - 1] + gap)
        over = vals[-1] - b
        if over > 0:                                # pushed past the end: slide back
            vals = [v - over for v in vals]
            for j in range(k - 2, -1, -1):
                vals[j] = min(vals[j], vals[j + 1] - gap)
        return vals

    def _replica_table(self, t_seen: dict[str, Any] | None, sides: dict[str, int],
                       n_chairs: int, why, reserve: float = 0,
                       by_side: dict[str, list[dict[str, Any]]] | None = None) -> None:
        """The table where the photo has it, sized for the chairs on each side —
        turned to run away from the far wall when the photo's table does."""
        s, room = self.s, self.room
        ring = 30
        deep = (t_seen or {}).get("axis") == "deep"
        # the table's own long sides and ends, in the room
        longs, ends = (("left", "right"), ("near", "far")) if deep else (("near", "far"),
                                                                        ("left", "right"))
        total = sum(sides.values())
        self.at_table = total
        every = [b for b in self.options("conference_table", tagged=False)
                 if not b.lead.get("high_top")] or self.options("conference_table",
                                                                tagged=False)
        shaped = [b for b in every if _shape_ok(b, s.table_shape)] or every
        seen_shape = (t_seen or {}).get("shape", "")
        like = {"round": ("round",), "square": ("square", "rectangular"),
                "rectangular": ("rectangular", "boat"), "oval": ("boat", "rectangular"),
                }.get(seen_shape, ("rectangular", "boat", "square"))
        if s.table_shape in ("Any", ""):         # the shape in the photo, never round unless it is
            shaped = ([b for b in shaped if b.lead.get("shape") == like[0]]
                      or [b for b in shaped if b.lead.get("shape") in like] or shaped)

        def holds(b: Base) -> bool:
            if b.lead.get("seats", 0) < total:
                return False
            if b.lead.get("shape") == "round":
                return True
            w, d = b.dims["w"], b.dims["d"]
            return (max(sides[longs[0]], sides[longs[1]]) <= max(1, int(w // 26))
                    and max(sides[ends[0]], sides[ends[1]]) <= max(1, int(d // 26)))

        def room_for(b: Base, ring: float) -> bool:
            w, d = (b.dims["d"], b.dims["w"]) if deep else (b.dims["w"], b.dims["d"])
            return w + 2 * ring <= room.L and d + 2 * ring <= room.W

        box = (t_seen or {}).get("box")
        wide = deep or not box or (box[2] - box[0]) > 1.6 * (box[3] - box[1])
        fits = [b for b in shaped if room_for(b, ring)]
        if not fits:                                   # a narrow room: a tighter
            fits = [b for b in shaped if room_for(b, 22)]      # walkway beats no table
            if fits:
                self.tight.append("only about 22″ to walk behind the chairs (36″ is "
                                  "comfortable)")
        if not fits:
            fits = [min(shaped, key=lambda b: b.dims["w"])]
            self.tight.append("even the smallest table leaves no room to pull out the chairs")
        good = [b for b in fits if holds(b)]
        if wide and s.table_shape in ("Any", ""):
            good = [b for b in good if b.lead.get("shape") != "round"] or good
        foot = self._footprint(t_seen)

        def off(b: Base) -> float:              # how far from the table in the photo:
            w_, d_ = (b.dims["d"], b.dims["w"]) if deep else (b.dims["w"], b.dims["d"])
            return abs(w_ - foot[2]) + 0.25 * abs(d_ - foot[3])    # its depth a hint
        if good and foot:                       # the size the photo shows, seating them all
            t = min(good, key=lambda b: (off(b), b.lead["seats"]))
        else:
            t = (min(good, key=lambda b: (b.lead["seats"], b.dims["w"] * b.dims["d"]))
                 if good else max(fits, key=lambda b: b.lead.get("seats", 0)))
        tv = self.add(t, 1, why("conference_table", 1) + f" · seats {t.lead.get('seats')}"
                      + (f", sized for the {total} chair{'s' if total != 1 else ''} round it"
                         if total else ""))
        self.table = tv
        w, d = (t.dims["d"], t.dims["w"]) if deep else (t.dims["w"], t.dims["d"])
        x0, y0 = (room.L - w) / 2, (room.W - d) / 2      # in the middle of the room
        if reserve and y0 - ring < reserve:
            if reserve <= room.W - d - 2 * ring:
                y0 = reserve + ring              # off the far wall, by what stands there
            else:
                self.reserve_missed = True       # the room is too shallow for both
        room.zone("chair", "", x0 - ring, y0 - ring, w + 2 * ring, d + 2 * ring)
        self.table_item = room.put(Item("table", f"Table · {t.lead.get('seats')}", x0, y0,
                                        w, d, "circle" if t.lead.get("shape") == "round"
                                        else "rect", (tv or {}).get("color_hex", "#8a6a4a"),
                                        product=tv, face=DOWN))
        cx, cy = x0 + w / 2, y0 + d / 2
        spots: list[tuple[float, float]] = []
        # a chair needs about 24" of table edge: the rest are on the list, not drawn
        sides = dict(sides)
        over = 0
        if t.lead.get("shape") == "round":
            room_for_n = max(1, int(2 * math.pi * (w / 2 + 16) // 24))
            while sum(sides.values()) > room_for_n:
                big = max(sides, key=lambda k: sides[k])
                sides[big] -= 1
                over += 1
        else:
            for side in sides:
                edge = w if side in ("near", "far") else d
                cap = max(1, int((edge + 8) // 24))
                over += max(0, sides[side] - cap)
                sides[side] = min(sides[side], cap)
        if over:
            self.miss("chair", over)
        if t.lead.get("shape") == "round":
            rr = w / 2 + 16
            for side, mid in (("near", math.pi / 2), ("far", -math.pi / 2),
                              ("left", math.pi), ("right", 0.0)):
                k = sides[side]
                step = min(math.pi / 2 / max(k, 1), 2 * math.pi / max(total, 1))
                spots += [(cx + rr * math.cos(mid + (j - (k - 1) / 2) * step),
                           cy + rr * math.sin(mid + (j - (k - 1) / 2) * step))
                          for j in range(k)]
        else:
            by_side = by_side or {}
            for side in ("near", "far"):
                k = sides[side]
                if k:
                    y = y0 + d + 16 if side == "near" else y0 - 16
                    fronts = []
                    if side == "near" and fronts:   # tucked in as far as the photo shows
                        fronts.sort()
                        y = min(max(fronts[len(fronts) // 2] - 12, y0 + d + 6), y0 + d + 20)
                    spots += [(x, y) for x in self._spread(by_side.get(side, []), k, x0, w, 0)]
            for side in ("left", "right"):
                k = sides[side]
                if k:
                    x = x0 - 16 if side == "left" else x0 + w + 16
                    # along a side running away from the camera a chair's place is a
                    # depth — shaky in a photo: evenly spaced, as a table's chairs are
                    spots += [(x, y0 + d * (j + 0.5) / k) for j in range(k)]
        if not spots and not over:
            return
        meeting = s.room_type in MEETING_ROOMS
        if meeting:
            cv = self.pick_chairs(len(spots) + over)
            line = next((ln for ln in self.lines if ln.product is cv), None)
            if line:
                line.why = f"at the table · {why('chair', n_chairs)}"
        else:
            cv = self.add(self.choose(self.visitor_chairs(), s.chair_color or s.color),
                          len(spots) + over, f"at the table · {why('chair', n_chairs)}",
                          chair=True)
        inside = [(px, py) for px, py in spots
                  if 12 <= px <= room.L - 12 and 12 <= py <= room.W - 12]
        self.miss("chair", len(spots) - len(inside))
        for px, py in inside:
            room.put(Item("chair", "", px - 12, py - 12, 24, 24, "circle",
                          (cv or {}).get("color_hex", "#333333"), product=cv,
                          face=_toward(px, py, cx, cy)))
            self.seat_xy.append((px, py))
        if t.lead.get("seats", 0) < total:
            self.tight.append(f"{total} chairs at a table that seats {t.lead.get('seats')}")
            self.warnings.append(f"The largest table that fits seats "
                                 f"{t.lead.get('seats')}; there are {total} chairs "
                                 f"round it.")

    def _replica_wall(self, kind: str, it: dict[str, Any] | None,
                      used: dict[str, list[tuple[float, float]]], why: str,
                      first: bool) -> None:
        """A TV or whiteboard on the wall the photo shows it on."""
        room, r = self.room, self.r
        if it and it.get("wall"):
            wall = it["wall"]
        elif kind == "whiteboard" and used.get("top"):
            wall = "left"
        else:
            wall = "top"
        length = room.L if wall == "top" else room.W
        meas = self._on_wall(it, wall)          # (centre along the wall, width, centre height)
        if kind == "tv":
            far = max((math.hypot(x - room.L / 2, y) if wall == "top" else
                       x if wall == "left" else room.L - x) for x, y in self.seat_xy) \
                if self.seat_xy else 120
            tvs = sorted(self.options("tv", tagged=False), key=lambda b: b.lead["screen_in"])
            if not tvs:
                return
            want = far / r["tv_viewing_ratio"]
            base = next((b for b in tvs if b.lead["screen_in"] >= want - 3), tvs[-1])
            if meas:                            # the size the photo shows
                base = min(tvs, key=lambda b: abs(b.dims["w"] - meas[1]))
            label, colour, z = f"TV {base.lead['screen_in']}″", "#111111", 44
            why += (f" · {base.lead['screen_in']}″, about the size in your photo" if meas else
                    f" · {base.lead['screen_in']}″ suits the farthest seat")
        else:
            boards = [b for b in self.options("whiteboard", tagged=False)
                      if b.dims["d"] <= 2 and b.dims["w"] <= length *
                      r["whiteboard_wall_fraction"]]
            if not boards:
                self.notes.append("No wall wide enough for the whiteboard.")
                return
            base = (min(boards, key=lambda b: abs(b.dims["w"] - meas[1])) if meas else
                    max(boards, key=lambda b: b.dims["w"]))
            label, colour, z = f"Whiteboard {fmt_len(base.dims['w'])}", "#f4f4f4", 34
        v = self.add(base, 1, why)
        ww = base.dims["w"]
        u = (it or {}).get("u", 0.5) if wall == "top" else (it or {}).get("v", 0.5)
        lo, hi = room.m, length - room.m - ww
        if hi < lo:
            self.notes.append(f"The {label.split(' ')[0].lower()} is wider than the wall — "
                              f"it's still on the list.")
            return
        start = min(max((meas[0] if meas else u * length) - ww / 2, lo), hi)
        if meas:                                # hung at the height the photo shows
            h = base.dims.get("h", 30)
            z = min(max(meas[2] - h / 2, 24.0), 96.0 - h)
        taken = used.setdefault(wall, [])
        for step in [0] + [k * sgn for k in range(6, int(length), 6) for sgn in (1, -1)]:
            a = start + step
            if lo <= a <= hi and all(a + ww <= b0 or a >= b1 for b0, b1 in taken):
                start = a
                break
        taken.append((start, start + ww))
        if wall == "top":
            item = Item(kind, label, start, 0, ww, 4, "wall", colour, product=v, face=DOWN, z=z)
        elif wall == "left":
            item = Item(kind, label, 0, start, 4, ww, "wall", colour, product=v, face=RIGHT, z=z)
        else:
            item = Item(kind, label, room.L - 4, start, 4, ww, "wall", colour, product=v,
                        face=LEFT, z=z)
        room.put(item)
        if kind == "tv" and first:
            self.tv_item = item

    def _on_wall(self, it: dict[str, Any] | None, wall: str
                 ) -> tuple[float, float, float] | None:
        """A TV or board in the photo, measured on its wall through the photo's
        camera: (centre along the wall on the plan, width, centre height)."""
        cam, box = self.cam, (it or {}).get("box")
        if not cam or not box or wall not in ("top", "left", "right"):
            return None
        room = self.room
        x1, y1, x2, y2 = box
        vm = (y1 + y2) / 2
        a = cam.on_wall(x1, vm, wall, room.L, room.W)
        b = cam.on_wall(x2, vm, wall, room.L, room.W)
        c = cam.on_wall((x1 + x2) / 2, vm, wall, room.L, room.W)
        if not a or not b or not c:
            return None
        width = abs(b[0] - a[0])
        if width < 12:
            return None
        along = c[0] if wall == "top" else room.W - c[0]       # world Y -> plan y
        return along, width, c[1]

    def pick(self, kind: str, **kw) -> Base | None:
        opts = self.options(kind, **kw)
        return self.choose(opts, self.s.color) if opts else None

    def furnishings(self) -> None:
        s, r = self.s, self.r
        n = len(self.seat_xy) or s.people
        work = s.room_type not in MEETING_ROOMS
        home = s.room_type in ("home office", "bedroom office")
        small_bin = lambda b: "Small" in b.lead["base_name"]  # noqa: E731
        if work:
            if s.room_type == "executive suite":
                self.place(self.pick("storage", where=lambda b: "Bookcase" in b.lead["base_name"]),
                           1, "shelving on a free wall", "Bookcase")
                if not (self.desk or {}).get("u_shaped"):
                    self.place(self.pick("storage", tagged=False,
                                         where=lambda b: "Credenza" in b.lead["base_name"]),
                               1, "a credenza behind the desk", "Credenza",
                               walls=("top", "left", "right"))
            elif s.room_type == "open office":
                self.place(self.pick("storage", where=lambda b: "File" in b.lead["base_name"]),
                           max(1, math.ceil(n / 4)), "1 file cabinet per 4 people", "Files")
            else:
                self.place(self.pick("storage", where=lambda b: "Bookcase" in b.lead["base_name"]),
                           1, "shelving for a wall that's free", "Bookcase")
                self.place(self.pick("storage", where=lambda b: "File" in b.lead["base_name"]),
                           1, "files beside the desk", "Files")
            self.printing(n)
            per_strip = 2 if any("Bench" in line.product.get("base_name", "") for line in
                                 self.lines) else 1
            self.add(self.pick("power_strip"), math.ceil(n / per_strip),
                     "1 surge protector per desk" if per_strip == 1 else
                     "1 surge protector per 2 seats")
            self.add(self.pick("organizer"), self.desks if self.desks and per_strip == 1
                     else n, "1 per desk")
            self.add(self.pick("pens"), math.ceil(n / 4), "1 pack of 12 per 4 people")
            self.add(self.pick("sticky_notes"), math.ceil(n / 4), "1 pack of 12 per 4 people")
            self.add(self.pick("stapler"), math.ceil(n / 6), "1 per 6 people")
            self.add(self.pick("permanent_marker"), math.ceil(n / 12), "1 pack of 12 for "
                     "the room" if n <= 12 else "1 pack of 12 per 12 people")
            open_plan = s.room_type == "open office"
            seats_n = n if per_strip == 2 else max(1, self.desks)    # benches seat several
            if home or s.room_type in ("private office", "executive suite"):
                lamp = self.pick("lamp", tagged=False,
                                 where=lambda b: "Desk" in b.lead["base_name"])
                self.add(lamp, max(1, self.desks), "task light on each desk")
            elif open_plan:
                lamp = self.pick("lamp", where=lambda b: "Clamp" in b.lead["base_name"])
                self.add(lamp, seats_n, "clamp-on task light at each desk")
            if s.monitors:
                compact = (lambda b: (b.lead.get("screen_in") or 99) <= 24) if open_plan \
                    else (lambda b: True)
                screen = (self.pick("monitor", tagged=False,      # two ultrawides won't fit
                                    where=lambda b: compact(b) and (
                                        s.monitors == 1 or not b.lead.get("ultrawide")))
                          or self.pick("monitor", tagged=False))
                self.add(screen, s.monitors * seats_n,
                         f"{s.monitors} per desk" + (" — you use two" if s.monitors == 2
                                                     else "")
                         + (" · compact screens for cubicle desks" if open_plan else ""))
                if open_plan and s.monitors >= 2:
                    self.add(self.pick("monitor_arm", tagged=False), seats_n,
                             "1 dual arm per desk — frees the desk surface")
            if s.carpet:
                chairs = sum(line.qty for line in self.lines if line.product["kind"] == "chair"
                             and line.product.get("use") not in ("guest", "meeting"))
                self.add(self.pick("chair_mat"), chairs, "carpeted floor: 1 under each "
                         "rolling chair")
            if s.decor and home:
                if not s.carpet:
                    rug = self.pick("rug", where=lambda b: not b.lead.get("round"))
                    if rug and self.place_under(rug, "Rug"):
                        self.add(rug, 1, "softens a bare floor")
                self.place(self.pick("lamp", where=lambda b: "Floor" in b.lead["base_name"]),
                           1, "ambient light for the room", "Lamp", access=0)
        else:
            strips = 1 if s.room_type == "huddle room" else max(1, math.ceil(n / 8))
            self.add(self.pick("power_strip", tagged=False), strips,
                     "power at the table" + (" · 1 per 8 seats" if strips > 1 else ""))
            self.add(self.pick("sticky_notes", tagged=False), 1, "for meetings")
            self.add(self.pick("permanent_marker", tagged=False), 1,
                     "sharpies for name tents and flip charts")
            if s.room_type == "boardroom":
                back = ("right", "bottom") if s.display_wall != "Wide wall" else ("bottom",
                                                                                 "right")
                self.place(self.pick("storage", tagged=False,
                                     where=lambda b: "Credenza" in b.lead["base_name"]),
                           1, "at the back of the room: storage and a catering station",
                           "Credenza", walls=back)
        if s.room_type in MEETING_ROOMS or s.room_type == "executive suite":
            bins = 2 if s.room_type == "boardroom" else 1
            self.place(self.pick("wastebasket", tagged=False, where=small_bin)
                       or self.pick("wastebasket", tagged=False), bins,
                       "small bins, one at each end" if bins > 1 else "a small bin",
                       "Bin", access=0)
        else:
            self.place(self.pick("wastebasket", where=lambda b: not small_bin(b))
                       or self.pick("wastebasket"),
                       max(1, math.ceil(n / r["people_per_bin"])),
                       f"1 per {r['people_per_bin']} people", "Bin", access=0)

    def printing(self, n: int) -> None:
        s, r = self.s, self.r
        printers: dict[str, Base] = {}
        for b in self.options("printer", tagged=False):       # cheapest of each type
            key = ("inkjet" if "Inkjet" in b.lead["base_name"] else
                   "color" if "Color" in b.lead["base_name"] else "mono")
            printers.setdefault(key, b)
        if s.printer == "inkjet":
            want, why = "inkjet", "an inkjet all-in-one is enough, as you said"
        elif s.printer == "color":
            want, why = "color", "you print a lot in colour"
        elif n <= 2:
            want, why = "inkjet", f"an all-in-one suits {n} {'person' if n == 1 else 'people'}"
        elif n <= 5:
            want, why = "mono", f"a laser keeps up with {n} people"
        else:
            want, why = "color", f"a colour multifunction printer for {n} people"
        printer = printers.get(want) or next(iter(printers.values()), None)
        if not printer:
            return
        count = math.ceil(n / r["people_per_printer"])
        self.place(printer, count, why + (f" · 1 per {r['people_per_printer']} people"
                                          if count > 1 else ""), "Printer", access=30)
        fits = self.options("cartridge", tagged=False,
                            where=lambda b: printer.base_id in
                            b.lead.get("compatible_printers", []))
        xl = s.tier == "Premium"              # Premium: high-yield cartridges
        for role in ("black", "color"):
            opts = [b for b in fits if b.lead.get("role") == role
                    and not b.lead.get("compatible")]
            pick = (next((b for b in opts if bool(b.lead.get("xl")) == xl), None)
                    or next(iter(opts), None))
            if pick:
                self.add(pick, 2 * count, "fits this printer · one in, one spare"
                         + (" · high yield" if pick.lead.get("xl") else ""))
        reams = math.ceil(n * r["reams_per_person_month"] * r["paper_months"])
        papers = sorted(self.options("paper", tagged=False,       # everyday copy paper
                                     where=lambda b: b.lead.get("paper_use", "copy") == "copy"
                                     and (b.lead.get("grade") == "premium") ==
                                     (s.tier == "Premium")),
                        key=lambda b: b.lead["price"]) or sorted(
            self.options("paper", tagged=False), key=lambda b: b.lead["price"])
        case = next((b for b in papers if "Case" in b.lead["base_name"]), None)
        ream = next((b for b in papers if "Case" not in b.lead["base_name"]), None)
        months = r["paper_months"]
        if reams >= 5 and case:
            self.add(case, math.ceil(reams / 10), f"{reams} reams = {months} months of "
                     f"paper for {n} people")
        elif ream:
            self.add(ream, reams, f"{months} months of paper for {n} "
                     f"{'person' if n == 1 else 'people'}")

    def place(self, base: Base | None, qty: int, why: str, label: str,
              access: float = 24,
              walls: tuple[str, ...] = ("bottom", "right", "left", "top")) -> None:
        if not base or qty <= 0:
            return
        v = self.add(base, qty, why)
        dims = base.dims
        for _ in range(qty):
            it = self.room.along_walls(base.lead["kind"], label, max(dims["w"], 12),
                                       max(dims["d"], 12),
                                       (v or {}).get("color_hex", "#999999"), access, walls)
            if not it and max(dims["w"], dims["d"]) <= 24:
                it = self.room.corner(base.lead["kind"], label, max(dims["w"], 12),
                                      max(dims["d"], 12), (v or {}).get("color_hex", "#999999"))
            if it:
                it.product = v
                qty -= 1
            else:
                noun = base.lead.get("base_name", label).split(",")[0].lower()
                self.notes.append(f"No free wall space left to draw the {noun} — "
                                  f"it's still on the list.")
                if self.s.replicate is not None:
                    self.miss(base.lead["kind"], qty)
                break

    def place_under(self, base: Base, label: str) -> bool:
        """A rug goes in the open floor, under nothing that blocks a walkway."""
        room = self.room
        w, d = base.dims["w"], base.dims["d"]
        for rw, rd in ((d, w), (w, d)):
            x, y = (room.L - rw) / 2, room.W - room.m - rd - 6
            if x > room.m and y > room.m:
                v = base.variant(self.s.color)
                room.items.insert(1, Item("rug", label, x, y, rw, rd, "under",
                                          v.get("color_hex", "#d8cfc4"), product=v,
                                          face=DOWN if rw < rd else RIGHT))
                return True
        return False

    def build(self) -> None:
        rt = self.s.room_type
        if self.s.replicate is not None:
            self.replicate()
        elif rt in ("conference room", "boardroom"):
            self.conference()
        elif rt == "huddle room":
            self.huddle()
        elif rt == "executive suite":
            self.executive()
        else:
            self.workstations()
        if self.s.replicate is None:
            self.wall_display()
            self.av_kit()
            self.furnishings()
        by_kind = {}
        for line in self.lines:
            by_kind.setdefault(line.product["kind"], line.product)
        for i in self.room.items:             # monitors, mats: the product on the list
            if i.product is None and i.shape != "zone":
                i.product = by_kind.get("conference_table" if i.kind == "table" else i.kind)
        order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
        self.lines.sort(key=lambda line: (order.get(line.group, 9),
                                          -line.product["price"] * line.qty))


def plan(spec: RoomSpec, *, customer_id: str | None = None, session_id: str | None = None,
         trace: Trace | None = None, save: bool = True) -> Plan:
    spec.defaults()
    d = governance.authorize("plan_room", {"room_type": spec.room_type,
                                           "people": spec.people},
                             customer_id=customer_id, session_id=session_id, trace=trace)
    if not d.allowed:
        raise PermissionError(d.message or d.policy)
    rules = _rules(trace)
    cands = _candidates(trace)
    t0 = time.perf_counter()
    b = _Builder(spec, rules, cands)
    b.build()
    # a budget we remember: over it on the usual picks, swap in lower-priced ones
    fit = None
    if spec.budget and spec.tier == "Recommended":
        spec, b, fit = _fit_budget(spec, rules, cands, b)
    to_budget = bool(fit and fit["cheaper"])
    p = Plan(spec, b.room.L, b.room.W, b.room.items, b.lines, len(b.seat_xy), b.capacity,
             b.warnings, b.notes, table=b.table, visitors=b.visitors,
             guide=rules.get("room_types", {}).get(spec.room_type, {}), desk=b.desk)
    p.unplaced, p.tight = dict(b.unplaced), list(b.tight)
    p.to_budget, p.budget_fit = to_budget, fit
    if fit and trace:
        trace.add("App logic", "Fitted the plan to the budget we remember",
                  budget_words(p) or "", "one kind at a time: the biggest saving for the "
                  "least drop in rating, then best-rated back where the budget allows",
                  fit["ms"], [])
    p.crowded = crowding(b.room, spec, b.unplaced)
    p.cam = b.cam.params() if b.cam else None
    if spec.replicate is not None and p.unplaced:
        p.roomier = _roomier(spec, rules, cands)
    if spec.replicate is not None:
        p.suggestions = _suggestions(spec, rules, cands, b.lines)
        if trace:
            trace.add("App logic", "Copied the room in your photo — only what it shows, "
                      "where it shows it", replica_words(spec.replicate)
                      + f"; {len(p.suggestions)} more ideas offered, not added",
                      "photo layout → room rules in inches", (time.perf_counter() - t0) * 1000)
    elif trace:
        trace.add("App logic", f"Laid out a {fmt_len(p.length)} × {fmt_len(p.width)} "
                  f"{ROOM_LABELS.get(spec.room_type, spec.room_type).split(' (')[0].lower()} "
                  f"for {spec.people}",
                  (f"{p.seats} seat{'s fit' if p.seats != 1 else ' fits'}" if p.fits else
                   f"Only {p.seats} of {spec.people} fit") +
                  f"; {sum(line.qty for line in p.lines)} items on the list",
                  "room rules applied in inches: chair space, walkways, door swing, "
                  "wall space", (time.perf_counter() - t0) * 1000)
    reprice(p, trace)
    if save:
        save_plan(p, customer_id, trace)
    return p


CORE_KINDS = ("chair", "conference_table", "desk")
COVER_OK = 0.45          # furniture on more of the floor than this leaves little room to walk
SQFT_PER_SEAT = 20       # a meeting room below this per chair is packed (25+ is comfortable)
ONE_PERSON = ("executive suite",)
CUBE_W, CUBE_D = 72, 96         # a cubicle: 6′ wide, 8′ deep
PANEL_T, PANEL_H, PANEL_HEX = 2, 53, "#c3c8ce"


def crowding(room: "Room", spec: RoomSpec, unplaced: dict[str, int] | None = None
             ) -> list[str]:
    """Why a room is getting congested, in plain words — empty when it isn't:
    furniture past the walls or on top of other furniture, too much of the floor
    covered, too many chairs for a meeting room's floor."""
    solid = [i for i in room.solids if i.kind not in ("rug", "door", "panel")]
    why = []
    out = [i for i in solid if not room.inside(i)]
    if out:
        names = sorted({REPLICA_NAMES.get("conference_table" if i.kind == "table" else i.kind,
                                          (i.kind.replace("_", " "),))[0] for i in out})
        why.append(f"{len(out)} piece{'s' if len(out) > 1 else ''} ({', '.join(names)}) "
                   f"run{'s' if len(out) == 1 else ''} past the walls")
    seat = ("table", "desk", "conference_table")
    clash = sum(1 for n, a in enumerate(solid) for b in solid[n + 1:] if _overlap(a, b)
                and not ({a.kind, b.kind} & {"chair"} and {a.kind, b.kind} & set(seat)))
    if clash:
        why.append(f"{clash} piece{'s' if clash > 1 else ''} of furniture overlap")
    floor = room.L * room.W
    cover = sum(i.w * i.d for i in solid) / floor if floor else 0
    if round(cover, 2) > COVER_OK:
        why.append(f"furniture covers {cover:.0%} of the floor (under "
                   f"{COVER_OK:.0%} leaves room to walk)")
    chairs = (sum(1 for i in solid if i.kind == "chair" and room.inside(i))
              + (unplaced or {}).get("chair", 0))          # wanted, if not drawn
    if spec.room_type in MEETING_ROOMS and chairs:
        per = floor / 144 / chairs
        if per < SQFT_PER_SEAT:
            why.append(f"{chairs} chairs leave {per:.0f} sq ft each (meeting rooms want "
                       f"about 25)")
    return why


SHARED_STRIP = 36       # along the door wall: files, printer and bins for the team


def cubicle_room(n: int) -> tuple[float, float]:
    """The floor (length, width in inches, whole feet) that holds n cubicles of
    6′ × 8′: rows off walkways, a cross aisle, and a strip by the door for what
    the team shares — the arrangement closest to square."""
    r = _rules(None)
    m, walk = r["wall_margin_in"], r["walkway_in"]
    best = None
    for rows in range(1, max(n, 1) + 1):
        per = math.ceil(n / rows)
        L = per * CUBE_W + walk + 2 * m
        W = rows * CUBE_D + math.ceil(rows / 2) * walk + 2 * m + SHARED_STRIP
        L, W = math.ceil(L / 12) * 12, math.ceil(W / 12) * 12
        key = (L * W, max(L, W) / min(L, W))
        if best is None or key < best[0]:
            best = (key, (float(L), float(W)))
    return best[1]


def max_people(spec: RoomSpec) -> int:
    """The most people this room type can seat at this size — where the People
    box stops, since past it the plan can't add another seat."""
    if spec.room_type in ONE_PERSON:
        return 1
    big = replace(spec, people=99, replicate=None).defaults()
    b = _Builder(big, _rules(None), _candidates(None))
    b.build()
    inside = sum(1 for x, y in b.seat_xy if 0 <= x <= b.room.L and 0 <= y <= b.room.W)
    return max(2 if spec.room_type in MEETING_ROOMS else 1, inside)


def _crowded(b: "_Builder", core_only: bool) -> bool:
    if any(k in CORE_KINDS or not core_only for k in b.unplaced) or b.reserve_missed:
        return True
    return bool(b.table) and (b.table or {}).get("seats", 0) < b.at_table


def _roomier(spec: RoomSpec, rules: dict[str, Any], cands: dict[str, list[Base]],
             core_only: bool = False) -> tuple[float, float] | None:
    """The smallest step up in room size (a foot each way, up to 8) that draws
    everything — or, core_only, just the tables, desks and chairs, with a
    table that seats everyone at it."""
    for k in range(1, 9):
        bigger = replace(spec, length_in=spec.length_in + 12 * k,
                         width_in=spec.width_in + 12 * k)
        b = _Builder(bigger, rules, cands)
        b.build()
        if not _crowded(b, core_only):
            return b.room.L, b.room.W
    return None


def replica_size(spec: RoomSpec) -> tuple[float, float] | None:
    """For a photo being copied: a room size (inches, longer side first) that
    takes the photo's own tables, desks and chairs — None if the given size
    already does. A size read from a photo is a guess; the furniture is not."""
    spec.defaults()
    rules, cands = _rules(None), _candidates(None)
    b = _Builder(spec, rules, cands)
    b.build()
    L, W = b.room.L, b.room.W
    if _crowded(b, True):
        got = _roomier(spec, rules, cands, core_only=True)
        if got:
            L, W = (max(got), min(got)) if L >= W else (min(got), max(got))
    # the table as big as the photo shows it, with room to walk round it
    seen = next((i for i in (spec.replicate or {}).get("items") or []
                 if i.get("kind") == "conference_table"), None)
    foot = b._footprint(seen)
    if foot:                    # its depth is only a hint: the width across decides
        L = min(max(L, foot[2] + 72), b.room.L + 96)
    if (L, W) == (b.room.L, b.room.W):
        return None
    return max(L, W), min(L, W)


def _suggestions(spec: RoomSpec, rules: dict[str, Any], cands: dict[str, list[Base]],
                 have: list[Line]) -> list[Line]:
    """What our usual plan for this room type adds that the photo doesn't have."""
    usual = replace(spec, replicate=None, tv=None, whiteboard=None, decor=None,
                    meeting_table=None, video_calls=None,
                    people=max(1, spec.people)).defaults()
    b = _Builder(usual, rules, cands)
    b.build()
    kinds = {line.product["kind"] for line in have}
    skip = {"chair", "desk", "conference_table"}       # the room's own furniture: edit counts
    out = []
    for line in b.lines:
        k = line.product["kind"]
        if k in kinds or k in skip:
            continue
        out.append(Line(line.product, line.qty, line.why))
    return out


# what can go from a plan and leave the room working
OPTIONAL = {"whiteboard", "rug", "lamp", "video_bar", "speaker", "camera", "side_table",
            "storage", "printer", "monitor_arm", "monitor_riser"}


def _fit_budget(spec: RoomSpec, rules: dict[str, Any], cands: dict[str, list[Base]],
                b: "_Builder") -> tuple[RoomSpec, "_Builder", dict[str, Any] | None]:
    """Bring a plan under the budget one kind of product at a time.

    Each round tries the lowest-priced pick for every kind still on its
    best-rated one, and keeps the swap that saves the most for the least drop in
    rating. Once it fits, swaps are undone — smallest saving first — while it
    still fits, so as much as the budget allows stays best-rated. If even the
    lowest-priced picks are over, the optional lines to untick are suggested;
    nothing is taken off the list. Builds take a millisecond or two each.
    """
    t0 = time.perf_counter()
    offs = commerce.active_offers()

    def total(bb: _Builder, skip: frozenset[str] = frozenset()) -> float:
        return offers.price_cart([{"product": ln.product, "qty": ln.qty} for ln in bb.lines
                                  if ln.product["sku"] not in skip], offs)["total"]

    def build(tiers: dict[str, str]) -> tuple[RoomSpec, _Builder]:
        s2 = replace(spec, tiers=dict(tiers))
        bb = _Builder(s2, rules, cands)
        bb.build()
        return s2, bb

    def rating(bb: _Builder, kind: str) -> float:
        ls = [ln for ln in bb.lines if ln.product["kind"] == kind]
        n = sum(ln.qty for ln in ls) or 1
        return sum(float(ln.product.get("rating") or 0) * ln.qty for ln in ls) / n

    budget = float(spec.budget)
    full = total(b)
    if full <= budget:
        return spec, b, None
    cur_s, cur_b, cur_t = spec, b, full
    tiers: dict[str, str] = {}
    saved: dict[str, float] = {}
    tried = set()                                  # kinds a lower price doesn't change
    while cur_t > budget:
        best = None
        for kind in sorted({ln.product["kind"] for ln in cur_b.lines} - set(tiers) - tried):
            s2, b2 = build({**tiers, kind: "Value"})
            t2 = total(b2)
            if cur_t - t2 < 0.01:
                tried.add(kind)
                continue
            score = (cur_t - t2) / max(0.1, rating(cur_b, kind) - rating(b2, kind))
            if best is None or score > best[0]:
                best = (score, kind, s2, b2, t2)
        if not best:
            break
        _, kind, cur_s, cur_b, t2 = best
        tiers[kind], saved[kind], cur_t = "Value", cur_t - t2, t2
    if cur_t <= budget:                            # best-rated back where it still fits
        for kind in sorted(tiers, key=lambda k: saved[k]):
            s2, b2 = build({k: v for k, v in tiers.items() if k != kind})
            t2 = total(b2)
            if t2 <= budget:
                cur_s, cur_b, cur_t = s2, b2, t2
                del tiers[kind]
    drop: list[tuple[str, float]] = []
    fits_after = False
    if cur_t > budget:                             # still over: what could go
        skip: set[str] = set()
        for ln in sorted((ln for ln in cur_b.lines if ln.product["kind"] in OPTIONAL),
                         key=lambda ln: -ln.product["price"] * ln.qty):
            skip.add(ln.product["sku"])
            drop.append((ln.product.get("base_name") or ln.product["name"],
                         round(ln.product["price"] * ln.qty, 2)))
            if total(cur_b, frozenset(skip)) <= budget:
                fits_after = True
                break
    if not tiers:                                  # nothing cheaper helped: as it was
        cur_s, cur_b, cur_t = spec, b, full
    return cur_s, cur_b, {
        "cheaper": sorted(tiers, key=lambda k: -saved.get(k, 0)),
        "each": {k: round(saved[k], 2) for k in tiers},
        "saved": round(full - cur_t, 2), "total": round(cur_t, 2), "budget": budget,
        "over": round(max(0.0, cur_t - budget), 2), "drop": drop,
        "fits_after_drop": fits_after,
        "ms": (time.perf_counter() - t0) * 1000}


def kind_words(kinds: list[str]) -> str:
    """["chair", "storage"] -> "chairs and cabinets and shelves"."""
    names = [{"storage": "storage"}.get(k) or REPLICA_NAMES.get(k, (k, k.replace("_", " ") + "s"))[1]
             for k in kinds]
    return (", ".join(names[:-1]) + " and " + names[-1]) if len(names) > 1 else \
        (names[0] if names else "")


def budget_words(p: Plan) -> str | None:
    """What fitting the budget did, in a sentence — for the planner and the chat."""
    f = p.budget_fit
    if not f:
        return None
    out = []
    if f["cheaper"]:
        big = [k for k in f["cheaper"] if f["each"].get(k, 0) >= max(10.0, f["saved"] * 0.05)]
        rest = len(f["cheaper"]) - len(big)
        what = (f"lower-priced {kind_words(big)}" + (" and a few small things" if rest else "")
                if big else "a few lower-priced small things")
        out.append(f"To keep to your ${f['budget']:,.0f} budget we picked {what} "
                   f"(saves ${f['saved']:,.0f}) and kept the rest best-rated.")
    if f["over"] > 0:
        names = [f"{n} (${c:,.0f})" for n, c in f["drop"]]
        what = (", ".join(names[:-1]) + " and " + names[-1]) if 1 < len(names) <= 3 else \
            (", ".join(names[:3]) + f" and {len(names) - 3} more" if names[3:] else
             names[0] if names else "")
        out.append(f"It's still ${f['over']:,.0f} over" +
                   (" with the lowest-priced picks" if f["cheaper"] else "") +
                   (f" — unticking {what} would bring it under." if f["fits_after_drop"] else
                    f" — unticking {what} gets closer." if what else "."))
    return " ".join(out) or None


def reprice(p: Plan, trace: Trace | None = None,
            lines: list[Line] | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    use = p.lines if lines is None else lines
    p.pricing = offers.price_cart([{"product": line.product, "qty": line.qty} for line in use],
                                  commerce.active_offers())
    if trace:
        saved = ", ".join(f"{s['name']} (−${s['amount']:,.2f})"
                          for s in p.pricing["savings"]) or "no bundle applies"
        trace.add("App logic", "Priced the list with today's bundle offers", saved,
                  "offer engine over catalog.offers (cached 30 s)",
                  (time.perf_counter() - t0) * 1000, ["catalog.offers"])
    return p.pricing


def save_plan(p: Plan, customer_id: str | None, trace: Trace | None = None) -> str:
    t0 = time.perf_counter()
    p.plan_id = f"PLAN-{datetime.now(UTC):%y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    s = p.spec
    kv_upsert("planning", "room_plans", f"PLAN::{p.plan_id}", {
        "type": "room_plan", "plan_id": p.plan_id, "customer_id": customer_id,
        "created_at": datetime.now(UTC).isoformat(),
        "room": {"type": s.room_type, "length_in": round(p.length, 1),
                 "width_in": round(p.width, 1), "people": s.people, "tier": s.tier,
                 "color": s.color, "carpet": s.carpet},
        "seats": p.seats, "capacity": p.capacity, "warnings": p.warnings,
        "mode": "replicate" if s.replicate is not None else "suggest",
        "lines": [{"sku": line.product["sku"], "name": line.product["name"],
                   "qty": line.qty, "unit_price": line.product["price"], "why": line.why}
                  for line in p.lines],
        "total": p.pricing.get("total"), "savings": p.pricing.get("discount")})
    if trace:
        trace.add("Data (KV)", "Saved the plan so you (or the assistant) can come back to it",
                  p.plan_id, f"KV upsert, key PLAN::{p.plan_id}",
                  (time.perf_counter() - t0) * 1000, writes=["planning.room_plans"])
    return p.plan_id


# --------------------------------------------------------------------------- #
# the floor plan
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text_on(hex_: str) -> str:
    h = hex_.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#222"
    return "#fff" if 0.299 * r + 0.587 * g + 0.114 * b < 140 else "#222"


def floor_plan_svg(p: Plan, units: str = "ft", max_w: int = 680, max_h: int = 460) -> str:
    L, W = p.length, p.width
    sc = min(max_w / L, max_h / W)
    ox, oy = 52, 30
    vw, vh = ox + L * sc + 16, oy + W * sc + 44

    def X(v: float) -> float:
        return ox + v * sc

    def Y(v: float) -> float:
        return oy + v * sc

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw:.0f} {vh:.0f}" '
         f'style="width:100%;max-width:{vw:.0f}px;font-family:Arial,sans-serif">',
         f'<rect x="{X(0)}" y="{Y(0)}" width="{L * sc}" height="{W * sc}" fill="#fbfaf6"/>']
    step = 12 if units == "ft" else IN_PER_M / 2
    v = step
    while v < L:
        o.append(f'<line x1="{X(v)}" y1="{Y(0)}" x2="{X(v)}" y2="{Y(W)}" stroke="#ece9e1" '
                 f'stroke-width="1"/>')
        v += step
    v = step
    while v < W:
        o.append(f'<line x1="{X(0)}" y1="{Y(v)}" x2="{X(L)}" y2="{Y(v)}" stroke="#ece9e1" '
                 f'stroke-width="1"/>')
        v += step
    for i in p.items:                         # clearances first, faint
        if i.shape == "zone" and i.kind in ("chair", "access"):
            o.append(f'<rect x="{X(i.x)}" y="{Y(i.y)}" width="{i.w * sc}" height="{i.d * sc}" '
                     f'fill="#e3f1e3" opacity="0.7"/>')
    for i in p.items:
        if i.shape == "under" and i.kind != "monitor":
            op = "0.55" if i.kind in ("rug", "chair_mat") else "1"
            o.append(f'<rect x="{X(i.x)}" y="{Y(i.y)}" width="{i.w * sc}" height="{i.d * sc}" '
                     f'rx="{2 if i.kind != "monitor" else 1}" fill="{i.color}" opacity="{op}" '
                     f'stroke="#9aa" stroke-width="0.5"/>')
    labels = []
    for i in p.items:
        if i.shape == "rect":
            o.append(f'<rect x="{X(i.x)}" y="{Y(i.y)}" width="{i.w * sc}" height="{i.d * sc}" '
                     f'rx="2" fill="{i.color}" stroke="#555" stroke-width="1"/>')
        elif i.shape == "circle":
            o.append(f'<ellipse cx="{X(i.x + i.w / 2)}" cy="{Y(i.y + i.d / 2)}" '
                     f'rx="{i.w * sc / 2}" ry="{i.d * sc / 2}" fill="{i.color}" '
                     f'stroke="#444" stroke-width="1"/>')
        else:
            continue
        need = len(i.label) * 6.3 + 4
        if i.label and i.w * sc >= need:
            labels.append((X(i.x + i.w / 2), Y(i.y + i.d / 2) + 4, i.label,
                           _text_on(i.color), 0))
        elif i.label and i.d * sc >= need:
            labels.append((X(i.x + i.w / 2) + 4, Y(i.y + i.d / 2), i.label,
                           _text_on(i.color), -90))
    for i in p.items:                         # speakerphones sit on the table
        if i.shape == "top":
            o.append(f'<circle cx="{X(i.x + i.w / 2)}" cy="{Y(i.y + i.d / 2)}" '
                     f'r="{max(i.w * sc / 2, 3)}" fill="#1b1c1e" stroke="#888" '
                     f'stroke-width="0.5"/>')
    for i in p.items:                         # monitors sit on the desks
        if i.kind == "monitor":
            o.append(f'<rect x="{X(i.x)}" y="{Y(i.y)}" width="{i.w * sc}" height="{i.d * sc}" '
                     f'rx="1" fill="#222" stroke="#888" stroke-width="0.5"/>')
    for i in p.items:
        if i.shape == "under" and i.kind == "rug":
            labels.append((X(i.x + i.w / 2), Y(i.y + i.d - 6), "Rug", "#555", 0))
    wall = 5
    o.append(f'<rect x="{X(0)}" y="{Y(0)}" width="{L * sc}" height="{W * sc}" fill="none" '
             f'stroke="#333" stroke-width="{wall}"/>')
    dx, dw = p.items[0].x, p.items[0].w
    o.append(f'<line x1="{X(dx)}" y1="{Y(W)}" x2="{X(dx + dw)}" y2="{Y(W)}" stroke="#fbfaf6" '
             f'stroke-width="{wall + 1}"/>')
    o.append(f'<path d="M {X(dx + dw)} {Y(W)} L {X(dx + dw)} {Y(W - dw)} A {dw * sc} {dw * sc} '
             f'0 0 0 {X(dx)} {Y(W)}" fill="none" stroke="#999" stroke-width="1" '
             f'stroke-dasharray="3 2"/>')
    labels.append((X(dx + dw / 2), Y(W) + 14, "Door", "#666", 0))
    for i in p.items:
        if i.shape == "wall":
            col = "#fff" if i.kind == "whiteboard" else "#111"
            o.append(f'<rect x="{X(i.x)}" y="{Y(i.y)}" width="{max(i.w * sc, 6)}" '
                     f'height="{max(i.d * sc, 6)}" fill="{col}" stroke="#555" stroke-width="1"/>')
            if not i.label:
                continue
            if i.kind == "tv" and i.x < 1 and i.d > i.w:
                labels.append((X(i.x) + 16, Y(i.y + i.d / 2), i.label, "#111", -90))
            elif i.kind == "whiteboard" and i.x < 1 and i.d > i.w:
                labels.append((X(i.x) + 16, Y(i.y + i.d / 2), i.label, "#333", -90))
            else:
                labels.append((X(i.x + i.w / 2), Y(i.y) + (16 if i.y < 1 else -8), i.label,
                               "#333", 0))
    for x, y, t, col, rot in labels:
        halo = "#fff" if col != "#fff" else "#333"
        turn = f' transform="rotate({rot} {x:.1f} {y:.1f})"' if rot else ""
        o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="11" text-anchor="middle" '
                 f'fill="{col}" stroke="{halo}" stroke-width="2.5" paint-order="stroke" '
                 f'stroke-opacity="0.6"{turn}>{_esc(t)}</text>')
    o.append(f'<text x="{X(L / 2)}" y="{oy - 10}" font-size="13" font-weight="bold" '
             f'text-anchor="middle" fill="#333">{fmt_len(L, units)}</text>')
    o.append(f'<text x="{ox - 14}" y="{Y(W / 2)}" font-size="13" font-weight="bold" '
             f'text-anchor="middle" fill="#333" transform="rotate(-90 {ox - 14} {Y(W / 2)})">'
             f'{fmt_len(W, units)}</text>')
    bar = 12 * 4 if units == "ft" else IN_PER_M
    by = Y(W) + 30
    o.append(f'<line x1="{X(0)}" y1="{by}" x2="{X(bar)}" y2="{by}" stroke="#333" '
             f'stroke-width="2"/><text x="{X(bar) + 6}" y="{by + 4}" font-size="11" '
             f'fill="#333">{"4 ft" if units == "ft" else "1 m"} · grid '
             f'{"1 ft" if units == "ft" else "0.5 m"}</text>')
    o.append(f'<rect x="{X(L) - 150}" y="{by - 9}" width="12" height="10" fill="#e3f1e3"/>'
             f'<text x="{X(L) - 134}" y="{by}" font-size="11" fill="#555">space to sit and '
             f'reach</text>')
    o.append("</svg>")
    return "".join(o)
