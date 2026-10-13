"""
FlowVision's loggers: an API log plus one-line-per-request extraction and feedback logs.

Every record goes to the console (stdout), one line per record, which is what CloudWatch collects from containers
and from journald. METER_LOG_FORMAT=json (the default when METER_ENV=production) writes JSON lines, with any
traceback inside the line. Daily-rotated files under logs/ are written too in development (METER_LOG_TO_FILES
turns them on or off; off by default in production and in the Docker image), in JSON when the console is JSON;
METER_LOG_RETENTION_DAYS of them are kept (30 by default). The web server's access and error lines use the same
handler.
"""
import json
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from conf.config import Config, environment, service_path


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                 "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class NoQueryFilter(logging.Filter):
    """Access-log lines carry the path without its query string: a key or token put in a URL by mistake is never
    logged (the API itself takes no query parameters)."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = args[:2] + (args[2].split("?", 1)[0] + "?<query removed>",) + args[3:]
        return True


class OneLineFormatter(logging.Formatter):
    """Text format, with a traceback folded into the same line (' | ' instead of line breaks)."""

    def format(self, record):
        return super().format(record).replace("\n", " | ")


def _flag(name, default):
    raw = os.getenv(name, "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


class CustomLoggers:
    def __init__(self, config: Config):
        self.config = config
        production = environment() == "production"
        self.json = os.getenv("METER_LOG_FORMAT", "json" if production else "text").lower() == "json"
        # Files: on by default in development; in production the console (stdout: CloudWatch, journald) is the log,
        # and several gunicorn workers must not rotate the same file. METER_LOG_TO_FILES=true turns them on.
        self.to_files = _flag("METER_LOG_TO_FILES", not production)
        self.retention_days = int(os.getenv("METER_LOG_RETENTION_DAYS", "30") or 30)
        level = os.getenv("METER_LOG_LEVEL", config.find("log_level", "info")).upper()
        root = logging.getLogger()
        root.setLevel(level)
        if not any(getattr(h, "_meter_console", False) for h in root.handlers):
            root.addHandler(self._console('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        self.create_logger(self.config.find("logs.api_logger.name"),
                           '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                           self.config.find("logs.api_logger.path"), "api.log")
        self.create_logger(self.config.find("logs.feedback_request_logger.name"), '%(message)s',
                           self.config.find("logs.feedback_request_logger.path"), "feedback_request.log")
        self.create_logger(self.config.find("logs.extraction_request_logger.name"), '%(message)s',
                           self.config.find("logs.extraction_request_logger.path"), "extraction_request.log")
        # The web server's own lines (access log, errors with tracebacks) in the same one-line format
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error", "gunicorn.access"):
            server_logger = logging.getLogger(name)
            server_logger.handlers.clear()
            server_logger.propagate = False
            handler = self._console('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            if name.endswith(".access"):
                handler.addFilter(NoQueryFilter())
            server_logger.addHandler(handler)

    def _console(self, text_format):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter() if self.json else OneLineFormatter(text_format))
        handler._meter_console = True
        return handler

    def create_logger(self, logger_name: str, log_format: str, log_path: str, log_file: str):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = False                  # its own console line, not a second one through the root logger
        logger.setLevel(logging.INFO)
        logger.addHandler(self._console('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        if self.to_files:
            log_path = service_path(log_path)     # relative paths are under the service folder
            os.makedirs(log_path, exist_ok=True)
            handler = TimedRotatingFileHandler(filename=f"{log_path}/{log_file}", when="midnight", interval=1,
                                               backupCount=self.retention_days, encoding="utf-8")
            handler.setFormatter(JsonFormatter() if self.json else logging.Formatter(log_format))
            logger.addHandler(handler)
