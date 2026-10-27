"""3D-style product renders, drawn as SVG from a product's kind, size and colour.

Every product is built from simple 3D parts (boxes, extruded shapes, cylinders)
in inches, lit from the front-left-top, shaded face by face, and projected in a
three-quarter view. Because the colour is an input, a chair picked in Red is
rendered red — no photo library, nothing to license.

    render.data_uri(product)  ->  "data:image/svg+xml;base64,..."
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------------------- #
# camera, light, colour
# --------------------------------------------------------------------------- #
_AZ, _EL = math.radians(32), math.radians(24)
V = (math.cos(_EL) * math.sin(_AZ), -math.cos(_EL) * math.cos(_AZ), math.sin(_EL))


def _norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


_F = tuple(-c for c in V)
_R = _norm(_cross(_F, (0, 0, 1)))
_U = _cross(_R, _F)
LIGHT = _norm((-0.45, -0.75, 0.9))

WOOD = {"Walnut", "Espresso", "Natural Oak"}
GRAPHITE, SILVER = "#3b3f44", "#c8ccd0"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in rgb))


def shade(hex_color: str, k: float) -> str:
    return _hex(tuple(c * k for c in _rgb(hex_color)))


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


# --------------------------------------------------------------------------- #
# scene
# --------------------------------------------------------------------------- #
@dataclass
class Face:
    pts: list[tuple[float, float, float]]
    color: str
    tag: str = ""
    opacity: float = 1.0
    normal: tuple[float, float, float] = (0, 0, 1)
    part: int = 0


@dataclass
class Scene:
    """Faces grouped into parts. Parts are drawn in the order the model adds
    them (so a model adds legs before the top it hides); faces within a part
    are depth-sorted. A plain global depth sort put table legs over tabletops."""
    faces: list[Face] = field(default_factory=list)
    overlays: list[Callable[["Projector"], str]] = field(default_factory=list)
    part: int = 0
    splits: list[int] = field(default_factory=list)   # layer boundaries (by part) in a room

    def next_part(self) -> None:
        self.part += 1

    # -- primitives ---------------------------------------------------------- #
    def poly(self, pts, color, tag="", opacity=1.0, two_sided=False):
        n = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
        if _dot(n, V) < 0:
            if not two_sided:
                return
            n = tuple(-c for c in n)
        self.faces.append(Face(list(pts), color, tag, opacity, n, self.part))

    def prism_z(self, footprint, z0, z1, color, tag="", top_tag="", opacity=1.0,
                side_color=None):
        """Extrude an (x, y) footprint (counter-clockwise) from z0 to z1."""
        self.next_part()
        n = len(footprint)
        for i in range(n):
            (x0, y0), (x1, y1) = footprint[i], footprint[(i + 1) % n]
            self.poly([(x0, y0, z0), (x1, y1, z0), (x1, y1, z1), (x0, y0, z1)],
                      side_color or color, tag, opacity)
        self.poly([(x, y, z1) for x, y in footprint], color, top_tag or tag, opacity)

    def prism_y(self, profile, y0, y1, color, tag="", front_tag="", opacity=1.0):
        """Extrude an (x, z) profile (counter-clockwise seen from the front) in depth."""
        self.next_part()
        n = len(profile)
        for i in range(n):
            (x0, z0), (x1, z1) = profile[i], profile[(i + 1) % n]
            self.poly([(x0, y1, z0), (x1, y1, z1), (x1, y0, z1), (x0, y0, z0)],
                      color, tag, opacity)
        self.poly([(x, y0, z) for x, z in profile], color, front_tag or tag, opacity)
        self.poly([(x, y1, z) for x, z in reversed(profile)], color, tag, opacity)  # back

    def prism_x(self, profile, x0, x1, color, tag=""):
        """Extrude a (y, z) profile along x."""
        self.next_part()
        n = len(profile)
        for i in range(n):
            (y0, z0), (y1, z1) = profile[i], profile[(i + 1) % n]
            self.poly([(x0, y0, z0), (x0, y1, z1), (x1, y1, z1), (x1, y0, z0)], color, tag)
        self.poly([(x1, y, z) for y, z in profile], color, tag)

    def box(self, x, y, z, w, d, h, color, tag="", top_tag="", front_tag="", opacity=1.0):
        self.next_part()
        fp = [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]
        pts = {
            "front": [(x, y, z), (x + w, y, z), (x + w, y, z + h), (x, y, z + h)],
            "right": [(x + w, y, z), (x + w, y + d, z), (x + w, y + d, z + h),
                      (x + w, y, z + h)],
            "left": [(x, y + d, z), (x, y, z), (x, y, z + h), (x, y + d, z + h)],
            "back": [(x + w, y + d, z), (x, y + d, z), (x, y + d, z + h),
                     (x + w, y + d, z + h)],
            "top": [(x, y, z + h), (x + w, y, z + h), (x + w, y + d, z + h),
                    (x, y + d, z + h)],
        }
        del fp
        for name, p in pts.items():
            t = top_tag if name == "top" and top_tag else (
                front_tag if name == "front" and front_tag else tag)
            self.poly(p, color, t, opacity)

    def cylinder(self, cx, cy, z0, z1, r0, r1=None, color="#888", segs=28, tag="",
                 axis="z", top=True, opacity=1.0):
        self.next_part()
        r1 = r0 if r1 is None else r1
        ring0, ring1 = [], []
        for i in range(segs):
            a = 2 * math.pi * i / segs
            c, s = math.cos(a), math.sin(a)
            if axis == "z":
                ring0.append((cx + r0 * c, cy + r0 * s, z0))
                ring1.append((cx + r1 * c, cy + r1 * s, z1))
            elif axis == "x":       # cx is y-centre, cy is z-centre, z0..z1 is x span
                ring0.append((z0, cx + r0 * c, cy + r0 * s))
                ring1.append((z1, cx + r1 * c, cy + r1 * s))
            else:                   # axis y: cx is x-centre, cy is z-centre
                ring0.append((cx + r0 * c, z0, cy + r0 * s))
                ring1.append((cx + r1 * c, z1, cy + r1 * s))
        for i in range(segs):
            j = (i + 1) % segs
            self.poly([ring0[i], ring0[j], ring1[j], ring1[i]], color, tag, opacity,
                      two_sided=False)
            self.poly([ring0[j], ring0[i], ring1[i], ring1[j]], color, tag, opacity,
                      two_sided=False)
        if top:
            self.poly(list(reversed(ring0)), color, tag, opacity)
            self.poly(ring1, color, tag, opacity)

    def overlay(self, fn):
        self.overlays.append(fn)


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def rounded_rect(x0, y0, x1, y1, r, steps=5):
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90),
                       (x0 + r, y0 + r, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def circle(cx, cy, r, segs=36):
    return [(cx + r * math.cos(2 * math.pi * i / segs), cy + r * math.sin(2 * math.pi * i / segs))
            for i in range(segs)]


# --------------------------------------------------------------------------- #
# projection and SVG output
# --------------------------------------------------------------------------- #
class Projector:
    def __init__(self, scale, ox, oy):
        self.scale, self.ox, self.oy = scale, ox, oy

    def __call__(self, p):
        return (self.ox + _dot(p, _R) * self.scale, self.oy - _dot(p, _U) * self.scale)

    def path(self, pts):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in (self(p) for p in pts))


W, H = 400, 300


def _lit(face: Face, light=None, ambient: float = 0.62) -> str:
    diffuse = max(0.0, _dot(face.normal, light or LIGHT))
    k = ambient + 0.55 * diffuse
    base = face.color
    col = shade(base, k)
    if k > 1.0:                 # light colours get brighter, not over-saturated
        col = mix(shade(base, 1.0), "#ffffff", min(0.5, (k - 1.0) * 0.9))
    return col


_DEFS = ('<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
           '<stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#eceef0"/>'
           '</linearGradient>'
           '<radialGradient id="sh"><stop offset="0" stop-color="#000" stop-opacity="0.28"/>'
           '<stop offset="1" stop-color="#000" stop-opacity="0"/></radialGradient>'
           '<pattern id="grain" width="14" height="5" patternUnits="userSpaceOnUse" '
           'patternTransform="rotate(-14)"><path d="M0 2.5 Q3.5 1 7 2.5 T14 2.5" '
           'stroke="#000" stroke-opacity="0.10" fill="none" stroke-width="0.8"/></pattern>'
           '<pattern id="mesh" width="4" height="4" patternUnits="userSpaceOnUse">'
           '<path d="M0 0 L4 4 M4 0 L0 4" stroke="#000" stroke-opacity="0.22" '
           'stroke-width="0.6"/></pattern>'
           '<linearGradient id="glass" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#2b3a4a"/><stop offset="0.55" stop-color="#0d1218"/>'
           '<stop offset="1" stop-color="#1c2833"/></linearGradient>'
           '<linearGradient id="gloss" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#fff" stop-opacity="0.18"/>'
           '<stop offset="0.45" stop-color="#fff" stop-opacity="0.04"/>'
           '<stop offset="0.46" stop-color="#fff" stop-opacity="0"/></linearGradient>'
           '<radialGradient id="glow"><stop offset="0" stop-color="#fff6c8" '
           'stop-opacity="0.9"/><stop offset="1" stop-color="#fff6c8" stop-opacity="0"/>'
           '</radialGradient>')


def to_svg(scene: Scene) -> str:
    pts2 = [(_dot(p, _R), -_dot(p, _U)) for f in scene.faces for p in f.pts]
    xs, ys = [p[0] for p in pts2], [p[1] for p in pts2]
    w, h = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    scale = min((W - 70) / w, (H - 80) / h)
    ox = (W - w * scale) / 2 - min(xs) * scale
    oy = (H - 30 - h * scale) / 2 - min(ys) * scale + 6
    proj = Projector(scale, ox, oy)

    # floor shadow under the footprint
    zs = [p[2] for f in scene.faces for p in f.pts]
    floor = min(zs)
    foot = [p for f in scene.faces for p in f.pts if p[2] <= floor + 0.5]
    if foot:
        fx = [proj(p)[0] for p in foot]
        fy = [proj(p)[1] for p in foot]
        sx, sy = (min(fx) + max(fx)) / 2, max(fy)
        rx = max(30, (max(fx) - min(fx)) / 2 + 14)
    else:
        sx, sy, rx = W / 2, H - 40, 120
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">',
           f'<defs>{_DEFS}</defs>',
           f'<rect width="{W}" height="{H}" fill="url(#bg)"/>',
           f'<ellipse cx="{sx:.1f}" cy="{sy + 4:.1f}" rx="{rx:.1f}" ry="{max(8, rx * 0.16):.1f}" '
           f'fill="url(#sh)"/>']
    faces = sorted(scene.faces, key=lambda f: (f.part, sum(_dot(p, V) for p in f.pts)
                                               / len(f.pts)))
    for f in faces:
        d = proj.path(f.pts)
        col = _lit(f)
        op = f' fill-opacity="{f.opacity}"' if f.opacity < 1 else ""
        out.append(f'<polygon points="{d}" fill="{col}"{op} stroke="{shade(col, 0.82)}" '
                   f'stroke-width="0.5" stroke-linejoin="round"/>')
        if f.tag == "wood":
            out.append(f'<polygon points="{d}" fill="url(#grain)"/>')
        elif f.tag == "mesh":
            out.append(f'<polygon points="{d}" fill="url(#mesh)"/>')
        elif f.tag == "screen":
            out.append(f'<polygon points="{d}" fill="url(#glass)"/>'
                       f'<polygon points="{d}" fill="url(#gloss)"/>')
        elif f.tag == "gloss":
            out.append(f'<polygon points="{d}" fill="url(#gloss)"/>')
    for fn in scene.overlays:
        out.append(fn(proj))
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# product models (all units in inches)
# --------------------------------------------------------------------------- #
def _frame_color(p) -> str:
    return SILVER if p.get("color") in ("White", "Natural Oak") else GRAPHITE


def _top_tag(p) -> str:
    return "wood" if p.get("color") in WOOD else "gloss"


def m_desk(s: Scene, p, c):
    d = p.get("dims") or {}
    w, dp, h = d.get("w", 48), d.get("d", 24), d.get("h", 30)
    fc, t = _frame_color(p), 1.4
    if p.get("u_shaped"):          # credenza at the back, bridge on the right, desk in front
        back, front, bridge = 20, 30, 24
        tag = _top_tag(p)
        s.box(0, dp - back, 0, w, back, h - t, shade(c, 0.92))                   # credenza
        for i in range(3):
            s.box(2 + i * (w - 4) / 3, dp - back - 0.3, 2, (w - 4) / 3 - 1.5, 0.3, h - t - 4,
                  shade(c, 1.04))
        s.box(0, dp - back, h - t, w, back, t, c, top_tag=tag)
        s.splits.append(s.part + 1)
        s.box(w - bridge + 1, front, 0, 2, 2, h - t, fc)                           # bridge leg
        s.box(w - bridge, front, h - t, bridge, dp - back - front, t, c, top_tag=tag)
        s.splits.append(s.part + 1)  # in a room, the chair inside the U draws between
        s.box(w - 18, 1.5, 0, 16, front - 3, h - t, shade(c, 0.95))               # pedestal
        for z in (4, 13, 21):
            s.box(w - 12, 1.1, z, 4, 0.4, 0.8, SILVER)
        s.box(1, 1, 0, 2, front - 2, h - t, c)                                    # end panel
        s.box(2.5, front - 2.5, h - t - 9, w - 20, 0.8, 9, shade(c, 0.9))          # modesty
        s.box(0, 0, h - t, w, front, t, c, top_tag=tag)
        return
    if p.get("l_shaped"):
        ret = 22
        fp = [(0, 0), (w, 0), (w, dp), (w - ret, dp), (w - ret, ret), (0, ret)]
        for x, y in ((w - 3, dp - 3), (w - ret + 1, dp - 3), (1, ret - 3), (1, 1), (w - 3, 1)):
            s.box(x, y, 0, 2, 2, h - t, fc)
        s.prism_z(fp, h - t, h, c, top_tag=_top_tag(p))
        return
    if p.get("standing"):
        for x in (w * 0.12, w * 0.88 - 3):
            s.box(x - 1, dp * 0.15, 0, 5, dp * 0.7, 1.4, fc)                 # foot
            s.box(x, dp / 2 - 1.6, 1.4, 3, 3.2, h - t - 2.8, shade(fc, 1.1))  # column
        s.box(w * 0.12, dp / 2 - 1, h - t - 3, w * 0.76, 2, 1.8, fc)          # crossbar
        s.box(0, 0, h - t, w, dp, t, c, top_tag=_top_tag(p))
        s.box(w * 0.8, -0.4, h - t - 1.2, 4, 0.6, 1.0, "#222")                # keypad
        return
    if p.get("benching"):
        for x in (0, w / 2 - 1, w - 2):
            s.box(x, 1, 0, 2, dp - 2, 2, fc)
            s.box(x, dp / 2 - 1, 2, 2, 2, h - t - 2, fc)
        s.box(0, 0, h - t, w, dp, t, c, top_tag=_top_tag(p))
        s.box(1, dp / 2 - 0.4, h, w - 2, 0.8, 12, "#d9dde1", opacity=0.92)    # privacy screen
        return
    legc = c if p.get("style") == "classic" else fc
    s.box(2.5, dp - 2.5, h - t - 6, w - 5, 0.8, 6, shade(c, 0.9))              # modesty panel
    for x, y in ((1, dp - 3), (w - 3, dp - 3), (1, 1), (w - 3, 1)):
        s.box(x, y, 0, 2, 2, h - t, legc)
    if w >= 55:
        s.box(w - 18, 1.5, h - t - 9, 15, dp - 4, 9, c)
        s.box(w - 11.5, 1.1, h - t - 5, 2.6, 0.5, 0.8, SILVER)
    s.box(0, 0, h - t, w, dp, t, c, top_tag=_top_tag(p))
    if p.get("panel"):             # cubicle: fabric privacy panel along the back and side
        s.box(0, dp - 1.4, h, w, 1.4, 16, "#c3c8ce", opacity=0.95)
        s.box(0, dp - 1.5, h + 16, w, 1.6, 0.8, SILVER)
        s.box(0, 0, h, 1.4, dp - 1.4, 16, "#c3c8ce", opacity=0.95)


def m_monitor_arm(s: Scene, p, c):
    """Desk clamp, pole and two articulated arms with VESA plates."""
    s.box(-3, -3, 0, 6, 6, 1.2, c)                                               # clamp
    s.box(-1.5, -3, -3, 3, 1.2, 3, c)
    s.cylinder(0, 0, 1.2, 16, 0.9, color=SILVER if c == SILVER else c, segs=16)  # pole
    for side in (-1, 1):
        s.prism_y([(0, 13.5), (side * 9, 15), (side * 9, 16.5), (0, 15)], -0.8, 0.8, c)
        s.prism_y([(side * 9, 15), (side * 17, 15.5), (side * 17, 17), (side * 9, 16.5)],
                  -0.8, 0.8, shade(c, 1.1))
        s.box(side * 17 - 2.5, -1.8, 13, 5, 1, 5, shade(c, 0.85))                # VESA plate


def m_chair(s: Scene, p, c):
    name = p.get("name", "")
    fc = GRAPHITE if p.get("color") != "White" else "#9aa0a6"
    if "Stacking" in name or "Guest" in name:
        for x, y in ((3, 16.5), (17.5, 16.5), (3, 3), (17.5, 3)):
            s.box(x, y, 0, 1.5, 1.5, 17, fc)
        s.prism_y([(3, 21), (19, 21), (19, 33), (3, 33)], 17.5, 19.5, c)
        s.prism_z(rounded_rect(2, 2, 20, 19, 3), 17, 19.5, c)
        if "Guest" in name:
            for x in (0.5, 20):
                s.box(x, 4, 24, 1.6, 13, 1.2, fc)
                s.box(x, 5, 19.5, 1.2, 1.2, 4.5, fc)
        return
    if "Stool" in name:
        seat = 26
        for x, y in ((2, 2), (14, 2), (2, 14), (14, 14)):
            s.box(x, y, 0, 1.6, 1.6, seat, fc)
        s.prism_z([(1.5, 1.5), (16.5, 1.5), (16.5, 16.5), (1.5, 16.5)], 9, 9.8, fc)  # footrest
        s.prism_z(circle(9, 9, 8.5, 28), seat, seat + 2, c)
        s.prism_y([(3, seat + 6), (15, seat + 6), (15, seat + 11), (3, seat + 11)], 15.5, 17, c)
        s.box(4, 15.8, seat + 2, 1.2, 1.2, 4, fc)
        s.box(13, 15.8, seat + 2, 1.2, 1.2, 4, fc)
        return
    tall = "Drafting" in name
    seat_z = 26 if tall else 18
    # five-star base with casters
    cx, cy = 11, 11
    for i in range(5):
        a = math.radians(90 + 72 * i)
        ex, ey = cx + 12 * math.cos(a), cy + 12 * math.sin(a)
        nx, ny = -math.sin(a) * 0.9, math.cos(a) * 0.9
        s.prism_z([(cx - nx, cy - ny), (cx + nx, cy + ny), (ex + nx, ey + ny),
                   (ex - nx, ey - ny)], 2.2, 3.6, fc)
        s.cylinder(ex, ey, 0, 2.2, 1.1, color="#1d1f22", segs=14)
    s.cylinder(cx, cy, 3.6, seat_z - 2, 1.4, color=SILVER if not tall else fc, segs=18)
    if tall:
        s.prism_z(circle(cx, cy, 8, 24), 11, 11.6, fc)                         # foot ring
    exec_ = "Executive" in name or "Big & Tall" in name
    back_h = 24 if exec_ else 19
    back = [(2.5, seat_z + 3), (19.5, seat_z + 3), (20.5, seat_z + back_h - 4),
            (18, seat_z + back_h), (4, seat_z + back_h), (1.5, seat_z + back_h - 4)]
    s.box(9.5, 19.5, seat_z, 3, 1.5, 6, fc)                                     # spine
    s.prism_y(back, 20, 22.5, c, front_tag="mesh" if "Mesh" in name or "Ergonomic" in name
              else "")
    if "Lumbar" in name or "Ergonomic" in name:
        s.prism_y([(5, seat_z + 6), (17, seat_z + 6), (17, seat_z + 9), (5, seat_z + 9)],
                  19.4, 20, shade(c, 0.85))
    s.prism_z(rounded_rect(1, 1, 21, 20, 4), seat_z - 2, seat_z + 1.5, c)       # seat
    if exec_ or "Ergonomic" in name or "Mesh" in name:
        for x in (-1.2, 21.2):
            s.box(x, 6, seat_z + 1.5, 1.4, 1.4, 6.5, fc)
            s.box(x - 0.4, 3.5, seat_z + 8, 2.2, 11, 1.4, "#26292c")


def m_conference_table(s: Scene, p, c):
    d = p.get("dims") or {}
    w, dp, h = d.get("w", 96), d.get("d", 42), d.get("h", 30)
    fc = _frame_color(p)
    if "Round" in p.get("name", ""):
        s.prism_z(circle(w / 2, dp / 2, 11, 24), 0, 1.5, fc)
        s.cylinder(w / 2, dp / 2, 1.5, h - 1.5, 2.2, color=fc)
        s.prism_z(circle(w / 2, dp / 2, w / 2), h - 1.5, h, c, top_tag=_top_tag(p))
        return
    if "Boat" in p.get("name", ""):
        fp = [(0, dp * 0.18), (w * 0.5, 0), (w, dp * 0.18), (w, dp * 0.82), (w * 0.5, dp),
              (0, dp * 0.82)]
    else:
        fp = rounded_rect(0, 0, w, dp, 3)
    for x in (w * 0.22, w * 0.78):
        s.box(x - 3, dp * 0.15, 0, 6, dp * 0.7, 1.5, fc)
        s.box(x - 2, dp / 2 - 2, 1.5, 4, 4, h - 3, fc)
    s.prism_z(fp, h - 1.5, h, c, top_tag=_top_tag(p))


def m_storage(s: Scene, p, c):
    d = p.get("dims") or {}
    w, dp, h = d.get("w", 30), d.get("d", 15), d.get("h", 40)
    name = p.get("name", "")
    tag = "wood" if p.get("color") in WOOD else ""
    if "Credenza" in name:
        s.box(0, 0, 3, w, dp, h - 3, c, top_tag=tag)
        for x in (2, w - 4):
            s.box(x, 2, 0, 2, 2, 3, GRAPHITE)
            s.box(x, dp - 4, 0, 2, 2, 3, GRAPHITE)
        doors = 3 if w >= 60 else 2
        dw = (w - 4) / doors
        for i in range(doors):
            s.box(2 + i * dw + 0.4, -0.3, 5, dw - 0.8, 0.3, h - 9, shade(c, 1.05), tag=tag)
            s.box(2 + i * dw + dw / 2 - 2, -0.7, h - 10, 4, 0.4, 0.8, SILVER)
        return
    if "Bookcase" in name:
        shelves = 3 if "3-Shelf" in name else 5
        s.box(0, dp - 0.6, 0, w, 0.6, h, shade(c, 0.85), tag=tag)              # back
        s.box(0, 0, 0, 1, dp, h, c, tag=tag)                                   # left side
        books = ["#8c2f39", "#24527a", "#d9a441", "#3a7d44", "#5b4b8a", "#c0c0c0"]
        for i in range(shelves + 1):
            z = i * (h - 1) / shelves
            s.box(1, 0, z, w - 2, dp - 0.6, 1, c, top_tag=tag)
            if i < shelves:
                bx, k = 2.0, 0
                gap = (h - 1) / shelves
                while bx < w - 6 and (i + k) % 7 != 6:
                    bw = 1.2 + (k % 3) * 0.5
                    s.box(bx, 2, z + 1, bw, dp - 4, gap * (0.62 + 0.08 * (k % 3)),
                          books[(i + k) % len(books)])
                    bx += bw + 0.15
                    k += 1
        s.box(w - 1, 0, 0, 1, dp, h, c, tag=tag)                               # right side
        return
    s.box(0, 0, 0, w, dp, h, c, top_tag=tag)
    if "Doors" in name:
        s.box(w / 2 - 0.1, -0.15, 1, 0.2, 0.2, h - 2, shade(c, 0.6))
        for x in (w / 2 - 2.2, w / 2 + 1.6):
            s.box(x, -0.5, h * 0.5, 0.6, 0.5, 5, SILVER)
        return
    drawers = 4 if "4-Drawer" in name else (3 if "3-Drawer" in name else 2)
    if "Pedestal" in name:
        s.cylinder(2, 2, -1.2, 0, 1, color="#1d1f22", segs=12)
    dh = (h - 1.5) / drawers
    for i in range(drawers):
        z = 0.8 + i * dh
        s.box(0.6, -0.4, z, w - 1.2, 0.4, dh - 0.6, shade(c, 1.04))
        s.box(w / 2 - 3, -0.8, z + dh * 0.62, 6, 0.4, 0.9, SILVER)


def m_printer(s: Scene, p, c):
    big = int(p.get("users_supported") or 5) >= 20
    w, dp, h = (22, 20, 30) if big else (17, 14, 9 if p.get("printer_type") == "inkjet" else 11)
    trim = "#2a2d31" if c in ("#F2F2F2", "#BFC4C9") or p.get("color") in ("White", "Gray") \
        else "#5a5f66"
    s.box(0, 0, 0, w, dp, h, c, top_tag="gloss")
    s.box(1, 1, h, w - 2, dp - 2, 1.6, shade(c, 0.97), top_tag="gloss")            # lid
    s.box(w * 0.18, -0.4, h * (0.12 if not big else 0.42), w * 0.64, 0.4, h * 0.16,
          "#1f2226")                                                               # output slot
    s.box(w * 0.2, -3.5, h * (0.1 if not big else 0.4), w * 0.6, 3.5, 0.6, trim)   # tray
    s.box(w * 0.56, 0.6, h + 1.6, w * 0.32, 3.2, 0.5, "#111", top_tag="screen")    # panel
    if p.get("ink_tank"):                     # supertank: refillable tanks on the front
        for i, col in enumerate(("#1c1c1c", "#00a3e0", "#e6007e", "#ffd400")):
            s.box(w + 0.2, 1 + i * 2.6, 1, 2.2, 2.2, h - 2, col, opacity=0.9)
    if big:
        for i in range(2):
            z = 1 + i * 6
            s.box(0.6, -0.3, z, w - 1.2, 0.3, 5.4, shade(c, 1.03))
            s.box(w / 2 - 3, -0.6, z + 3.5, 6, 0.3, 0.8, trim)


INK = {"Black": "#1c1c1c", "Cyan": "#00a3e0", "Magenta": "#e6007e", "Yellow": "#ffd400"}


def _ink_colours(p) -> list[str]:
    """The colours in this pack: explicit `ink_colors`, else from its colour/name."""
    if p.get("ink_colors"):
        return [INK.get(x, x) for x in p["ink_colors"]]
    if p.get("color") in INK and p.get("color") != "Black":
        return [INK[p["color"]]]
    name = p.get("name", "")
    if "Tri-Color" in name or "Cyan/Magenta/Yellow" in name:
        return [INK["Cyan"], INK["Magenta"], INK["Yellow"]]
    return [INK["Black"]]


def m_cartridge(s: Scene, p, c):
    style = p.get("cart_style") or ("ink" if "Ink" in p.get("name", "") else "toner")
    cols = _ink_colours(p)
    xl = p.get("xl")
    gold = "#c9a227"
    if style == "bottle":                     # ink bottles for a supertank printer
        for i, col in enumerate(cols):
            x = i * 3.4
            s.cylinder(x, 0, 0, 6.5, 1.4, color="#f2f2f2", segs=20)
            s.cylinder(x, 0, 1.2, 5.2, 1.42, color=col, segs=20, top=False, opacity=0.85)
            s.cylinder(x, 0, 6.5, 8.2, 0.8, color=col, segs=16)
        return
    if style == "drum":                       # imaging drum: grey body, green drum
        s.box(0, 0, 0, 15, 4.5, 3.5, "#6b7075", top_tag="gloss")
        s.box(0.5, -0.4, 0.6, 14, 1.2, 2.2, "#2e7d32", top_tag="gloss")           # drum
        s.box(5.5, 1.2, 3.5, 4, 2, 1.2, "#3a3f44")                               # handle
        return
    if style == "toner":                      # long laser toner(s), one per colour
        L = (12 if xl else 10) if p.get("compact_toner") else (16 if xl else 13)
        H = 6 if p.get("compact_toner") else 4.2       # colour-laser toners: squarer
        body = "#3b3f44" if p.get("compatible") else "#24272b"
        for i, col in enumerate(cols):
            y = i * 5.2
            s.box(0, y, 0, L, 4.4, H, body, top_tag="gloss")
            s.box(0.8, y - 0.05, H * 0.3, L - 1.6, 0.05, 1.4, col if col != INK["Black"]
                  else "#55595e")
            s.box(L / 2 - 2, y + 1.2, H, 4, 2, 1.1, "#3a3f44")                       # handle
            if xl:
                s.box(L - 3, y - 0.06, H * 0.7, 2.4, 0.06, 0.8, gold)
            if p.get("compatible"):                                                   # eco label
                s.box(1, y - 0.06, H * 0.72, 3, 0.06, 0.9, "#3a9d4a")
        return
    # ink: a retail box with the colour bands, cartridges in front
    bw = 7.5 if xl else 6
    s.box(0, 6, 0, bw, 3.4, 9 if xl else 7.5, "#f4f4f4", top_tag="gloss")
    for i, col in enumerate(cols[:3]):
        s.box(0, 5.95, 2.2 + i * 1.2, bw, 0.05, 1.1, col)
    s.box(0, 5.94, 6.0, bw, 0.06, 0.8, gold if xl else "#CC0000")
    groups = cols if p.get("ink_colors") and len(p["ink_colors"]) > 1 else [cols]
    for j, g in enumerate(groups):
        x = j * 3.4
        s.box(x, 0, 0, 2.8, 2.6, 4.6, "#2a2a2a", top_tag="gloss")
        bands = g if isinstance(g, list) else [g]
        for k, col in enumerate(bands):
            s.box(x + k * 2.8 / len(bands), -0.05, 2.4, 2.8 / len(bands), 0.05, 1.2, col)


def m_tv(s: Scene, p, c):
    size = float(p.get("screen_in") or 55)
    w, h = size * 0.87, size * 0.49
    bezel = {"White": "#e9ebee", "Silver": "#aeb3b8", "Gray": "#8a8d91"}.get(
        p.get("color"), "#1b1c1e")                  # the frame and stand in the SKU's colour
    is_monitor = p.get("kind") == "monitor"
    z0 = h * 0.28 if is_monitor else (0 if p.get("wall_mount") else 3.5)
    # stand first: the screen sits in front of it
    if p.get("wall_mount"):
        pass
    elif is_monitor:
        s.prism_z(rounded_rect(w / 2 - 6, -2, w / 2 + 6, 6, 2), 0, 1, bezel)
        s.box(w / 2 - 1.5, 1.4, 1, 3, 1.8, z0 + h * 0.4, bezel)
    else:
        for x in (w * 0.12, w * 0.82):
            s.prism_z([(x, -4), (x + 6, -4), (x + 5, 5), (x + 1, 5)], 0, 0.8, bezel)
            s.box(x + 2.5, 0.5, 0.8, 1, 1, z0 - 0.8, bezel)
    s.box(0, 0, z0, w, 1.4, h, bezel)
    s.next_part()
    s.poly([(0.6, -0.02, z0 + 0.6), (w - 0.6, -0.02, z0 + 0.6), (w - 0.6, -0.02, z0 + h - 0.6),
            (0.6, -0.02, z0 + h - 0.6)], "#0f1418", "screen")


def m_hdmi_cable(s: Scene, p, c):
    col = {"Black": "#1e1e1e", "White": "#f0f0f0", "Gray": "#8a8d91"}.get(p.get("color"), c)
    s.box(-3, 2, 0, 3.6, 1.6, 1.1, "#2b2b2b")
    s.box(-4.2, 2.3, 0.2, 1.2, 1.0, 0.7, "#c9a227")
    s.box(17, 9, 0, 3.6, 1.6, 1.1, "#2b2b2b")
    s.box(20.6, 9.3, 0.2, 1.2, 1.0, 0.7, "#c9a227")

    def coil(proj):
        paths = []
        for i, (r, z) in enumerate(((7.5, 0.6), (6.6, 1.5), (7.0, 2.4), (6.2, 3.2))):
            pts = [proj((9 + r * math.cos(a / 30 * 2 * math.pi),
                         6 + r * 0.7 * math.sin(a / 30 * 2 * math.pi), z)) for a in range(31)]
            d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            paths.append(f'<path d="{d}" stroke="{shade(col, 0.75)}" stroke-width="7.5" '
                         f'fill="none" stroke-linecap="round"/>'
                         f'<path d="{d}" stroke="{col}" stroke-width="5.2" fill="none" '
                         f'stroke-linecap="round"/>'
                         f'<path d="{d}" stroke="#fff" stroke-opacity="0.18" '
                         f'stroke-width="1.4" fill="none"/>')
        a, b = proj((0.6, 2.8, 0.5)), proj((2, 6, 0.6))
        e, f2 = proj((17, 9.8, 0.5)), proj((15.5, 7, 3))
        tail = (f'<path d="M{a[0]:.1f},{a[1]:.1f} Q{b[0]:.1f},{b[1]:.1f} {b[0] + 10:.1f},'
                f'{b[1] - 4:.1f}" stroke="{col}" stroke-width="5" fill="none"/>'
                f'<path d="M{e[0]:.1f},{e[1]:.1f} Q{f2[0]:.1f},{f2[1]:.1f} {f2[0] - 10:.1f},'
                f'{f2[1] - 3:.1f}" stroke="{col}" stroke-width="5" fill="none"/>')
        return tail + "".join(paths)
    s.overlay(coil)


def m_power(s: Scene, p, c):
    if p.get("kind") == "cable_mgmt":
        for x in (0, 22):
            s.box(x, 0, 0, 0.6, 5, 4, c)
        s.box(0, 0, 0, 22.6, 5, 0.4, c)
        s.box(0, 4.6, 0, 22.6, 0.4, 4, c)
        s.box(0, 0, 0, 22.6, 0.4, 1.6, c)
        for x in range(2, 21, 3):
            s.box(x, 0.4, 0.4, 0.3, 4.2, 0.3, shade(c, 0.8))
        return
    n = 12 if "12" in p.get("name", "") else 6
    w = 2.1 * n / (2 if n == 12 else 1) + 3
    rows = 2 if n == 12 else 1
    s.prism_z(rounded_rect(0, 0, w, 2.8 * rows + 0.6, 0.8), 0, 1.6, c, top_tag="gloss")
    for r in range(rows):
        for i in range(n // rows):
            x, y = 2 + i * 2.1, 0.7 + r * 2.8
            s.box(x, y, 1.6, 1.2, 1.4, 0.05, "#2a2a2a")
    s.box(w - 2.2, 0.7, 1.6, 1.5, 1.2, 0.5, "#cc2b2b")                                # switch

    def cord(proj):
        a, b, e = proj((0, 1.5, 0.6)), proj((-8, 3, 0.5)), proj((-10, 12, 0.5))
        return (f'<path d="M{a[0]:.1f},{a[1]:.1f} Q{b[0]:.1f},{b[1]:.1f} {e[0]:.1f},'
                f'{e[1]:.1f}" stroke="{shade(c, 0.7)}" stroke-width="3.5" fill="none" '
                f'stroke-linecap="round"/>')
    s.overlay(cord)


def m_lamp(s: Scene, p, c):
    if "Floor" in p.get("name", ""):
        s.cylinder(0, 0, 0, 1.2, 6, color=c, segs=24)
        s.cylinder(0, 0, 1.2, 60, 0.5, color=c, segs=10)

        def arc(proj):
            pts = [proj((math.sin(t / 20 * math.pi / 2) * 22, 0,
                         60 + math.sin(t / 20 * math.pi) * 8 - t * 0.3)) for t in range(21)]
            d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            return f'<path d="{d}" stroke="{shade(c, 0.8)}" stroke-width="3" fill="none"/>'
        s.overlay(arc)
        s.cylinder(22, 0, 48, 54, 7, 3, color=c, segs=24, top=False)
        s.overlay(lambda proj: f'<ellipse cx="{proj((22, 0, 46))[0]:.1f}" '
                               f'cy="{proj((22, 0, 46))[1]:.1f}" rx="26" ry="10" '
                               f'fill="url(#glow)"/>')
        return
    s.cylinder(0, 0, 0, 1, 3.5, color=c, segs=24)
    s.prism_y([(-0.4, 1), (0.4, 1), (5.4, 11), (4.6, 11)], -0.4, 0.4, c)
    s.prism_y([(4.6, 11), (5.4, 11), (11.4, 15.2), (10.8, 15.8)], -0.4, 0.4, c)
    s.cylinder(12.6, 13.6, 0.5, 0.0, 1.2, 3.2, color=c, axis="y", segs=20)
    s.overlay(lambda proj: f'<ellipse cx="{proj((12.6, 0, 12))[0]:.1f}" '
                           f'cy="{proj((12.6, 0, 10))[1]:.1f}" rx="22" ry="12" '
                           f'fill="url(#glow)"/>')


def m_whiteboard(s: Scene, p, c):
    w, h = float(p.get("board_w_ft") or 4) * 12, float(p.get("board_h_ft") or 3) * 12
    frame = SILVER if p.get("color") != "Black" else "#2a2a2a"
    z0 = 30 if p.get("mobile") else 0
    if p.get("mobile"):
        for x in (2, w - 4):
            s.box(x, 0, 2, 2, 2, z0 + h, frame)
            s.box(x - 6, -6, 0.5, 14, 2, 1.6, frame)
            s.cylinder(x - 5, -5, -1.5, 0.5, 1.2, color="#1d1f22", segs=12)
            s.cylinder(x + 7, -5, -1.5, 0.5, 1.2, color="#1d1f22", segs=12)
    s.box(0, 0, z0, w, 1.2, h, frame)
    s.poly([(1.2, -0.02, z0 + 1.2), (w - 1.2, -0.02, z0 + 1.2), (w - 1.2, -0.02, z0 + h - 1.2),
            (1.2, -0.02, z0 + h - 1.2)], "#fbfbfb", "gloss")
    s.box(w * 0.25, -2.2, z0 - 0.5, w * 0.5, 2.4, 0.9, frame)                        # tray

    def doodle(proj):
        def pt(u, v):
            return proj((1.2 + u * (w - 2.4), -0.05, z0 + 1.2 + v * (h - 2.4)))
        a, b, cc, d2 = pt(0.1, 0.75), pt(0.25, 0.9), pt(0.4, 0.62), pt(0.55, 0.78)
        e, f2 = pt(0.12, 0.35), pt(0.5, 0.35)
        g, hh = pt(0.62, 0.25), pt(0.88, 0.6)
        return (f'<path d="M{a[0]:.1f},{a[1]:.1f} Q{b[0]:.1f},{b[1]:.1f} {cc[0]:.1f},'
                f'{cc[1]:.1f} T{d2[0]:.1f},{d2[1]:.1f}" stroke="#2e6db4" stroke-width="2" '
                f'fill="none"/><line x1="{e[0]:.1f}" y1="{e[1]:.1f}" x2="{f2[0]:.1f}" '
                f'y2="{f2[1]:.1f}" stroke="#cc0000" stroke-width="2"/>'
                f'<rect x="{min(g[0], hh[0]):.1f}" y="{min(g[1], hh[1]):.1f}" '
                f'width="{abs(hh[0] - g[0]):.1f}" height="{abs(hh[1] - g[1]):.1f}" '
                f'stroke="#3a7d44" stroke-width="1.6" fill="none"/>')
    s.overlay(doodle)


def m_markers(s: Scene, p, c):
    n = min(8, int(p.get("pack_qty") or 4))
    perm = p.get("kind") == "permanent_marker"
    caps = ([c] * n if p.get("color") not in (None, "Black") and perm else
            (["#1c1c1c", "#2e6db4", "#cc0000", "#3a7d44", "#7b3fa0", "#e67e22",
              "#1c1c1c", "#2e6db4"][:n]))
    body = "#f4f4f4" if not perm else "#d9d9d9"
    s.box(-1, -1, 0, n * 1.9 + 1, 3.4, 1.6, "#ffffff")                               # tray
    s.box(-1, -1.05, 0.2, n * 1.9 + 1, 0.05, 1.2, "#CC0000")
    for i in range(n):
        x = i * 1.9
        s.cylinder(x + 0.7, 0.7, 1.0, 5.6, 0.75, color=body, segs=16)
        s.cylinder(x + 0.7, 0.7, 5.6, 7.6, 0.8, color=caps[i], segs=16)


def m_eraser(s: Scene, p, c):
    if p.get("kind") in ("whiteboard_eraser", "whiteboard_cleaner"):
        if "Spray" in p.get("name", ""):
            s.cylinder(0, 0, 0, 7, 1.4, color="#e9eef2", segs=20)
            s.box(-0.6, -0.6, 7, 1.2, 1.2, 1.6, "#2e6db4")
            s.box(-0.3, -1.6, 7.6, 0.6, 1.0, 0.4, "#2e6db4")
            s.box(-1.4, -1.45, 2, 2.8, 0.1, 2.5, "#CC0000")
            return
        s.prism_z(rounded_rect(0, 0, 5, 2.2, 0.6), 0, 0.7, "#9aa0a6")
        s.prism_z(rounded_rect(0, 0, 5, 2.2, 0.8), 0.7, 1.9, "#2e6db4", top_tag="gloss")
        return
    for i, col in enumerate(("#f7c6cf", "#ffffff", "#f7c6cf")):
        s.prism_z(rounded_rect(i * 2.6, i * 0.4, i * 2.6 + 2.2, i * 0.4 + 1, 0.2), 0, 0.7, col)


PAPER = {"Ivory": "#fff6dc", "Pastel Blue": "#d6e8f7", "Yellow": "#fff3a6",
         "Pink": "#ffd6e0", "Green": "#d9f2d0", "Beige": "#f3e6c8"}
SHEETS = {"letter": (11, 8.5), "legal": (14, 8.5), "ledger": (17, 11), "photo": (6, 4)}


def m_paper(s: Scene, p, c):
    reams = int(p.get("reams") or 1)
    paper = PAPER.get(p.get("color"), "#ffffff")
    band = p.get("label") or "#CC0000"           # wrapper colour tells the grades apart
    L, W = SHEETS.get(p.get("sheet") or "letter")
    if reams > 1:                                 # carton, one ream on top
        s.box(0, 0, 0, 18, 12, 10.5, "#c99b62")
        s.box(0, -0.05, 6.5, 18, 0.05, 2.5, band)
        s.box(0, -0.06, 2, 7, 0.06, 3, "#ffffff")
        if p.get("recycled"):
            s.box(13, -0.07, 2, 3, 0.06, 3, "#3a9d4a")
        s.box(3.5, 1.5, 10.5, 11, 8.5, 2.2, paper)
        s.box(3.5, 1.45, 11, 11, 0.05, 1.2, band)
        return
    if p.get("assorted"):                         # pastel stack
        for i, col in enumerate(("#fff3a6", "#ffd6e0", "#d6e8f7", "#d9f2d0")):
            s.box(i * 0.25, i * 0.25, i * 0.6, L, W, 0.6, col, top_tag="gloss")
        s.box(1, -0.05, 0.3, L - 2, 0.05, 0.8, band)
        return
    if p.get("sheet") == "photo":                 # small glossy pack
        s.box(0, 0, 0, L, W, 1.2, "#f4f4f4", top_tag="gloss")
        s.box(0.6, 0.6, 1.2, L - 1.2, W - 1.2, 0.05, "#7fb3d9", top_tag="gloss")
        s.box(0, -0.05, 0.3, L, 0.05, 0.6, band)
        return
    h = 1.2 if p.get("cardstock") else 2.2
    s.box(0, 0, 0, L, W, h, paper, top_tag="gloss")
    s.box(0, -0.05, h * 0.25, L, 0.05, h * 0.55, band)
    s.box(L + 0.02, 0, h * 0.25, 0.05, W, h * 0.55, band)


def m_supply(s: Scene, p, c):
    """Everyday desk supplies, one simple model each."""
    k = p.get("kind")
    if k == "notebook":
        for i in range(3):
            s.box(i * 0.2, i * 0.2, i * 0.5, 8, 10.5, 0.5, c if i == 2 else shade(c, 0.9))
        for j in range(12):
            s.cylinder(0.2, 0.6 + j * 0.8, 1.6, 1.8, 0.28, color=SILVER, segs=8)
        return
    if k == "legal_pad":
        for i in range(3):
            s.box(i * 0.3, i * 0.3, i * 0.6, 8.5, 11.5, 0.6, c)
        s.box(0.6, 0.6, 1.8, 8.5, 1.4, 0.15, "#c0392b")                       # binding
        for j in range(6):
            s.box(1.2, 2.6 + j * 1.4, 1.81, 7, 0.08, 0.02, "#7fb3d9")
        return
    if k == "folders":
        for i in range(4):
            s.prism_y([(0, 0), (12, 0), (12, 9), (8, 9), (7.5, 9.6), (4, 9.6), (3.5, 9)],
                      i * 0.4, i * 0.4 + 0.2, "#e8d3a3")
        return
    if k == "binder":
        w = 2.2 if (p.get("ring_in") or 1) >= 2 else 1.5
        s.box(0, 0, 0, w, 10.5, 11.5, c, top_tag="gloss")                      # spine
        s.box(w, 0, 0, 0.2, 10.5, 11.5, shade(c, 0.85))
        s.box(-0.05, 1.5, 5, 0.05, 7.5, 3, "#ffffff")                          # label
        return
    if k == "scissors":
        for a in (-1, 1):
            s.prism_z([(0, 0), (8, a * 0.4), (8, a * 0.1), (0, -a * 0.3)], 0, 0.15, SILVER)
            s.prism_z(circle(-1.6, a * 1.1, 1.1, 18), 0, 0.5, c)
        return
    if k == "tape_dispenser":
        s.prism_y([(0, 0), (6, 0), (6.5, 2.2), (4, 3.4), (1, 3.4), (0, 2)], 0, 2.4, c)
        s.cylinder(3, 1.2, 2.2, 3.4, 1.6, color="#f3f1e6", axis="y", segs=20, opacity=0.8)
        return
    if k == "calculator":
        s.prism_z(rounded_rect(0, 0, 6, 8, 0.6), 0, 0.8, c)
        s.box(0.6, 5.6, 0.8, 4.8, 1.6, 0.05, "#a9b8a1")                        # display
        for r_ in range(4):
            for q in range(4):
                s.box(0.7 + q * 1.2, 0.6 + r_ * 1.2, 0.8, 0.9, 0.9, 0.25,
                      "#e8e8e8" if q < 3 else "#CC0000")
        return
    if k == "clips":
        for i, (x, y) in enumerate(((0, 0), (2.4, 0.4), (4.6, -0.2), (1.2, 2.4))):
            s.prism_y([(x, 0), (x + 2, 0), (x + 1.6, 1.4), (x + 0.4, 1.4)], y, y + 1.6,
                      "#1e1e1e")
            s.box(x + 0.3, y + 0.7, 1.4, 1.4, 0.15, 1.2, SILVER)
        return
    if k == "hole_punch":
        s.prism_z(rounded_rect(0, 0, 11, 3.6, 0.6), 0, 0.6, "#2a2a2a")
        s.prism_y([(0.5, 0.6), (10.5, 0.6), (10.5, 2.2), (8, 2.8), (3, 2.8), (0.5, 2.2)],
                  0.2, 3.2, c)
        return
    if k == "letter_tray":
        for i in range(3):
            z = i * 2.6
            s.box(0, 0, z, 10, 13, 0.3, c)
            s.box(0, 12.6, z, 10, 0.4, 2.2, c)
            s.box(0, 0, z, 0.4, 13, 2.2, c, opacity=0.9)
            s.box(9.6, 0, z, 0.4, 13, 2.2, c, opacity=0.9)
        return
    if k == "highlighter":
        for i, col in enumerate(("#f7ef4a", "#ff8fc7", "#8cf08c", "#ffb04a", "#7fd4ff",
                                 "#c79bff")[:int(p.get("pack_qty") or 4)]):
            s.box(i * 1.6, 0, 0, 1.3, 5.5, 1.0, col, top_tag="gloss")
            s.box(i * 1.6, 5.5, 0.15, 1.3, 1.2, 0.7, shade(col, 0.8))
        return
    if k == "envelopes":
        s.box(0, 0, 0, 10, 5, 4.5, "#ffffff", top_tag="gloss")                  # box
        s.box(0, -0.05, 3, 10, 0.05, 0.8, "#CC0000")
        s.prism_y([(1, 4.5), (9, 4.5), (5, 6.5)], 0.5, 0.6, "#f6f6f6")
        return
    if k == "index_cards":
        for i in range(4):
            s.box(i * 0.15, i * 0.15, i * 0.4, 5, 3, 0.4, "#ffffff" if i < 3 else c)
        return
    s.box(0, 0, 0, 6, 4, 2, c)


def m_desk_accessory(s: Scene, p, c):
    k = p.get("kind")
    if k == "pens":
        for i in range(5):
            s.cylinder(i * 1.1, 0.4, 0, 6, 0.38, color=c if i % 2 == 0 else "#f0f0f0",
                       axis="x", segs=12)
        return
    if k == "sticky_notes":
        for i, col in enumerate(("#ffe45c", "#ff9eb5", "#9be7a8", "#8ecbff")):
            s.box(i * 0.3, i * 0.3, i * 0.8, 3, 3, 0.8, col)
        return
    if k == "stapler":
        s.prism_z(rounded_rect(0, 0, 7, 1.8, 0.8), 0, 0.6, "#2a2a2a")
        s.prism_y([(0, 0.6), (7, 0.6), (7.2, 1.6), (6.6, 2.4), (0.4, 2.0)], 0.2, 1.6, c)
        return
    if "Riser" in p.get("name", ""):
        s.box(0, 0, 0, 22, 9, 4, c, top_tag="wood" if p.get("color") in WOOD else "gloss")
        s.box(3, -0.1, 0.6, 16, 0.1, 2.6, shade(c, 0.85))
        return
    s.box(0, 0, 0, 12, 6, 0.6, c)                                                     # organizer
    for x, w, h in ((0, 3.6, 4.5), (3.8, 3.6, 3.2), (7.6, 4.4, 2.2)):
        s.box(x, 0, 0.6, w, 6, h, c, opacity=0.95)


def m_bin(s: Scene, p, c):
    gal = float(p.get("gallons") or 7)
    big = gal >= 13
    r0, r1, h = (4.2, 5.4, 11) if gal < 5 else ((5.2, 6.6, 15) if not big else (6.5, 7.2, 24))
    if gal >= 20:
        for i, col in enumerate((c, "#2e6db4", "#3a7d44")):
            s.box(i * 9.4, 0, 0, 9, 9, 30, col, top_tag="gloss")
            s.box(i * 9.4 + 2.5, 2.5, 30, 4, 4, 0.05, "#1d1f22")
        return
    s.cylinder(0, 0, 0, h, r0, r1, color=c, segs=32, top=False)
    s.cylinder(0, 0, h - 0.6, h, r1 + 0.25, color=shade(c, 1.05), segs=32, top=False)

    def inside(proj):
        pts = [proj((r1 * math.cos(a / 24 * 2 * math.pi), r1 * math.sin(a / 24 * 2 * math.pi),
                     h)) for a in range(24)]
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        mark = ""
        if p.get("kind") == "recycling_bin":
            cx, cy = proj((0, -r0 - 0.5, h * 0.5))
            mark = (f'<text x="{cx:.1f}" y="{cy + 8:.1f}" font-size="26" text-anchor="middle" '
                    f'fill="#ffffff" font-family="Arial">♻</text>')
        return f'<polygon points="{d}" fill="{shade(c, 0.45)}"/>' + mark
    s.overlay(inside)


def m_rug(s: Scene, p, c):
    d = p.get("dims") or {}
    w, dp = d.get("w", 60), d.get("d", 84)
    if p.get("kind") == "chair_mat":
        s.prism_z([(0, 8), (36, 8), (36, 48), (0, 48)] if False else
                  [(0, 0), (36, 0), (36, 40), (22, 40), (22, 48), (14, 48), (14, 40), (0, 40)],
                  0, 0.35, "#dfe8ec", opacity=0.55)
        return
    s.prism_z([(0, 0), (w, 0), (w, dp), (0, dp)], 0, 0.6, c)

    def border(proj):
        def ring(m):
            return " ".join(f"{x:.1f},{y:.1f}" for x, y in (proj((a, b, 0.62)) for a, b in (
                (m, m), (w - m, m), (w - m, dp - m), (m, dp - m))))
        return (f'<polygon points="{ring(4)}" fill="none" stroke="{shade(c, 1.35)}" '
                f'stroke-width="2.2"/><polygon points="{ring(9)}" fill="none" '
                f'stroke="{shade(c, 0.75)}" stroke-width="1.2"/>')
    s.overlay(border)


def m_side_table(s: Scene, p, c):
    d = p.get("dims") or {}
    w, h = d.get("w", 24), d.get("h", 22)
    r = w / 2
    s.prism_z(circle(r, r, r * 0.55, 24), 0, 1, GRAPHITE)
    s.cylinder(r, r, 1, h - 1, 1.1, color=GRAPHITE, segs=14)
    s.prism_z(circle(r, r, r, 32), h - 1, h, c, top_tag=_top_tag(p))


def m_camera(s: Scene, p, c):
    s.box(0, 0, 0, 10, 4, 3, "#1b1c1e", top_tag="gloss")
    s.cylinder(5, 1.5, -0.9, 0.0, 1.2, color="#0b0d10", axis="y", segs=18)
    s.poly([(4.2, -0.95, 0.8), (5.8, -0.95, 0.8), (5.8, -0.95, 2.2), (4.2, -0.95, 2.2)],
           "#0f1418", "screen")


def m_video_bar(s: Scene, p, c):
    s.box(0, 0, 0, 30, 4, 3, "#1b1c1e")
    s.next_part()
    s.poly([(1, -0.02, 0.4), (12, -0.02, 0.4), (12, -0.02, 2.6), (1, -0.02, 2.6)], "#2a2d31",
           "mesh")
    s.poly([(18, -0.02, 0.4), (29, -0.02, 0.4), (29, -0.02, 2.6), (18, -0.02, 2.6)], "#2a2d31",
           "mesh")
    s.cylinder(15, 1.5, -0.9, 0.0, 1.2, color="#0b0d10", axis="y", segs=18)


def m_speaker(s: Scene, p, c):
    if "Soundbar" in p.get("name", ""):
        s.box(0, 0, 0, 36, 4, 3, "#1b1c1e")
        s.next_part()
        s.poly([(0.6, -0.02, 0.4), (35.4, -0.02, 0.4), (35.4, -0.02, 2.6), (0.6, -0.02, 2.6)],
               "#2a2d31", "mesh")
        return
    s.cylinder(4.5, 4.5, 0, 1.6, 4.5, 3.8, color="#1b1c1e", segs=28)
    s.prism_z(circle(4.5, 4.5, 1.2, 16), 1.6, 1.7, "#3fa9f5")


def m_panel(s: Scene, p, c):
    """A cubicle's fabric panel, with a metal cap along the top."""
    d = p.get("dims") or {}
    w, dp, h = d.get("w", 72), d.get("d", 2), d.get("h", 53)
    s.box(0, 0, 0, w, dp, h - 1, c, opacity=0.96)
    s.box(0, 0, h - 1, w, dp, 1, SILVER)


MODELS = {"desk": m_desk, "chair": m_chair, "conference_table": m_conference_table,
          "storage": m_storage, "printer": m_printer, "cartridge": m_cartridge, "tv": m_tv,
          "monitor": m_tv, "hdmi_cable": m_hdmi_cable, "power_strip": m_power,
          "cable_mgmt": m_power, "lamp": m_lamp, "whiteboard": m_whiteboard,
          "dry_erase_marker": m_markers, "permanent_marker": m_markers,
          "whiteboard_eraser": m_eraser, "eraser": m_eraser,
          "whiteboard_cleaner": m_eraser, "monitor_riser": m_desk_accessory,
          "monitor_arm": m_monitor_arm, "notebook": m_supply, "legal_pad": m_supply,
          "folders": m_supply, "binder": m_supply, "scissors": m_supply,
          "tape_dispenser": m_supply, "calculator": m_supply, "clips": m_supply,
          "hole_punch": m_supply, "letter_tray": m_supply, "highlighter": m_supply,
          "envelopes": m_supply, "index_cards": m_supply, "paper": m_paper,
          "pens": m_desk_accessory, "sticky_notes": m_desk_accessory,
          "organizer": m_desk_accessory, "stapler": m_desk_accessory,
          "wastebasket": m_bin, "recycling_bin": m_bin, "rug": m_rug, "chair_mat": m_rug,
          "side_table": m_side_table, "camera": m_camera, "video_bar": m_video_bar,
          "speaker": m_speaker, "panel": m_panel}


def _key(p: dict) -> tuple:
    d = p.get("dims") or {}
    return (p.get("kind"), p.get("color"), p.get("color_hex"), p.get("name", "").split(",")[0],
            p.get("u_shaped"), p.get("high_top"),
            d.get("w"), d.get("d"), d.get("h"), p.get("screen_in"), p.get("board_w_ft"),
            p.get("standing"), p.get("l_shaped"), p.get("benching"), p.get("users_supported"),
            p.get("printer_type"), p.get("reams"), p.get("gallons"), p.get("pack_qty"),
            p.get("mobile"), p.get("style"))


_cache: dict[tuple, str] = {}


def svg(p: dict) -> str:
    """The product's render as an SVG string (cached per kind/colour/size)."""
    key = _key(p)
    if key not in _cache:
        s = Scene()
        model = MODELS.get(p.get("kind"))
        if model is None:
            s.box(0, 0, 0, 10, 8, 6, p.get("color_hex") or "#888")
        else:
            model(s, p, p.get("color_hex") or "#888888")
        _cache[key] = to_svg(s)
    return _cache[key]


def data_uri(p: dict) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg(p).encode()).decode()


# --------------------------------------------------------------------------- #
# a whole room: every placed item, drawn with its product model
# --------------------------------------------------------------------------- #
ROOM_AZ, ROOM_EL = math.radians(35), math.radians(36)
ROOM_V = (math.cos(ROOM_EL) * math.sin(ROOM_AZ), -math.cos(ROOM_EL) * math.cos(ROOM_AZ),
          math.sin(ROOM_EL))
_RF = tuple(-c for c in ROOM_V)
ROOM_R = _norm(_cross(_RF, (0, 0, 1)))
ROOM_U = _cross(ROOM_R, _RF)
WALL_H = 96
ROOM_LIGHT = _norm((0.3, -0.55, 0.8))     # from behind the camera, up and to the right
MOUNT_Z = {"tv": 44, "whiteboard": 34}


class _Collect(Scene):
    """Runs a model only to learn its footprint."""
    def poly(self, pts, color, tag="", opacity=1.0, two_sided=False):
        self.faces.append(Face(list(pts), color, tag, opacity, (0, 0, 1), self.part))


class _Placed(Scene):
    """A model built straight into the room: every point is rotated and moved
    into place, and faces turned away from the camera are dropped."""
    def __init__(self, xf, facing=None):
        super().__init__()
        self.xf = xf
        self.facing = facing

    def poly(self, pts, color, tag="", opacity=1.0, two_sided=False):
        pts = [self.xf(p) for p in pts]
        n = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
        toward = self.facing(n, pts) if self.facing else _dot(n, ROOM_V) >= 0
        if not toward:
            if not two_sided:
                return
            n = tuple(-c for c in n)
        self.faces.append(Face(pts, color, tag, opacity, n, self.part))

    def overlay(self, fn):
        xf = self.xf
        self.overlays.append(lambda proj: fn(lambda p: proj(xf(p))))


class _Camera:
    """A perspective camera behind the near wall, a little above head height,
    looking at the far wall — how a photo of a room is taken from its doorway."""
    def __init__(self, L: float, W: float):
        self.eye = (L / 2, -(0.1 * max(L, W) + 18), 72.0)
        target = (L / 2, W * 0.6, 26.0)
        f = _norm(_sub(target, self.eye))
        self.f = f
        self.r = _norm(_cross(f, (0, 0, 1)))
        self.u = _cross(self.r, f)

    def flat(self, q) -> tuple[float, float]:
        d = _sub(q, self.eye)
        z = max(_dot(d, self.f), 1.0)
        return _dot(d, self.r) / z, -_dot(d, self.u) / z

    def facing(self, n, pts) -> bool:
        c = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
        return _dot(n, _sub(self.eye, c)) >= 0

    def dist(self, q) -> float:
        d = _sub(q, self.eye)
        return math.sqrt(_dot(d, d))


def _ft(inches: float) -> str:
    ft, i = divmod(int(round(inches)), 12)
    return f"{ft}′" + (f" {i}″" if i else "")


def _room_frame(L: float, W: float, k: int):
    """Plan (x, y) -> world (X, Y) seen from one of 4 corners. Each quarter-turn
    rotates the plan; then y is flipped so world Y grows away from the camera."""
    Lk, Wk = (L, W) if k % 2 == 0 else (W, L)

    def turn(x: float, y: float) -> tuple[float, float]:
        b = W
        a = L
        for _ in range(k):
            x, y = b - y, x
            a, b = b, a
        return x, Wk - y
    return turn, Lk, Wk


def _box_depth(faces) -> float:
    """How near the camera an object is: the centre of its bounding box (the mean
    of its points is pulled down by detail like chair casters)."""
    pts = [q for f in faces for q in f.pts]
    c = tuple((min(q[i] for q in pts) + max(q[i] for q in pts)) / 2 for i in range(3))
    return _dot(c, ROOM_V)


def room_svg(items, length: float, width: float, *, carpet: bool = False,
             width_px: int = 900, height_px: int = 560, fixed: bool = False,
             cam=None) -> str:
    """A 3D view of a planned room, seen from the doorway: floor, the far and side
    walls, and every item placed with its own product model and colour.

    fixed=True keeps the room as planned — the far wall at the back, the
    photo's left on the left — so it lines up with the photo it copies.
    Otherwise the room is turned so the fewest wall-mounted things sit on the
    near wall, which is cut away.

    cam: the camera a photo was taken with (retail.photo_cam.PhotoCam) — the room
    is drawn through it, at the photo's shape, so it lines up with the photo."""
    photo = cam is not None
    def hidden(k: int) -> tuple[int, int, int]:
        turn, _, wk = _room_frame(length, width, k)
        near = shown_tv = 0
        for it in items:
            if it.shape != "wall":
                continue
            _, cy = turn(it.x + it.w / 2, it.y + it.d / 2)
            near += cy < 12
            shown_tv += it.kind == "tv" and cy > wk - 12
        return near, -shown_tv, k
    k = 0 if fixed or photo else min(range(4), key=hidden)
    turn, Lk, Wk = _room_frame(length, width, k)
    if photo:
        height_px = round(width_px / cam.aspect)
    else:
        cam = _Camera(Lk, Wk)

    def ahead(q) -> bool:               # in front of a photo's camera, not behind it
        return not photo or cam.depth(q) > 3

    def far(faces) -> float:
        """How far from the camera an object is (negated: the farthest first),
        from the centre of its bounding box — the mean of its points is pulled
        down by detail like chair casters."""
        pts = [q for f in faces for q in f.pts]
        ctr = tuple((min(q[i] for q in pts) + max(q[i] for q in pts)) / 2 for i in range(3))
        return -cam.dist(ctr)

    objects = []        # (depth, faces, overlays)
    for it in items:
        if it.shape == "zone" or not it.product:
            continue
        p = dict(it.product)
        if it.kind == "tv":
            p["wall_mount"] = True
        model = MODELS.get(p.get("kind"))
        if model is None:
            continue
        c = p.get("color_hex") or "#888888"
        probe = _Collect()
        model(probe, p, c)
        if not probe.faces:
            continue
        xs = [q[0] for f in probe.faces for q in f.pts]
        ys = [q[1] for f in probe.faces for q in f.pts]
        mx, my = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        cx, cy = turn(it.x + it.w / 2, it.y + it.d / 2)
        # the item's front, in world terms (plan y is flipped, then turned)
        fx, fy = math.cos(it.face), math.sin(it.face)
        for _ in range(k):
            fx, fy = -fy, fx
        wx, wy = fx, -fy
        theta = math.atan2(wx, -wy)        # rotates local front (0, -1) onto (wx, wy)
        if it.shape == "rect" and it.kind == "table":
            # a long table turned to run away from the far wall: its footprint on
            # the plan is deep, the model wide — turn the model to match
            mw, md = max(xs) - min(xs), max(ys) - min(ys)
            along_y = abs(math.sin(theta)) > 0.7       # model x already runs along y
            plan_deep = (it.d > it.w * 1.15) != (k % 2 == 1)
            if (mw > md * 1.15) and plan_deep != along_y:
                theta += math.pi / 2
        ct, st = math.cos(theta), math.sin(theta)
        z0 = getattr(it, "z", 0) or MOUNT_Z.get(it.kind, 0)
        if it.kind == "monitor":
            z0 = 29.5
        if it.shape == "wall":              # flat against its wall, not centred on it
            if cy < 12:
                continue                    # the near wall is cut away to look in
            depth = max(ys) - min(ys)
            cx, cy = cx + wx * (depth / 2 + 0.5), cy + wy * (depth / 2 + 0.5)

        def xf(q, mx=mx, my=my, ct=ct, st=st, cx=cx, cy=cy, z0=z0):
            lx, ly = q[0] - mx, q[1] - my
            return (cx + lx * ct - ly * st, cy + lx * st + ly * ct, q[2] + z0)
        sc = _Placed(xf, cam.facing)
        model(sc, p, c)
        if it.kind == "chair" and sc.faces:
            # seen from behind, a chair's back hides its seat: the parts nearer the
            # camera (the model draws them first, for a front view) go last
            hx, hy = cx - cam.eye[0], cy - cam.eye[1]
            if wx * hx + wy * hy > 0.3 * math.hypot(hx, hy):
                parts: dict[int, list] = {}
                for f in sc.faces:
                    parts.setdefault(f.part, []).append(f)
                mid = math.hypot(hx, hy)
                for faces_ in parts.values():
                    q = [pt for f in faces_ for pt in f.pts]
                    px = sum(pt[0] for pt in q) / len(q) - cam.eye[0]
                    py = sum(pt[1] for pt in q) / len(q) - cam.eye[1]
                    if math.hypot(px, py) < mid - 4:
                        for f in faces_:
                            f.part += 10000
        if photo:                       # behind the camera: out of the picture
            sc.faces = [f for f in sc.faces if all(ahead(q) for q in f.pts)]
        if sc.faces and it.shape != "wall" and not photo:
            # tall things right by the camera (a bookcase by the door) would hide
            # the room: drawn see-through, like a cut-away
            q = [pt for f in sc.faces for pt in f.pts]
            near_y = sum(pt[1] for pt in q) / len(q)
            if near_y < 0.3 * Wk and max(pt[2] for pt in q) > 40:
                for f in sc.faces:
                    f.opacity = min(f.opacity, 0.22)
        depth = far(sc.faces) if sc.faces else -cam.dist((cx, cy, z0 + 10))
        if it.shape == "top":         # on a table or desk: after the surface it sits on
            depth += 1000
        flat = it.shape == "under" and it.kind != "monitor"
        if sc.splits:                 # layers (e.g. a U desk's credenza, bridge, front),
            bounds = [0] + sc.splits + [10 ** 9]          # each sorted on its own
            for lo, hi in zip(bounds, bounds[1:]):
                faces = [f for f in sc.faces if lo <= f.part < hi]
                if faces:
                    objects.append((far(faces), faces, []))
            continue
        objects.append((-1e9 if flat else depth, sc.faces, sc.overlays))

    # fit the floor and the far wall into the picture; the side walls run off its
    # edges, as they do in a photo (a photo's camera frames it as the photo did)
    corners = ([(x, y, 0) for x in (0, Lk) for y in (0.12 * Wk, Wk)]
               + [(0, Wk, WALL_H), (Lk, Wk, WALL_H)])
    pts = [cam.flat(q) for q in corners]
    x0, x1 = min(q[0] for q in pts), max(q[0] for q in pts)
    y0, y1 = min(q[1] for q in pts), max(q[1] for q in pts)
    pad = 34
    scale = min((width_px - 2 * pad) / (x1 - x0), (height_px - 2 * pad) / (y1 - y0))
    ox = (width_px - (x1 - x0) * scale) / 2 - x0 * scale
    oy = (height_px - (y1 - y0) * scale) / 2 - y0 * scale
    if photo:
        scale, ox, oy = width_px, 0.0, 0.0

    def proj(q):
        a, b = cam.flat(q)
        return ox + a * scale, oy + b * scale

    def clip(pts_):
        """A wall or the floor cut where it passes behind a photo's camera."""
        if not photo:
            return pts_
        out_, n = [], len(pts_)
        for i in range(n):
            a, b = pts_[i], pts_[(i + 1) % n]
            da, db = cam.depth(a) - 3, cam.depth(b) - 3
            if da >= 0:
                out_.append(a)
            if (da >= 0) != (db >= 0):
                t = da / (da - db)
                out_.append(tuple(a[j] + t * (b[j] - a[j]) for j in range(3)))
        return out_

    def poly(pts_, fill, stroke="none", extra=""):
        pts_ = clip(pts_)
        if len(pts_) < 3:
            return ""
        d = " ".join(f"{a:.1f},{b:.1f}" for a, b in (proj(q) for q in pts_))
        return f'<polygon points="{d}" fill="{fill}" stroke="{stroke}" {extra}/>'

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width_px} {height_px}" '
           f'style="width:100%;background:#e6e4de">', f"<defs>{_DEFS}</defs>"]
    floor = "#cfc6b8" if carpet else "#e6dccb"
    edge = 'stroke-width="1"'
    out.append(poly([(0, 0, 0), (0, Wk, 0), (0, Wk, WALL_H), (0, 0, WALL_H)], "#e4e2dc",
                    "#c9c6bd", edge))
    out.append(poly([(Lk, 0, 0), (Lk, 0, WALL_H), (Lk, Wk, WALL_H), (Lk, Wk, 0)], "#e9e7e1",
                    "#c9c6bd", edge))
    out.append(poly([(0, Wk, 0), (Lk, Wk, 0), (Lk, Wk, WALL_H), (0, Wk, WALL_H)], "#f3f2ee",
                    "#c9c6bd", edge))
    out.append(poly([(0, 0, 0), (Lk, 0, 0), (Lk, Wk, 0), (0, Wk, 0)], floor, "#b9ad9a", edge))
    step = 12
    for gx in range(step, int(Lk), step * 2):
        q1, q2 = (gx, 0, 0), (gx, Wk, 0)
        if photo:
            if cam.depth(q2) <= 3:
                continue
            d1 = cam.depth(q1) - 3
            if d1 < 0:                      # starts behind the camera: from where it's seen
                t = d1 / (d1 - (cam.depth(q2) - 3))
                q1 = (gx, t * Wk, 0)
        (a1, b1), (a2, b2) = proj(q1), proj(q2)
        out.append(f'<line x1="{a1:.1f}" y1="{b1:.1f}" x2="{a2:.1f}" y2="{b2:.1f}" '
                   f'stroke="#000" stroke-opacity="0.05"/>')
    for _d, faces, overlays in sorted(objects, key=lambda o: o[0]):
        for f in sorted(faces, key=lambda f: (f.part, -sum(cam.dist(q) for q in f.pts)
                                              / len(f.pts))):
            d = " ".join(f"{a:.1f},{b:.1f}" for a, b in (proj(q) for q in f.pts))
            col = _lit(f, ROOM_LIGHT, 0.74)        # lit from the doorway, like a photo
            op = f' fill-opacity="{f.opacity}"' if f.opacity < 1 else ""
            out.append(f'<polygon points="{d}" fill="{col}"{op} stroke="{shade(col, 0.82)}" '
                       f'stroke-width="0.5" stroke-linejoin="round"/>')
            if f.tag == "wood":
                out.append(f'<polygon points="{d}" fill="url(#grain)"/>')
            elif f.tag == "mesh":
                out.append(f'<polygon points="{d}" fill="url(#mesh)"/>')
            elif f.tag == "screen":
                out.append(f'<polygon points="{d}" fill="url(#glass)"/>'
                           f'<polygon points="{d}" fill="url(#gloss)"/>')
            elif f.tag == "gloss":
                out.append(f'<polygon points="{d}" fill="url(#gloss)"/>')
        for fn in overlays:
            out.append(fn(proj))
    # the room's size, on the near floor edge and the left one
    lbl = ('font-family="Arial,Helvetica,sans-serif" font-size="15" font-weight="700" '
           'fill="#444" text-anchor="middle" paint-order="stroke" stroke="#fff" '
           'stroke-width="3"')
    if photo:       # the near edge is behind the camera: along the far wall's foot instead
        a, b = proj((Lk / 2, Wk, 0))
        out.append(f'<text x="{a:.1f}" y="{min(b + 18, height_px - 6):.1f}" {lbl}>'
                   f'{_ft(Lk)} wide</text>')
        a, b = proj((0, max(Wk * 0.6, Wk - 60), 0))
        out.append(f'<text x="{min(max(a + 40, 50), width_px - 50):.1f}" '
                   f'y="{min(max(b, 20), height_px - 6):.1f}" {lbl}>{_ft(Wk)} deep</text>')
    else:
        a, b = proj((Lk / 2, 0, 0))
        out.append(f'<text x="{a:.1f}" y="{min(b + 20, height_px - 6):.1f}" {lbl}>'
                   f'{_ft(Lk)}</text>')
        a, b = proj((0, Wk / 2, 0))
        out.append(f'<text x="{max(a - 24, 24):.1f}" y="{b + 5:.1f}" {lbl}>{_ft(Wk)}</text>')
    out.append("</svg>")
    return "".join(out)
