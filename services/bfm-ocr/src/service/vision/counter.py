"""The counter row: which detected digits are the meter's drums, and in what order.

A photo of a bulk meter shows more digits than the counter: dial numbers below it, scale-ring numbers around the
dials, the meter's serial number. The drums are the digits that sit on one straight line (at any tilt), have the
same height and a regular pitch. Ported from the scratch harness (bfmeval/assemble.counter_row), which picked the
row by vertical centre only and so failed on tilted photos.
"""
import math

import cv2
import numpy as np

from service.vision.types import CounterRead, Det, Drum, setting

# Measured on the 100 Annexure photos: the drum pitch is 0.7-1.05 x the digit height, so two boxes closer than 0.4 x
# height along the row are the same drum, and one missing drum leaves a gap of at most about 2.1 x height.
SAME_DRUM = 0.4          # x height along the row
MAX_LINK = 2.4           # x height along the row between neighbouring drums (allows one missed drum)
LINE_OFFSET = 0.5        # x height across the row
HEIGHT_RATIO = 1.5       # neighbours' heights differ by less than this factor
MAX_TILT_DIFF = 20.0     # degrees between neighbours' directions
CLEAR_ASPECT = 1.2       # boxes this much taller than wide have a trustworthy direction
VERTICAL = 80.0          # degrees: only a row steeper than this is read top to bottom; others left to right
GAP_FACTOR = 1.8         # a gap above this x the median pitch: a drum was missed
AMBIGUOUS = 0.9          # a second row scoring this close to the best one


def iou(a: Det, b: Det) -> float:
    """Intersection over union of two box polygons."""
    pa = cv2.convexHull(a.pts.astype(np.float32))
    pb = cv2.convexHull(b.pts.astype(np.float32))
    inter, _ = cv2.intersectConvexConvex(pa, pb)
    union = cv2.contourArea(pa) + cv2.contourArea(pb) - inter
    return float(inter / union) if union > 0 else 0.0


def suppress(dets: list[Det], iou_threshold: float = 0.3) -> list[Drum]:
    """Greedy NMS by confidence. A suppressed box showing a different digit becomes the drum's alternate (a drum
    half-way between two digits)."""
    rest = sorted(dets, key=lambda d: -d.conf)
    drums = []
    while rest:
        best = rest.pop(0)
        overlapping = [d for d in rest if iou(best, d) >= iou_threshold]
        rest = [d for d in rest if not any(d is o for o in overlapping)]
        others = [d for d in overlapping if d.value != best.value]
        drums.append(Drum(det=best, alternate=max(others, key=lambda d: d.conf) if others else None))
    return drums


def _direction(angles: list[float]) -> float | None:
    """Median of axis directions (degrees, modulo 180)."""
    if not angles:
        return None
    a = np.radians(np.asarray(angles) * 2.0)
    ref = math.degrees(math.atan2(np.sin(a).mean(), np.cos(a).mean())) / 2.0
    dev = (np.asarray(angles) - ref + 90.0) % 180.0 - 90.0
    return (ref + float(np.median(dev)) + 90.0) % 180.0 - 90.0


def _unit(angle: float) -> np.ndarray:
    """Reading direction for a row at this angle: rightwards (as a tilted photo of an upright meter reads, rising or
    falling), or downwards for a nearly vertical row. A sideways photo cannot tell left-to-right from right-to-left;
    orientation handles that."""
    u = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))])
    if abs(angle) > VERTICAL:
        return u if u[1] > 0 else -u
    return u if u[0] >= 0 else -u


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 90.0) % 180.0 - 90.0)


def _linked(a: Det, b: Det) -> bool:
    """True if two boxes could be neighbouring drums of one counter."""
    if max(a.h, b.h) > HEIGHT_RATIO * min(a.h, b.h):
        return False
    clear = [d.angle for d in (a, b) if d.aspect >= CLEAR_ASPECT]
    if len(clear) == 2 and _angle_diff(*clear) > MAX_TILT_DIFF:
        return False
    if clear:
        u = _unit(_direction(clear))
    else:                                                # both boxes squarish: trust the line between them
        v = np.array([b.cx - a.cx, b.cy - a.cy])
        u = v / (np.linalg.norm(v) or 1.0)
    d = np.array([b.cx - a.cx, b.cy - a.cy])
    h = (a.h + b.h) / 2.0
    along, across = abs(float(d @ u)), abs(float(d @ np.array([-u[1], u[0]])))
    return across < LINE_OFFSET * h and along <= MAX_LINK * h


def _components(drums: list[Drum]) -> list[list[Drum]]:
    parent = list(range(len(drums)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(drums)):
        for j in range(i + 1, len(drums)):
            if _linked(drums[i].det, drums[j].det):
                parent[root(i)] = root(j)
    groups = {}
    for i, d in enumerate(drums):
        groups.setdefault(root(i), []).append(d)
    return list(groups.values())


def _row_axis(row: list[Drum]) -> np.ndarray:
    """Reading direction of a row: the median box direction, else the main axis of the box centres."""
    angle = _direction([d.det.angle for d in row if d.det.aspect >= CLEAR_ASPECT])
    if angle is None:
        c = np.array([[d.det.cx, d.det.cy] for d in row])
        c = c - c.mean(axis=0)
        _, _, vt = np.linalg.svd(c, full_matrices=False)
        angle = (math.degrees(math.atan2(vt[0][1], vt[0][0])) + 90.0) % 180.0 - 90.0
    return _unit(angle)


class _Row:
    """A candidate counter row, cleaned and ordered along its axis."""

    def __init__(self, drums: list[Drum]):
        self.alternates = []
        self.u = _row_axis(drums)
        self.n_vec = np.array([-self.u[1], self.u[0]])
        h = float(np.median([d.det.h for d in drums]))
        drums = [d for d in drums if 0.6 * h <= d.det.h <= 1.5 * h]
        row_angle = float(np.degrees(np.arctan2(self.u[1], self.u[0])))
        drums = [d for d in drums                        # turned against the row: a scale-ring number
                 if d.det.aspect < CLEAR_ASPECT or _angle_diff(d.det.angle, row_angle) <= MAX_TILT_DIFF]
        if len(drums) >= 3:                              # off the row's straight line: not a drum of this row
            along = np.array([self._along(d) for d in drums])
            across = np.array([self._across(d) for d in drums])
            fit = np.polyfit(along, across, 1)
            resid = np.abs(across - np.polyval(fit, along))
            drums = [d for d, r in zip(drums, resid) if r < LINE_OFFSET * h]
        drums.sort(key=self._along)
        self.drums = self._merge_same_drum(drums) if drums else []
        self.h = float(np.median([d.det.h for d in self.drums])) if self.drums else 0.0
        pos = [self._along(d) for d in self.drums]
        self.gaps = np.diff(pos) if len(pos) > 1 else np.array([])

    def _along(self, d: Drum) -> float:
        return float(np.array([d.det.cx, d.det.cy]) @ self.u)

    def _across(self, d: Drum) -> float:
        return float(np.array([d.det.cx, d.det.cy]) @ self.n_vec)

    def _merge_same_drum(self, drums: list[Drum]) -> list[Drum]:
        """Two boxes at one position along the row are one drum (often a half-rolled one): keep the more confident
        box, the other becomes its alternate."""
        if len(drums) < 2:
            return drums
        h = float(np.median([d.det.h for d in drums]))
        out = [drums[0]]
        for d in drums[1:]:
            prev = out[-1]
            if self._along(d) - self._along(prev) < SAME_DRUM * h:
                keep, other = sorted((prev, d), key=lambda x: -x.det.conf)
                if keep.alternate is None and other.det.value != keep.det.value:
                    keep.alternate = other.det
                out[-1] = keep
            else:
                out.append(d)
        return out

    @property
    def pitch(self) -> float:
        return float(np.median(self.gaps)) if len(self.gaps) else 0.0

    def score(self, tallest: float, max_digits: int) -> float:
        """More drums (up to max_digits), taller digits, even heights and a regular pitch score higher."""
        n = len(self.drums)
        if n == 0 or tallest <= 0:
            return 0.0
        count = n if n <= max_digits else max(2 * max_digits - n, 1)
        hs = np.array([d.det.h for d in self.drums])
        even = 1.0 if hs.std() / hs.mean() < 0.25 else 0.75
        regular = 1.0
        if len(self.gaps) >= 2:
            p = self.pitch
            regular = 0.6 + 0.4 * float(np.mean((self.gaps >= 0.75 * p) & (self.gaps <= 1.35 * p)))
        return count * (self.h / tallest) * even * regular


def select_row(dets: list[Det], cfg=None) -> CounterRead | None:
    """Picks the counter row from all detected boxes. None when no row has 3 or more digits.

    cfg (Config or dict): detector.min_box_conf (0.3), detector.nms_iou (0.3), counter.max_digits (9).
    Decimals and orientation are filled in later (decimals.decide, the reader).
    """
    min_conf = float(setting(cfg, "detector.min_box_conf", 0.3))
    nms_iou = float(setting(cfg, "detector.nms_iou", 0.3))
    max_digits = int(setting(cfg, "counter.max_digits", 9))

    boxes = [d for d in dets if d.conf > min_conf]
    drums = suppress(boxes, nms_iou)
    rows = [_Row(c) for c in _components(drums) if len(c) >= 2]
    rows = [r for r in rows if len(r.drums) >= 2]
    if not rows:
        return None
    tallest = max(r.h for r in rows)
    ranked = sorted(rows, key=lambda r: -r.score(tallest, max_digits))
    best = ranked[0]
    if len(best.drums) < 3:
        return None
    best_score = best.score(tallest, max_digits)
    runner_up = ranked[1].score(tallest, max_digits) if len(ranked) > 1 else None

    issues = []
    if len(ranked) > 1 and len(ranked[1].drums) >= 4 and runner_up >= AMBIGUOUS * best_score:
        issues.append("AMBIGUOUS_ROW")
    if len(best.gaps) >= 2 and float(best.gaps.max()) > GAP_FACTOR * best.pitch:
        issues.append("GAP_IN_ROW")
    _attach_alternates(best, drums)
    read = CounterRead(drums=best.drums, digits="", row_score=round(best_score, 4),
                       runner_up_score=None if runner_up is None else round(runner_up, 4), issues=issues)
    read.refresh_digits()
    return read


def _attach_alternates(row: _Row, drums: list[Drum]):
    """A box left out of the row but sitting on one of its drums (above or below it: the drum is half-way between
    two digits) becomes that drum's alternate."""
    members = {id(d) for d in row.drums}
    pitch = row.pitch or row.h
    for other in drums:
        if id(other) in members or not 0.6 * row.h <= other.det.h <= 1.5 * row.h:
            continue
        for d in row.drums:
            along = abs(row._along(other) - row._along(d))
            across = abs(row._across(other) - row._across(d))
            if along < 0.35 * pitch and across < 0.9 * row.h:
                if other.det.value != d.det.value and (d.alternate is None or other.det.conf > d.alternate.conf):
                    d.alternate = other.det
                break
