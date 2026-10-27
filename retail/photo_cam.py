"""The camera a room photo was taken with, estimated from the boxes the vision
model drew — so the replica can be laid out where things are in the photo, and
drawn back through the same camera to line up with it.

World coordinates match render.room_svg: X across the room (0 = left wall),
Y away from the camera (0 = the near wall, W = the far wall), Z up; inches.
Photo coordinates are u (0 = left edge, 1 = right) and v (0 = top, 1 = bottom).

The lens isn't in the photo: a phone's main camera (about 66° across) is
assumed. The tilt and the height it was held at are fitted to the chairs, which
are about the same size everywhere. Errors there scale the room, but don't move
anything in the picture — placing and drawing use the same camera, so the
replica still lines up with the photo."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

HFOV = 66.0          # degrees across the photo
EYE_H = 58.0         # inches; a photo taken standing, when there are no chairs to go by
FLOOR_KINDS = ("chair", "conference_table", "desk", "storage", "side_table", "lamp",
               "printer", "wastebasket")
TABLE_TOP = 29.0
CHAIR_W = 25.0       # a chair's width or depth


@dataclass
class PhotoCam:
    aspect: float               # photo width / height
    pitch: float                # radians, looking down
    ex: float = 0.0             # where the camera stands, in world X / Y
    ey: float = 0.0
    eye_h: float = EYE_H
    hfov: float = math.radians(HFOV)
    hidden: frozenset = frozenset()     # boxes of chairs seen only above a table

    # ---- camera basis ------------------------------------------------------
    @property
    def eye(self) -> tuple[float, float, float]:
        return (self.ex, self.ey, self.eye_h)

    @property
    def k(self) -> float:
        """Photo widths per unit of image-plane x."""
        return 0.5 / math.tan(self.hfov / 2)

    def _basis(self):
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        f = (0.0, cp, -sp)          # forward
        r = (1.0, 0.0, 0.0)         # right
        up = (0.0, sp, cp)          # up
        return f, r, up

    # ---- world -> photo ----------------------------------------------------
    def flat(self, q) -> tuple[float, float]:
        """A point in world space -> (u, v / aspect): the photo in widths, so a
        renderer only has to multiply by its width in pixels."""
        f, r, up = self._basis()
        d = (q[0] - self.ex, q[1] - self.ey, q[2] - self.eye_h)
        z = max(d[0] * f[0] + d[1] * f[1] + d[2] * f[2], 1.0)
        x = (d[0] * r[0] + d[1] * r[1] + d[2] * r[2]) / z
        y = -(d[0] * up[0] + d[1] * up[1] + d[2] * up[2]) / z
        return 0.5 + x * self.k, 0.5 / self.aspect + y * self.k

    def project(self, q) -> tuple[float, float]:
        a, b = self.flat(q)
        return a, b * self.aspect

    def depth(self, q) -> float:
        """How far in front of the camera a point is, along its view."""
        f, _, _ = self._basis()
        return ((q[0] - self.ex) * f[0] + (q[1] - self.ey) * f[1]
                + (q[2] - self.eye_h) * f[2])

    def dist(self, q) -> float:
        d = (q[0] - self.ex, q[1] - self.ey, q[2] - self.eye_h)
        return math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)

    def facing(self, n, pts) -> bool:
        c = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
        return n[0] * (self.ex - c[0]) + n[1] * (self.ey - c[1]) + n[2] * (self.eye_h - c[2]) >= 0

    # ---- photo -> world ----------------------------------------------------
    def ray(self, u: float, v: float) -> tuple[float, float, float]:
        f, r, up = self._basis()
        x = (u - 0.5) / self.k
        y = (v - 0.5) / (self.aspect * self.k)
        return tuple(f[i] + x * r[i] - y * up[i] for i in range(3))

    def at_height(self, u: float, v: float, z: float = 0.0) -> tuple[float, float] | None:
        """Where the ray through (u, v) meets the level plane at height z."""
        d = self.ray(u, v)
        if d[2] >= -1e-6 and z < self.eye_h or d[2] <= 1e-6 and z > self.eye_h:
            return None                             # at or above the horizon
        t = (z - self.eye_h) / d[2]
        if t <= 0:
            return None
        return self.ex + t * d[0], self.ey + t * d[1]

    def on_wall(self, u: float, v: float, wall: str, L: float, W: float
                ) -> tuple[float, float] | None:
        """Where the ray through (u, v) meets a wall: (position along it, height).
        Along the far wall that's X; along a side wall, Y."""
        d = self.ray(u, v)
        if wall == "top":
            if d[1] <= 1e-6:
                return None
            t = (W - self.ey) / d[1]
            hit = (self.ex + t * d[0], self.eye_h + t * d[2])
        else:
            x = 0.0 if wall == "left" else L
            if abs(d[0]) < 1e-6 or (x - self.ex) / d[0] <= 0:
                return None
            t = (x - self.ex) / d[0]
            hit = (self.ey + t * d[1], self.eye_h + t * d[2])
        return hit if t > 0 else None

    def params(self) -> dict[str, float]:
        return {"aspect": self.aspect, "pitch": self.pitch, "ex": self.ex, "ey": self.ey,
                "eye_h": self.eye_h, "hfov": self.hfov}


CHAIR_H = 38.0       # a chair, floor to the top of its back


def _behind(it: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    """A chair behind a table, boxed only where its back shows above the top: its
    box ends in the upper half of the table's (feet beside a table end lower)."""
    if it.get("kind") != "chair":
        return False
    x1, _, x2, y2 = it["box"]
    u = (x1 + x2) / 2
    return any(t.get("kind") in ("conference_table", "desk") and t.get("box")
               and t["box"][0] <= u <= t["box"][2] and t["box"][1] - 0.02 <= y2
               < (t["box"][1] + t["box"][3]) / 2 for t in items)


def stand(cam: "PhotoCam", it: dict[str, Any], items: list[dict[str, Any]]
          ) -> tuple[float, float] | None:
    """Where a thing in the photo stands on the floor (world X, Y): below the
    bottom middle of its box — or, for a chair hidden behind a table, below the
    top of its back."""
    x1, y1, x2, y2 = it["box"]
    if tuple(it["box"]) in cam.hidden:
        return cam.at_height((x1 + x2) / 2, y1, CHAIR_H)
    return cam.at_height((x1 + x2) / 2, y2, 0.0)


def _pitch(hv: float, aspect: float, k: float) -> float:
    """The downward tilt that puts the horizon at hv down the photo."""
    return max(math.radians(-8), min(math.radians(30), math.atan((0.5 - hv) / (aspect * k))))


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def _chair_box(cam: "PhotoCam", at: tuple[float, float]) -> list[float] | None:
    """A life-size chair standing at `at` (the front of its feet), in the photo."""
    x, y = at
    pts = []
    for dx in (-CHAIR_W / 2, CHAIR_W / 2):
        for dy in (0.0, CHAIR_W):
            for z in (0.0, CHAIR_H):
                q = (x + dx, y + dy, z)
                if cam.depth(q) < 3:
                    return None
                pts.append(cam.project(q))
    b = [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
         max(p[1] for p in pts)]
    return [min(max(v, 0.0), 1.0) for v in b]


def _chair_fit(cam: "PhotoCam", it: dict[str, Any]) -> float:
    """How well a life-size chair covers this chair's box through `cam` — for a
    chair behind a table, only the part above the top."""
    x1, y1, x2, y2 = it["box"]
    u = (x1 + x2) / 2
    hidden = tuple(it["box"]) in cam.hidden
    at = cam.at_height(u, y1, CHAIR_H) if hidden else cam.at_height(u, y2)
    b = _chair_box(cam, at) if at else None
    if not b:
        return 0.0
    if hidden:
        b[3] = min(b[3], y2)                         # the table hides the rest
    return _iou(b, it["box"])


def estimate(items: list[dict[str, Any]], aspect: float | None, L: float, W: float
             ) -> PhotoCam | None:
    """The camera for a photo of a room L × W, from what the vision model boxed:
    its tilt and height from the chairs (with no chairs, level with the far
    wall's screen), stood back so the farthest thing on the floor is just short
    of the far wall, and across so what's on the floor is centred in the room."""
    if not aspect or not items:
        return None
    floor = [i for i in items if i.get("kind") in FLOOR_KINDS and i.get("box")]
    if not floor:
        return None
    cam = PhotoCam(aspect, 0.0)
    # the horizon and the camera's height, from the chairs: try each pair, stand
    # a life-size chair where every chair in the photo stands, and keep the camera
    # whose chairs best cover the photo's (cut off at its edges, as the photo is)
    chairs = [i for i in items if i.get("kind") == "chair" and i.get("box")]
    hidden = frozenset(tuple(i["box"]) for i in chairs if _behind(i, items))
    best = None
    for hv in [0.18 + 0.025 * n for n in range(21)]:            # 0.18 .. 0.68
        for eye_h in range(34, 86, 4):
            c = PhotoCam(aspect, _pitch(hv, aspect, cam.k), eye_h=float(eye_h),
                         hidden=hidden)
            fits = [_chair_fit(c, i) for i in chairs]
            if fits:
                score = sum(fits) / len(fits)
                if best is None or score > best[0]:
                    best = (score, c)
    if best and best[0] > 0.15:
        cam = best[1]
    else:
        cam.hidden = hidden
        # no chairs to go by: a screen or board on the far wall hangs about at eye
        # height
        wall = [i for i in items if i.get("kind") in ("tv", "whiteboard") and i.get("box")
                and i.get("wall", "top") == "top"]
        hv = (sum((i["box"][1] + i["box"][3]) / 2 for i in wall) / len(wall)) if wall \
            else 0.42
        hv = max(0.12, min(hv, min(i["box"][3] for i in floor) - 0.04))
        cam.pitch = _pitch(hv, aspect, cam.k)
    # with the camera at the origin: where each thing stands
    pts = [p for p in (stand(cam, i, items) for i in floor) if p]
    if not pts:
        return None
    # stood back so the nearest thing on the floor is 2½ ft inside the near wall:
    # it's measured surely, while depths near the horizon are shaky (a pixel is a
    # foot) — if the room is too shallow for what's behind, the plan says so and
    # a deeper room is offered
    cam.ey = 30.0 - min(y for _, y in pts)
    xs = [x for x, _ in pts]
    cam.ex = min(max(L / 2 - (min(xs) + max(xs)) / 2, 12.0), L - 12.0)
    return cam


HEIGHTS = {"chair": 18.0, "table": TABLE_TOP, "conference_table": TABLE_TOP,
           "desk": TABLE_TOP, "storage": 36.0}      # what must show: a chair's seat


def frame(cam: PhotoCam | None, items, L: float, W: float, aspect: float | None,
          lean: float = 0.5) -> PhotoCam:
    """A camera on the photo's side of the room that shows the whole table and
    every chair: it keeps the photo's shape and roughly where it stood across
    the room, aims at the furniture's middle, and steps back until it all fits.

    items: the plan's items (x, y across and down the plan; world Y = W - y).
    lean: where the table sat across the photo (0 = left edge, 1 = right) —
    the camera stands off to the other side, as the photographer did."""
    aspect = aspect or (cam.aspect if cam else 4 / 3)
    pts, tops = [], []          # tops: what must show whole (chair backs, screens)
    for it in items:
        if it.kind in ("tv", "whiteboard") and it.shape == "wall":
            h = float(((it.product or {}).get("dims") or {}).get("h", 30))
            for x in (it.x, it.x + it.w):
                tops.append((x, W - (it.y + it.d / 2), it.z + h))
            continue
        h = HEIGHTS.get(it.kind)
        if h is None or it.shape in ("wall", "zone", "under", "top"):
            continue
        if it.kind == "chair":                  # its seat, and the top of its back
            pts.append((it.x + it.w / 2, W - (it.y + it.d / 2), h))
            tops.append((it.x + it.w / 2, W - (it.y + it.d / 2), CHAIR_H))
            continue
        for x in (it.x, it.x + it.w):
            for y in (it.y, it.y + it.d):
                pts.append((x, W - y, h))
    if not pts:
        return cam or PhotoCam(aspect, 0.35, ex=L / 2, ey=-24.0)
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    eye_h = min(max(cam.eye_h if cam else EYE_H, 54.0), 70.0)
    span = max(xs) - min(xs)
    ex = min(max(cx - (lean - 0.5) * span * 0.9, 0.1 * L), 0.9 * L)
    best = None
    for back in range(0, 480, 6):
        ey = min(ys) - 6.0 - back
        target = (cx, cy + 0.1 * (max(ys) - min(ys)), 20.0)
        pitch = math.atan2(eye_h - target[2], target[1] - ey)
        c = PhotoCam(aspect, pitch, ex=ex, ey=ey, eye_h=eye_h)
        if all(c.depth(q) > 6 for q in pts + tops) and \
                all(0.03 <= u <= 0.97 and 0.04 <= v <= 0.97
                    for u, v in map(c.project, pts)) and \
                all(0.0 <= u <= 1.0 and v >= 0.03 for u, v in map(c.project, tops)):
            return c
        best = c
    return best


def from_params(d: dict[str, float] | None) -> PhotoCam | None:
    return PhotoCam(**d) if d else None
