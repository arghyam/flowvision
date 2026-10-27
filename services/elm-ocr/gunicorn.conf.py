"""
Gunicorn settings for production (Docker image and systemd unit): gunicorn -c gunicorn.conf.py routes:app

The timeouts fit an AWS Application Load Balancer with its idle timeout set to 120 s (see docs/DEPLOY_AWS.md):
- a reading takes 10-30 s with GPT-6 Sol (about 4 s with Claude) and never more than the request deadline (METER_REQUEST_DEADLINE, else
  limits.request_deadline_seconds in the service's config.yaml, 100 s by default);
- keep-alive is longer than the load balancer's idle timeout, so the load balancer, not gunicorn, closes idle
  connections (otherwise it can reuse a connection gunicorn just closed, and the caller gets a 502);
- on shutdown or redeploy, in-flight readings get the full deadline to finish (give the container at least
  graceful_timeout to stop: ECS stopTimeout 120, docker stop -t 130).
Every value can be overridden with the environment variable named next to it. Access and error lines from the
workers are written by the service's own logger (one line each, JSON in production); the master process's own lines
(worker restarts, out-of-memory kills, timeouts) use the same format.
"""
import json
import logging
import os
from pathlib import Path

try:                                    # the same .env the service reads (variables already set win), so a deadline
    from dotenv import load_dotenv      # or worker count set there applies to gunicorn too
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def _num(name, default):
    return int(float(os.getenv(name, "").strip() or default))


def _configured_deadline():
    """limits.request_deadline_seconds from the config file the service reads (CONFIG_PATH, else src/conf)."""
    try:
        import yaml
        path = os.getenv("CONFIG_PATH") or Path(__file__).resolve().parent / "src" / "conf" / "config.yaml"
        with open(path, encoding="utf-8") as f:
            return float(((yaml.safe_load(f) or {}).get("limits") or {}).get("request_deadline_seconds") or 100)
    except Exception:  # noqa: BLE001 - the service itself reports a broken config file
        return 100


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                 "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


deadline = _num("METER_REQUEST_DEADLINE", _configured_deadline())

bind = os.getenv("BIND", "0.0.0.0:8000")
worker_class = "uvicorn_worker.UvicornWorker"
workers = _num("WORKERS", 2)                    # each worker runs up to METER_MAX_CONCURRENT_READS readings at once
timeout = _num("TIMEOUT", deadline + 60)        # worker heartbeat; readings run in threads and are bounded by the deadline
graceful_timeout = _num("GRACEFUL_TIMEOUT", deadline + 20)
keepalive = _num("KEEPALIVE", 130)              # > the load balancer's idle timeout (120 s)
# 0: workers are not recycled on a schedule. Recycling was tried and dropped: gunicorn's worker timeout can cut a
# reading during the old worker's shutdown, and memory stays flat without it (MALLOC_ARENA_MAX=2 and the body limit).
max_requests = _num("MAX_REQUESTS", 0)
max_requests_jitter = _num("MAX_REQUESTS_JITTER", 0)
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "*")   # trust X-Forwarded-* from the load balancer
accesslog = "-"                                  # to stdout/stderr (CloudWatch, journald)
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
control_socket_disable = True                    # no control socket: nothing is written to a read-only filesystem

_production = os.getenv("METER_ENV", "").strip().lower() == "production"
if os.getenv("METER_LOG_FORMAT", "json" if _production else "text").strip().lower() == "json":
    # the master process's lines (worker started, killed, timed out) as JSON lines too, for the CloudWatch filter
    logconfig_dict = {
        "version": 1, "disable_existing_loggers": False,
        "formatters": {"json": {"()": _JsonFormatter}},
        "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stdout"}},
        "loggers": {"gunicorn.error": {"level": loglevel.upper(), "handlers": ["console"], "propagate": False},
                    "gunicorn.access": {"level": loglevel.upper(), "handlers": ["console"], "propagate": False}},
    }
