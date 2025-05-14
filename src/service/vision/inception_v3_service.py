from service.vision.base_vision_service import BaseVisionService
from fastai.vision.all import *
from PIL import Image
import numpy as np
import cv2
import requests
from io import BytesIO
import logging
from conf.config import Config
from ultralytics import YOLO
import yaml

from service.vision.inference_utils import (
    load_bfm_classification,
    load_individual_numbers_model,
    load_color_classification_model,
    classify_bfm_image,
    direct_recognize_meter_reading,
    classify_color_image
)


class InceptionV3VisionService(BaseVisionService):
    def __init__(self, config: Config):
        super().__init__()
        api_logger_name = config.find("logs.api_logger.name")
        self.base_logger = logging.getLogger(api_logger_name)
        self.base_logger.info("Loading fine-tuned models...")
        
        # Load models from config
        self.bfm_classification_model = load_bfm_classification()
        self.individual_numbers_model = load_individual_numbers_model()
        self.color_classification_model = load_color_classification_model()
        
    def extract(self, image=None, image_bytes: bytes | None = None, download_url: str | None = None):
        try:
            # Convert input to image
            if image_bytes:
                image = Image.open(BytesIO(image_bytes))
            elif download_url:
                image = Image.open(BytesIO(requests.get(download_url).content))
            
            # Convert to numpy array
            img = np.array(image)
            
            is_meter = classify_bfm_image(img, self.bfm_classification_model)
            self.base_logger.info(f"Meter classification result: {is_meter}")
            
            if is_meter['prediction'] != 'good':
                return "nometer"
                
            reading = direct_recognize_meter_reading(img, self.individual_numbers_model)
            return reading
            
        except Exception as e:
            self.base_logger.error(f"Error in meter reading extraction: {str(e)}", exc_info=True)
            return "unclear"