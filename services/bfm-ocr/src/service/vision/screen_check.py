"""
Screen / paper check: is this a photo of the meter itself, or of a phone / computer screen or a piece of paper?

One ONNX file (models.screen_check): the CLIP ViT-B-32 image encoder (laion2b_s34b_b79k, MIT licence) and a small
trained layer on top that gives P(meter), P(screen), P(paper) for the whole photo. Trained 11 Nov 2026 and again 12 Nov
(round r4) on 61,015 field photos (1,365 phone screens and 87 paper photos labelled by Claude or by two independent AI looks);
grouped cross-validation: 91% of screens / paper caught while 0.1% of other photos are refused (threshold 0.907). The service refuses a photo when
P(screen) + P(paper) is above the threshold, before any digit is read.

Switch it off (e.g. if phone-screen photos should be read one day) with FLOWVISION_SCREEN_CHECK=off, or
screen_check: false in config.yaml; no rebuild needed.
"""
import numpy as np
from PIL import Image

MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
SIZE = 224


def preprocess(image):
    """The photo as the add-on was trained on it: at most 384 px, shorter side to 224 (bicubic), centre 224 x 224, CLIP-normalised."""
    image = image.convert("RGB")
    image.thumbnail((384, 384))
    w, h = image.size
    s = SIZE / min(w, h)
    image = image.resize((max(SIZE, round(w * s)), max(SIZE, round(h * s))), Image.BICUBIC)
    w, h = image.size
    left, top = int(round((w - SIZE) / 2.0)), int(round((h - SIZE) / 2.0))
    x = np.asarray(image.crop((left, top, left + SIZE, top + SIZE)), np.float32) / 255.0
    return ((x - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)


class ScreenCheck:
    """Loaded once; call with a PIL image. Returns dict(meter, screen, paper, refused, kind)."""

    def __init__(self, model_path, threshold=None):
        import onnxruntime as ort
        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        meta = self.session.get_modelmeta().custom_metadata_map
        self.threshold = float(threshold if threshold is not None else meta.get("threshold", 0.907))

    def __call__(self, image):
        p = self.session.run(None, {"image": preprocess(image)})[0][0]
        meter, screen, paper = (float(v) for v in p)
        refused = screen + paper > self.threshold
        return dict(meter=meter, screen=screen, paper=paper, refused=refused,
                    kind=("paper" if paper > screen else "screen") if refused else None)
