""""Set up a conference room for 10" — a whole room, not one product.

What goes in a room depends on its size: without one, the assistant plans a
typical size with the Room Planner and offers the others to re-plan. Plain regexes, no model call: a
small model can't be relied on to ask and then remember the answer.
"""

from __future__ import annotations

import math
import re
from typing import Any

from retail import moving, planner

ROOM_WORDS = [  # (pattern, room type; None = a meeting room sized by headcount)
    (r"board\s*room", "boardroom"),
    (r"huddle(?: room| space)?", "huddle room"),
    (r"(?:conference|meeting|training) (?:room|space)", None),
    (r"home office|office at home", "home office"),
    (r"(?:spare )?bedroom office", "bedroom office"),
    (r"executive (?:office|suite)", "executive suite"),
    (r"(?:private|manager'?s?) office", "private office"),
    (r"cubicles?|open office|open[- ]plan", "open office"),
    (r"(?:bigger|larger|new|another|different) (?:room|office|space)", "any"),  # moving
    (r"office|room|space|workspace", "generic"),  # only with a headcount (HEADCOUNT)
]
SET_UP = re.compile(r"\b(set ?up|setting up|furnish\w*|outfit\w*|kit out|equip\w*|new|"
                    r"plan\w*|design\w*|build\w*|fit out|mov\w*|relocat\w*|bigger|"
                    r"larger)\b", re.I)
MOVING = re.compile(r"\b(mov(?:e|ing)|relocat\w*|bigger|larger)\b", re.I)
PEOPLE = re.compile(r"\b(?:seats?|seating|fits?|for|hold\w*)\s+(?:up to\s+|about\s+)?(\d{1,2})"
                    r"\b|\b(\d{1,2})\s*(?:people|persons|person|employees|staff|seats|"
                    r"attendees|of us)\b", re.I)
SIZE = re.compile(r"(\d{1,3}(?:\.\d)?)\s*(?:ft|feet|foot|'|′)?\s*(?:x|by|×|\*)\s*"
                  r"(\d{1,3}(?:\.\d)?)\s*(?:ft|feet|foot|'|′)?", re.I)
AREA = re.compile(r"(\d{2,4})\s*(?:sq\.?\s*(?:ft|feet)|square (?:feet|foot)|sf)\b", re.I)
THING = re.compile(r"\s+(?:a |an |new )?(?:chairs?|desks?|tables?|lamps?|rugs?|tvs?|displays?|monitors?|"
                   r"whiteboards?|phones?|cameras?|speakers?|furniture|supplies|decor)\b")
# "an office which can seat 12": a plain room word counts only with a real headcount —
# never a bare "for 2", as in "is there room for 2 more chairs?"
HEADCOUNT = re.compile(r"\b(?:seats?|seating|sit|fits?|hold\w*|accommodat\w*)\s+"
                       r"(?:up to\s+|about\s+|around\s+)?(\d{1,2})\b|\bfor\s+(?:up to\s+|"
                       r"about\s+)?(\d{1,2})\s*(?:people|persons|employees|staff|attendees|"
                       r"colleagues)\b|\bteam of (\d{1,2})\b", re.I)
# "N people" alone ("we have 3 people in the room") is a room only when setting one up
COUNT = re.compile(r"\b(\d{1,2})\s*(?:people|persons|person|employees|staff|attendees|"
                   r"colleagues)\b", re.I)
DESKS = re.compile(r"\b(desks?|workstations?|work at|cubicles?|each (?:have|get) a desk)\b",
                   re.I)
UNSURE = re.compile(r"\b(not sure|no idea|don'?t know|dunno|typical|standard|average|"
                    r"normal|usual|whatever)\b", re.I)


# "no video conference needed", "skip the camera": a room without a camera or speakerphone
NO_VIDEO = re.compile(r"\b(?:no|without|skip|drop|remove|don'?t (?:need|want)|do not (?:need|"
                      r"want)|not)\b[\w\s-]{0,20}?\b(?:video|camera|webcam)s?\b|"
                      r"\b(?:video|camera|webcam)s?\b[\w\s-]{0,25}?\b(?:not|isn'?t|aren'?t)\s+"
                      r"(?:needed|required|necessary|wanted)\b", re.I)
WITH_VIDEO = re.compile(r"\b(?:video|zoom|teams calls?|webcam|camera)s?\b", re.I)
# "add a camera back": turning video calls on for the room just planned
ADD_VIDEO = re.compile(r"\b(?:add|with|need|want|include|put)\b[\w\s-]{0,20}?\b"
                       r"(?:video|camera|webcam)s?\b", re.I)


def video_in(text: str, *, toggle: bool = False) -> bool | None:
    """Video calls asked for (True), turned down (False), or not said (None).
    toggle: a change to a room already planned, so a plain mention isn't enough."""
    if NO_VIDEO.search(text):
        return False
    return True if (ADD_VIDEO if toggle else WITH_VIDEO).search(text) else None


def _meeting_room(people: int) -> str:
    return "huddle room" if people <= 5 else "conference room" if people <= 14 else "boardroom"


def size_in(text: str) -> tuple[float, float] | None:
    """'20 x 15 ft', "20' by 15'", '300 sq ft' -> (length, width) in feet."""
    m = SIZE.search(text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if 4 <= min(a, b) and max(a, b) <= 100:
            return max(a, b), min(a, b)
    m = AREA.search(text)
    if m and 40 <= int(m.group(1)) <= 5000:
        w = math.sqrt(int(m.group(1)) * 3 / 4)     # a 4:3 room
        return round(w * 4 / 3), round(w)
    return None


def parse(text: str) -> dict[str, Any] | None:
    """A request to set up a whole room -> {room_type, people, size}; None when
    it's a product question ("show me conference tables", "a new home office chair")."""
    low = text.lower()
    for pat, room in ROOM_WORDS:
        m = re.search(rf"\b(?:{pat})\b", low)
        if m:
            kind = room
            break
    else:
        return None
    if THING.match(low, m.end()):
        return None                                # "conference room chairs": a product
    if kind == "generic":                          # "an office which can seat 12"
        h = HEADCOUNT.search(low) or (SET_UP.search(low) and COUNT.search(low))
        people = int(next(g for g in h.groups() if g)) if h else None
        if not people or not 1 <= people <= 30:
            return None
        kind = ("private office" if people == 1 else "open office" if DESKS.search(low)
                else _meeting_room(max(people, 2)))
        return {"room_type": kind, "people": people, "size": size_in(text),
                "bring": moving.said_owned(text), "moving": bool(MOVING.search(text))}
    before = low[:m.start()].split()[-5:]          # "set up a new conference room"
    asks = bool(SET_UP.search(" ".join(before))) and not THING.search(" " + " ".join(before))
    n = PEOPLE.search(low)
    people = int(n.group(1) or n.group(2)) if n else None
    if not (asks or people):
        return None
    bring = moving.said_owned(text)
    if kind == "any":                              # "a bigger room": what it's for
        n = people or (bring.get("chair", 0) if bring.get("conference_table") or
                       bring.get("chair", 0) >= 4 else 1)
        kind = "private office" if n <= 1 else _meeting_room(max(n, 2))
    if kind is None:
        kind = _meeting_room(people or planner.DEFAULT_PEOPLE["conference room"])
    elif kind in planner.MEETING_ROOMS and people and kind != "boardroom":
        kind = _meeting_room(people)               # "huddle room for 12" is a conference room
    people = people or planner.DEFAULT_PEOPLE[kind]
    return {"room_type": kind, "people": people, "size": size_in(text), "bring": bring,
            "moving": bool(MOVING.search(text)) or bool(bring)}


def ask_bring(room_type: str, remembered: str | None = None) -> str:
    """Moving: what's coming from the current room, asked before anything is planned."""
    return (f"Moving to a new {label(room_type)} — happy to help. What would you like to "
            f"bring from your current room? List it (e.g. \"2 chairs, a desk and a "
            f"cabinet\") and I'll plan the new room around it, so you only buy what's "
            f"missing."
            + (f" Last time you told us your current room has {remembered} — pick that "
               f"below if it's all coming with you." if remembered else ""))


def needs_size(room_type: str) -> bool:
    """Cubicles are sized by headcount; every other room by its walls."""
    return room_type != "open office"


def typical(room_type: str, people: int) -> list[tuple[int, int]]:
    """Three sizes to offer — snug, typical, roomy — in feet, longest side first."""
    if room_type in planner.MEETING_ROOMS:
        out = []
        for per in (22, 28, 36):                   # sq ft a seat, table and walkway included
            area = max(90, per * people)
            w = math.sqrt(area * 3 / 4)
            out.append((round(w * 4 / 3), round(w)))
        return out
    return {"executive suite": [(13, 12), (15, 13), (18, 14)],
            "private office": [(10, 10), (12, 10), (14, 12)]}.get(
        room_type, [(10, 9), (12, 10), (14, 12)])


def label(room_type: str) -> str:
    return planner.ROOM_LABELS.get(room_type, room_type).split(" (")[0].lower()
