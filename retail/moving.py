"""Moving to a bigger room: bring what you have, buy only the difference.

The customer tells us what's in their current room — typed counts, a photo of
it, or a sentence in the chat — and the new room's plan is split into what to
bring and what to buy. Plain code, no model call: "2 chairs, a desk, a TV" is
counted with regexes, and the split is arithmetic over the plan's lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# kind -> what people call it; a chunk's last-ending match is its noun ("desk chairs")
WORDS = {
    "chair": r"chairs?|seats?|stools?",
    "side_table": r"(?:side|end|coffee) tables?",
    "conference_table": r"(?:conference |meeting |boardroom )?tables?",
    "desk": r"(?:standing |sit[- ]stand |l[- ]shaped |u[- ]shaped )?desks?|workstations?",
    "tv": r"tvs?|televisions?|displays?|screens?",
    "monitor": r"monitors?",
    "storage": r"(?:file |filing )?cabinets?|bookcases?|book ?shel(?:f|ves)|shel(?:f|ves)|"
               r"shelving|credenzas?|lockers?|storage",
    "whiteboard": r"white ?boards?",
    "lamp": r"(?:floor |desk )?lamps?",
    "printer": r"printers?",
    "rug": r"rugs?",
    # "bin", "trash can" — never a bare "can" ("Can you help…" is not a bin)
    "wastebasket": r"(?:waste ?|trash |garbage |recycling )?(?:bins?|baskets?)|"
                   r"(?:waste ?|trash |garbage |recycling )cans?|wastebaskets?",
    "video_bar": r"video ?bars?|webcams?|conference cameras?|cameras?",
}
_WORD_RE = {k: re.compile(rf"\b(?:{v})\b", re.I) for k, v in WORDS.items()}
_NUMS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "couple": 2, "pair": 2,
         "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12}
_NUM = re.compile(r"\b(\d{1,2}|" + "|".join(_NUMS) + r")\b(?!\s*(?:\"|″|'|inch|in\b|ft|feet|"
                  r"foot|x\b|×|by\b|people|persons|seats?\b))", re.I)
# where the list of what they have starts
HAVE = re.compile(r"\b(?:(?:i|we)(?:'ve| have| own| already have| currently have| got)|"
                  r"(?:current|existing|old|present) (?:room|office|space)(?: has| with|:)?|"
                  r"room (?:has|with)|bringing|bring|reuse|keep(?:ing)?)\b[:\s]*", re.I)
# "nothing — all new": bringing nothing from the current room
NONE = re.compile(r"\b(?:nothing|none|no(?:thing)? to bring|all new|everything new|"
                  r"start(?:ing)? fresh|buy(?:ing)? everything|not bringing anything)\b", re.I)
# a remembered "current room" sentence
CURRENT = re.compile(r"\b(?:current|existing) (?:room|office) has\b", re.I)

COMMON = ["chair", "desk", "conference_table", "tv", "storage", "whiteboard", "monitor"]
MORE = ["lamp", "printer", "rug", "wastebasket", "video_bar", "side_table"]
NAMES = {"chair": ("chair", "chairs"), "conference_table": ("table", "tables"),
         "desk": ("desk", "desks"), "tv": ("TV", "TVs"), "monitor": ("monitor", "monitors"),
         "storage": ("cabinet or shelf", "cabinets or shelves"),
         "whiteboard": ("whiteboard", "whiteboards"), "lamp": ("lamp", "lamps"),
         "printer": ("printer", "printers"), "rug": ("rug", "rugs"),
         "wastebasket": ("bin", "bins"), "video_bar": ("video bar", "video bars"),
         "side_table": ("side table", "side tables")}


def owned_in(text: str) -> dict[str, int]:
    """'2 chairs, a desk, tv display and a cabinet' -> {chair: 2, desk: 1, tv: 1, storage: 1}."""
    out: dict[str, int] = {}
    for chunk in re.split(r",|;|\band\b|\bplus\b|&|\+|\bwith\b|\n", text, flags=re.I):
        best = None                                # (end, length, kind)
        for kind, rx in _WORD_RE.items():
            for m in rx.finditer(chunk):
                cand = (m.end(), m.end() - m.start(), kind, m.start())
                if best is None or cand[:2] > best[:2]:
                    best = cand
        if best is None:
            continue
        n = _NUM.search(chunk[:best[3]])
        count = 1
        if n:
            w = n.group(1).lower()
            count = int(w) if w.isdigit() else _NUMS[w]
        if 0 < count <= 60:
            out[best[2]] = out.get(best[2], 0) + count
    return out


def said_owned(text: str) -> dict[str, int]:
    """What a chat message says they already have — only after "I have", "my
    current room has" and the like, so "show me tables and chairs" isn't it."""
    m = HAVE.search(text)
    if not m:
        return {}
    tail = re.split(r"[.?!](?:\s|$)", text[m.end():], maxsplit=1)[0]   # this sentence only
    stop = re.search(r"\b(?:and (?:i'?m|we'?re|am|are|is) |(?:i'?m|we'?re) |moving|move to|"
                     r"going to|want|need|looking)\b", tail, re.I)
    return owned_in(tail[:stop.start()] if stop else tail)


def words(counts: dict[str, int], yours: bool = False) -> str:
    """{chair: 2, tv: 1} -> '2 chairs and 1 TV' (yours: '2 chairs and TV', after "your")."""
    parts = [(NAMES[k][0] if yours else f"1 {NAMES[k][0]}") if n == 1 else f"{n} {NAMES[k][1]}"
             for k in COMMON + MORE if (n := int(counts.get(k, 0))) > 0]
    return (", ".join(parts[:-1]) + " and " + parts[-1]) if len(parts) > 1 else \
        (parts[0] if parts else "nothing")


def memory_text(counts: dict[str, int]) -> str:
    return f"My current room has {words(counts)}."


def from_memories(texts: list[str]) -> tuple[dict[str, int], str] | None:
    """The newest remembered 'current room has …' sentence and its counts."""
    for t in texts:
        if CURRENT.search(t):
            counts = owned_in(t[CURRENT.search(t).end():])
            if counts:
                return counts, t
    return None


@dataclass
class Delta:
    bring: dict[str, int] = field(default_factory=dict)   # sku -> pieces they bring
    spare: dict[str, int] = field(default_factory=dict)   # kind -> pieces not needed
    pieces: int = 0                                        # pieces brought in all
    value: float = 0.0                                     # what those would cost new

    def buy(self, sku: str, qty: int) -> int:
        return max(0, qty - min(self.bring.get(sku, 0), qty))


def delta(lines: list[Any], have: dict[str, int]) -> Delta:
    """Split the plan's lines into what they bring and what they buy. Each kind's
    pieces go to that kind's biggest line first; whatever is left over isn't
    needed in the new room (too many, or nothing like it there — a desk in a
    conference room)."""
    left = {k: int(n) for k, n in have.items() if int(n) > 0}
    d = Delta()
    for line in sorted(lines, key=lambda ln: -ln.qty):
        k = line.product["kind"]
        n = min(left.get(k, 0), line.qty)
        if n:
            d.bring[line.product["sku"]] = n
            left[k] -= n
            d.pieces += n
            d.value += n * float(line.product["price"])
    d.spare = {k: n for k, n in left.items() if n > 0}
    return d


def summary(lines: list[Any], d: Delta) -> tuple[str, str]:
    """'Bring your 2 chairs and 1 TV', 'buy 10 more chairs, …' for an answer."""
    brought: dict[str, int] = {}
    buying: list[str] = []
    for ln in sorted(lines, key=lambda ln: -ln.product["price"] * ln.qty):
        k, b = ln.product["kind"], d.bring.get(ln.product["sku"], 0)
        if b:
            brought[k] = brought.get(k, 0) + b
        n = ln.qty - b
        if n > 0:
            name = ln.product.get("base_name") or ln.product["name"]
            if b:                                  # "10 more chairs (Conference Room Chair)"
                one, many = NAMES.get(k, (k.replace("_", " "),) * 2)
                buying.append(f"{n} more {one if n == 1 else many} ({name})")
            else:
                buying.append(f"{n} × {name}" if n > 1 else name)
    return words(brought, yours=True), ", ".join(buying[:4])
