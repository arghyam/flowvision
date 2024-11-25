import os
import logging
from logging.handlers import TimedRotatingFileHandler

from conf.config import Config


class CustomLoggers:

    def __init__(self, config: Config):
        self.config = config
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
        logging.basicConfig(
            level=logging.INFO,
            format=log_format
        )
        self.base_logger = logging.getLogger(logger_name)
        if self.base_logger.hasHandlers():
            self.base_logger.handlers.clear()

        formatter = logging.Formatter(log_format)
        base_log_handler = TimedRotatingFileHandler(filename=f"{log_path}/{log_file}", when='midnight', interval=1)
        base_log_handler.setFormatter(formatter)
        self.base_logger.addHandler(base_log_handler)
