import atexit
import os
import logging
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from queue import SimpleQueue

from conf.config import Config


class CustomLoggers:

    def __init__(self, config: Config):
        self.config = config
        self._listeners: list[QueueListener] = []
        atexit.register(self._stop_listeners)
        self.create_logger(
            logger_name=self.config.find("logs.api_logger.name"),
            log_format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            log_path=self.config.find("logs.api_logger.path"),
            log_file="api.log"
        )
        self.create_logger(
            logger_name=self.config.find("logs.feedback_request_logger.name"),
            log_format='%(message)s',
            log_path=self.config.find("logs.feedback_request_logger.path"),
            log_file="feedback_request.log"
        )
        self.create_logger(
            logger_name=self.config.find("logs.extraction_request_logger.name"),
            log_format='%(message)s',
            log_path=self.config.find("logs.extraction_request_logger.path"),
            log_file="extraction_request.log"
        )

    def create_logger(self, logger_name: str, log_format: str, log_path: str, log_file: str):
        if not os.path.isdir(log_path):
            os.makedirs(log_path, exist_ok=True)
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        if logger.hasHandlers():
            logger.handlers.clear()

        formatter = logging.Formatter(log_format)
        rotating_handler = TimedRotatingFileHandler(
            filename=f"{log_path}/{log_file}",
            when='midnight',
            interval=1,
        )
        rotating_handler.setFormatter(formatter)

        log_queue: "SimpleQueue[logging.LogRecord]" = SimpleQueue()
        queue_handler = QueueHandler(log_queue)
        listener = QueueListener(log_queue, rotating_handler)
        listener.start()
        self._listeners.append(listener)

        logger.addHandler(queue_handler)
        logger.propagate = False

        return logger

    def _stop_listeners(self) -> None:
        for listener in self._listeners:
            listener.stop()
