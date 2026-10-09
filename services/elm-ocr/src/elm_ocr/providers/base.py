"""Contract between the ELM service and its reading providers.

A provider turns one meter image into a reading. Concrete providers (for example a
local model or a hosted vision API) implement ``ReadingProvider``. The service
downloads the image, calls the configured provider and maps its ``ProviderResult``
to the API response.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionContext:
    """One extraction request, as handed to a provider."""

    correlation_id: str
    image: bytes
    """Downloaded image bytes. Providers never fetch ``image_url`` themselves."""
    image_url: str
    """Source URL, for logging and provenance only."""
    media_type: str | None = None
    """MIME type reported by the download (e.g. ``"image/jpeg"``), if known."""


class ReadingOutcome(StrEnum):
    """What the provider found in the image. The service maps this to the API status."""

    READING_FOUND = "READING_FOUND"
    NO_READING = "NO_READING"
    """The image is usable, but no meter display or digits were found."""
    UNCLEAR = "UNCLEAR"
    """The image is too poor (blur, glare, cropping) to attempt a reading."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderResult:
    """A provider's answer for one image."""

    outcome: ReadingOutcome
    reading: str | None = None
    """Characters as shown on the display (e.g. ``"001234.5"``). Set only when
    ``outcome`` is ``READING_FOUND``; kept as text to preserve leading zeros."""
    confidence: float | None = None
    """Provider-reported confidence in ``[0, 1]``, if the provider reports one."""
    details: Mapping[str, Any] = field(default_factory=dict)
    """Provider-specific diagnostics (model version, raw output). Not part of the
    API response."""

    def __post_init__(self) -> None:
        if (self.outcome is ReadingOutcome.READING_FOUND) != (self.reading is not None):
            raise ValueError("reading must be set if and only if outcome is READING_FOUND")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


class ProviderError(Exception):
    """The provider failed to produce a result (timeout, bad upstream response, ...).

    Raise this for failures of the provider itself. An image without a readable
    meter is not an error; return ``NO_READING`` or ``UNCLEAR`` instead.
    """


class ReadingProvider(Protocol):
    """Extracts a reading from one meter image.

    ``extract`` runs on the event loop and must not block it. Run CPU-bound
    inference with ``anyio.to_thread.run_sync`` (or a process pool) and use async
    clients for network calls.
    """

    name: str
    """Stable identifier recorded with every result (e.g. ``"local-model"``)."""

    async def extract(self, context: ExtractionContext) -> ProviderResult:
        """Return the reading for ``context.image``. Raise ``ProviderError`` on failure."""
        ...
