"""
The YOLOX reader (window finder + digit reader, ONNX on onnxruntime, Apache-2.0).

  1. window model on the whole photo -> best box and how far to turn it (0/90/180/270)
  2. cut the box out (plus a margin), turn it upright
  3. digit model on the crop -> digits left to right, with confidences
"""
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image

ROT = {0: None, 1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_COUNTERCLOCKWISE}
MARGIN = 0.10  # grow the window box by this share of its shorter side before cutting


class Yolox:
    def __init__(self, path):
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.inp = self.s.get_inputs()[0]
        self.h, self.w = self.inp.shape[2], self.inp.shape[3]

    def __call__(self, img, conf=0.25, nms=0.45, probs=False):
        """YOLOX ValTransform (letterbox, pad 114, BGR, no normalisation) -> boxes in image pixels.
        probs: also return each box's class probabilities (to see a wheel split between two digits)."""
        if img is None or img.size == 0:
            return []  # an empty cut-out holds nothing to find (its size would divide by zero below)
        r = min(self.h / img.shape[0], self.w / img.shape[1])
        pad = np.full((self.h, self.w, 3), 114, dtype=np.uint8)
        rs = cv2.resize(img, (int(img.shape[1] * r), int(img.shape[0] * r)), interpolation=cv2.INTER_LINEAR)
        pad[: rs.shape[0], : rs.shape[1]] = rs
        x = pad.transpose(2, 0, 1)[None].astype(np.float32)
        p = self.s.run(None, {self.inp.name: x})[0][0]
        cls = p[:, 5:].argmax(1)
        score = p[:, 4] * p[np.arange(len(p)), 5 + cls]
        keep = score >= conf
        p, cls, score = p[keep], cls[keep], score[keep]
        if not len(p):
            return []
        xyxy = np.stack([p[:, 0] - p[:, 2] / 2, p[:, 1] - p[:, 3] / 2, p[:, 0] + p[:, 2] / 2, p[:, 1] + p[:, 3] / 2], 1) / r
        xywh = [[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in xyxy]
        idx = cv2.dnn.NMSBoxes(xywh, score.tolist(), conf, nms)
        idx = np.array(idx).ravel()
        if probs:
            return [(xyxy[i], int(cls[i]), float(score[i]), p[i, 5:].copy()) for i in idx]
        return [(xyxy[i], int(cls[i]), float(score[i])) for i in idx]


class CounterCheck:
    """How sure it is that every wheel of a cut-out counter is centred and readable: the reviewers'
    centred / midturn / unclear decisions, learned by a small classifier (training: kaggle_gate)."""

    def __init__(self, path):
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.inp = self.s.get_inputs()[0]
        self.h, self.w = self.inp.shape[2], self.inp.shape[3]

    def __call__(self, crop):
        """crop: BGR array -> {"centred": P, "midturn": P, "unclear": P}."""
        rgb = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).resize((self.w, self.h), Image.BILINEAR)
        x = np.asarray(rgb, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        logits = self.s.run(None, {self.inp.name: x})[0][0]
        scores = np.exp(logits - logits.max())
        return dict(zip(("centred", "midturn", "unclear"), (scores / scores.sum()).tolist()))


class UnsureColour(ValueError):
    """The colour check could not decide; red: its P(red)."""

    def __init__(self, red):
        super().__init__(f"the last wheel's colour is unclear ({red:.2f})")
        self.red = red


class ColourCheck:
    """Whether the last wheel is red or black, from a small classifier (training: kaggle_colour).
    A red wheel called black makes the stored value 10 times too high, so the red limit is low."""

    def __init__(self, path, red_limit, unsure=(0.3, 0.7)):
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.inp = self.s.get_inputs()[0]
        self.h, self.w = self.inp.shape[2], self.inp.shape[3]
        self.red_limit = red_limit
        self.unsure = unsure
        # A model wider than tall sees the wheel before the last one beside it (round 9): a photo's light tints both,
        # so the model compares the two inks instead of judging the tint of one wheel
        self.pair = self.w > self.h

    def __call__(self, crop, before=None):
        """crop: BGR array of the last wheel; before: the wheel before it, needed by a pair model.
        -> the same result as classify_color_image."""
        if getattr(self, "pair", False):
            if before is None:
                raise ValueError("the wheel before the last one is missing")
            half = (self.w // 2, self.h)
            crop = np.hstack([cv2.resize(before, half, interpolation=cv2.INTER_AREA),
                              cv2.resize(crop, half, interpolation=cv2.INTER_AREA)])
        rgb = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).resize((self.w, self.h), Image.BILINEAR)
        x = np.asarray(rgb, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        logits = self.s.run(None, {self.inp.name: x})[0][0]
        scores = np.exp(logits - logits.max())
        black, red = (scores / scores.sum()).tolist()
        if self.unsure[0] <= red < self.unsure[1]:
            raise UnsureColour(red)
        if red >= self.red_limit:
            return {"prediction": "red", "confidence": red, "all_probs": [black, red]}
        return {"prediction": "black", "confidence": black, "all_probs": [black, red]}


def _ink_redness(crop, corners):
    """How much redder a digit's ink is than its paper (darkest quarter against brightest 40%)."""
    x0, y0 = np.maximum(corners.min(0).astype(int), 0)
    x1, y1 = corners.max(0).astype(int)
    pixels = crop[y0:y1, x0:x1].reshape(-1, 3).astype(float)
    if len(pixels) < 20:
        return 0.0
    light = pixels.mean(1)
    b, g, r = pixels[light <= np.percentile(light, 25)].mean(0)
    pb, pg, pr = pixels[light >= np.percentile(light, 60)].mean(0)
    return (r - (g + b) / 2) - (pr - (pg + pb) / 2)


def last_wheel_redness(crop, corners):
    """The last digit's ink redness above the row's: a washed-out photo fades every digit alike, so this
    still shows a pale red wheel. corners: the row's digit corners in the crop, left to right."""
    values = [_ink_redness(crop, c) for c in corners]
    return values[-1] - float(np.median(values[:-1]))


@dataclass
class Reading:
    window_score: float = 0.0
    rotation_cw: int = 0
    crop_xyxy: tuple = (0, 0, 0, 0)  # cut-out region in the photo
    digits: list = field(default_factory=list)  # (value, score, corners in the photo), left to right
    crop_corners: list = field(default_factory=list)  # the same digits' corners in the upright crop
    crop: object = None  # the cut-out counter, turned upright (BGR array)
    tilt: float = 0.0  # the row's slope in the first cut-out, degrees

    @property
    def text(self):
        return "".join(str(v) for v, _, _ in self.digits)

    @property
    def min_score(self):
        return min((s for _, s, _ in self.digits), default=0.0)


def _to_photo(pts, code, crop_w, crop_h, x0, y0):
    """Points in the upright crop -> points in the photo (undo the turn, then the cut)."""
    u, v = pts[:, 0], pts[:, 1]
    if code == 1:  # crop was turned 90 clockwise
        x, y = v, crop_h - 1 - u
    elif code == 2:
        x, y = crop_w - 1 - u, crop_h - 1 - v
    elif code == 3:  # turned 90 counter-clockwise
        x, y = crop_w - 1 - v, u
    else:
        x, y = u, v
    return np.stack([x + x0, y + y0], axis=1)


def counter_row(ds):
    """Digit detections in the counter row, left to right: within one digit height of the row's middle."""
    if not ds:
        return []
    height = np.median([d[0][3] - d[0][1] for d in ds])
    middle = np.median([(d[0][1] + d[0][3]) / 2 for d in ds])
    return sorted([d for d in ds if abs((d[0][1] + d[0][3]) / 2 - middle) < height], key=lambda d: d[0][0])


def turned(crop, degrees):
    h, w = crop.shape[:2]
    return cv2.warpAffine(crop, cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0), (w, h), borderMode=cv2.BORDER_REPLICATE)


def row_tilt(boxes):
    """The counter row's slope in degrees (y down), from digit boxes (x0, y0, x1, y1): the median over all pairs,
    so a stray digit does not move it."""
    centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
    slopes = [(y1 - y0) / (x1 - x0) for i, (x0, y0) in enumerate(centres) for x1, y1 in centres[i + 1:] if abs(x1 - x0) > 1]
    return float(np.degrees(np.arctan(np.median(slopes)))) if slopes else 0.0


class Reader:
    def __init__(self, models_dir, red_limit=0.7, level_tilt=2.0, colour_unsure=(0.3, 0.7)):
        self.level_tilt = level_tilt
        models_dir = Path(models_dir)
        self.win = Yolox(models_dir / "bfm_window.onnx")
        self.dig = Yolox(models_dir / "bfm_digits.onnx")
        self.check = CounterCheck(models_dir / "bfm_gate.onnx") if (models_dir / "bfm_gate.onnx").exists() else None
        # Trained further on 5-wheel counters. It lets through too many hard 6-wheel counters, so only 5-digit rows use it.
        self.check_five = CounterCheck(models_dir / "bfm_gate_five.onnx") if (models_dir / "bfm_gate_five.onnx").exists() else None
        self.colour = ColourCheck(models_dir / "bfm_colour.onnx", red_limit, colour_unsure) if (models_dir / "bfm_colour.onnx").exists() else None
        # A second opinion when the colour check is unsure (colour_second_opinion): the one-wheel colour model before it
        single = models_dir / "bfm_colour_single.onnx"
        self.colour_single = ColourCheck(single, red_limit, colour_unsure) if single.exists() else None

    def __call__(self, img, window_conf=0.10, digit_conf=0.25, rotation_cw=None, level=False, tight=False):
        """rotation_cw: read the window at this turn instead of the one the model predicts.
        level: turn a tilted cut-out level before reading. Returns None if the counter is not tilted or found.
        tight: cut to the row, level or not. Returns None if too few digits were found to do that."""
        h, w = img.shape[:2]
        found = self.win(img, conf=window_conf)
        if not found:
            return None
        (x0, y0, x1, y1), c, s = max(found, key=lambda t: t[2])
        if rotation_cw is not None:
            c = [0, 90, 180, 270].index(rotation_cw)
        m = MARGIN * min(x1 - x0, y1 - y0)
        x0, y0 = int(max(0, x0 - m)), int(max(0, y0 - m))
        x1, y1 = int(min(w, x1 + m)), int(min(h, y1 + m))
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            # The window box lies outside the photo (it can, in the padding around the scaled photo): no counter here.
            # Without this the cut-out was empty and reading it raised a server error (Alan's Phase 3 note, 19 Nov 2026).
            return None
        if ROT[c] is not None:
            crop = cv2.rotate(crop, ROT[c])
        ds = sorted(self.dig(crop, conf=digit_conf), key=lambda t: (t[0][0] + t[0][2]) / 2)
        back = None
        tilt = row_tilt([d[0] for d in ds]) if len(ds) >= 4 else 0.0
        if level or tight:
            crop, ds, back = self._levelled(crop, ds, digit_conf, always=tight)
            if back is None:
                return None
        digits, crop_corners = [], []
        for (a, b, c2, d), value, score in ds:
            corners = np.array([[a, b], [c2, b], [c2, d], [a, d]], dtype=float)
            crop_corners.append(corners)
            cut = corners if back is None else cv2.transform(corners[None], back)[0]
            digits.append((value, score, _to_photo(cut, c, x1 - x0, y1 - y0, x0, y0)))
        return Reading(window_score=s, rotation_cw=[0, 90, 180, 270][c], crop_xyxy=(x0, y0, x1, y1), digits=digits,
                       crop_corners=crop_corners, crop=crop, tilt=tilt)

    def versions(self, crop, digit_conf=0.10):
        """The digits read on three more versions of a tight cut-out: contrast raised, turned 3 degrees each way.
        A misread usually changes with the version; a digit truly on the wheel does not."""
        lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
        lab[..., 0] = cv2.createCLAHE(2.0, (8, 8)).apply(lab[..., 0])
        views = (cv2.cvtColor(lab, cv2.COLOR_LAB2BGR), turned(crop, 3), turned(crop, -3))
        return ["".join(str(d[1]) for d in counter_row(self.dig(v, conf=digit_conf))) for v in views]

    def plain_text(self, crop, digit_conf=0.10):
        """The digits read on one crop as it is."""
        return "".join(str(d[1]) for d in counter_row(self.dig(crop, conf=digit_conf)))

    def _levelled(self, crop, ds, digit_conf, always=False):
        """A tilted counter does not fit an upright box and can be misread: turn the cut-out level, trim it to the row
        and read again. Returns (crop, digits, the transform from the new crop back to the old one, or None)."""
        if len(ds) < 4:
            return crop, ds, None
        tilt = row_tilt([d[0] for d in ds])
        if abs(tilt) < self.level_tilt and not always:
            return crop, ds, None
        h, w = crop.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), tilt, 1.0)
        cos, sin = abs(M[0, 0]), abs(M[0, 1])
        W, H = int(h * sin + w * cos), int(h * cos + w * sin)
        M[0, 2] += W / 2 - w / 2
        M[1, 2] += H / 2 - h / 2
        flat = cv2.warpAffine(crop, M, (W, H), borderMode=cv2.BORDER_REPLICATE)
        # A steep counter's cut-out is nearly square, so its digits are too small to find again: carry the first
        # digits through the turn and trim to them
        boxes = np.array([d[0] for d in ds])
        height = np.median(boxes[:, 3] - boxes[:, 1])
        centres = cv2.transform(((boxes[:, :2] + boxes[:, 2:]) / 2)[None], M)[0]
        middle = max(centres[:, 1], key=lambda y: (np.abs(centres[:, 1] - y) < height / 2).sum())
        row = centres[np.abs(centres[:, 1] - middle) < height * 0.6]  # leaves out dial numbers
        X0, X1 = int(max(0, row[:, 0].min() - height * 0.55)), int(min(W, row[:, 0].max() + height * 0.55))
        Y0, Y1 = int(max(0, middle - height)), int(min(H, middle + height))
        trim = flat[Y0:Y1, X0:X1]
        M[0, 2] -= X0
        M[1, 2] -= Y0
        return trim, sorted(self.dig(trim, conf=digit_conf), key=lambda t: (t[0][0] + t[0][2]) / 2), cv2.invertAffineTransform(M)


def colour_second_opinion(reader, last_crop, unsure):
    """When the colour check is unsure: the one-wheel colour model's call, if it is sure and the unsure check leans the
    same way (P(red) above or below 0.5). None when there is no one-wheel model, it is unsure too, or they disagree."""
    single = getattr(reader, "colour_single", None)
    if single is None:
        return None
    try:
        call = single(last_crop)
    except ValueError:  # unsure too
        return None
    leans = "red" if unsure.red >= 0.5 else "black"
    return call if call["prediction"] == leans else None
