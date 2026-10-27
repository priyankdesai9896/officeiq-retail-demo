"""Room photo -> what the planner needs: room type, rough size, floor, colours,
and the furniture already there. The local vision model (qwen2.5vl) reads the
photo; the answer is checked and mapped onto the planner's own vocabulary.

Reading a photo takes about a minute on a laptop CPU, so every reading is kept
in Couchbase by the photo's fingerprint (`planning.room_plans`, key
PHOTO::<sha1>): the same photo again comes back at once.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from PIL import Image

from config import settings
from retail import agentc_bridge, governance
from retail.cb import fq, kv_get, kv_upsert, query
from retail.planner import MEETING_ROOMS, ROOM_TYPES
from retail.trace import Trace

PROMPT = """You are a workspace planner. Look at this photo of a room and reply with JSON only:
{"room_type": one of ["home office","bedroom office","private office","open office","huddle room","conference room","boardroom","executive suite"] (a room with a bed is a "bedroom office"),
 "length_ft": your estimate of the longer floor dimension in feet,
 "width_ft": your estimate of the shorter floor dimension in feet,
 "floor": "carpet" or "hard",
 "existing": [furniture and equipment already in the room, short nouns],
 "colours": [the main furniture colours, simple colour words],
 "style": "one or two words",
 "notes": "one short sentence about the space"}"""

# What the photo shows -> the product kinds the plan would otherwise buy again.
OWNED = [(r"\bdesks?\b|workstation", "desk"), (r"\bchairs?\b|stool", "chair"),
         (r"book ?(case|shel)|shel(f|ves)|cabinet|drawers?|credenza|storage", "storage"),
         (r"\blamps?\b|light", "lamp"), (r"printer", "printer"),
         (r"monitor|computer|screen|imac", "monitor"), (r"\btv\b|television|display", "tv"),
         (r"whiteboard", "whiteboard"), (r"\brug\b|carpet", "rug"),
         (r"\btables?\b", "conference_table"), (r"bin\b|trash|wastebasket", "wastebasket")]

# The catalogue's colour names for what the model calls things.
COLOUR_WORDS = {"wood": "Walnut", "wooden": "Walnut", "brown": "Walnut", "walnut": "Walnut",
                "oak": "Natural Oak", "light wood": "Natural Oak", "beige": "Natural Oak",
                "white": "White", "black": "Black", "grey": "Gray", "gray": "Gray",
                "blue": "Blue", "navy": "Navy", "green": "Green", "red": "Red",
                "espresso": "Espresso", "dark brown": "Espresso"}

MAX_SIDE = 560          # pixels: plenty for the model, and keeps the request small

LOCATE_PROMPT = (
    "Locate every piece of furniture and equipment in this room: every chair (including "
    "chairs partly hidden behind a table), tables, desks, TVs or screens, whiteboards, "
    "cabinets, shelves, bookcases, credenzas, side tables, lamps, printers, computer "
    "monitors, video bars, rugs and bins. Also box framed pictures or artwork (call them "
    "'picture'), plants, phones and people, so they are not mistaken for screens. "
    'Reply with JSON only: {"objects": [{"label": "chair", "bbox_2d": [x1, y1, x2, y2]}, ...]} '
    "with one entry per object.")

COUNT_PROMPT = ("How many chairs are in this photo? Count each one, including chairs partly "
                "hidden by a table or by other chairs. And what shape is the main table's top, "
                "seen from above? Reply with JSON only: "
                '{"chairs": N, "table": "round" or "rectangular" or "square" or "oval" or "none"}')

CHAIRS_PROMPT = (
    "Locate every chair in this room, one box per chair, including chairs partly hidden "
    'behind a table or behind other chairs. Reply with JSON only: {"objects": [{"label": '
    '"chair", "bbox_2d": [x1, y1, x2, y2]}, ...]}')

# What the model calls a thing -> the catalogue kind that replicates it. First match wins.
REPLICA = [(r"picture|painting|\bart\b|artwork|frame|poster|photo|mirror|window|plant|"
            r"person|people|woman|man\b|phone|bottle|glass|cup|laptop|tablet|ceiling", None),
           (r"video ?bar|soundbar|sound bar|webcam|camera", "video_bar"),
           (r"side ?table|end ?table|night ?stand", "side_table"),
           (r"\bdesks?\b|workstation", "desk"),
           (r"\btables?\b", "conference_table"),
           (r"chair|stool|\bseats?\b", "chair"),
           (r"monitor|computer|imac|laptop", "monitor"),
           (r"\btv\b|television|screen|display", "tv"),
           (r"whiteboard|white board", "whiteboard"),
           (r"book ?(case|shel)|shel(f|ves)|cabinet|drawers?|credenza|storage|dresser",
            "storage"),
           (r"\blamps?\b", "lamp"), (r"printer", "printer"),
           (r"\brugs?\b|carpet", "rug"), (r"\bbins?\b|trash|wastebasket", "wastebasket")]
ON_WALL = ("tv", "whiteboard")


def _native_url() -> str:
    """Ollama's native chat endpoint (it takes images directly)."""
    return re.sub(r"/v1/?$", "", settings.model_endpoint) + "/api/chat"


def fingerprint(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()


def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        v = re.split(r",|;| and ", v)
    return [str(x).strip().lower() for x in (v or []) if str(x).strip()]


def _num(v: Any, default: float) -> float:
    try:
        return float(re.findall(r"[\d.]+", str(v))[0])
    except (IndexError, ValueError):
        return default


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """The model's answer, checked and mapped onto what the planner understands."""
    existing = _as_list(raw.get("existing"))
    room = str(raw.get("room_type", "")).strip().lower()
    if any("bed" == x or x.startswith("bed ") or x.endswith(" bed") for x in existing):
        room = "bedroom office"
    if room not in ROOM_TYPES:
        room = next((t for t in ROOM_TYPES if t.split()[0] in room), "home office")
    a, b = _num(raw.get("length_ft"), 12), _num(raw.get("width_ft"), 10)
    length, width = max(a, b), min(a, b)
    length, width = min(max(round(length), 7), 60), min(max(round(width), 6), 40)
    colours = []
    for word in _as_list(raw.get("colours") or raw.get("colors")):
        c = COLOUR_WORDS.get(word) or next((v for k, v in COLOUR_WORDS.items() if k in word),
                                           None)
        if c and c not in colours:
            colours.append(c)
    owned = sorted({kind for item in existing for pat, kind in OWNED if re.search(pat, item)})
    return {"room_type": room, "length_ft": length, "width_ft": width,
            "carpet": "carpet" in str(raw.get("floor", "")).lower(),
            "existing": existing, "owned_kinds": owned, "colours": colours,
            "style": str(raw.get("style", "")).strip(), "notes": str(raw.get("notes", "")).strip()}


def _used(body: dict[str, Any], usage: dict[str, int] | None) -> None:
    """Add a native Ollama answer's token counts to `usage`."""
    if usage is not None:
        usage["prompt"] = usage.get("prompt", 0) + int(body.get("prompt_eval_count") or 0)
        usage["completion"] = usage.get("completion", 0) + int(body.get("eval_count") or 0)


def _ask_model(image_bytes: bytes, usage: dict[str, int] | None = None) -> dict[str, Any]:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    r = httpx.post(_native_url(), timeout=settings.model_timeout, json={
        "model": settings.vision_model, "stream": False, "format": "json",
        "keep_alive": "30m", "options": {"temperature": 0.1, "num_predict": 260},
        "messages": [{"role": "user", "content": agentc_bridge.prompt("room_photo_read",
                                                                      PROMPT)[0],
                      "images": [base64.b64encode(buf.getvalue()).decode()]}]})
    r.raise_for_status()
    _used(r.json(), usage)
    text = r.json()["message"]["content"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # cut off or garbled (a long list can run out of tokens, or repeat itself):
    # keep every complete box in it
    found = [{"label": lab, "bbox_2d": [float(x) for x in nums.split(",")]}
             for lab, nums in re.findall(r'"label"\s*:\s*"([^"]+)"\s*,\s*"bbox_2d"\s*:\s*'
                                         r'\[\s*([\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,'
                                         r'\s*[\d.]+)\s*\]', text)]
    if found:
        seen, out = set(), []
        for o in found:                      # a repeating answer lists the same box again
            k = (o["label"], tuple(o["bbox_2d"]))
            if k not in seen:
                seen.add(k)
                out.append(o)
        return {"objects": out}
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def forget_photos() -> int:
    """Delete every saved photo reading (PHOTO:: and PHOTO-LAYOUT::), so each
    photo is read by the vision model again. -> how many were deleted."""
    rows = query(f"DELETE FROM {fq('planning', 'room_plans')} AS p "
                 f"WHERE META(p).id LIKE 'PHOTO%' RETURNING META(p).id")
    return len(rows)


def analyze_room_photo(image_bytes: bytes, *, customer_id: str | None = None,
                       session_id: str | None = None, trace: Trace | None = None
                       ) -> dict[str, Any]:
    """{"ok": bool, "room": normalised reading, "cached": bool, "message": str}"""
    d = governance.authorize("analyze_room_photo", {"bytes": len(image_bytes)},
                             customer_id=customer_id, session_id=session_id, trace=trace)
    if not d.allowed:
        return {"ok": False, "message": d.message}
    key = f"PHOTO::{fingerprint(image_bytes)}"
    t0 = time.perf_counter()
    doc = kv_get("planning", "room_plans", key)
    if trace:
        trace.add("Data (KV)", "Checked whether this photo was read before",
                  "Yes — reusing that reading" if doc else "No — new photo",
                  f"KV get, key {key[:20]}…", (time.perf_counter() - t0) * 1000,
                  ["planning.room_plans"],
                  tokens_saved=sum((doc or {}).get("tokens", {}).values()))
    if doc:
        return {"ok": True, "room": doc["room"], "cached": True, "raw": doc.get("raw")}
    t0 = time.perf_counter()
    try:
        usage: dict[str, int] = {}
        raw = _ask_model(image_bytes, usage)
    except Exception as exc:  # model not running, out of memory, timeout
        return {"ok": False, "message": f"The vision model couldn't read the photo ({exc})."}
    ms = (time.perf_counter() - t0) * 1000
    room = normalise(raw)
    if trace:
        trace.add("Model", "Read the photo: room type, estimated size, floor, colours and "
                  "furniture already there",
                  f"{room['room_type']}, about {room['length_ft']}′ × {room['width_ft']}′, "
                  f"{len(room['existing'])} things seen",
                  f"{settings.vision_model}, JSON answer · prompt room_photo_read · "
                  f"{agentc_bridge.prompt('room_photo_read', PROMPT)[1]}", ms,
                  model=settings.vision_model,
                  tokens_spent=sum(usage.values()))
        trace.add("App logic", "Mapped the answer onto the planner's room types, sizes "
                  "and catalogue colours",
                  ", ".join(room["colours"]) or "no colour preference", "rules")
    t0 = time.perf_counter()
    kv_upsert("planning", "room_plans", key, {
        "type": "room_photo", "created_at": datetime.now(UTC).isoformat(),
        "customer_id": customer_id, "model": settings.vision_model, "seconds": round(ms / 1000, 1),
        "tokens": usage, "raw": raw, "room": room})
    if trace:
        trace.add("Data (KV)", "Saved the reading, so this photo is instant next time",
                  key[:20] + "…", "KV upsert", (time.perf_counter() - t0) * 1000,
                  writes=["planning.room_plans"])
    return {"ok": True, "room": room, "cached": False, "raw": raw}


# --------------------------------------------------------------------------- #
# where things are: for replicating the room as it is
# --------------------------------------------------------------------------- #
def _frame(w: int, h: int) -> tuple[int, int]:
    """The size Ollama resizes a picture to for qwen2.5vl — the frame its boxes
    are in (28-pixel steps, scaled up to at least 1024 patches)."""
    f, lo, hi = 28, 28 * 28 * 1024, 28 * 28 * 16384
    hb, wb = max(f, round(h / f) * f), max(f, round(w / f) * f)
    if hb * wb > hi:
        b = math.sqrt(h * w / hi)
        hb, wb = math.floor(h / b / f) * f, math.floor(w / b / f) * f
    elif hb * wb < lo:
        b = math.sqrt(lo / (h * w))
        hb, wb = math.ceil(h * b / f) * f, math.ceil(w * b / f) * f
    return wb, hb


def _replica_kind(label: str) -> str | None:
    return next((k for pat, k in REPLICA if re.search(pat, label.lower())), None)


TABLE_SHAPES = ("round", "rectangular", "square", "oval")


FLOOR_KINDS = ("chair", "conference_table", "desk", "storage", "side_table")


def _inside(a: list[float], b: list[float]) -> float:
    """How much of box a lies inside box b."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1])
    return ix * iy / area if area > 0 else 0.0


def _iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def layout_from_boxes(objects: list[dict[str, Any]], frame: tuple[int, int],
                      room_type: str | None = None,
                      table_shape: str | None = None) -> dict[str, Any]:
    """Boxes in the photo -> what's in the room and roughly where.

    Each item gets u (0 = left of the photo, 1 = right) and v (0 = far wall,
    1 = nearest the camera), a wall for things hung on one, and — for chairs at a
    table or desk — which side they're on and their order along it."""
    fw, fh = frame
    boxes = []
    for o in objects:
        b = o.get("bbox_2d") or o.get("bbox")
        if isinstance(b, list) and len(b) == 4 and all(isinstance(x, int | float) for x in b):
            boxes.append((str(o.get("label", "")).strip().lower(), [float(x) for x in b]))
    # boxes past the expected frame: the model used another scale — fit to what it used
    sx = max([fw] + [b[2] for _, b in boxes]) / fw
    sy = max([fh] + [b[3] for _, b in boxes]) / fh
    items, other = [], []
    for label, b in boxes:
        nb = [b[0] / (fw * sx), b[1] / (fh * sy), b[2] / (fw * sx), b[3] / (fh * sy)]
        nb = [min(max(x, 0.0), 1.0) for x in nb]
        if nb[2] - nb[0] < 0.01 or nb[3] - nb[1] < 0.01:
            continue
        kind = _replica_kind(label)
        if kind == "lamp" and (nb[1] < 0.02 or nb[3] < 0.35):   # on the ceiling or a
            kind = None                                          # wall: not a lamp we sell
        bw, bh = nb[2] - nb[0], nb[3] - nb[1]
        if kind == "rug" and bw < 0.3:              # a corner of carpet, not a rug
            kind = None
        if kind in ("storage", "side_table", "printer") and bw * bh < 0.004:
            continue                                # a phone or a bottle, boxed
        if kind == "monitor" and bw * bh < 0.004:
            continue                                # a tablet or a room's touch panel
        if kind is None:
            if label and label not in other:
                other.append(label)
            continue
        if kind in FLOOR_KINDS and kind != "chair" and nb[3] < 0.3:
            continue                               # hangs from the ceiling: a light
        if any(i["kind"] == kind and _iou(i["box"], nb) > 0.6 for i in items):
            continue                               # the same thing, seen twice
        screens = ON_WALL + ("monitor",)
        if kind in screens and any(i["kind"] in screens and _iou(i["box"], nb) > 0.6
                                   for i in items):
            continue                               # one screen, called two things
        items.append({"kind": kind, "label": label, "box": [round(x, 3) for x in nb]})
    # a potted plant read as a cabinet: mostly inside something we don't sell
    skip = [b for label, b in boxes if _replica_kind(label) is None and label]
    skip = [[b[0] / (fw * sx), b[1] / (fh * sy), b[2] / (fw * sx), b[3] / (fh * sy)]
            for b in skip]
    items = [i for i in items if not (i["kind"] in ("storage", "lamp") and
                                      any(_inside(i["box"], o) > 0.5 for o in skip))]
    desks = [i["box"] for i in items if i["kind"] == "desk"]
    items = [i for i in items if not (i["kind"] == "conference_table"     # a desk, seen
                                      and any(_iou(i["box"], d) > 0.6 for d in desks))]
    floor = [i for i in items if i["kind"] in ("chair", "conference_table", "desk", "storage")]
    far = min([i["box"][3] for i in floor] or [0.5])           # highest floor contact
    for i in items:
        x1, y1, x2, y2 = i["box"]
        i["u"] = round((x1 + x2) / 2, 3)
        i["v"] = round(min(1.0, max(0.0, (y2 - far) / max(1 - far, 0.05))), 3)
        if i["kind"] in ON_WALL:   # from the photo's edge in: on that side wall
            i["wall"] = ("left" if i["u"] < 0.2 or (x1 < 0.08 and i["u"] < 0.25) else
                         "right" if i["u"] > 0.8 or (x2 > 0.92 and i["u"] > 0.75) else "top")
    hosts = [i for i in items if i["kind"] in ("conference_table", "desk")]
    for c in (i for i in items if i["kind"] == "chair"):
        x1, y1, x2, y2 = c["box"]
        cx = (x1 + x2) / 2
        best = None
        for n, t in enumerate(hosts):
            tx1, ty1, tx2, ty2 = t["box"]
            tw, th = tx2 - tx1, ty2 - ty1
            if not (tx1 - 0.15 * tw <= cx <= tx2 + 0.15 * tw):
                continue
            if not (ty1 - 0.5 * th <= y2 <= ty2 + max(0.8 * th, 0.18)):   # a table seen
                continue                                         # low: a thin box
            gap = abs(cx - (tx1 + tx2) / 2) / max(tw, 0.01)
            if best is None or gap < best[0]:
                best = (gap, n, t)
        if best is None:
            continue
        _, n, t = best
        tx1, ty1, tx2, ty2 = t["box"]
        rel = (cx - tx1) / max(tx2 - tx1, 0.01)
        c["at"] = n
        if t["kind"] == "desk":
            c["side"] = "desk"
        elif rel < 0.12:
            c["side"] = "left"
        elif rel > 0.88:
            c["side"] = "right"
        else:
            # in front, its feet reach the table's front edge; behind, they stop short
            c["side"] = "near" if y2 >= ty2 - 0.15 * (ty2 - ty1) else "far"
        c["order"] = round(rel, 3)
    for n, t in enumerate(hosts):
        if t["kind"] == "conference_table":
            at = [c for c in items if c["kind"] == "chair" and c.get("at") == n]
            _seat_sides(t, at)
            t["shape"] = _table_shape(table_shape, t, at)
            if t["shape"] == "round":
                t.pop("axis", None)             # a round table runs no way in particular
    deep = next((t for t in hosts if t.get("axis") == "deep"), None)
    if deep:          # seen from a corner: the far wall is only as wide as the table end
        tx1, _, tx2, _ = deep["box"]
        for i in items:
            if i.get("wall") == "top" and not tx1 <= i["u"] <= tx2:
                i["wall"] = "left" if i["u"] < tx1 else "right"
                i["v"] = round(max(0.0, min(1.0, (i["box"][3] - 0.2) / 0.8)), 3)
            elif i.get("wall") == "top" and i["kind"] == "tv":
                i["u"] = deep["u"]                  # at the end of the table, facing it
    for n, t in enumerate(hosts):
        t["host"] = n
    # Outside a meeting room, a "table" with a chair or two is the desk.
    if room_type and room_type not in MEETING_ROOMS and not desks:
        for t in hosts:
            at = [c for c in items if c["kind"] == "chair" and c.get("at") == t["host"]]
            if t["kind"] == "conference_table" and len(at) <= 2:
                t["kind"] = "desk"
                for c in at:
                    c["side"] = "desk"
    counts: dict[str, int] = {}
    for i in items:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return {"items": items, "counts": counts, "not_sold": other,
            "aspect": round(fw / fh, 4)}      # the photo's shape, for its camera


def _table_shape(said: str | None, table: dict[str, Any], chairs: list[dict[str, Any]]) -> str:
    """The table top's shape: what the model said, checked against the chairs —
    six or more in two straight rows sit at a long table, whatever it said.
    Said nothing: two rows mean rectangular; otherwise leave it to the planner."""
    rows = table.get("axis") is not None
    if said == "round" and rows and len(chairs) >= 6:
        return "rectangular"
    if said in ("round", "square", "rectangular", "oval"):
        return said
    return "rectangular" if rows else ""


def _two_rows(pts: list[tuple[float, float]]) -> tuple[float, float, list[int]] | None:
    """The direction that splits the points into two tight, parallel rows:
    (score, angle in radians, the indexes in the row with the smaller offset)."""
    best = None
    for deg in range(0, 180, 3):
        a = math.radians(deg)
        au, av = math.cos(a), math.sin(a)
        off = sorted((p[1] * au - p[0] * av, i) for i, p in enumerate(pts))
        vals = [o for o, _ in off]
        for k in range(1, len(vals)):
            lo, hi = vals[:k], vals[k:]
            ml, mh = sum(lo) / len(lo), sum(hi) / len(hi)
            within = sum((x - ml) ** 2 for x in lo) + sum((x - mh) ** 2 for x in hi)
            score = within / ((mh - ml) ** 2 + 1e-9)
            if best is None or score < best[0]:
                best = (score, a, [i for _, i in off[:k]])
    return best


def _rank_corr(a: list[float], b: list[float]) -> float:
    """Spearman's rank correlation: do b's grow as a's do?"""
    def ranks(x: list[float]) -> list[int]:
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0] * len(x)
        for k, i in enumerate(order):
            r[i] = k
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma = mb = (n - 1) / 2
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 0.0


def _flanked(table: dict[str, Any], chairs: list[dict[str, Any]]) -> bool:
    """Every chair at the table's left or right edge, none in front of its middle,
    and a table about as tall as wide in the photo: it runs away from the camera
    with the chairs down its two long sides (four chairs at the corners look the
    same either way; this is how they tell apart)."""
    tx1, ty1, tx2, ty2 = table["box"]
    tw, th = tx2 - tx1, ty2 - ty1
    if th < 0.6 * tw:
        return False
    cx = [(c["box"][0] + c["box"][2]) / 2 for c in chairs]
    rel = [(x - tx1) / max(tw, 0.01) for x in cx]
    if all(r < 0.3 or r > 0.7 for r in rel):
        left = [c for c, r in zip(chairs, rel) if r < 0.5]
        right = [c for c, r in zip(chairs, rel) if r >= 0.5]
    elif not (left := _receding_row(chairs, cx, tx1, tw, -1)) or \
            not (right := _receding_row(chairs, cx, tx1, tw, 1)):
        return False
    if not left or not right:
        return False
    for row, side in ((left, "left"), (right, "right")):
        row.sort(key=lambda c: c["box"][3])
        for k, c in enumerate(row):
            c["side"] = side
            c["order"] = round(k / max(len(row) - 1, 1), 3)
    table["axis"] = "deep"
    return True


def _receding_row(chairs: list[dict[str, Any]], cx: list[float], tx1: float, tw: float,
                  sign: int) -> list[dict[str, Any]] | None:
    """The chairs down one side (-1 left, 1 right) of a table seen from its end, or
    None. In perspective the table's far end is narrow, so its far chairs sit near
    the middle of the box, not its edges: a row is the chairs off one side of the
    centreline, each nearer one lower in the photo, farther out, and a step down
    more than across — a row seen across the photo is flat."""
    mid = tx1 + tw / 2
    if any(abs(x - mid) < 0.08 * tw for x in cx):
        return None                                   # one at the table's end
    row = sorted((c for c, x in zip(chairs, cx) if (x - mid) * sign > 0),
                 key=lambda c: c["box"][3])
    if len(row) < 2:
        return row or None
    feet = [c["box"][3] for c in row]
    out = [abs((c["box"][0] + c["box"][2]) / 2 - mid) for c in row]
    if feet[-1] - feet[0] < 0.15 or _rank_corr(feet, out) < 0.3:
        return None
    if any(feet[k + 1] - feet[k] < 0.7 * abs(out[k + 1] - out[k])
           for k in range(len(row) - 1)):
        return None
    return row


def _seat_sides(table: dict[str, Any], chairs: list[dict[str, Any]]) -> None:
    """Which side of the table each chair is on, from the rows the chairs make.

    Chairs round a table sit in two parallel rows along its long sides, and the
    rows run the way the table does — across the photo, or away from the camera
    when the photo is taken from a corner or an end. Find the direction that
    splits the chairs' feet into two tight rows (leaving out a chair at either
    end if that fits better); a chair left out sits at that end. Fewer than four
    chairs, or no clear rows: the box rule stands."""
    if len(chairs) < 4:
        return
    if _flanked(table, chairs):
        return
    pts = [((c["box"][0] + c["box"][2]) / 2, c["box"][3]) for c in chairs]
    mu = sum(p[0] for p in pts) / len(pts)
    mv = sum(p[1] for p in pts) / len(pts)
    suu = sum((p[0] - mu) ** 2 for p in pts)
    svv = sum((p[1] - mv) ** 2 for p in pts)
    suv = sum((p[0] - mu) * (p[1] - mv) for p in pts)
    ang = 0.5 * math.atan2(2 * suv, suu - svv)          # the spread of all of them
    rank = sorted(range(len(pts)), key=lambda i: (pts[i][0] - mu) * math.cos(ang)
                  + (pts[i][1] - mv) * math.sin(ang))
    best = None
    for ends in ([], [rank[0]], [rank[-1]], [rank[0], rank[-1]]):
        rest = [i for i in range(len(pts)) if i not in ends]
        if len(rest) < 3:
            continue
        fit = _two_rows([pts[i] for i in rest])
        if fit is None:
            continue
        score = fit[0] + 0.02 * len(ends)               # an end chair must earn its place
        if best is None or score < best[0]:
            best = (score, fit[1], [rest[i] for i in fit[2]], ends)
    if best is None or best[0] > 0.5:
        return
    _, a, low, ends = best
    au, av = math.cos(a), math.sin(a)
    deep = abs(av) > 0.7 * abs(au)                      # runs away from the camera
    flip = not deep and au < 0                          # along: left→right, or far→near;
    if flip:                                            # turning it round swaps the rows
        au, av = -au, -av
    along = [p[0] * au + p[1] * av for p in pts]
    rows = [i for i in range(len(pts)) if i not in ends]
    lo, hi = min(along[i] for i in rows), max(along[i] for i in rows)
    mid = (lo + hi) / 2
    for i, c in enumerate(chairs):
        if i in ends:
            tx1, _, tx2, _ = table["box"]
            if deep and abs(pts[i][0] - (tx1 + tx2) / 2) > 0.3 * (tx2 - tx1):
                c.pop("at", None)             # off to one side, not at the table's end:
                c.pop("side", None)           # a chair by the wall
                c.pop("order", None)
                continue
            first = along[i] < mid
            side = ("far" if first else "near") if deep else ("left" if first else "right")
        else:
            smaller = (i in low) != flip      # offset ~ depth − across along the axis
            side = ("right" if smaller else "left") if deep else ("far" if smaller else "near")
        c["side"] = side
        c["order"] = round(min(1.0, max(0.0, (along[i] - lo) / max(hi - lo, 0.01))), 3)
    table["axis"] = "deep" if deep else "across"


def _ask(img: Image.Image, prompt: str, predict: int = 1200,
         usage: dict[str, int] | None = None) -> Any:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    r = httpx.post(_native_url(), timeout=max(settings.model_timeout, 300), json={
        "model": settings.vision_model, "stream": False, "format": "json",
        "keep_alive": "30m", "options": {"temperature": 0, "seed": 7, "num_predict": predict},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(buf.getvalue()).decode()]}]})
    r.raise_for_status()
    _used(r.json(), usage)
    text = r.json()["message"]["content"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # cut off or garbled (a long list can run out of tokens, or repeat itself):
    # keep every complete box in it
    found = [{"label": lab, "bbox_2d": [float(x) for x in nums.split(",")]}
             for lab, nums in re.findall(r'"label"\s*:\s*"([^"]+)"\s*,\s*"bbox_2d"\s*:\s*'
                                         r'\[\s*([\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,'
                                         r'\s*[\d.]+)\s*\]', text)]
    if found:
        seen, out = set(), []
        for o in found:                      # a repeating answer lists the same box again
            k = (o["label"], tuple(o["bbox_2d"]))
            if k not in seen:
                seen.add(k)
                out.append(o)
        return {"objects": out}
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def _boxes(data: Any) -> list[dict[str, Any]]:
    objs = data.get("objects", data) if isinstance(data, dict) else data
    return [o for o in objs if isinstance(o, dict)] if isinstance(objs, list) else []


def _is_chair(o: dict[str, Any]) -> bool:
    return _replica_kind(str(o.get("label", ""))) == "chair"


def _group(o: dict[str, Any], fw: int, fh: int) -> bool:
    """A "chair" box far wider than a chair: several chairs in one box."""
    b = o.get("bbox_2d")
    if not (isinstance(b, list) and len(b) == 4):
        return False
    w, h = (b[2] - b[0]) / fw, (b[3] - b[1]) / fh
    return w > 0.45 or (w > 0.25 and w > 2.2 * h)


def _ok_box(o: dict[str, Any]) -> bool:
    b = o.get("bbox_2d")
    return (isinstance(b, list) and len(b) == 4 and all(isinstance(x, int | float) for x in b)
            and b[2] > b[0] and b[3] > b[1])


def _not_a_screen(img: Image.Image, box: tuple[float, float, float, float]) -> bool:
    """Something to look at inside the frame (a painting, a view — not a blank or
    dark screen) that isn't TV-shaped (a screen is about 16:9; art is often
    squarer). A lit TV showing a wide picture still reads as a TV."""
    from PIL import ImageStat
    x1, y1, x2, y2 = box
    if (x2 - x1) >= 1.45 * (y2 - y1):
        return False
    mx, my = (x2 - x1) * 0.15, (y2 - y1) * 0.15
    inner = img.crop((int(x1 + mx), int(y1 + my), int(x2 - mx), int(y2 - my))).convert("L")
    return inner.width > 2 and inner.height > 2 and ImageStat.Stat(inner).stddev[0] > 25


def _one_each(chairs: list[dict[str, Any]], fw: int, fh: int) -> list[dict[str, Any]]:
    """One box per chair. The same chair boxed twice: nearly the same box, or one
    mostly inside the other with the feet at the same height (a second box round
    just the seat and legs). One inside another with its feet higher up is a
    chair behind it, as down a row seen from its end."""
    def nb(o: dict[str, Any]) -> list[float]:
        b = o["bbox_2d"]
        return [b[0] / fw, b[1] / fh, b[2] / fw, b[3] / fh]

    def cover(a: list[float], b: list[float]) -> float:     # of the smaller box
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return ix * iy / small if small > 0 else 1.0

    out: list[tuple[list[float], dict[str, Any]]] = []
    for o in sorted((o for o in chairs if _ok_box(o) and not _group(o, fw, fh)),
                    key=lambda o: -(nb(o)[2] - nb(o)[0]) * (nb(o)[3] - nb(o)[1])):
        b = nb(o)
        if not any(_iou(b, x) > 0.6 or (cover(b, x) > 0.6 and abs(b[3] - x[3]) < 0.05)
                   for x, _ in out):
            out.append((b, o))
    return [o for _, o in out]


def _locate(image_bytes: bytes) -> tuple[list[dict[str, Any]], tuple[int, int], dict]:
    """Every object's box, one per chair: the chairs from a chairs-only look when
    it finds more of them."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((640, 640))
    usage: dict[str, int] = {}
    objs = _boxes(_ask(img, agentc_bridge.prompt("room_photo_locate", LOCATE_PROMPT)[0],
                       usage=usage))
    fw, fh = _frame(*img.size)
    group = [o for o in objs if _is_chair(o) and _group(o, fw, fh)]
    objs = [o for o in objs if not any(o is g for g in group)]   # a row of chairs, boxed
    have = [o for o in objs if _is_chair(o)]                     # as one
    extra = {"chairs_seen": len(have), "chair_groups": len(group)}
    ans = _ask(img, agentc_bridge.prompt("chair_count", COUNT_PROMPT)[0], 60, usage)
    ans = ans if isinstance(ans, dict) else {}
    try:
        n = int(ans.get("chairs", 0))
    except (TypeError, ValueError):
        n = 0
    extra["chairs_counted"] = n
    shape = str(ans.get("table", "")).strip().lower()
    extra["table_shape"] = next((t for t in TABLE_SHAPES if t in shape),
                                "rectangular" if "rect" in shape or "long" in shape else "")
    # framed art read as a TV: on the far wall, something to look at inside the
    # frame, and not TV-shaped (side-wall boxes are foreshortened, so left alone)
    fw, fh = _frame(*img.size)
    sx = max([fw] + [o["bbox_2d"][2] for o in objs if _ok_box(o)]) / fw
    sy = max([fh] + [o["bbox_2d"][3] for o in objs if _ok_box(o)]) / fh
    fixed = []
    for o in objs:
        if not (_ok_box(o) and _replica_kind(str(o.get("label", ""))) == "tv"):
            continue
        b = o["bbox_2d"]
        box = (b[0] / (fw * sx) * img.width, b[1] / (fh * sy) * img.height,
               b[2] / (fw * sx) * img.width, b[3] / (fh * sy) * img.height)
        u = (box[0] + box[2]) / 2 / img.width
        if 0.2 < u < 0.8 and _not_a_screen(img, box):
            fixed.append(f"{o['label']} → picture")
            o["label"] = "picture"
    if fixed:
        extra["wall_fixed"] = fixed
    # chairs behind a table or each other get merged or missed among everything
    # else: a chairs-only look too, and the set that finds more of them is kept
    have = _one_each(have, fw, fh)
    alone = _one_each([o for o in _boxes(_ask(img, agentc_bridge.prompt(
        "chairs_locate", CHAIRS_PROMPT)[0], usage=usage)) if _is_chair(o)], fw, fh)
    extra["chairs_alone"] = len(alone)
    if len(alone) > len(have):
        extra["chairs_added"] = len(alone) - len(have)
        have = alone
    objs = [o for o in objs if not _is_chair(o)] + have
    extra["tokens"] = usage
    return objs, _frame(*img.size), extra


def _with_count(layout: dict[str, Any], counted: Any) -> dict[str, Any]:
    """Chairs the model counted but didn't box (half hidden behind the table, or
    in a row it drew as one): on the list too — the replica seats them round the
    table."""
    try:
        n = int(counted or 0)
    except (TypeError, ValueError):
        n = 0
    c = layout["counts"]
    if c.get("conference_table") and n > c.get("chair", 0):
        c["chair"] = n
    return layout


def layout_saved(image_bytes: bytes) -> bool:
    """Whether this photo's furniture was found before (so finding it is instant)."""
    try:
        return kv_get("planning", "room_plans",
                      f"PHOTO-LAYOUT::{fingerprint(image_bytes)}") is not None
    except Exception:
        return False


def locate_objects(image_bytes: bytes, *, room_type: str | None = None,
                   customer_id: str | None = None, session_id: str | None = None,
                   trace: Trace | None = None) -> dict[str, Any]:
    """{"ok": bool, "layout": {"items", "counts", "not_sold"}, "cached": bool}"""
    d = governance.authorize("analyze_room_photo", {"bytes": len(image_bytes),
                                                    "mode": "replicate"},
                             customer_id=customer_id, session_id=session_id, trace=trace)
    if not d.allowed:
        return {"ok": False, "message": d.message}
    key = f"PHOTO-LAYOUT::{fingerprint(image_bytes)}"
    t0 = time.perf_counter()
    doc = kv_get("planning", "room_plans", key)
    if trace:
        trace.add("Data (KV)", "Checked whether this photo's layout was read before",
                  "Yes — reusing it" if doc else "No — new photo",
                  f"KV get, key {key[:27]}…", (time.perf_counter() - t0) * 1000,
                  ["planning.room_plans"],
                  tokens_saved=sum((doc or {}).get("tokens", {}).values()))
    if doc:
        fw, fh = doc["frame"]
        objs = [o for o in doc["objects"] if not _is_chair(o)] + _one_each(
            [o for o in doc["objects"] if _is_chair(o)], fw, fh)   # today's rule for one each
        return {"ok": True, "layout": _with_count(layout_from_boxes(
            objs, (fw, fh), room_type, doc.get("table_shape")),
            doc.get("chairs_counted")), "cached": True}
    t0 = time.perf_counter()
    try:
        objects, frame, extra = _locate(image_bytes)
    except Exception as exc:  # model not running, out of memory, timeout
        return {"ok": False, "message": f"The vision model couldn't read the photo ({exc})."}
    ms = (time.perf_counter() - t0) * 1000
    layout = _with_count(layout_from_boxes(objects, frame, room_type, extra.get("table_shape")),
                         extra.get("chairs_counted"))
    if trace:
        trace.add("Model", "Found each piece of furniture in the photo and where it is "
                  "(a box round each one)", f"{len(objects)} boxes",
                  "vision model, grounding (bbox_2d) · then a chair count "
                  f"({extra.get('chairs_counted', 0)} counted, {extra.get('chairs_seen', 0)} "
                  f"boxed, {extra.get('chairs_added', 0)} added from a chairs-only pass) "
                  f"· table shape: {extra.get('table_shape') or 'not said'} · "
                  + (f"framed art, not a TV: {'; '.join(extra['wall_fixed'])}"
                     if extra.get("wall_fixed") else "no framed art taken for a TV")
                  + " · prompts room_photo_locate, chair_count, chairs_locate · "
                  + agentc_bridge.prompt("room_photo_locate", LOCATE_PROMPT)[1],
                  ms, model=settings.vision_model,
                  tokens_spent=sum(extra.get("tokens", {}).values()))
        seen = ", ".join(f"{n} × {k.replace('_', ' ')}" for k, n in layout["counts"].items())
        trace.add("App logic", "Turned the boxes into what's in the room and where: the "
                  "side of the room, the wall, which side of the table each chair is on",
                  seen or "nothing we sell", "rules")
    t0 = time.perf_counter()
    kv_upsert("planning", "room_plans", key, {
        "type": "room_photo_layout", "created_at": datetime.now(UTC).isoformat(),
        "customer_id": customer_id, "model": settings.vision_model,
        "seconds": round(ms / 1000, 1), "frame": list(frame), "objects": objects, **extra})
    if trace:
        trace.add("Data (KV)", "Saved the layout, so this photo is instant next time",
                  key[:27] + "…", "KV upsert", (time.perf_counter() - t0) * 1000,
                  writes=["planning.room_plans"])
    return {"ok": True, "layout": layout, "cached": False}
