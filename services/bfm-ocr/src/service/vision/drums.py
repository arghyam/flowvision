"""A small CNN that looks at one counter drum at a time: P(red ink), for the decimal drum (decimal_drum.py).

It takes the drum's oriented box warped upright to 32 x 48 pixels (straighten) from the upright photo. Trained on
18,394 drums of 3,007 production photos of 14 Sep 2026, on CPU. torch is imported when the model is loaded, so this
module imports without the ML stack.
"""
import threading

import cv2
import numpy as np

CROP_W, CROP_H = 32, 48


def straighten(bgr: np.ndarray, pts) -> np.ndarray:
    """The drum's oriented box warped to an upright CROP_W x CROP_H crop (corners ordered top-left first).
    Must stay as the training crops were cut."""
    rect = cv2.minAreaRect(np.asarray(pts, dtype=np.float32).reshape(-1, 2))
    box = cv2.boxPoints(rect)
    box = box[np.argsort(box[:, 1])]
    top = box[:2][np.argsort(box[:2, 0])]
    bottom = box[2:][np.argsort(box[2:, 0])]
    src = np.array([top[0], top[1], bottom[1], bottom[0]], dtype=np.float32)
    dst = np.array([[0, 0], [CROP_W - 1, 0], [CROP_W - 1, CROP_H - 1], [0, CROP_H - 1]], dtype=np.float32)
    return cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(src, dst), (CROP_W, CROP_H))


def colour_batch(crops) -> np.ndarray:
    """BGR uint8 crops -> (N, 3, 48, 32) float32 RGB in [-1, 1] (the colour model's input)."""
    x = np.stack([c[..., ::-1] for c in crops]).astype(np.float32) / 127.5 - 1.0
    return np.ascontiguousarray(x.transpose(0, 3, 1, 2))


def _network():
    """The colour network (torch is imported here): 48 x 32 RGB -> 1 logit (red ink). 13k parameters."""
    import torch.nn as nn

    def stem(cin, cout):
        return nn.Sequential(nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout),
                             nn.ReLU(inplace=True), nn.MaxPool2d(2))

    def block(cin, cout):
        return nn.Sequential(nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout),
                             nn.ReLU(inplace=True), nn.Conv2d(cout, cout, 3, padding=1, bias=False),
                             nn.BatchNorm2d(cout), nn.ReLU(inplace=True), nn.MaxPool2d(2))

    class RedNet(nn.Module):
        def __init__(self, in_ch: int = 3, width: int = 12):
            super().__init__()
            w = width
            self.in_ch = in_ch
            self.features = nn.Sequential(stem(in_ch, w), stem(w, 2 * w), block(2 * w, 2 * w))
            self.head = nn.Sequential(nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.2), nn.Linear(2 * w, 1))

        def forward(self, x):
            return self.head(self.features(x)).squeeze(1)

    return RedNet()


class DrumColourModel:
    """red_probabilities(bgr, dets) -> P(red ink) per box. bgr: the upright photo the boxes were found on.
    Safe to call from several threads."""

    def __init__(self, path):
        import torch
        self._torch = torch
        net = _network()
        net.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
        net.eval()
        self.net = net
        self._lock = threading.Lock()

    def red_probabilities(self, bgr: np.ndarray, dets) -> list[float]:
        if not dets:
            return []
        batch = colour_batch([straighten(bgr, d.pts) for d in dets])
        with self._lock, self._torch.inference_mode():
            logits = self.net(self._torch.from_numpy(batch)).numpy()
        return [float(p) for p in 1.0 / (1.0 + np.exp(-logits.astype(np.float64)))]
