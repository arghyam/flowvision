import warnings
warnings.filterwarnings('ignore')

from fastai.vision.all import *
from PIL import Image
import numpy as np
import os
import cv2
from ultralytics import YOLO

# Imported after the fastai star import so these names are not shadowed by it.
from dataclasses import dataclass, field
from enum import StrEnum

from conf.config import Config
from service.vision.counter import select_row
from service.vision.orientation import OrientationModel
from service.vision.screen_check import ScreenCheck
from service.vision.types import Det
from service.vision.yolox_reader import Reader, last_wheel_redness
import logging

CONFIG = Config().config
base_logger = logging.getLogger(CONFIG['logs']['api_logger']['name'])
extraction_logger = logging.getLogger(CONFIG['logs']['extraction_request_logger']['name'])

#Load the Bulk Flow Meter FastAI classification model
def load_bfm_classification(model_path=None):
    """
    Load the Bulk Flow Meter FastAI classification model
    
    Args:
        model_path: Path to the FastAI model file
        
    Returns:
        Loaded FastAI learner object
    """
    if model_path is None:
        model_path = CONFIG['models']['bfm_classification']
        base_logger.debug("Model path: %s", model_path)
    learn = load_learner(model_path)
    return learn

#Load only the individual numbers model
def load_individual_numbers_model(model_path=None):
    """
    Load the individual numbers model
    
    Args:
        model_path: Path to the individual numbers model file
        
    Returns:
        Loaded YOLO model object
    
    """
    if model_path is None:
        model_path = CONFIG['models']['individual_numbers']
    individual_numbers_model = YOLO(model_path)
    return individual_numbers_model

#Load the color classification model
def load_color_classification_model(model_path=None):
    """
    Load the color classification model
    """
    if model_path is None:
        model_path = CONFIG['models']['color_classification']
    learn = load_learner(model_path)
    return learn

#Load the orientation model
def load_orientation_model(model_path=None):
    """
    Load the orientation model (a small yolo11n classifier) that tells how far a
    photo is rotated, so it can be turned upright before anything else looks at it.

    Returns None when models.orientation is empty, FLOWVISION_ORIENTATION is off,
    or the model cannot be loaded (logged as an error): photos are then read as
    taken, as before, and the service still starts.
    """
    if model_path is None:
        model_path = CONFIG['models'].get('orientation')
    if not model_path or not orientation_enabled():
        return None
    try:
        return OrientationModel(model_path, CONFIG.get('orientation_min_confidence', 0.9))
    except Exception:
        base_logger.error("Orientation model %s could not be loaded; photos will be read as taken",
                          model_path, exc_info=True)
        return None


#Load the YOLOX reader
def load_digit_reader(models_dir=None):
    """
    Load the YOLOX reader (window finder and digit reader) when it is switched on.

    Returns None when FLOWVISION_YOLOX_READER is off: digits are then read by the
    individual numbers model, as before.
    """
    if not yolox_reader_enabled():
        return None
    return Reader(models_dir or CONFIG['models']['yolox_reader'], red_limit=CONFIG.get('reader_red_limit', 0.7),
                  level_tilt=CONFIG.get('reader_level_tilt', 2.0), colour_unsure=tuple(CONFIG.get('reader_colour_unsure', [0.3, 0.7])))


#Load the screen / paper check
def load_screen_check(model_path=None):
    """
    Load the screen / paper check when it is switched on.

    Returns None when models.screen_check is empty, FLOWVISION_SCREEN_CHECK is off, or the model cannot be
    loaded (logged as an error): photos are then read without it, as before, and the service still starts.
    """
    if model_path is None:
        model_path = CONFIG['models'].get('screen_check')
    if not model_path or not screen_check_enabled():
        return None
    try:
        return ScreenCheck(model_path, CONFIG.get('screen_check_threshold'))
    except Exception:
        base_logger.error("Screen check %s could not be loaded; screens and paper will not be refused",
                          model_path, exc_info=True)
        return None


def check_screen(image, model=None):
    """
    Is the photo of a phone / computer screen or of paper rather than the meter? Returns the check's dict
    (meter, screen, paper, refused, kind), or None when the check is off, not loaded or fails (the photo is
    then read as before).
    """
    if model is None or not screen_check_enabled():
        return None
    try:
        return model(image)
    except Exception:
        base_logger.warning("Screen check failed; reading the photo without it", exc_info=True)
        return None


def get_quality_threshold():
    """
    Resolve the quality threshold used to classify an image as good/bad.

    Precedence: FLOWVISION_QUALITY_THRESHOLD env var > config.yaml
    (quality_threshold) > hardcoded default (0.5). Exposing it as an env var
    lets ops tune the threshold at deploy time (set the variable and restart
    the container) without rebuilding the image. Read on each call so the
    value reflects the current environment.

    An env value that is not a float in [0, 1] is ignored with a warning and
    the config value is used, so a bad override never breaks classification.
    """
    config_default = CONFIG.get('quality_threshold', 0.5)
    env_value = os.environ.get('FLOWVISION_QUALITY_THRESHOLD')
    if env_value is None:
        return config_default
    try:
        threshold = float(env_value)
    except ValueError:
        base_logger.warning(
            "Invalid FLOWVISION_QUALITY_THRESHOLD=%r (not a number); "
            "falling back to config value %s", env_value, config_default)
        return config_default
    if not 0.0 <= threshold <= 1.0:
        base_logger.warning(
            "FLOWVISION_QUALITY_THRESHOLD=%s is out of range [0, 1]; "
            "falling back to config value %s", threshold, config_default)
        return config_default
    return threshold


def get_digit_padding_color():
    """
    Resolve the fill color used for the area of a digit crop that falls outside
    the detected digit polygon (see extract_digit_image).

    Precedence: FLOWVISION_DIGIT_PADDING env var > config.yaml
    (digit_padding_color) > hardcoded default ('black', the original behaviour).
    Read on each call so the value reflects the current environment.

    The color classifier is sensitive to this padding: the original model was
    trained on black-padded crops, while the finetuned v2 model classifies
    tilted digits noticeably better with white padding. Keeping it switchable
    lets both combinations be evaluated on production data without a code
    change or rebuild.

    An env value that is not 'black' or 'white' is ignored with a warning and
    the config value is used, so a bad override never breaks color extraction.
    """
    config_default = CONFIG.get('digit_padding_color', 'black')
    env_value = os.environ.get('FLOWVISION_DIGIT_PADDING')
    if env_value is None:
        return config_default
    padding = env_value.strip().lower()
    if padding not in ('black', 'white'):
        base_logger.warning(
            "Invalid FLOWVISION_DIGIT_PADDING=%r (expected 'black' or 'white'); "
            "falling back to config value %s", env_value, config_default)
        return config_default
    return padding


def _switch(env_name, config_key, default=True):
    """
    An on/off setting: the env var (true/false, on/off, 1/0) wins over config.yaml,
    so ops can switch a fix off at deploy time (set the variable and restart the
    container) without rebuilding the image. Read on each call. An env value that
    is not recognised is ignored with a warning and the config value is used.
    """
    config_default = bool(CONFIG.get(config_key, default))
    env_value = os.environ.get(env_name)
    if env_value is None:
        return config_default
    value = env_value.strip().lower()
    if value in ('true', 'on', '1', 'yes'):
        return True
    if value in ('false', 'off', '0', 'no'):
        return False
    base_logger.warning("Invalid %s=%r (expected true or false); falling back to config value %s",
                        env_name, env_value, config_default)
    return config_default


def counter_row_only():
    """Read only the meter's counter row (FLOWVISION_COUNTER_ROW_ONLY > config.yaml counter_row_only > on)."""
    return _switch('FLOWVISION_COUNTER_ROW_ONLY', 'counter_row_only')


def yolox_reader_enabled():
    """Read digits with the YOLOX reader (FLOWVISION_YOLOX_READER > config.yaml yolox_reader > off)."""
    return _switch('FLOWVISION_YOLOX_READER', 'yolox_reader', default=False)


def turning_wheel_allowed():
    """Read a turning wheel instead of refusing it (FLOWVISION_TURNING_WHEEL > config.yaml turning_wheel_allowed > on).
    Which of its two digits counts: turning_wheel_lower."""
    return _switch('FLOWVISION_TURNING_WHEEL', 'turning_wheel_allowed', default=True)


def turning_wheel_lower():
    """On: a wheel caught between two digits gives the lower one, the digit going out (JalSoochak's Rollover Digits
    Handling Spec, considered 14 Nov 2026 and not adopted). Off: the owner's half rule of 12 Nov, the digit that shows
    more (FLOWVISION_TURNING_WHEEL_LOWER > config.yaml turning_wheel_lower > off)."""
    return _switch('FLOWVISION_TURNING_WHEEL_LOWER', 'turning_wheel_lower', default=False)


def orientation_enabled():
    """Turn rotated photos upright (FLOWVISION_ORIENTATION > on; models.orientation "" also disables it)."""
    return _switch('FLOWVISION_ORIENTATION', 'orientation_enabled')


def screen_check_enabled():
    """Refuse photos of a phone screen or paper (FLOWVISION_SCREEN_CHECK > config.yaml screen_check > on)."""
    return _switch('FLOWVISION_SCREEN_CHECK', 'screen_check')


def classify_bfm_image(img, model=None, threshold=None):
    """
    Classify a Bulk Flow Meter image as good or bad.

    Args:
        img: PIL Image or numpy array
        model: Optional pre-loaded FastAI learner
        threshold: Minimum P(good) to classify as 'good'. Defaults to the
                   FLOWVISION_QUALITY_THRESHOLD env var, else config value
                   quality_threshold (0.5 if neither is set).

    Returns:
        {
            'prediction': str,    # 'good' or 'bad'
            'confidence': float,  # probability of the predicted class
            'all_probs': list     # [P(bad), P(good)]
        }
    """
    if model is None:
        model = load_bfm_classification()

    if threshold is None:
        threshold = get_quality_threshold()

    with model.no_bar():  # fastai otherwise prints a progress bar into the server log for every photo
        _, _, probs = model.predict(img)
    all_probs = [float(p) for p in probs]

    # vocab is alphabetical: ['bad', 'good'] — index 1 is always 'good'
    good_idx = list(model.dls.vocab).index('good')
    good_prob = all_probs[good_idx]

    prediction = 'good' if good_prob >= threshold else 'bad'
    confidence = good_prob if prediction == 'good' else 1.0 - good_prob

    return {
        'prediction': prediction,
        'confidence': confidence,
        'all_probs': all_probs
    }

def classify_color_image(image_path, model=None):
    """
    Classify the last digit crop as red or black.
    """
    if model is None:
        model = load_color_classification_model()
        
    # Load the image
    if isinstance(image_path, str):
        image = cv2.imread(image_path)
    elif isinstance(image_path, np.ndarray):
        image = image_path
    else:
        raise ValueError("Input must be either a file path or a numpy array")
    
    # Convert BGR to RGB since FastAI expects RGB
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    
    # Convert to FastAI image format
    img = PILImage.create(image)

    # Make prediction
    with model.no_bar():  # no progress bar in the server log
        pred_class, pred_idx, probs = model.predict(img)

    return {
        'prediction': str(pred_class),
        'confidence': float(probs[pred_idx]),
        'all_probs': [float(p) for p in probs]
    }

def enhance_image(image):
    """
    Enhance the image to improve readability for image while preserving color
    """
    # Get enhancement parameters from config
    enhance_config = CONFIG['image_enhancement']
    clahe_clip = enhance_config['clahe_clip_limit']
    clahe_grid = tuple(enhance_config['clahe_tile_grid_size'])
    sharp_alpha = enhance_config['sharpening_alpha']
    sharp_beta = enhance_config['sharpening_beta']
    color_alpha = enhance_config['color_boost_alpha']
    color_beta = enhance_config['color_boost_beta']
    
    # Convert to HSV color space to separate brightness from color
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    
    # Apply unsharp masking for sharpening to the value channel
    gaussian = cv2.GaussianBlur(v, (0, 0), 3.0)
    sharpened_v = cv2.addWeighted(v, sharp_alpha, gaussian, sharp_beta, 0)
    
    # Increase contrast in the value channel
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=clahe_grid)
    enhanced_v = clahe.apply(sharpened_v)
    
    # Merge channels back with enhanced value channel
    enhanced_hsv = cv2.merge([h, s, enhanced_v])
    
    # Convert back to BGR color space
    enhanced = cv2.cvtColor(enhanced_hsv, cv2.COLOR_HSV2BGR)
    
    # Apply color boost to make digits stand out more
    enhanced_image = cv2.convertScaleAbs(enhanced, alpha=color_alpha, beta=color_beta)
    
    return enhanced_image

# How to undo each rotation the orientation model reports (how far the photo is turned clockwise).
UNDO_ROTATION = {90: cv2.ROTATE_90_COUNTERCLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_CLOCKWISE}


def turn_upright(image_bytes, model=None):
    """
    Turn a sideways or upside-down photo upright before anything else looks at it.

    The digit model only reads upright meters (upside down it reads the drums in
    reverse with 6 and 9 swapped), and the quality model rates almost every rotated
    photo bad (median P(good) 0.000 against 0.999 for upright ones, 1,061 rotated
    production photos of 14 Sep 2026). So this runs first and the quality check,
    the digit model and the colour check all see the upright photo.

    Args:
        image_bytes: The photo as encoded bytes (the PNG from download_image)
        model: The loaded OrientationModel, or None (photos are read as taken)

    Returns:
        (image_bytes, angle, confidence): the upright photo as PNG bytes when it was
        turned, else the bytes unchanged; angle is how far it was turned back (0, 90,
        180 or 270). Any failure leaves the photo as taken.
    """
    if model is None or not orientation_enabled():
        return image_bytes, 0, None
    try:
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return image_bytes, 0, None
        angle, confidence = model.predict(image)
        if angle not in UNDO_ROTATION:
            return image_bytes, 0, confidence
        ok, buffer = cv2.imencode('.png', cv2.rotate(image, UNDO_ROTATION[angle]))
        if not ok:
            return image_bytes, 0, confidence
        return buffer.tobytes(), angle, confidence
    except Exception:
        base_logger.warning("Orientation check failed; reading the photo as taken", exc_info=True)
        return image_bytes, 0, None


def sort_boxes_by_position(boxes, classes, confidences=None):
    """
    Sort detected digit boxes by their x-coordinate for left-to-right reading order.
    """
    if confidences is not None:
        box_class_pairs = list(zip(boxes, classes, confidences))
        sorted_pairs = sorted(box_class_pairs, key=lambda pair: np.min(pair[0][:, 0]))
        if not sorted_pairs:
            return [], [], []
        sorted_boxes, sorted_classes, sorted_confs = zip(*sorted_pairs)
        return list(sorted_boxes), list(sorted_classes), list(sorted_confs)

    box_class_pairs = list(zip(boxes, classes))
    sorted_pairs = sorted(box_class_pairs, key=lambda pair: np.min(pair[0][:, 0]))
    sorted_boxes, sorted_classes = zip(*sorted_pairs) if box_class_pairs else ([], [])
    return list(sorted_boxes), list(sorted_classes)

def calculate_iou(box1, box2):
    """
    Calculate the Intersection over Union (IoU) between two polygon boxes.

    Args:
        box1: First box as a numpy array of 4 points [(x1,y1), (x2,y2), (x3,y3), (x4,y4)]
        box2: Second box as a numpy array of 4 points [(x1,y1), (x2,y2), (x3,y3), (x4,y4)]

    Returns:
        IoU value between 0 and 1
    """
    # Build a canvas that tightly fits both boxes so coordinates are never
    # clipped — the previous (1000, 1000) canvas silently dropped any boxes
    # whose points lay beyond x=1000 or y=1000, returning IoU=0 for them.
    all_pts = np.vstack([box1, box2])
    x_min = max(int(np.min(all_pts[:, 0])), 0)
    y_min = max(int(np.min(all_pts[:, 1])), 0)
    x_max = int(np.max(all_pts[:, 0])) + 1
    y_max = int(np.max(all_pts[:, 1])) + 1

    w = x_max - x_min
    h = y_max - y_min

    # Translate both boxes to local (0-based) coordinates
    offset = np.array([x_min, y_min])
    b1 = (box1-offset).reshape(-1, 1, 2).astype(np.int32)
    b2 = (box2-offset).reshape(-1, 1, 2).astype(np.int32)

    box1_mask = np.zeros((h, w), dtype=np.uint8)
    box2_mask = np.zeros((h, w), dtype=np.uint8)

    cv2.fillPoly(box1_mask, [b1], 1)
    cv2.fillPoly(box2_mask, [b2], 1)

    intersection = np.logical_and(box1_mask, box2_mask).sum()
    union = np.logical_or(box1_mask, box2_mask).sum()

    if union == 0:
        return 0
    return intersection / union

def remove_overlapping_boxes(boxes, classes, confidences, iou_threshold=0.5):
    """
    Remove overlapping boxes, keeping only the one with the highest confidence
    
    Args:
        boxes: List of bounding boxes
        classes: List of class labels
        confidences: List of confidence scores
        iou_threshold: IoU threshold above which boxes are considered overlapping
        
    Returns:
        Filtered boxes, classes, confidences, and rollover pairs
        (winner_box, winner_class, winner_conf, loser_class, loser_conf)
    """
    if len(boxes) == 0:
        return [], [], [], []
    
    # Combine data for sorting
    box_data = list(zip(boxes, classes, confidences))
    
    # Sort by confidence (descending)
    box_data.sort(key=lambda x: x[2], reverse=True)
    
    # Initialize lists for keeping filtered boxes
    filtered_boxes = []
    filtered_classes = []
    filtered_confidences = []
    # Each entry: (winner_box, winner_class, winner_conf, loser_class, loser_conf)
    rollover_pairs = []

    # Process boxes in order of confidence
    while box_data:
        # Get the box with the highest confidence
        current_box, current_class, current_conf = box_data.pop(0)

        # Add to filtered list
        filtered_boxes.append(current_box)
        filtered_classes.append(current_class)
        filtered_confidences.append(current_conf)

        # Check remaining boxes; collect all overlapping ones then pick the best alternate
        remaining_boxes = []
        suppressed = []
        for box, cls, conf in box_data:
            iou = calculate_iou(current_box, box)
            if iou < iou_threshold:
                remaining_boxes.append((box, cls, conf))
            else:
                suppressed.append((int(cls), float(conf)))

        # Keep only the highest-confidence suppressed box as the alternate digit;
        if suppressed:
            best_alt_class, best_alt_conf = max(suppressed, key=lambda x: x[1])
            rollover_pairs.append((current_box, int(current_class), float(current_conf), best_alt_class, best_alt_conf))

        box_data = remaining_boxes

    return filtered_boxes, filtered_classes, filtered_confidences, rollover_pairs


def counter_refusal(counter):
    """Why this counter row must not be read (no row, a missed drum, too few drums), else None."""
    if counter is None:
        return "no counter row found"
    if "GAP_IN_ROW" in counter[3]:
        return "a drum is missing from the counter row"
    digits = len(counter[1])
    # A row starting with 0 and one digit short is a 6-wheel counter with its last wheel missed; a row starting
    # with any other digit is a 5-wheel meter
    enough = CONFIG.get('counter_min_digits', 5) - (1 if int(counter[1][0]) != 0 else 0)
    if digits < enough:
        return "too few digits in the counter row"
    return None


def keep_counter_row(boxes, classes, confidences, rollover_pairs):
    """
    Keep only the meter's counter drums, in reading order.

    A bulk meter photo shows more digits than the counter: the x0.1/x0.01 dial
    numbers, scale-ring numbers, the 'H' label, bolt stamps and the meter's serial
    number. Joining every digit left to right put those into the reading (e.g. a
    meter ID read as 20,224,404,265). The drums are the digits on one straight line
    (at any tilt) with the same height and a regular pitch (counter.select_row).
    Two boxes on one drum (a drum half-way between two digits) are one position:
    the more confident digit is read and the other is reported as its rollover
    alternate, as remove_overlapping_boxes does for overlapping boxes.

    Args:
        boxes, classes, confidences: The boxes kept by remove_overlapping_boxes
        rollover_pairs: Its (winner_box, winner_class, winner_conf, loser_class, loser_conf)

    Returns:
        (boxes, classes, rollover_positions, issues) along the counter, or None when
        no row of 3 or more digits is found. issues: the row's warnings, e.g. GAP_IN_ROW.
    """
    dets = [Det.from_polygon(box, cls, conf) for box, cls, conf in zip(boxes, classes, confidences)]
    read = select_row(dets, CONFIG)
    if read is None:
        return None
    index = {id(det): i for i, det in enumerate(dets)}
    row = [index[id(drum.det)] for drum in read.drums]
    overlapped = {id(winner_box): (loser_class, loser_conf)
                  for winner_box, _, _, loser_class, loser_conf in rollover_pairs}

    rollover_positions = []
    for position, (i, drum) in enumerate(zip(row, read.drums), start=1):
        alternate = overlapped.get(id(boxes[i]))
        if alternate is None and drum.alternate is not None:
            alternate = (drum.alternate.value, drum.alternate.conf)
        if alternate is not None:
            rollover_positions.append({
                'position': position,
                'selectedDigit': {'value': int(classes[i]), 'confidence': float(confidences[i])},
                'alternateDigit': {'value': int(alternate[0]), 'confidence': float(alternate[1])}
            })
    # same order as before: most confident drum first (the order remove_overlapping_boxes found them)
    rollover_positions.sort(key=lambda rp: -rp['selectedDigit']['confidence'])
    return [boxes[i] for i in row], [classes[i] for i in row], rollover_positions, read.issues


class RecognitionOutcome(StrEnum):
    """
    Why digit recognition ended the way it did.

    Callers map these to API statuses; they must never infer the outcome from
    the reading text itself.
    """
    DIGITS_FOUND = "DIGITS_FOUND"    # at least one digit was detected
    NO_DIGITS = "NO_DIGITS"          # image was processed, zero digits found
    REFUSED = "REFUSED"              # digits found, but the counter could not be read safely
    INVALID_IMAGE = "INVALID_IMAGE"  # input could not be decoded at all


@dataclass
class DigitRecognitionResult:
    """
    Result of a digit recognition attempt.

    `reading` and the box/class lists are only populated when `outcome` is
    DIGITS_FOUND. `detail` carries a human-readable reason for logging and is
    never surfaced in the API response.
    """
    outcome: RecognitionOutcome
    reading: str | None = None
    boxes: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    rollover_positions: list = field(default_factory=list)
    detail: str | None = None
    last_wheel_redness: float | None = None  # from the YOLOX reader, for the colour step


def recognize_with_yolox_reader(image, reader):
    """
    Read the counter with the YOLOX reader. The digits pass the same counter-row logic
    and refusals as the individual numbers model; a digit below
    reader_min_digit_confidence is refused as well.

    Args:
        image: The photo as a BGR array, not enhanced (the reader was trained on plain photos)
        reader: The loaded yolox_reader.Reader

    Returns:
        DigitRecognitionResult, with boxes in photo coordinates for the colour step
    """
    reading = reader(image)
    if reading is None or not reading.digits:
        return DigitRecognitionResult(outcome=RecognitionOutcome.NO_DIGITS, detail="No counter found in the image")

    if CONFIG.get('reader_versions_agree', False) and hasattr(reader, 'versions'):
        return read_by_agreement(image, reading, reader)

    result = read_counter(reading, reader)
    if result.outcome is RecognitionOutcome.REFUSED:
        # Second try with the cut-out turned level. Only after a refusal: the models learned on cut-outs that are not
        # level, and levelling every tilted counter lost more readings than it gained.
        level = reader(image, level=True)
        if level is not None:
            second = read_counter(level, reader)
            if second.outcome is RecognitionOutcome.DIGITS_FOUND:
                return second
    return result


def read_by_agreement(image, reading, reader):
    """The counter cut tight to its row must read the same on every version of it, or the reading is refused. A wheel
    the model cannot read keeps changing between versions, which caught more misreads than the counter check did.
    A steep counter's first cut-out holds tiny digits, so it has no vote."""
    tight = reader(image, tight=True)
    if tight is None:
        return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED, detail="too few digits in the counter row")
    result = read_counter(tight, reader, counter_check=False)
    if result.outcome is not RecognitionOutcome.DIGITS_FOUND:
        return result
    versions = reader.versions(tight.crop)
    first = [reader.plain_text(reading.crop)] if abs(reading.tilt) < CONFIG.get('reader_steep_tilt', 15.0) else []
    if any(version != result.reading for version in first) or \
            sum(version != result.reading for version in versions) > CONFIG.get('reader_versions_may_differ', 1):
        turning = turning_last_wheel(result.reading, first + versions) if CONFIG.get('reader_rule_e', False) else None
        if turning is not None:
            base_logger.info("Rule E: the last wheel is turning (%s: %s), read as %s",
                             result.reading, ' '.join(first + versions), turning)
            if turning != result.reading:
                result.reading = turning
                result.classes = [type(cls)(int(ch)) for cls, ch in zip(result.classes, turning)]
            return result
        # When every version that differs differs only on the last wheel, the operator is told it is the last wheel
        differing = [v for v in first + versions if v != result.reading]
        last_only = all(len(v) == len(result.reading) and v[:-1] == result.reading[:-1] for v in differing)
        which = "the last wheel" if last_only else "the counter"
        return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED,
                                      detail=f"{which} reads differently each time ({result.reading}: {' '.join(first + versions)})")
    return result


def turning_last_wheel(main, versions):
    """Rule E. When every version, the main reading included, reads one of two numbers one step apart on the last
    wheel, that wheel is caught between two digits, about half of each showing. By the owner's half rule (12 Nov 2026)
    a tie goes to the incoming digit, so the upcoming number, with the carry (9 to 0 included). With turning_wheel_lower
    on it gives the lower number (9 to 0: the number ending in 9). Versions with no digits are left out. Returns
    the reading with the main reading's number of wheels, or None when the rule does not apply."""
    if any(v and not v.isdigit() for v in [main] + list(versions)):
        return None
    values = {int(v) for v in [main] + list(versions) if v}
    if len(values) != 2:
        return None
    low, high = sorted(values)
    if high - low != 1:
        return None
    text = str(low if turning_wheel_lower() else high).zfill(len(main))
    return text if len(text) == len(main) else None


def take_lower_digit(result):
    """Lower rule (used only when turning_wheel_lower is on): a wheel caught between two digits gives the lower one, the digit going out
    (9 when it is between 9 and 0), and the wheels to its right then show 9. So the reading is the lower of the two
    numbers: 024507 with the last wheel between 6 and 7 -> 024506; 058150 with the last wheel between 9 and 0 -> 058149.
    Applied only where the reader found both digits on one wheel (rolloverPositions), and after the agreement check,
    because the versions are read without it. The digit model (round 17a) learned the half rule, so where only the
    more visible digit was found, that digit stays until the model is retrained on lower-rule labels. Changes `result`
    in place (reading, classes, and which rollover digit is the selected one) and returns it."""
    reading = result.reading
    if not reading or not reading.isdigit():
        return result
    # positions whose wheel shows the incoming digit of a one-step pair; an inner wheel only turns during a carry, when
    # every wheel to its right shows its incoming 0
    turning = [rp['position'] for rp in result.rollover_positions
               if rp['selectedDigit']['value'] == (rp['alternateDigit']['value'] + 1) % 10
               and reading[rp['position'] - 1] == str(rp['selectedDigit']['value'])
               and set(reading[rp['position']:]) <= {'0'}]
    if not turning:
        return result
    p = min(turning)
    head = int(reading[:p]) - 1
    if head < 0:
        return result
    lower = str(head).zfill(p) + '9' * (len(reading) - p)
    for rp in result.rollover_positions:
        i = rp['position'] - 1
        if lower[i] != reading[i] and str(rp['alternateDigit']['value']) == lower[i]:
            rp['selectedDigit'], rp['alternateDigit'] = rp['alternateDigit'], rp['selectedDigit']
    base_logger.info("Lower rule: a wheel is turning (%s), read as %s", reading, lower)
    result.reading = lower
    result.classes = [type(cls)(int(ch)) for cls, ch in zip(result.classes, lower)]
    return result


def apply_turning_rule(result):
    """The lower rule (take_lower_digit) on a found reading, when turning_wheel_lower is on."""
    if result.outcome is RecognitionOutcome.DIGITS_FOUND and turning_wheel_lower():
        return take_lower_digit(result)
    return result


def read_counter(reading, reader, counter_check=True):
    """The counter-row logic and refusals on one reading of the counter."""
    boxes = [corners.astype(np.int32) for corners in reading.crop_corners]
    classes = [value for value, _, _ in reading.digits]
    confidences = [score for _, score, _ in reading.digits]
    in_photo = {id(box): digit[2] for box, digit in zip(boxes, reading.digits)}
    boxes, classes, confidences, rollover_pairs = remove_overlapping_boxes(boxes, classes, confidences, iou_threshold=0.3)

    counter = keep_counter_row(boxes, classes, confidences, rollover_pairs)
    reason = counter_refusal(counter)
    if reason:
        return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED, detail=reason)
    row_boxes, row_classes, rollover_positions, _ = counter

    confidence_by_box = {id(box): confidence for box, confidence in zip(boxes, confidences)}
    row_confidences = [confidence_by_box[id(box)] for box in row_boxes]
    lowest = min(row_confidences)
    if lowest < CONFIG.get('reader_min_digit_confidence', 0.8):
        # Which wheel: the operator is told when it is the last one (image_service.retake_message)
        which = "the last wheel" if row_confidences[-1] == lowest else "a digit"
        return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED, detail=f"{which} is too uncertain ({lowest:.2f})")

    # A wheel caught between two digits is read about as confidently as a centred one, so only
    # the counter check can catch it
    check = getattr(reader, 'check', None) if counter_check else None
    if counter_check and len(row_classes) == 5:
        check = getattr(reader, 'check_five', None) or check
    if check is not None:
        scores = check(reading.crop)
        if turning_wheel_allowed():
            if scores['unclear'] >= CONFIG.get('reader_max_unclear', 0.5):
                return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED, detail=f"counter unclear ({scores['unclear']:.2f})")
            # At 9 to 0 two wheels turn together and one can be read old, the other new (58140 for 58150)
            if int(row_classes[-1]) in (0, 9) and scores['centred'] < CONFIG.get('reader_min_carry_centred', 0.1):
                return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED, detail=f"two wheels may be turning together ({scores['centred']:.2f})")
        elif scores['centred'] < CONFIG.get('reader_min_centred', 0.8):
            return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED, detail=f"a wheel may be mid-turn ({scores['centred']:.2f})")

    return DigitRecognitionResult(
        outcome=RecognitionOutcome.DIGITS_FOUND,
        reading=''.join(str(cls) for cls in row_classes),
        boxes=[in_photo[id(box)].astype(np.int32) for box in row_boxes],
        classes=row_classes,
        rollover_positions=rollover_positions,
        last_wheel_redness=last_wheel_redness(reading.crop, row_boxes) if hasattr(reading, 'crop') else None
    )


def direct_recognize_meter_reading(image_path, individual_numbers_model=None, digit_reader=None):
    """
    Process image and directly recognize digits without meter detection

    Args:
        image_path: Path to the input image or PIL Image object
        individual_numbers_model: Pre-loaded YOLO model (optional)
        digit_reader: The loaded YOLOX reader; when given it reads the digits instead

    Returns:
        DigitRecognitionResult — always, on every path. Check `.outcome`
        before reading `.reading`.
    """
    # Step 1: Load the image
    if isinstance(image_path, str):
        image = cv2.imread(image_path)
    elif isinstance(image_path, (np.ndarray, Image.Image)):
        if isinstance(image_path, Image.Image):
            image = np.array(image_path)
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        else:
            image = image_path
    else:
        return DigitRecognitionResult(
            outcome=RecognitionOutcome.INVALID_IMAGE,
            detail="Invalid image input"
        )

    if image is None:
        return DigitRecognitionResult(
            outcome=RecognitionOutcome.INVALID_IMAGE,
            detail="Could not load image"
        )
    
    if digit_reader is not None:
        return apply_turning_rule(recognize_with_yolox_reader(image, digit_reader))

    # Step 2: Enhance the image for better digit recognition
    enhanced_image = enhance_image(image)
    
    # Step 3: Detect individual numbers directly on the enhanced image
    if individual_numbers_model is None:
        individual_numbers_model = load_individual_numbers_model()
    digit_results = individual_numbers_model(enhanced_image)
    
    digit_boxes = []
    digit_classes = []
    digit_confidences = []
    
    # Process digit detection results
    for result in digit_results:
        if hasattr(result, 'obb') and result.obb is not None:
            xyxyxyxy = result.obb.xyxyxyxy  # polygon format with 4-points
            class_ids = result.obb.cls.int()  # class ids of each box
            confidences = result.obb.conf  # confidence scores
            
            # Process each detected digit
            for i, box in enumerate(xyxyxyxy):
                # Convert tensor to numpy array and reshape to 4 points format
                points = box.cpu().numpy().reshape(-1, 2).astype(np.int32)
                class_id = class_ids[i].item()
                confidence = confidences[i].item()
                
                # Only consider high confidence detections
                if confidence > 0.3:
                    digit_boxes.append(points)
                    digit_classes.append(class_id)
                    digit_confidences.append(confidence)
                extraction_logger.debug("Original digit results: %s", digit_classes)
    
    # Step 4: Remove overlapping boxes
    rollover_pairs = []
    if digit_boxes:
        digit_boxes, digit_classes, digit_confidences, rollover_pairs = remove_overlapping_boxes(
            digit_boxes, digit_classes, digit_confidences, iou_threshold=0.3
        )

    # Step 5: Post-processing - sort the digits from left to right
    if not digit_boxes:
        # The image was readable but contains no digits — e.g. it is not a photo
        # of a meter at all. This is a valid outcome, not an error.
        return DigitRecognitionResult(
            outcome=RecognitionOutcome.NO_DIGITS,
            detail="No digits detected in the image"
        )

    # Only the counter drums (no dial, scale-ring or serial-number digits), in reading order
    counter = keep_counter_row(digit_boxes, digit_classes, digit_confidences, rollover_pairs) \
        if counter_row_only() else None
    if counter_row_only():
        # Measured on the 1,000 test photos (30 Oct 2026): these 3 cases were 0 right out of
        # 49 readings. Joining stray digits gives a made-up number, so refuse instead.
        reason = counter_refusal(counter)
        if reason:
            return DigitRecognitionResult(outcome=RecognitionOutcome.REFUSED, detail=reason)
    if counter is not None:
        sorted_boxes, sorted_classes, rollover_positions, _ = counter
    else:
        sorted_boxes, sorted_classes, sorted_confidences = sort_boxes_by_position(
            digit_boxes, digit_classes, digit_confidences
        )

        # Step 6: Map rollover pairs to their 1-indexed position in sorted order
        rollover_positions = []
        for winner_box, winner_class, winner_conf, loser_class, loser_conf in rollover_pairs:
            for i, box in enumerate(sorted_boxes):
                if np.array_equal(box, winner_box):
                    rollover_positions.append({
                        'position': i + 1,
                        'selectedDigit': {'value': winner_class, 'confidence': winner_conf},
                        'alternateDigit': {'value': loser_class, 'confidence': loser_conf}
                    })
                    break

    # Step 7: Extract the class labels and join them to form the digit sequence
    meter_reading = ''.join([str(cls) for cls in sorted_classes])

    return apply_turning_rule(DigitRecognitionResult(
        outcome=RecognitionOutcome.DIGITS_FOUND,
        reading=meter_reading,
        boxes=sorted_boxes,
        classes=sorted_classes,
        rollover_positions=rollover_positions
    ))

# Function to extract digit image from its bounding box
def extract_digit_image(image, box, padding=None):
    """
    Crop a single detected digit out of the meter image.

    YOLO returns an oriented (rotated) box, so the axis-aligned crop contains
    corner regions that lie outside the digit polygon. Those regions are filled
    with a flat padding color.

    Args:
        image: Full meter image as a BGR numpy array
        box: Oriented bounding box as 4 polygon points
        padding: 'black' or 'white'. Defaults to the FLOWVISION_DIGIT_PADDING
                 env var, else config value digit_padding_color ('black' if
                 neither is set).

    Returns:
        Cropped digit image as a BGR numpy array
    """
    if padding is None:
        padding = get_digit_padding_color()

    # Bounding rectangle of the polygon, clipped to the image: a digit at the
    # photo's edge can have corners outside it, and a negative start index
    # would cut the wrong part of the image (or nothing)
    x, y, w, h = cv2.boundingRect(box)
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, image.shape[1]), min(y + h, image.shape[0])
    if x1 <= x0 or y1 <= y0:
        raise ValueError("digit box lies outside the image")
    x, y, w, h = x0, y0, x1 - x0, y1 - y0

    # Extract region from image
    cropped = image[y:y+h, x:x+w].copy()

    # Create mask for the polygon
    mask = np.zeros(cropped.shape[:2], dtype=np.uint8)

    # Shift polygon coordinates to the local rectangle
    shifted_box = box - np.array([x, y])

    # Fill the polygon on the mask
    cv2.fillPoly(mask, [shifted_box], 255)

    # Apply mask to get only the digit, padding the rest of the crop
    if padding == 'white':
        result = np.full_like(cropped, 255)
        result[mask == 255] = cropped[mask == 255]
    else:
        result = cv2.bitwise_and(cropped, cropped, mask=mask)

    return result
