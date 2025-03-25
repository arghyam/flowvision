from models.models import Error, Status, ReadingExtractionRequest, ReadingExtractionResponse, ReadingExtractionResult, ReadingExtractionResultData, ResponseCode, FeedbackRequest, FeedbackResponseStatus, FeedbackResponse, FeedbackStatus, BaseResponse
from service.api.database import DatabaseService
from conf.config import Config
from conf import queries
import json
import logging
import traceback


class MetadataStore:
    def __init__(self, config: Config):
        self.config = config
        self.database_service = DatabaseService(config=config)
        self.timestamp_format = "%m-%d-%Y, %H:%M:%S"
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))

    def store_request(self, request: ReadingExtractionRequest):
        try:
            to_store = {
                "request_id": str(request.id),
                "image_url": request.imageURL,
                "image_id": None,
                "metadata": json.dumps(request.metadata) if (request.metadata is not None) else request.metadata,
                "request_timestamp": request.ts.strftime(self.timestamp_format)
            }
            # print(f"STORING REQUEST: {to_store}")
            self.database_service.upsert(queries.store_request, to_store)
        except Exception as e:
            self.base_logger.error("\nError type: %s\nRequest id: %s\nTrace: %s", type(e).__name__, request.id, traceback.format_exc())

    def store_response(self, response: ReadingExtractionResponse):
        try:
            to_store = {
                "meter_reading_status": response.result.status.value,
                "meter_reading": response.result.data.meterReading,
                "correlation_id": str(response.result.correlationId),
                "response_timestamp": response.ts.strftime(self.timestamp_format),
                "request_id": str(response.id)
            }
            # print(f"STORING RESPONSE: {to_store}")
            self.database_service.upsert(queries.store_response, to_store)
        except Exception as e:
            self.base_logger.error("\nError type: %s\nRequest id: %s\nTrace: %s", type(e).__name__, response.id, traceback.format_exc())

    def store_feedback(self, feedback: FeedbackRequest):
        try:
            to_store = {
                "extracted_reading_accurate": feedback.data.accurate,
                "actual_reading": feedback.data.actual,
                "feedback_timestamp": feedback.ts.strftime(self.timestamp_format),
                "correlation_id": str(feedback.correlationId)
            }
            # print(f"STORING FEEDBACK: {to_store}")
            self.database_service.upsert(queries.store_feedback, to_store)
        except Exception as e:
            self.base_logger.error("\nError type: %s\nRequest id: %s\nTrace: %s", type(e).__name__, feedback.id, traceback.format_exc())
