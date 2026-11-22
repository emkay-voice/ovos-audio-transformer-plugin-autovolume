"""From room noise to volume: the room's range, the user's points and the curve.

Everything here is plain arithmetic on numbers (dBFS levels and volumes from 0
to 1), with no audio and no bus, so it is easy to test and cheap to run.

- ``RoomRange`` learns how quiet and how noisy this room gets: a histogram of
  the measured levels that slowly forgets, read as its 10th and 90th
  percentile. There is no "right" noise level to configure.
- ``Curve`` turns a noise level into a volume. With no points of its own it is
  a soft S-curve from the low to the high volume across the room's range. Each
  time the user sets the volume by hand a point (noise now, chosen volume) is
  added; the curve then goes through the points (interpolated, never falling
  as the noise rises), newer points counting more than old ones.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence

#: histogram bins, 1 dB wide, from FLOOR_DB to 0 dBFS
FLOOR_DB = -100
BINS = -FLOOR_DB
#: the quiet and noisy ends of the room, as percentiles of what was measured
QUIET_PCT, NOISY_PCT = 0.10, 0.90
#: the curve spans at least this many dB, so a very steady room doesn't make it jumpy
MIN_SPAN_DB = 10.0
#: how much weight the default low/high anchors keep next to the user's points
ANCHOR_WEIGHT = 0.25


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class RoomRange:
    """How quiet and how noisy this room gets, learned from the levels measured.

    ``add`` is called with the room's running level now and then (every few
    seconds); older measurements fade with ``half_life_days``, so a new room or
    a new season is picked up without a reset.
    """

    def __init__(self, half_life_days: float = 14.0, counts: Optional[Sequence[float]] = None,
                 last: Optional[float] = None):
        self.half_life = half_life_days * 86400.0
        self.counts: List[float] = list(counts) if counts and len(counts) == BINS else [0.0] * BINS
        self.last = last            # wall time of the last add, for the fading

    def add(self, level_db: float, now: float, weight: float = 1.0):
        if self.last is not None and now > self.last and self.half_life > 0:
            f = 0.5 ** ((now - self.last) / self.half_life)
            self.counts = [c * f for c in self.counts]
        self.last = now
        i = int(clamp(math.floor(level_db - FLOOR_DB), 0, BINS - 1))
        self.counts[i] += weight

    @property
    def total(self) -> float:
        return sum(self.counts)

    def percentile(self, p: float) -> Optional[float]:
        total = self.total
        if total <= 0:
            return None
        want, acc = p * total, 0.0
        for i, c in enumerate(self.counts):
            acc += c
            if acc >= want:
                return FLOOR_DB + i + 0.5
        return 0.0

    def span(self) -> Optional[tuple]:
        """(quiet, noisy) dBFS, at least MIN_SPAN_DB apart, or None before anything was measured."""
        q, n = self.percentile(QUIET_PCT), self.percentile(NOISY_PCT)
        if q is None:
            return None
        if n - q < MIN_SPAN_DB:
            mid = (q + n) / 2
            q, n = mid - MIN_SPAN_DB / 2, mid + MIN_SPAN_DB / 2
        return q, n

    def clear(self):
        self.counts = [0.0] * BINS
        self.last = None

    def to_dict(self) -> Dict:
        return {"counts": [round(c, 4) for c in self.counts], "last": self.last}


@dataclass
class Point:
    noise_db: float     # the room's running level when the user chose
    volume: float       # what they chose, 0..1
    time: float         # wall time, for the weighting


def isotonic(xs: List[float], ys: List[float], ws: List[float]) -> List[float]:
    """Weighted least-squares fit of ys that never falls as xs rise (pool adjacent violators).

    xs must be sorted. Returns one fitted value per input point.
    """
    blocks = []         # [value, weight, count]
    for y, w in zip(ys, ws):
        blocks.append([y, w, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            w = w1 + w2
            blocks.append([(v1 * w1 + v2 * w2) / w if w > 0 else (v1 + v2) / 2, w, n1 + n2])
    out = []
    for v, _, n in blocks:
        out.extend([v] * n)
    return out


class Curve:
    """Noise level (dBFS) to volume (0..1).

    low, high: the default volumes for the quiet and the noisy end of the room.
    min_points: how many of the user's own points before they shape the curve.
    merge_db: a new point replaces an old one this close in noise level.
    half_life_days: a point's weight halves over this many days.
    max_points: the oldest points are dropped beyond this many.
    """

    def __init__(self, low: float = 0.35, high: float = 0.75, min_points: int = 2,
                 merge_db: float = 3.0, half_life_days: float = 30.0, max_points: int = 20,
                 points: Optional[List[Point]] = None):
        self.low, self.high = clamp(low, 0.0, 1.0), clamp(high, 0.0, 1.0)
        if self.high < self.low:
            self.low, self.high = self.high, self.low
        self.min_points = max(1, int(min_points))
        self.merge_db = merge_db
        self.half_life = half_life_days * 86400.0
        self.max_points = max(1, int(max_points))
        self.points: List[Point] = list(points or [])

    # ---- learning -----------------------------------------------------------

    def learn(self, noise_db: float, volume: float, now: float):
        """The user chose ``volume`` at ``noise_db``: remember it, replacing a point close by."""
        self.points = [p for p in self.points if abs(p.noise_db - noise_db) >= self.merge_db]
        self.points.append(Point(round(noise_db, 2), round(clamp(volume, 0.0, 1.0), 3), now))
        if len(self.points) > self.max_points:
            self.points.sort(key=lambda p: p.time)
            self.points = self.points[-self.max_points:]
        self.points.sort(key=lambda p: p.noise_db)

    def forget(self):
        self.points = []

    def _weight(self, p: Point, now: float) -> float:
        if self.half_life <= 0:
            return 1.0
        return 0.5 ** (max(0.0, now - p.time) / self.half_life)

    # ---- the curve ----------------------------------------------------------

    def default(self, noise_db: float, span: tuple) -> float:
        """The soft S-curve from low to high across the room's range."""
        quiet, noisy = span
        x = clamp((noise_db - quiet) / (noisy - quiet), 0.0, 1.0)
        x = x * x * (3 - 2 * x)          # smoothstep: gentle at both ends
        return self.low + (self.high - self.low) * x

    def nodes(self, span: Optional[tuple], now: float) -> List[tuple]:
        """The (noise, volume) nodes the curve runs through: the user's points,
        fitted so they never fall, plus the low/high anchors at the room's ends
        (with little weight) so it still rises where the user hasn't said anything."""
        pts = [(p.noise_db, p.volume, self._weight(p, now)) for p in self.points]
        if span is not None:
            quiet, noisy = span
            lo_x = min([quiet] + [p[0] - 1 for p in pts])
            hi_x = max([noisy] + [p[0] + 1 for p in pts])
            # the anchors never pull against the user: if they chose quieter than
            # the default low (or louder than high), their choice is the end of the curve
            lo_v = min([self.low] + [p[1] for p in pts])
            hi_v = max([self.high] + [p[1] for p in pts])
            pts += [(lo_x, lo_v, ANCHOR_WEIGHT), (hi_x, hi_v, ANCHOR_WEIGHT)]
        pts.sort(key=lambda t: t[0])
        fitted = isotonic([p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts])
        return [(p[0], v) for p, v in zip(pts, fitted)]

    def volume(self, noise_db: float, span: Optional[tuple], now: float) -> Optional[float]:
        """The volume for ``noise_db``, or None when there is nothing to go on yet."""
        if len(self.points) < self.min_points:
            return None if span is None else self.default(noise_db, span)
        nodes = self.nodes(span, now)
        if noise_db <= nodes[0][0]:
            return nodes[0][1]
        if noise_db >= nodes[-1][0]:
            return nodes[-1][1]
        for (x0, y0), (x1, y1) in zip(nodes, nodes[1:]):
            if x0 <= noise_db <= x1:
                return y0 if x1 == x0 else y0 + (y1 - y0) * (noise_db - x0) / (x1 - x0)
        return nodes[-1][1]

    def to_list(self) -> List[Dict]:
        return [asdict(p) for p in self.points]

    @staticmethod
    def points_from(data) -> List[Point]:
        out = []
        for d in data or []:
            try:
                out.append(Point(float(d["noise_db"]), float(d["volume"]), float(d["time"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out
