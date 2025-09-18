import atexit
import json
import logging
import queue
import threading
import traceback
from typing import Callable, Tuple

from models.models import FeedbackRequest, ReadingExtractionRequest, ReadingExtractionResponse
from service.api.database import DatabaseService
from conf.config import Config
from conf import queries


class MetadataStore:
    def __init__(self, config: Config):
        self.config = config
        self.database_service = DatabaseService(config=config)
        self.timestamp_format = "%m-%d-%Y, %H:%M:%S"
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))

        queue_size = config.find("metadata.queue_maxsize", 0) or 0
        self._task_queue: "queue.Queue[Tuple[Callable, tuple, dict]]" = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        worker_count = max(1, int(config.find("metadata.worker_threads", 2) or 1))
        self._workers = [self._start_worker(index) for index in range(worker_count)]
        atexit.register(self._shutdown)

    def _start_worker(self, index: int) -> threading.Thread:
        worker = threading.Thread(
            target=self._worker_loop,
            name=f"metadata-worker-{index}",
            daemon=True,
        )
        worker.start()
        return worker

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task, args, kwargs = self._task_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                task(*args, **kwargs)
            except Exception as exc:
                self.base_logger.error(
                    "\nError type: %s\nTrace: %s",
                    type(exc).__name__,
                    traceback.format_exc(),
                )
            finally:
                self._task_queue.task_done()

    def _shutdown(self) -> None:
        self._stop_event.set()
        for worker in self._workers:
            if worker.is_alive():
                worker.join(timeout=0.1)

    def _submit_task(self, func: Callable, *args, **kwargs) -> None:
        try:
            self._task_queue.put((func, args, kwargs), timeout=0.1)
        except queue.Full:
            self.base_logger.warning("Metadata queue is full; dropping %s task", func.__name__)

    def enqueue_request(self, request: ReadingExtractionRequest) -> None:
        self._submit_task(self.store_request, request)

    def enqueue_response(self, response: ReadingExtractionResponse) -> None:
        self._submit_task(self.store_response, response)

    def enqueue_feedback(self, feedback: FeedbackRequest) -> None:
        self._submit_task(self.store_feedback, feedback)

    def store_request(self, request: ReadingExtractionRequest):
        try:
            to_store = {
                "request_id": str(request.id),
                "image_url": request.imageURL,
                "image_id": None,
                "metadata": json.dumps(request.metadata) if (request.metadata is not None) else request.metadata,
                "request_timestamp": request.ts.strftime(self.timestamp_format),
            }
            self.database_service.upsert(queries.store_request, to_store)
        except Exception as exc:
            self.base_logger.error(
                "\nError type: %s\nRequest id: %s\nTrace: %s",
                type(exc).__name__,
                request.id,
                traceback.format_exc(),
            )

    def store_response(self, response: ReadingExtractionResponse):
        try:
            to_store = {
                "meter_reading_status": response.result.status.value,
                "meter_reading": response.result.data.meterReading,
                "correlation_id": str(response.result.correlationId),
                "response_timestamp": response.ts.strftime(self.timestamp_format),
                "request_id": str(response.id),
                "quality_status": response.result.data.qualityStatus,
                "quality_confidence": response.result.data.qualityConfidence,
                "last_digit_color": response.result.data.lastDigitColor,
                "color_confidence": response.result.data.colorConfidence,
                "processing_time": response.result.data.processingTime,
            }
            self.database_service.upsert(queries.store_response, to_store)
        except Exception as exc:
            self.base_logger.error(
                "\nError type: %s\nRequest id: %s\nTrace: %s",
                type(exc).__name__,
                response.id,
                traceback.format_exc(),
            )

    def store_feedback(self, feedback: FeedbackRequest):
        try:
            to_store = {
                "extracted_reading_accurate": feedback.data.accurate,
                "actual_reading": feedback.data.actual,
                "feedback_timestamp": feedback.ts.strftime(self.timestamp_format),
                "correlation_id": str(feedback.correlationId),
            }
            self.database_service.upsert(queries.store_feedback, to_store)
        except Exception as exc:
            self.base_logger.error(
                "\nError type: %s\nRequest id: %s\nTrace: %s",
                type(exc).__name__,
                feedback.id,
                traceback.format_exc(),
            )
