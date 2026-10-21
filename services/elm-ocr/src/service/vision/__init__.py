"""Meter reading with GPT-6 Sol or Claude (settings.provider) and safety checks (no web framework or database
dependencies)."""
from .checks import ACCEPTED, NEEDS_REVIEW, RETAKE, Previous
from .images import ImageError
from .meter_types import MeterType, MeterTypeRegistry
from .openai_client import ModelError, OpenAIVisionClient
from .reader import MeterReader, ReadingResult, previous_from
from .settings import ReaderSettings

__all__ = ["ACCEPTED", "NEEDS_REVIEW", "RETAKE", "Previous", "ImageError", "MeterType", "MeterTypeRegistry",
           "ModelError", "OpenAIVisionClient", "MeterReader", "ReadingResult", "previous_from", "ReaderSettings"]
