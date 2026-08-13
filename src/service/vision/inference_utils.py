import warnings
warnings.filterwarnings('ignore')

from fastai.vision.all import *
from PIL import Image
import numpy as np
import os
import cv2
from ultralytics import YOLO
import yaml

from conf.config import Config
import logging

# Load configuration
# def load_config(config_path="src/conf/config.yaml"):
#     """
#     Load configuration from YAML file
    
#     Args:
#         config_path: Path to the config YAML file
        
#     Returns:
#         Configuration dictionary
#     """
#     with open(config_path, 'r') as file:
#         config = yaml.safe_load(file)
#     return config

# CONFIG = load_config()
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
    
    # Apply adaptive thresholding to the value channel (brightness)
    thresh = cv2.adaptiveThreshold(
        v, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
        cv2.THRESH_BINARY, 11, 2
    )
    
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

def test_image_prediction(image_path=None):
    """
    Test function to run prediction on a single image
    
    Args:
        image_path: Path to the image file to classify
        
    Returns:
        None, prints prediction results
    """
    try:
        if image_path is None:
            image_path = CONFIG['test']['sample_image']
            
        # Run classification
        results = classify_bfm_image(image_path)
        extraction_logger.info(results)
            
    except Exception as e:
        extraction_logger.error(f"Error processing image: {str(e)}")


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
        Filtered boxes, classes, and confidences
    """
    if len(boxes) == 0:
        return [], [], []
    
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

def direct_recognize_meter_reading(image_path, individual_numbers_model=None):
    """
    Process image and directly recognize digits without meter detection
    
    Args:
        image_path: Path to the input image or PIL Image object
        individual_numbers_model: Pre-loaded YOLO model (optional)
    
    Returns:
        Meter reading as a string
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
        return "Error: Invalid image input"
        
    if image is None:
        return "Error: Could not load image"
    
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
        return "Error: No digits detected in the image"

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

    return meter_reading, sorted_boxes, sorted_classes, rollover_positions

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

    # Get bounding rectangle for the polygon
    rect = cv2.boundingRect(box)
    x, y, w, h = rect

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


def is_last_digit_color_different_hsv(digit_crops, threshold=0.85):
    """
    Returns True if the last digit's color differs from the others based on HSV analysis.
    
    Args:
        digit_crops: List of the last 3 digit images [n-2, n-1, n]
        threshold: Match ratio threshold below which the digit is considered different color
        
    Returns:
        Tuple (is_red, confidence) where is_red is a boolean and confidence is a float
    """
    # Convert all digit crops to HSV color space
    hsv_digits = [cv2.cvtColor(crop, cv2.COLOR_BGR2HSV) for crop in digit_crops]

    # Combine pixels of the 2nd and 3rd last digits as reference
    ref_pixels = np.concatenate([hsv.reshape(-1, 3) for hsv in hsv_digits[:2]], axis=0)
    last_pixels = hsv_digits[2].reshape(-1, 3)
    
    # Define tolerance range (H, S, V)
    margin = np.array([12, 40, 40])
    
    # Compute HSV bounds from reference digits
    min_vals = np.maximum(np.min(ref_pixels, axis=0) - margin, [0, 0, 0])
    max_vals = np.minimum(np.max(ref_pixels, axis=0) + margin, [180, 255, 255])

    # Only compare Hue and Saturation channels
    last_pixels_hs = last_pixels[:, :2]
    min_vals_hs = min_vals[:2]
    max_vals_hs = max_vals[:2]

    # Count how many pixels in last digit are within the reference range
    in_range = np.all((last_pixels_hs >= min_vals_hs) & (last_pixels_hs <= max_vals_hs), axis=1)
    match_ratio = np.sum(in_range) / len(last_pixels)
    
    is_red = match_ratio < threshold
    confidence = 1.0 - match_ratio if is_red else match_ratio
    
    return {"is_red": is_red, "confidence": confidence}

if __name__ == "__main__":
    # Example usage
    test_image_path = CONFIG['test']['sample_image']
    
    # First classify the image
    classification_result = classify_bfm_image(test_image_path)
    extraction_logger.info("Classification result: %s", classification_result)
    
    # Only proceed with digit detection if image is classified as "Good"
    if classification_result['prediction'].lower() == 'good':
        meter_reading = direct_recognize_meter_reading(test_image_path)
        extraction_logger.info("Detected meter reading: %s", meter_reading)
    else:
        extraction_logger.info("Image classified as bad quality - skipping digit detection")

