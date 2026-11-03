"""The decimal drum: is the last drum of the counter red, and is anything after it not a drum?

On these meters a red drum at the right end holds the digit after the decimal point. Only some meters have one, so
the colour must be read from the drum. A small colour model (drums.DrumColourModel) looks at the upright drum, on the
true colours of the photo. On 96 of the 100 hand-checked Annexure photos the last drum was called right by this model
89 times, against 63 for the fastai classifier and 79 for an ink-redness rule; red drums scored 0.60 or more and black
ones 0.03 or less. Five of the seven misses were a box after the red drum, which this module drops.

Checked by eye on 1,125 random production photos of 14 Sep 2026: no clear false red among the 132 red calls, and the 8
drums the fastai classifier called red that this model calls black were all black. All 21 boxes dropped in 1,900
photos were strays.
"""
import numpy as np

from service.vision.types import Det

RED = 0.5               # P(red) at which a drum counts as red
# Every meter family with a red drum has 7 windows, so on a 7-drum row a fainter score counts: faint red drums on
# production photos scored 0.31-0.49 (06158, 07727, 10135), and black drums start below 0.3 (05816 at 0.23). On other
# rows 0.3 would call washed-out black drums red (02252, 07565, 10350: 0.30-0.32).
RED_ON_7_DRUMS = 0.3
RED_DRUM_WINDOWS = 7
MAX_TAIL = 2            # at most this many boxes after the seventh drum can be strays
# Boxes after the red drum that were not drums sat at 0.56-0.70 x the row's pitch (008, 009, 016, 045) or were weak
# (026: conf 0.46). Real drums after a reflection that looks red are evenly spaced and confident (011): they stay.
STRAY_PITCH = 0.8
WEAK_STRAY = 0.5


def _centres(boxes):
    return np.array([np.asarray(b, dtype=np.float64).reshape(-1, 2).mean(axis=0) for b in boxes])


def _off_the_row(boxes, confidences, drum):
    """Whether the boxes after this drum look like strays: the next one is off the drum pitch, or all are weak."""
    steps = np.linalg.norm(np.diff(_centres(boxes), axis=0), axis=1)
    # the lower median: a missed drum before this one must not inflate the pitch and make real drums look off it
    pitch = float(np.sort(steps[:drum])[(drum - 1) // 2])
    return bool(steps[drum] < STRAY_PITCH * pitch or all(c < WEAK_STRAY for c in confidences[drum + 1:]))


def is_red(p_red, drums):
    """Whether a last drum with this P(red) is red, on a row of this many drums."""
    return p_red >= (RED_ON_7_DRUMS if drums == RED_DRUM_WINDOWS else RED)


def stray_tail(boxes, confidences, p_red):
    """How many boxes at the right end are not drums. Only a row that would be left with a red seventh drum is trimmed:
    every meter with a red drum has 7 windows, so a real digit is never cut from any other row."""
    tail = len(boxes) - RED_DRUM_WINDOWS
    if not 0 < tail <= MAX_TAIL or not is_red(p_red[RED_DRUM_WINDOWS - 1], RED_DRUM_WINDOWS):
        return 0
    return tail if _off_the_row(boxes, confidences, RED_DRUM_WINDOWS - 1) else 0


def read_last_drum(bgr, boxes, confidences, model):
    """(boxes to drop at the right end, the last drum is red, P(red) of the last drum left).

    bgr: the upright photo in true colours; boxes: the counter drums in reading order, with their detector confidences.
    """
    dets = [Det.from_polygon(box, 0, conf) for box, conf in zip(boxes, confidences)]
    p_red = model.red_probabilities(bgr, dets)
    drop = stray_tail(boxes, confidences, p_red)
    drums = len(p_red) - drop
    last = float(p_red[drums - 1])
    red = is_red(last, drums)
    if red and not drop and last < RED and _off_the_row(boxes, confidences, drums - 2):
        red = False             # a faint score on a stray after 6 black drums is not a seventh, red drum
    return drop, red, last


def read_last_digit(bgr, box, model):
    """(the last digit is red, its P(red)) when no counter row was found: no row to trim, so the plain threshold."""
    p_red = float(model.red_probabilities(bgr, [Det.from_polygon(box, 0, 1.0)])[0])
    return is_red(p_red, 1), p_red
