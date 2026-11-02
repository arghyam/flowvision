"""Phase 1 fixes: only the meter's counter row is read, and a rotated photo is turned upright before the quality
check and the digit model. Everything else (API, statuses, fields) behaves as before."""
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import BackgroundTasks

from models.models import ReadingExtractionRequest
from service.api import image_service
from service.vision import decimal_drum, inference_utils as iu


def box(cx, cy, w=20.0, h=36.0, angle=0.0):
    """A digit box polygon (4x2 int32) centred at (cx, cy), turned by angle degrees."""
    return np.rint(cv2.boxPoints(((cx, cy), (w, h), angle))).astype(np.int32)


def v1_kept(boxes, classes, confs):
    """What direct_recognize_meter_reading keeps before reading: overlapping boxes removed at IoU 0.3."""
    return iu.remove_overlapping_boxes(boxes, classes, confs, iou_threshold=0.3)


def counter_digits(boxes, classes, confs):
    kept = v1_kept(boxes, classes, confs)
    row = iu.keep_counter_row(*kept)
    return None if row is None else "".join(str(c) for c in row[1])


def v1_digits(boxes, classes, confs):
    b, c, k, _ = v1_kept(boxes, classes, confs)
    return "".join(str(x) for x in iu.sort_boxes_by_position(b, c, k)[1])


# ---- counter row only ----

def test_dial_and_serial_number_digits_are_left_out():
    drums = [(100 + 30 * i, 100, d) for i, d in enumerate([0, 3, 0, 7, 0, 1])]
    dials = [(130, 190, 9), (160, 200, 1), (250, 185, 5)]          # x0.1 / x0.01 dial numbers below the counter
    serial = [(95 + 12 * i, 40, d) for i, d in enumerate([2, 0, 2, 4, 0, 9, 1, 7])]   # small stamped serial number
    boxes = [box(x, y) for x, y, _ in drums] + [box(x, y, 9, 15) for x, y, _ in dials] + \
            [box(x, y, 7, 12) for x, y, _ in serial]
    classes = [d for *_, d in drums + dials + serial]
    confs = [0.9] * len(boxes)
    assert counter_digits(boxes, classes, confs) == "030701"
    assert len(v1_digits(boxes, classes, confs)) == len(boxes)       # before: every digit joined left to right


@pytest.mark.parametrize("angle", [15.0, -15.0, 46.0, -46.0, 60.0, -60.0, 75.0, -75.0])
def test_a_tilted_counter_is_read_along_the_row(angle):
    """Falling (+) or rising (-) to the right: read left to right, as the old leftmost-x sort did (image y points
    down, so a negative angle rises to the right)."""
    u = np.array([np.cos(np.radians(angle)), np.sin(np.radians(angle))])
    digits = [0, 4, 5, 0, 9, 4]
    centres = [np.array([200.0, 200.0]) + 32 * i * u for i in range(len(digits))]
    boxes = [box(c[0], c[1], angle=angle) for c in centres]
    # a box sorted by its leftmost corner can come out of order on a tilted row; along the row it cannot
    assert counter_digits(boxes, digits, [0.9] * len(digits)) == "045094"


def test_two_boxes_on_one_drum_are_one_position_with_a_rollover_alternate():
    xs = [100 + 30 * i for i in range(6)]
    # last drum half-way between 5 and 6: a second box half a digit lower (IoU 0.24, so overlap removal keeps both)
    boxes = [box(x, 100) for x in xs] + [box(xs[-1], 122)]
    classes = [0, 0, 6, 5, 4, 5, 6]
    confs = [0.95, 0.95, 0.95, 0.95, 0.95, 0.84, 0.76]
    assert v1_digits(boxes, classes, confs) == "0065456"               # before: the half-turned drum gave 2 digits
    row = iu.keep_counter_row(*v1_kept(boxes, classes, confs))
    assert "".join(map(str, row[1])) == "006545"
    assert row[2] == [{"position": 6, "selectedDigit": {"value": 5, "confidence": 0.84},
                       "alternateDigit": {"value": 6, "confidence": 0.76}}]


def test_an_overlapping_box_stays_a_rollover_at_its_counter_position():
    xs = [100 + 30 * i for i in range(5)]
    boxes = [box(x, 100) for x in xs] + [box(xs[2] + 2, 101), box(160, 200, 9, 15)]
    classes = [1, 2, 3, 4, 5, 8, 7]
    confs = [0.9, 0.9, 0.9, 0.9, 0.9, 0.6, 0.9]
    row = iu.keep_counter_row(*v1_kept(boxes, classes, confs))
    assert "".join(map(str, row[1])) == "12345"
    assert row[2] == [{"position": 3, "selectedDigit": {"value": 3, "confidence": 0.9},
                       "alternateDigit": {"value": 8, "confidence": 0.6}}]


def test_rollovers_keep_the_old_order_most_confident_drum_first():
    xs = [100 + 30 * i for i in range(6)]
    boxes = [box(x, 100) for x in xs] + [box(xs[1] + 2, 101), box(xs[4] + 2, 101)]
    classes = [1, 2, 3, 4, 5, 6, 7, 9]
    confs = [0.9, 0.7, 0.9, 0.9, 0.95, 0.9, 0.5, 0.5]
    row = iu.keep_counter_row(*v1_kept(boxes, classes, confs))
    assert [rp["position"] for rp in row[2]] == [5, 2]


def test_without_a_row_of_three_digits_the_photo_is_read_as_before():
    boxes, classes, confs = [box(100, 100), box(300, 220)], [7, 2], [0.9, 0.9]
    assert iu.keep_counter_row(*v1_kept(boxes, classes, confs)) is None


def test_counter_row_on_the_production_detections_of_the_annexure(annex_production_detections):
    """The production model's own boxes on the 100 hand-checked photos: the counter row drops stray digits on 8
    photos, fixes 4 readings and never breaks a right one (digits compared without the decimal point)."""
    def digits(s):
        return str(s).replace(".", "").lstrip("0") or "0"

    fixed, broke, dropped, by_photo = [], [], 0, {}
    for item in annex_production_detections:
        kept = [d for d in item["dets"] if d["conf"] > 0.3]
        if not kept:
            continue
        args = ([np.array(d["pts"], dtype=np.int32) for d in kept], [d["cls"] for d in kept], [d["conf"] for d in kept])
        before, after = v1_digits(*args), counter_digits(*args) or v1_digits(*args)
        truth = digits(item["truth"])
        by_photo[item["photo"]] = (before, after)
        fixed += [item["photo"]] if digits(before) != truth and digits(after) == truth else []
        broke += [item["photo"]] if digits(before) == truth and digits(after) != truth else []
        dropped += len(after) < len(before)
    assert broke == []
    assert sorted(fixed) == ["005", "012", "013", "062"]
    assert dropped == 8
    assert by_photo["001"] == ("10903015", "19315")                  # scale-ring digits around a kilolitre counter
    assert by_photo["012"][1] == "0268045"                            # '60 m3/h' spec text no longer interleaved


@pytest.mark.parametrize("value, expected", [(None, True), ("false", False), ("off", False), ("TRUE", True),
                                             ("maybe", True)])
def test_counter_row_switch(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("FLOWVISION_COUNTER_ROW_ONLY", raising=False)
    else:
        monkeypatch.setenv("FLOWVISION_COUNTER_ROW_ONLY", value)
    assert iu.counter_row_only() is expected


# ---- turned upright ----

class FakeOrientation:
    def __init__(self, angle, conf=0.99, fail=False):
        self.angle, self.conf, self.fail, self.calls = angle, conf, fail, 0

    def predict(self, bgr):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model broke")
        return self.angle, self.conf


def png(width, height):
    """A PNG with a different value in every pixel, so a rotation can be checked exactly."""
    img = np.arange(width * height * 3, dtype=np.uint32).reshape(height, width, 3) % 251
    ok, buf = cv2.imencode(".png", img.astype(np.uint8))
    return buf.tobytes()


def decode(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


@pytest.mark.parametrize("angle, undo", [(90, cv2.ROTATE_90_COUNTERCLOCKWISE), (180, cv2.ROTATE_180),
                                         (270, cv2.ROTATE_90_CLOCKWISE)])
def test_a_rotated_photo_is_turned_upright(angle, undo):
    data = png(60, 40)
    out, turned, conf = iu.turn_upright(data, FakeOrientation(angle))
    assert (turned, conf) == (angle, 0.99)
    assert np.array_equal(decode(out), cv2.rotate(decode(data), undo))


@pytest.mark.parametrize("model", [None, FakeOrientation(0), FakeOrientation(90, fail=True)])
def test_an_upright_photo_or_a_failed_check_leaves_the_bytes_as_they_were(model):
    data = png(60, 40)
    out, turned, _ = iu.turn_upright(data, model)
    assert out is data and turned == 0


def test_a_missing_or_broken_orientation_model_does_not_stop_the_service(tmp_path, monkeypatch):
    assert iu.load_orientation_model(str(tmp_path / "missing.pt")) is None
    broken = tmp_path / "broken.pt"
    broken.write_bytes(b"not a model")
    assert iu.load_orientation_model(str(broken)) is None
    monkeypatch.setenv("FLOWVISION_ORIENTATION", "off")
    assert iu.load_orientation_model() is None                       # switched off: not even loaded


def test_the_shipped_orientation_model_loads():
    model = iu.load_orientation_model()
    assert model is not None and sorted(model.angles.values()) == [0, 90, 180, 270]


def test_orientation_switched_off_leaves_the_photo_as_taken(monkeypatch):
    monkeypatch.setenv("FLOWVISION_ORIENTATION", "off")
    model, data = FakeOrientation(180), png(60, 40)
    assert iu.turn_upright(data, model) == (data, 0, None) and model.calls == 0


# ---- the request path: quality judged upright, response unchanged ----

V1_DATA_FIELDS = {"meterReading", "hasRollover", "processingTime", "qualityStatus", "qualityConfidence",
                  "lastDigitColor", "colorConfidence"}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(image_service, "MetadataStore", lambda config: SimpleNamespace(
        store_request=lambda *a: None, store_response=lambda *a: None, store_feedback=lambda *a: None))
    for loader in ("load_bfm_classification", "load_individual_numbers_model", "load_color_classification_model"):
        monkeypatch.setattr(image_service, loader, lambda: object())
    monkeypatch.setattr(image_service, "load_orientation_model", lambda: FakeOrientation(90))
    seen = {}

    def quality(image_bytes, model=None):
        seen["quality"] = decode(image_bytes).shape[:2]
        return {"prediction": "good", "confidence": 0.99, "all_probs": [0.01, 0.99]}

    def recognise(array, model=None):
        seen["digits"] = array.shape[:2]
        return iu.DigitRecognitionResult(outcome=iu.RecognitionOutcome.DIGITS_FOUND, reading="0307011",
                                         boxes=[box(30, 30)], classes=[0, 3, 0, 7, 0, 1, 1], rollover_positions=[])

    monkeypatch.setattr(image_service, "classify_bfm_image", quality)
    monkeypatch.setattr(image_service, "direct_recognize_meter_reading", recognise)
    monkeypatch.setattr(image_service, "classify_color_image", lambda crop, model=None: {"prediction": "red",
                                                                                        "confidence": 0.9})
    from conf.config import Config
    svc = image_service.ImageService(Config())
    svc.download_image = lambda url: png(300, 200)                 # arrives sideways: 300 wide, 200 high
    return svc, seen


# ---- the decimal drum ----

def drums(n=7, step=30.0):
    return [box(100 + step * i, 100) for i in range(n)]


def test_box_after_the_red_drum_is_dropped_when_off_the_pitch():
    boxes = drums(7) + [box(100 + 30 * 6 + 18, 100)]                 # gear teeth: 0.6 x the pitch after the red drum
    assert decimal_drum.stray_tail(boxes, [0.9] * 8, [0.01] * 6 + [0.99, 0.02]) == 1


def test_weak_box_after_the_red_drum_is_dropped_even_on_the_pitch():
    assert decimal_drum.stray_tail(drums(8), [0.9] * 7 + [0.46], [0.01] * 6 + [0.99, 0.02]) == 1


def test_real_drums_after_a_reflection_that_looks_red_stay():
    assert decimal_drum.stray_tail(drums(7), [0.9] * 7, [0.01, 0.01, 0.01, 0.9, 0.01, 0.01, 0.01]) == 0     # too far
    assert decimal_drum.stray_tail(drums(7), [0.9] * 7, [0.01] * 5 + [0.9, 0.01]) == 0     # evenly spaced, confident


def test_a_missed_drum_does_not_inflate_the_pitch_and_cut_a_real_digit():
    boxes = [box(x, 100) for x in (100, 170, 240, 270, 300, 330, 360, 390)]   # two wide gaps: drums were missed
    assert decimal_drum.stray_tail(boxes, [0.9] * 8, [0.01] * 6 + [0.9, 0.01]) == 0


def test_only_a_row_left_with_seven_drums_is_trimmed():
    for n in (5, 6, 7, 10):
        boxes = drums(n - 1) + [box(100 + 30 * (n - 2) + 18, 100)]
        assert decimal_drum.stray_tail(boxes, [0.9] * n, [0.01] * (n - 2) + [0.99, 0.02]) == 0


def test_a_reflection_never_cuts_a_weak_real_last_drum():
    photo = np.zeros((200, 400, 3), np.uint8)
    drop, red, _ = decimal_drum.read_last_drum(photo, drums(7), [0.9] * 6 + [0.45], FakeColour([0.01] * 5 + [0.6, 0.45]))
    assert drop == 0


def test_a_faint_red_drum_still_loses_the_stray_after_it():
    boxes = drums(7) + [box(100 + 30 * 6 + 18, 100)]
    assert decimal_drum.stray_tail(boxes, [0.9] * 8, [0.01] * 6 + [0.45, 0.02]) == 1


def test_a_stray_scoring_red_after_the_red_drum_still_goes():
    boxes = drums(7) + [box(100 + 30 * 6 + 18, 100)]
    assert decimal_drum.stray_tail(boxes, [0.9] * 8, [0.01] * 6 + [0.99, 0.6]) == 1


@pytest.mark.parametrize("gap, dropped", [(23, 1), (25, 0)])
def test_the_stray_pitch_limit_is_0_8(gap, dropped):
    boxes = drums(7) + [box(100 + 30 * 6 + gap, 100)]
    assert decimal_drum.stray_tail(boxes, [0.9] * 8, [0.01] * 6 + [0.99, 0.02]) == dropped


def test_two_boxes_after_the_red_drum_go_only_if_both_are_weak():
    p_red = [0.01] * 6 + [0.99, 0.01, 0.01]
    assert decimal_drum.stray_tail(drums(9), [0.9] * 7 + [0.4, 0.4], p_red) == 2
    assert decimal_drum.stray_tail(drums(9), [0.9] * 7 + [0.4, 0.9], p_red) == 0


@pytest.mark.parametrize("last_box, confs, red", [(box(280, 100), [0.9] * 7, True),            # the seventh drum
                                                  (box(268, 100), [0.9] * 7, False),           # off the pitch
                                                  (box(280, 100), [0.9] * 6 + [0.4], False)])  # weak
def test_a_faint_score_counts_only_on_a_real_seventh_drum(last_box, confs, red):
    photo = np.zeros((200, 400, 3), np.uint8)
    boxes = drums(6) + [last_box]
    assert decimal_drum.read_last_drum(photo, boxes, confs, FakeColour([0.01] * 6 + [0.35]))[1] is red


def test_the_colour_model_tells_real_red_and_black_drums_apart():
    model = iu.load_drum_colour_model()
    corners = np.array([[0, 0], [63, 0], [63, 95], [0, 95]], dtype=np.int32)
    score = {}
    for name in ("red", "black"):
        drum = cv2.imread(str(Path(__file__).parent / "fixtures" / f"drum_{name}.png"))
        score[name] = model.red_probabilities(drum, [iu.Det.from_polygon(corners, 0, 0.9)])[0]
    assert score["red"] >= 0.6 and score["black"] <= 0.03


@pytest.mark.parametrize("p_red", [[0.01] * 7, [0.01] * 6 + [0.9]])
def test_nothing_is_dropped_without_a_box_after_a_red_drum(p_red):
    assert decimal_drum.stray_tail(drums(7), [0.9] * 7, p_red) == 0


@pytest.mark.parametrize("p_red, drums, red", [(0.32, 7, True), (0.29, 7, False), (0.32, 6, False), (0.49, 6, False),
                                               (0.5, 6, True), (0.49, 8, False), (0.9, 5, True)])
def test_a_faint_red_counts_only_on_a_7_drum_row(p_red, drums, red):
    assert decimal_drum.is_red(p_red, drums) is red


def test_last_drum_colour_on_the_hand_checked_annexure(annex_counter_drums):
    """P(red) of the production boxes on the hand-checked photos: the last drum is right on 94 of 96 (032 and 097:
    the digit model missed the red drum), and exactly the five stray boxes go."""
    red_zero = {"009", "068", "069"}                                  # red .0 drums the truth column leaves out
    wrong, dropped = [], []
    for item in annex_counter_drums:
        drop = decimal_drum.stray_tail(item["pts"], item["confs"], item["p_red"])
        red = decimal_drum.is_red(item["p_red"][len(item["p_red"]) - 1 - drop], len(item["p_red"]) - drop)
        truth = float(item["truth"])
        has_decimal = truth != int(truth) or item["photo"] in red_zero
        wrong += [item["photo"]] if red != has_decimal else []
        dropped += [item["photo"]] if drop else []
    assert len(annex_counter_drums) == 96
    assert wrong == ["032", "097"]
    assert dropped == ["008", "009", "016", "026", "045"]


class FakeColour:
    def __init__(self, p_red=None, fail=False):
        self.p_red, self.fail, self.seen = p_red, fail, None

    def red_probabilities(self, bgr, dets):
        self.seen = bgr
        if self.fail:
            raise RuntimeError("model broke")
        return self.p_red


def recognised(n=8, confidences=True):
    boxes = drums(n - 1) + [box(100 + 30 * (n - 2) + 18, 100)]
    return iu.DigitRecognitionResult(
        outcome=iu.RecognitionOutcome.DIGITS_FOUND, reading="03070115", boxes=boxes, classes=[0, 3, 0, 7, 0, 1, 1, 5],
        rollover_positions=[{"position": 3, "selectedDigit": {}, "alternateDigit": {}},
                            {"position": 8, "selectedDigit": {}, "alternateDigit": {}}],
        confidences=[0.9] * n if confidences else None)


def test_a_stray_box_leaves_the_reading_and_the_red_drum_is_the_last_digit():
    result = recognised()
    colour = iu.settle_last_drum(result, np.zeros((200, 400, 3), np.uint8), FakeColour([0.01] * 6 + [0.99, 0.02]))
    assert colour == {"prediction": "red", "confidence": 0.99}
    assert result.reading == "0307011" and len(result.boxes) == 7 and len(result.confidences) == 7
    assert [rp["position"] for rp in result.rollover_positions] == [3]


def test_a_black_last_drum_is_reported_black():
    result = recognised()
    colour = iu.settle_last_drum(result, np.zeros((200, 400, 3), np.uint8), FakeColour([0.01] * 8))
    assert colour["prediction"] == "black" and colour["confidence"] == pytest.approx(0.99)
    assert result.reading == "03070115"


@pytest.mark.parametrize("model, confidences", [(None, True), (FakeColour(fail=True), True),
                                                (FakeColour([0.9] * 8), False)])
def test_without_the_model_or_a_counter_row_the_old_colour_check_decides(model, confidences):
    result = recognised(confidences=confidences)
    assert iu.settle_last_drum(result, np.zeros((200, 400, 3), np.uint8), model) is None
    assert result.reading == "03070115"


def test_decimal_drum_switched_off(monkeypatch):
    monkeypatch.setenv("FLOWVISION_DECIMAL_DRUM", "off")
    assert iu.settle_last_drum(recognised(), np.zeros((200, 400, 3), np.uint8), FakeColour([0.9] * 8)) is None
    assert iu.load_drum_colour_model() is None


def test_a_missing_or_broken_drum_model_does_not_stop_the_service(tmp_path):
    assert iu.load_drum_colour_model(str(tmp_path / "missing.pt")) is None
    broken = tmp_path / "broken.pt"
    broken.write_bytes(b"not a model")
    assert iu.load_drum_colour_model(str(broken)) is None


def test_the_shipped_drum_colour_model_loads_and_scores_a_drum():
    model = iu.load_drum_colour_model()
    assert model is not None
    drum = iu.Det.from_polygon(box(60, 60), 1, 0.9)
    image = np.full((120, 120, 3), 255, np.uint8)
    cv2.putText(image, "1", (50, 75), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
    assert 0.0 <= model.red_probabilities(image, [drum])[0] <= 1.0


def test_quality_and_digits_see_the_upright_photo_and_the_response_is_unchanged(service):
    svc, seen = service
    response = svc.extract_reading(ReadingExtractionRequest(imageURL="https://example.org/meter.jpg"),
                                   BackgroundTasks())
    assert seen["quality"] == (300, 200) and seen["digits"] == (300, 200)      # turned upright: 200 wide, 300 high
    body = response.model_dump(mode="json", exclude_none=True)
    assert set(body) == {"id", "ts", "responseCode", "statusCode", "result"}
    assert set(body["result"]) == {"status", "correlationId", "data"}
    assert body["result"]["status"] == "SUCCESS" and set(body["result"]["data"]) == V1_DATA_FIELDS
    assert body["result"]["data"]["meterReading"] == "0307011" and body["result"]["data"]["lastDigitColor"] == "red"


@pytest.fixture
def decimal_service(service, monkeypatch):
    svc, seen = service
    svc.drum_colour_model = FakeColour([0.01] * 6 + [0.99])
    monkeypatch.setattr(image_service, "direct_recognize_meter_reading", lambda array, model=None: iu.DigitRecognitionResult(
        outcome=iu.RecognitionOutcome.DIGITS_FOUND, reading="0307011", boxes=drums(7),
        classes=[0, 3, 0, 7, 0, 1, 1], rollover_positions=[], confidences=[0.9] * 7))
    monkeypatch.setattr(image_service, "classify_color_image", lambda crop, model=None: {"prediction": "black",
                                                                                         "confidence": 0.8})
    return svc


def read(svc):
    return svc.extract_reading(ReadingExtractionRequest(imageURL="https://example.org/meter.jpg"), BackgroundTasks()
                               ).model_dump(mode="json", exclude_none=True)["result"]


def test_the_drum_model_decides_the_colour_and_the_reading_stays_raw_digits(decimal_service):
    result = read(decimal_service)
    assert set(result["data"]) == V1_DATA_FIELDS
    assert result["data"]["meterReading"] == "0307011"                # the fastai classifier said black
    assert result["data"]["lastDigitColor"] == "red" and result["data"]["colorConfidence"] == 0.99


def test_decimal_in_reading_writes_the_point_only_on_a_red_last_drum(decimal_service, monkeypatch):
    monkeypatch.setenv("FLOWVISION_DECIMAL_IN_READING", "true")
    result = read(decimal_service)
    assert result["data"]["meterReading"] == "030701.1" and set(result["data"]) == V1_DATA_FIELDS
    decimal_service.drum_colour_model = FakeColour([0.01] * 7)
    assert read(decimal_service)["data"]["meterReading"] == "0307011"


def test_a_dropped_stray_reaches_the_response(decimal_service, monkeypatch):
    boxes = drums(7) + [box(100 + 30 * 6 + 18, 100)]
    keep, gone = {"position": 7, "selectedDigit": {"value": 1, "confidence": 0.9},
                  "alternateDigit": {"value": 2, "confidence": 0.5}}, \
                 {"position": 8, "selectedDigit": {"value": 5, "confidence": 0.9},
                  "alternateDigit": {"value": 6, "confidence": 0.5}}
    monkeypatch.setattr(image_service, "direct_recognize_meter_reading", lambda array, model=None: iu.DigitRecognitionResult(
        outcome=iu.RecognitionOutcome.DIGITS_FOUND, reading="03070115", boxes=boxes,
        classes=[0, 3, 0, 7, 0, 1, 1, 5], rollover_positions=[keep, gone], confidences=[0.9] * 8))
    decimal_service.drum_colour_model = FakeColour([0.01] * 6 + [0.99, 0.02])
    data = read(decimal_service)["data"]
    assert data["meterReading"] == "0307011" and data["lastDigitColor"] == "red"
    assert data["hasRollover"] is True and [rp["position"] for rp in data["rolloverPositions"]] == [7]


def test_the_colour_model_gets_the_upright_photo_in_true_colours(decimal_service):
    read(decimal_service)
    assert decimal_service.drum_colour_model.seen.shape[:2] == (300, 200)
    assert np.array_equal(decimal_service.drum_colour_model.seen,
                          cv2.rotate(decode(png(300, 200)), cv2.ROTATE_90_COUNTERCLOCKWISE))


def test_the_point_is_never_written_on_the_old_colour_check(decimal_service, monkeypatch):
    monkeypatch.setenv("FLOWVISION_DECIMAL_IN_READING", "true")
    monkeypatch.setattr(image_service, "classify_color_image", lambda crop, model=None: {"prediction": "red",
                                                                                         "confidence": 0.8})
    decimal_service.drum_colour_model = None
    data = read(decimal_service)["data"]
    assert data["meterReading"] == "0307011" and data["lastDigitColor"] == "red"
