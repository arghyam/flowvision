"""Orientation model: tells how far a photo is rotated so it can be turned upright before the digits are read.

A yolo11n-cls classifier trained by training/orientation/train_orientation.py. Its classes say how far the photo
is rotated clockwise: r000, r090, r180, r270. Measured on 169 rotated production photos: readings that agreed with
the confirmed value went from 0 to 90 once the photo was turned upright. ultralytics is imported when the model is
loaded, so this module imports without the ML stack.
"""
import threading

import cv2
import numpy as np

# Class name -> the clockwise rotation to undo (images.rotate takes the same angle).
ANGLES = {"r000": 0, "r090": 90, "r180": 180, "r270": 270}

# The training copies were squashed to this square before the classifier's own resize (imgsz 224).
INPUT_SIZE = 320
# The classifier's own input size (trained at imgsz 224; the ONNX export has this fixed size and must be told it).
MODEL_SIZE = 224


class OrientationModel:
    """predict(bgr) -> (angle to undo, confidence). Below min_confidence the photo is read as taken: (0, conf).

    min_confidence 0.9 is what the 169-photo measurement used (config orientation_min_confidence).
    """

    def __init__(self, path, min_confidence: float = 0.9):
        from ultralytics import YOLO
        # The model is an ONNX export of the trained .pt (16 Nov 2026: same turn on 400 of 400 test photos, no confidence
        # difference). An ONNX file does not say it is a classifier, so the task is given here.
        self.model = YOLO(path, task="classify")
        names = {int(k): str(v) for k, v in dict(self.model.names).items()}
        unknown = sorted(set(names.values()) - set(ANGLES))
        if unknown or len(names) != len(ANGLES):
            raise ValueError(f"Orientation model {path} classes {sorted(names.values())} are not {sorted(ANGLES)}")
        self.angles = {index: ANGLES[name] for index, name in names.items()}
        self.min_confidence = float(min_confidence)
        self._lock = threading.Lock()

    def predict(self, bgr: np.ndarray) -> tuple[int, float]:
        # Same resize as the measured run (scratch run_orient_fix.py, cv2 default interpolation).
        small = cv2.resize(bgr, (INPUT_SIZE, INPUT_SIZE))
        with self._lock:
            result = self.model.predict(small, verbose=False, imgsz=MODEL_SIZE)[0]
        angle = self.angles[int(result.probs.top1)]
        conf = float(result.probs.top1conf)
        if conf < self.min_confidence:
            return 0, conf
        return angle, conf
