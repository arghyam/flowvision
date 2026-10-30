"""Shared types of the BFM reading pipeline: digit boxes, counter drums, the counter read and photo facts."""
import math
from dataclasses import dataclass, field

import cv2
import numpy as np


def setting(cfg, path, default=None):
    """A config value by dotted path ("counter.min_digits").

    cfg may be a conf.config.Config, the full config dict, or just the section dict ({"min_digits": 4}).
    Missing or null values give the default; false and 0 are kept.
    """
    if cfg is None:
        return default
    data = getattr(cfg, "config", cfg)
    if not isinstance(data, dict):
        return default
    node = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            node = None
            break
        node = node[key]
    if node is None and "." in path and path.split(".", 1)[0] not in data:
        node = data.get(path.rsplit(".", 1)[1])        # a section dict was passed
    return default if node is None else node


@dataclass
class Det:
    """One detected digit box. Geometry comes from cv2.minAreaRect of the polygon.

    h is the long side (the digit's height), w the short side. angle is the direction of the short side, i.e. of the
    row the digit sits on, in degrees in [-90, 90): 0 for an upright digit, about +-90 for a digit lying on its side.
    """
    pts: np.ndarray
    value: int
    conf: float
    cx: float
    cy: float
    w: float
    h: float
    angle: float
    redness: float | None = None    # precomputed ink redness (fixtures, replay); decimals measures it when None
    p_red: float | None = None      # P(red ink) from the drum colour model (drums.py); set when the model looked
    second: tuple | None = None     # (digit, probability) from the drum digit model (drums.py): the second opinion

    @classmethod
    def from_polygon(cls, pts, value: int, conf: float, redness: float | None = None, p_red: float | None = None,
                     second=None) -> "Det":
        pts = np.asarray(pts, dtype=np.float32).reshape(-1, 2)
        (cx, cy), _, _ = cv2.minAreaRect(pts)
        corners = cv2.boxPoints(cv2.minAreaRect(pts))
        e1, e2 = corners[1] - corners[0], corners[2] - corners[1]
        l1, l2 = float(np.hypot(*e1)), float(np.hypot(*e2))
        short = e1 if l1 <= l2 else e2
        angle = math.degrees(math.atan2(float(short[1]), float(short[0])))
        angle = (angle + 90.0) % 180.0 - 90.0
        return cls(pts=np.rint(pts).astype(np.int32), value=int(value), conf=float(conf), cx=float(cx), cy=float(cy),
                   w=min(l1, l2), h=max(l1, l2), angle=angle,
                   redness=None if redness is None else float(redness),
                   p_red=None if p_red is None else float(p_red),
                   second=None if second is None else (int(second[0]), float(second[1])))

    @property
    def aspect(self) -> float:
        return self.h / self.w if self.w > 0 else 0.0


@dataclass
class Drum:
    """One position on the counter."""
    det: Det
    alternate: Det | None = None    # a different digit overlapping this drum (half-rolled drum), if any
    redness: float = 0.0            # ink redness relative to the row median
    red: bool = False


@dataclass
class CounterRead:
    drums: list[Drum]               # along the counter (left to right when the photo is upright)
    digits: str
    decimals: int = 0
    decimal_confidence: float = 0.0
    row_score: float = 0.0
    runner_up_score: float | None = None
    orientation: int = 0
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)     # facts worth logging that are not issues
    review_notes: list[str] = field(default_factory=list)   # for the person checking the reading (decision notes)
    seen_decimals: int = 0          # the red suffix seen in the photo (decimals may follow a meter profile instead)
    colour_sure: bool = False       # the colour of the drums that decide seen_decimals is clear (outside the unsure band)

    def refresh_digits(self):
        self.digits = "".join(str(d.det.value) for d in self.drums)

    def region(self, pad: float = 0.1) -> tuple[int, int, int, int]:
        """(x, y, w, h) around the drums, padded by pad x the median drum height: the counter area."""
        pts = np.vstack([d.det.pts for d in self.drums]).astype(np.int32)
        x, y, w, h = cv2.boundingRect(pts)
        p = int(pad * float(np.median([d.det.h for d in self.drums])))
        return max(x - p, 0), max(y - p, 0), w + 2 * p, h + 2 * p


@dataclass
class PhotoInfo:
    sha1: str
    width: int
    height: int
    quality_good: float | None = None
    brightness: float = 0.0         # mean V (0-255)
    blur: float = 0.0               # variance of the Laplacian (higher = sharper)
    glare: float = 0.0              # share of near-saturated, colourless pixels on the counter area
