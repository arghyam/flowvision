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


# def classify_bfm_image(image_path, model=None):
def classify_bfm_image(img, model=None):
    """
    Classify a Bulk Flow Meter image as good or bad
    
    Args:
        image_path: Path to the image file or PIL Image object
        model: Optional pre-loaded model. If None, will load the model
        
    Returns:
        Dictionary with classification results:
        {
            'prediction': str,       # 'good' or 'bad'
            'confidence': float,     # Probability of the prediction
            'all_probs': list        # Probabilities for all classes
        }
    """
    # Load model if not provided
    if model is None:
        model = load_bfm_classification()
    
    # Load the image
    # if isinstance(image_path, str):
    #     image = cv2.imread(image_path)
    # elif isinstance(image_path, np.ndarray):
    #     image = image_path
    # else:
    #     raise ValueError("Input must be either a file path or a numpy array")
    
    # # Convert BGR to RGB since FastAI expects RGB
    # image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    
    # # Convert to FastAI image format
    # img = PILImage.create(image)
    
    # Make prediction
    pred_class, pred_idx, probs = model.predict(img)
    
    return {
        'prediction': str(pred_class),
        'confidence': float(probs[pred_idx]),
        'all_probs': [float(p) for p in probs]
    }

def classify_color_image(image_path, model=None):
    """
    Classify a color image as red, black, or blue
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


def sort_boxes_by_position(boxes, classes):
    """
    Sort detected digit boxes by their x-coordinate for left-to-right reading order.
    """
    # Create a list of (box, class) tuples
    box_class_pairs = list(zip(boxes, classes))
    
    # For oriented bounding boxes, use the leftmost x-coordinate of each box
    sorted_pairs = sorted(box_class_pairs, key=lambda pair: np.min(pair[0][:, 0]))
    
    # Unzip the sorted pairs back into separate lists
    sorted_boxes, sorted_classes = zip(*sorted_pairs) if box_class_pairs else ([], [])
    
    return list(sorted_boxes), list(sorted_classes)

def calculate_iou(box1, box2):
    """
    Calculate the Intersection over Union (IoU) between two polygon boxes
    
    Args:
        box1: First box as a numpy array of 4 points [(x1,y1), (x2,y2), (x3,y3), (x4,y4)]
        box2: Second box as a numpy array of 4 points [(x1,y1), (x2,y2), (x3,y3), (x4,y4)]
        
    Returns:
        IoU value between 0 and 1
    """
    # Convert polygon points to contour format for OpenCV
    box1_contour = box1.reshape(-1, 1, 2).astype(np.int32)
    box2_contour = box2.reshape(-1, 1, 2).astype(np.int32)
    
    # Create blank binary images
    img_size = (1000, 1000)  # Large enough canvas
    box1_mask = np.zeros(img_size, dtype=np.uint8)
    box2_mask = np.zeros(img_size, dtype=np.uint8)
    
    # Fill polygons
    cv2.fillPoly(box1_mask, [box1_contour], 1)
    cv2.fillPoly(box2_mask, [box2_contour], 1)
    
    # Calculate intersection and union
    intersection = np.logical_and(box1_mask, box2_mask).sum()
    union = np.logical_or(box1_mask, box2_mask).sum()
    
    # Calculate IoU
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
    
    # Process boxes in order of confidence
    while box_data:
        # Get the box with the highest confidence
        current_box, current_class, current_conf = box_data.pop(0)
        
        # Add to filtered list
        filtered_boxes.append(current_box)
        filtered_classes.append(current_class)
        filtered_confidences.append(current_conf)
        
        # Check remaining boxes
        remaining_boxes = []
        for box, cls, conf in box_data:
            # Calculate IoU between current box and this box
            iou = calculate_iou(current_box, box)
            
            # If IoU is below threshold, keep this box for next iteration
            if iou < iou_threshold:
                remaining_boxes.append((box, cls, conf))
        
        # Update box_data with remaining boxes
        box_data = remaining_boxes
    
    return filtered_boxes, filtered_classes, filtered_confidences

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
    if digit_boxes:
        digit_boxes, digit_classes, digit_confidences = remove_overlapping_boxes(
            digit_boxes, digit_classes, digit_confidences, iou_threshold=0.3
        )
    
    # Step 5: Post-processing - sort the digits from left to right
    if not digit_boxes:
        return "Error: No digits detected in the image"
    
    sorted_boxes, sorted_classes = sort_boxes_by_position(digit_boxes, digit_classes)
    
    # Step 6: Extract the class labels and join them to form the digit sequence
    meter_reading = ''.join([str(cls) for cls in sorted_classes])
    
    return meter_reading , sorted_boxes , sorted_classes

# Function to extract digit image from its bounding box
def extract_digit_image(image, box):
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
    
    # Apply mask to get only the digit
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

