from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import cv2
import numpy as np
import io
import os
import tempfile
import time
import uuid
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from datetime import datetime
import requests
from enum import Enum
from contextlib import asynccontextmanager

# Import necessary functions from inference_utils
from final_inference.inference_utils import (
    classify_bfm_image, 
    direct_recognize_meter_reading,
    load_bfm_classification,
    load_individual_numbers_model,
    load_color_classification_model,
    classify_color_image,
    extract_digit_image
)

# Enums as per API spec
class ResponseCode(str, Enum):
    OK = "OK"
    ERROR = "ERROR"

class ExtractReadingStatus(str, Enum):
    NOMETER = "NOMETER"
    UNCLEAR = "UNCLEAR"
    INVALID = "INVALID"
    SUCCESS = "SUCCESS"

# Request Models
class ExtractReadingRequest(BaseModel):
    id: str
    ts: datetime
    imageURL: str
    metadata: Optional[Dict[str, Any]] = None

# Response Models
class ResponseError(BaseModel):
    errorCode: str
    errorMsg: str

class GetUploadUrlResultData(BaseModel):
    meterReading: float
    meterBrand: str = "Belanto"  # Default value as per example

class ExtractReadingResultWrapper(BaseModel):
    status: ExtractReadingStatus
    correlationId: Optional[str] = None
    data: Optional[GetUploadUrlResultData] = None

class ExtractReadingResponse(BaseModel):
    id: str
    ts: datetime
    responseCode: ResponseCode
    statusCode: int
    errorCode: Optional[ResponseError] = None
    result: Optional[ExtractReadingResultWrapper] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global models
    models['bfm_classification'] = load_bfm_classification()
    models['individual_numbers'] = load_individual_numbers_model()
    models['color_classification'] = load_color_classification_model()
    yield
    # Shutdown
    models.clear()

app = FastAPI(
    title="JJM Meter Reading Extraction API(s)",
    description="Set of API specifications for extracting meter reading from an image `/flowvision/v1`",
    version="v1",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create temp directory for storing uploaded images if needed
TEMP_DIR = os.path.join(tempfile.gettempdir(), "meter_reading_api")
os.makedirs(TEMP_DIR, exist_ok=True)

# Global variable to store loaded models
models = {
    'bfm_classification': None,
    'individual_numbers': None,
    'color_classification': None
}

async def download_and_save_image(url: str, save_path: str):
    """Download image from URL and save to local path"""
    response = requests.get(url)
    response.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(response.content)

@app.post("/flowvision/v1/extract-reading", response_model=ExtractReadingResponse)
async def extract_reading(request: ExtractReadingRequest):
    """
    Extract meter reading from an image URL
    """
    start_time = time.time()
    correlation_id = str(uuid.uuid4())
    
    try:
        # Download and save image to temp file
        temp_file_path = os.path.join(TEMP_DIR, f"{correlation_id}_image.jpg")
        await download_and_save_image(request.imageURL, temp_file_path)
        
        # Process image quality
        classification_result = classify_bfm_image(temp_file_path, model=models['bfm_classification'])
        quality_status = classification_result['prediction'].lower()
        
        if quality_status != 'good':
            return ExtractReadingResponse(
                id=request.id,
                ts=request.ts,
                responseCode=ResponseCode.OK,
                statusCode=200,
                result=ExtractReadingResultWrapper(
                    status=ExtractReadingStatus.UNCLEAR,
                    correlationId=correlation_id
                )
            )
        
        # Perform digit recognition
        meter_reading, sorted_boxes, sorted_classes = direct_recognize_meter_reading(
            temp_file_path,
            models['individual_numbers']
        )
        
        if not sorted_boxes:
            return ExtractReadingResponse(
                id=request.id,
                ts=request.ts,
                responseCode=ResponseCode.OK,
                statusCode=200,
                result=ExtractReadingResultWrapper(
                    status=ExtractReadingStatus.NOMETER,
                    correlationId=correlation_id
                )
            )
        
        # Process color classification
        image = cv2.imread(temp_file_path)
        last_box = sorted_boxes[-1]
        last_digit_image = extract_digit_image(image, last_box)
        color_result = classify_color_image(last_digit_image, model=models['color_classification'])
        
        try:
            numeric_reading = float(meter_reading)
            if color_result['prediction'].lower() == 'red':
                numeric_reading = numeric_reading / 10
            
            return ExtractReadingResponse(
                id=request.id,
                ts=request.ts,
                responseCode=ResponseCode.OK,
                statusCode=200,
                result=ExtractReadingResultWrapper(
                    status=ExtractReadingStatus.SUCCESS,
                    correlationId=correlation_id,
                    data=GetUploadUrlResultData(
                        meterReading=numeric_reading,
                        meterBrand="Belanto"
                    )
                )
            )
        except ValueError:
            return ExtractReadingResponse(
                id=request.id,
                ts=request.ts,
                responseCode=ResponseCode.OK,
                statusCode=200,
                result=ExtractReadingResultWrapper(
                    status=ExtractReadingStatus.INVALID,
                    correlationId=correlation_id
                )
            )
            
    except Exception as e:
        return ExtractReadingResponse(
            id=request.id,
            ts=request.ts,
            responseCode=ResponseCode.ERROR,
            statusCode=500,
            errorCode=ResponseError(
                errorCode="ERR_READING_EXTRACTION_FAILED",
                errorMsg=f"Failed to extract meter reading: {str(e)}"
            )
        )
    finally:
        # Cleanup temp file
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

if __name__ == "__main__":
    uvicorn.run("fastapi_enhanced:app", host="0.0.0.0", port=8000, reload=True)

#to run
# uvicorn src.final_inference.fastapi_enhanced:app --host 0.0.0.0 --port 8000 --reload