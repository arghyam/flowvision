import json
import os
import operator
from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from functools import reduce
from pathlib import Path

import yaml
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

from service.vision.settings import ReaderSettings

_MISSING = object()
# meter_reader_service/ (in Docker: /app). Relative data and log paths are resolved from here, not from the
# directory the service happens to be started in.
SERVICE_ROOT = Path(__file__).resolve().parents[2]


def service_path(path) -> str:
    """An absolute path; a relative one is taken relative to the service folder."""
    p = Path(path).expanduser()
    return str(p if p.is_absolute() else SERVICE_ROOT / p)


class Config:
    """FlowVision's config loader: conf/config.yaml, or the file named by CONFIG_PATH."""

    def __init__(self, path=None):
        load_dotenv()
        default_config_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        config_file = path or os.getenv("CONFIG_PATH") or default_config_file_path
        with open(config_file, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}

    def find(self, path: str, default=None):
        # Unlike FlowVision's version, a configured false/0/empty value is returned as-is, not replaced by the default.
        try:
            value = reduce(operator.getitem, path.split("."), self.config)
        except (KeyError, TypeError):
            value = _MISSING
        return default if value is _MISSING or value is None else value


def _env(name, fallback, cast=str):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return fallback
    return cast(raw.strip())


def _bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def environment() -> str:
    """METER_ENV: 'production' turns on the startup checks in check_production(); anything else is development."""
    return os.getenv("METER_ENV", "development").strip().lower() or "development"


PROVIDERS = ("openai", "claude")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def load_settings(config: Config) -> ReaderSettings:
    """Reader settings from config.yaml, overridable per deployment with METER_* environment variables.
    `provider` (METER_PROVIDER) chooses the model: "openai" (GPT-6 Sol, the default) or "claude"; only that provider's
    API key is needed, and METER_MODEL, METER_REASONING_EFFORT and METER_REQUEST_TIMEOUT apply to it."""
    provider = str(_env("METER_PROVIDER", config.find("provider", "openai"))).strip().lower()
    if provider not in PROVIDERS:
        raise RuntimeError(f"METER_PROVIDER (or provider in config.yaml) must be one of {', '.join(PROVIDERS)}, "
                           f"not {provider!r}")
    claude = provider == "claude"
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if claude and not anthropic_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (METER_PROVIDER=claude)")
    if not claude and not openai_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    section = "claude" if claude else "openai"
    model = _env("METER_MODEL", config.find(f"{section}.model", "claude-sonnet-5-5" if claude else "gpt-6-sol"))
    if str(model).lower().startswith("claude") != claude:      # e.g. METER_MODEL=gpt-6-sol left set after switching
        raise RuntimeError(f"model {model!r} is not {'an' if provider == 'openai' else 'a'} {provider} model: set METER_MODEL (or {section}.model in "
                           f"config.yaml) to match METER_PROVIDER={provider}")
    effort = _env("METER_REASONING_EFFORT",
                  config.find("claude.effort" if claude else "openai.reasoning_effort", "high"))
    effort = None if str(effort).lower() in ("none", "off", "") else str(effort).lower()
    if claude and effort is not None and effort not in CLAUDE_EFFORTS:
        raise RuntimeError(f"Claude effort must be one of {', '.join(CLAUDE_EFFORTS)} (or none), not {effort!r}")
    prices = "pricing.claude." if claude else "pricing."
    return ReaderSettings(
        openai_api_key=openai_key,
        provider=provider,
        model=model,
        reasoning_effort=effort,
        image_detail=_env("METER_IMAGE_DETAIL", config.find("openai.image_detail", "high")),
        reads=max(1, _env("METER_READS", int(config.find("reader.reads", 1)), int)),
        min_confidence=_env("METER_MIN_CONFIDENCE", float(config.find("reader.min_confidence", 0.9)), float),
        clear_read_confidence=_env("METER_CLEAR_READ_CONFIDENCE",
                                   float(config.find("reader.clear_read_confidence", 0.95)), float),
        max_daily_increase=_env("METER_MAX_DAILY_INCREASE", float(config.find("reader.max_daily_increase", 300)), float),
        openai_base_url=_env("OPENAI_BASE_URL", config.find("openai.base_url", "https://api.openai.com/v1")).rstrip("/"),
        anthropic_api_key=anthropic_key,
        anthropic_base_url=_env("ANTHROPIC_BASE_URL",
                                config.find("claude.base_url", "https://api.anthropic.com")).rstrip("/"),
        anthropic_workspace_id=str(_env("ANTHROPIC_WORKSPACE_ID", config.find("claude.workspace_id", ""))).strip(),
        request_timeout=_env("METER_REQUEST_TIMEOUT", float(config.find(f"{section}.timeout_seconds", 90)), float),
        # METER_OPENAI_ATTEMPTS keeps its meaning (OpenAI only); Claude's comes from claude.max_attempts
        max_attempts=max(1, int(config.find("claude.max_attempts", 2)) if claude
                         else _env("METER_OPENAI_ATTEMPTS", int(config.find("openai.max_attempts", 2)), int)),
        max_image_bytes=int(config.find("file_size_limit", 20971520)),
        max_image_side=int(config.find("reader.max_image_side", 2048)),
        max_image_pixels=int(config.find("reader.max_image_pixels", 16_000_000)),
        max_jpeg_pixels=int(config.find("reader.max_jpeg_pixels", 250_000_000)),
        meter_types_file=_env("METER_TYPES_FILE", config.find("reader.meter_types_file")),
        price_input_per_m=float(config.find(prices + "input_per_million", 2.0)),
        price_output_per_m=float(config.find(prices + "output_per_million", 10.0)),
    )


def database_url(config: Config) -> str:
    """METER_DB_URL, else database.url in config.yaml (SQLite by default; use PostgreSQL in production).
    A relative SQLite file path is resolved from the service folder."""
    raw = _env("METER_DB_URL", config.find("database.url", "sqlite:///data/meter_reader.db"))
    url = make_url(raw)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        return f"sqlite:///{service_path(url.database)}"
    return raw


def image_location(config: Config) -> str | None:
    """Where photos are kept: an s3://bucket/prefix URI, a folder (relative paths are under the service folder),
    or None (METER_IMAGE_DIR / storage.images_dir empty or 'none': photos are not kept)."""
    d = _env("METER_IMAGE_DIR", config.find("storage.images_dir", "data/images"))
    if not d or str(d).strip().lower() in ("none", "off", "false"):
        return None
    d = str(d).strip()
    return d if d.lower().startswith("s3://") else service_path(d)


def _offset(text: str) -> tzinfo:
    """'+05:30' -> a fixed UTC offset."""
    sign = -1 if text.strip().startswith("-") else 1
    hours, _, minutes = text.strip().lstrip("+-").partition(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes or 0)))


@dataclass(frozen=True)
class ServiceSettings:
    """Settings for the web service around the reader (security, limits, storage, integration)."""
    environment: str = "development"
    api_keys: tuple = ()                        # accepted X-API-Key values (several, to rotate without downtime)
    docs_enabled: bool = True                   # /docs, /redoc, /openapi.json
    errors_as_http_200: bool = False            # FlowVision style: error envelopes with HTTP 200
    request_deadline: float = 100.0             # seconds for one reading, download to answer
    max_concurrent_reads: int = 8               # per worker process; more wait busy_wait seconds, then get 503
    busy_wait: float = 30.0
    daily_budget_usd: float | None = None       # stop calling the model once today's spend reaches this (config: $25)
    budget_configured: bool = False             # METER_DAILY_BUDGET_USD was set (a number, or "none" on purpose)
    duplicate_photo_hours: float = 168.0        # the same photo of the same consumer again: earlier result, no call
    reserve_per_call_usd: float = 0.05          # reserved against the daily budget before each model call
    naive_time_zone: tzinfo = field(default_factory=lambda: _offset("+05:30"))  # for times sent without an offset
    max_body_bytes: int = 30 * 1024 * 1024
    max_inflight_body_bytes: int = 150 * 1024 * 1024  # photo bodies held at once per worker; more: 503, retry
    allowed_hosts: tuple = ()                   # image URL hosts; () = any public host
    allow_private: bool = False                 # image hosts on private addresses (same VPC); link-local never
    download_timeout: float = 30.0
    download_headers: dict = field(default_factory=dict)  # sent only to allowed_hosts


def load_service_settings(config: Config) -> ServiceSettings:
    env = environment()
    keys = os.getenv("METER_SERVICE_API_KEY", "")
    hosts = os.getenv("METER_IMAGE_URL_ALLOWED_HOSTS")
    hosts = [h for h in (hosts.split(",") if hosts is not None else config.find("image_download.allowed_hosts", []))]
    headers = os.getenv("METER_IMAGE_URL_HEADERS")
    budget_raw = os.getenv("METER_DAILY_BUDGET_USD", "").strip()
    budget = budget_raw or config.find("limits.daily_budget_usd")
    return ServiceSettings(
        environment=env,
        api_keys=tuple(k.strip() for k in keys.split(",") if k.strip()),
        docs_enabled=_bool(_env("METER_DOCS_ENABLED", config.find("api.docs_enabled", env != "production"))),
        errors_as_http_200=_bool(_env("METER_ERRORS_AS_HTTP_200", config.find("api.errors_as_http_200", False))),
        request_deadline=_env("METER_REQUEST_DEADLINE", float(config.find("limits.request_deadline_seconds", 100)), float),
        max_concurrent_reads=max(1, _env("METER_MAX_CONCURRENT_READS",
                                         int(config.find("limits.max_concurrent_reads", 8)), int)),
        busy_wait=_env("METER_BUSY_WAIT_SECONDS", float(config.find("limits.busy_wait_seconds", 30)), float),
        daily_budget_usd=float(budget) if str(budget or "").strip().lower() not in ("", "none") else None,
        budget_configured=bool(budget_raw) or config.find("limits.daily_budget_usd") is not None,
        duplicate_photo_hours=_env("METER_DUPLICATE_PHOTO_HOURS",
                                   float(config.find("limits.duplicate_photo_hours", 168)), float),
        reserve_per_call_usd=float(config.find("limits.reserve_per_call_usd", 0.05)),
        naive_time_zone=_offset(_env("METER_NAIVE_TIME_OFFSET", config.find("api.naive_time_offset", "+05:30"))),
        max_body_bytes=_env("METER_MAX_BODY_BYTES", int(config.find("limits.max_body_bytes", 30 * 1024 * 1024)), int),
        max_inflight_body_bytes=_env("METER_MAX_INFLIGHT_BODY_BYTES",
                                     int(config.find("limits.max_inflight_body_bytes", 150 * 1024 * 1024)), int),
        allowed_hosts=tuple(h.strip().lower() for h in hosts if h.strip() and h.strip() != "*"),
        allow_private=_bool(_env("METER_ALLOW_PRIVATE_IMAGE_URLS", config.find("image_download.allow_private", False))),
        download_timeout=float(config.find("image_download.timeout_seconds", 30)),
        download_headers=json.loads(headers) if headers and headers.strip()
        else dict(config.find("image_download.headers", {})),
    )


def check_production(service: ServiceSettings, db_url: str, images: str | None) -> list[str]:
    """What is missing for a production deployment (METER_ENV=production refuses to start until the list is empty)."""
    problems = []
    if not service.api_keys:
        problems.append("METER_SERVICE_API_KEY is not set: every endpoint would be open")
    if make_url(db_url).get_backend_name() == "sqlite":
        problems.append("METER_DB_URL is not set to PostgreSQL: each instance would keep its own SQLite file, "
                        "and the previous-reading check would lose its history")
    if not os.getenv("METER_IMAGE_DIR", "").strip():
        problems.append("METER_IMAGE_DIR is not set: use s3://bucket/prefix (or a persistent folder, or 'none')")
    listed = [h.strip() for h in os.getenv("METER_IMAGE_URL_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not service.allowed_hosts and "*" not in listed:
        problems.append("METER_IMAGE_URL_ALLOWED_HOSTS is not set: list the central system's image host(s), "
                        "or '*' to allow any public host")
    if not service.budget_configured:
        problems.append("METER_DAILY_BUDGET_USD is not set: choose a daily limit for model spend in USD "
                        "(or 'none' to run without one, on purpose)")
    weak = [k for k in service.api_keys if len(k) < 16 or k.lower() in ("change-me", "changeme", "secret", "test")]
    if weak:
        problems.append("METER_SERVICE_API_KEY is too short or a placeholder: use at least 16 random characters")
    if service.allow_private and not service.allowed_hosts:
        problems.append("METER_ALLOW_PRIVATE_IMAGE_URLS=true needs METER_IMAGE_URL_ALLOWED_HOSTS to list the hosts "
                        "(not '*'), or any address inside the network could be fetched")
    if service.download_headers and not service.allowed_hosts:
        problems.append("METER_IMAGE_URL_HEADERS needs METER_IMAGE_URL_ALLOWED_HOSTS to list the hosts (not '*'): "
                        "the credentials are only ever sent to listed hosts")
    return problems
