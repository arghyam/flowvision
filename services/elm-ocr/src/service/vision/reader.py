"""
The meter reader: photo in, ACCEPTED reading / RETAKE / NEEDS_REVIEW out.

It does not store anything. A `store` (optional) only has to provide:
    get_profile(consumer_id) -> object with meter_type_id, decimals, digits, billing_unit (or None)
    last_trusted(consumer_id, unit=None, before=None) -> checks.Previous (or None)
service.api.metadata_service.MetadataStore implements both on the database.
"""
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from . import checks
from .checks import REASONS, MeterRule, Previous
from .images import prepare_image
from .meter_types import MeterTypeRegistry
from .openai_client import ModelError, OpenAIVisionClient
from .prompt import PROMPT_VERSION

logger = logging.getLogger(__name__)


@dataclass
class ReadingResult:
    status: str                          # ACCEPTED | RETAKE | NEEDS_REVIEW
    reading: str | None                  # e.g. "0019712.7", whatever the status; status says how far to trust it
    value: Decimal | None                # set only for readings that passed the per-read checks
    unit: str | None
    reasons: list                        # [{"code", "message"}]
    retake_hint: str | None
    meter: dict                          # typeId, make, modelCode, faceplateText
    previous: dict | None
    reads: list                          # one summary per model call
    notes: list
    model: str
    prompt_version: str
    processing_seconds: float
    cost_usd: float
    image_sha1: str = ""
    decimal_source: str | None = None    # consumer | registry | visible_point | previous_reading | model_guess
    uncertain_digits: list = field(default_factory=list)
    raw_answers: list = field(default_factory=list)  # the model's full answers, for offline replay of rule changes

    @property
    def reason_codes(self):
        return [r["code"] for r in self.reasons]


def _aware(dt):
    """Treat a naive datetime as UTC."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def previous_from(value, unit=None, captured_at=None):
    """Raises ValueError if value is not a reading (digits with an optional decimal point)."""
    return Previous(value=checks.parse_reading(value), unit=checks.normalize_unit(unit), captured_at=_aware(captured_at))


def make_client(settings):
    """The model client for settings.provider: OpenAI (the default) or Claude."""
    if settings.provider == "claude":
        try:
            from .claude_client import ClaudeVisionClient       # Anthropic's SDK is only needed for Claude
        except ImportError as e:
            raise RuntimeError("METER_PROVIDER=claude needs the anthropic package "
                               "(pip install --require-hashes -r requirements.lock)") from e
        return ClaudeVisionClient(settings)
    return OpenAIVisionClient(settings)


class MeterReader:
    def __init__(self, settings, client=None, store=None, registry=None):
        self.settings = settings
        self.client = client or make_client(settings)
        self.store = store
        self.registry = registry or MeterTypeRegistry.load(settings.meter_types_file)

    def _rule(self, profile, answer):
        """The meter's format. Each consumer setting wins; a setting left empty comes from the consumer's meter type,
        else from the type identified in the photo, so a partial setting never drops the digit-count check."""
        chosen = self.registry.get(profile.meter_type_id) if profile is not None and profile.meter_type_id else None
        t = chosen or self.registry.identify(answer)
        own_decimals = profile.decimals if profile is not None else None
        own_digits = profile.digits if profile is not None else None
        decimals = own_decimals if own_decimals is not None else (t.decimals if t else None)
        digits = own_digits if own_digits is not None else (t.digits if t else None)
        if decimals is None and digits is None:
            return None
        from_consumer = own_decimals is not None or (chosen is not None and decimals is not None)
        return MeterRule(decimals, digits, "consumer" if from_consumer else "registry", t.id if t else None,
                         tariff_indicator=bool(t and t.tariff_indicator))

    def _cost(self, usage):
        s = self.settings
        return round((usage.get("prompt_tokens", 0) * s.price_input_per_m
                      + usage.get("completion_tokens", 0) * s.price_output_per_m) / 1e6, 5)

    def read(self, image_bytes, consumer_id=None, captured_at=None, previous=None, deadline=None):
        """
        image_bytes: the photo (JPEG/PNG/WebP).
        consumer_id: enables the consumer's meter settings and stored previous reading (needs a store).
        captured_at: when the photo was taken; defaults to now. Naive datetimes are treated as UTC.
        previous: a checks.Previous to compare against; defaults to the store's last trusted reading.
        deadline: time.monotonic() value by which the model must have answered (None: no overall limit).
        Raises images.ImageError for a bad image and ModelError (defined in openai_client, raised by either model
        client) if the model call fails.
        """
        t0 = time.time()
        s = self.settings
        captured_at = _aware(captured_at) or datetime.now(timezone.utc)
        if previous is not None:
            previous = Previous(previous.value, checks.normalize_unit(previous.unit), _aware(previous.captured_at))
        data, mime = prepare_image(image_bytes, s.max_image_bytes, s.max_image_side, s.max_image_pixels,
                                   s.max_jpeg_pixels)
        profile = self.store.get_profile(consumer_id) if (self.store and consumer_id) else None

        with ThreadPoolExecutor(max_workers=s.reads) as ex:
            futures = [ex.submit(self.client.read, data, mime, deadline=deadline) for _ in range(s.reads)]
        responses, errors = [], []
        for f in futures:
            try:
                responses.append(f.result())
            except ModelError as e:
                errors.append(e)
        if errors:                 # what every call may have cost (the answered ones too), for the record and budget
            e = errors[0]
            e.cost_usd = round(sum(self._cost(x.usage) if x.usage else (s.timeout_cost_usd if x.maybe_billed else 0.0)
                                   for x in errors)
                               + sum(self._cost(r.get("usage") or {}) for r in responses), 5)
            raise e

        cost = round(sum(self._cost(r.get("usage") or {}) for r in responses), 5)
        try:
            return self._result(responses, profile, previous, consumer_id, captured_at, t0, image_bytes, cost)
        except ModelError:
            raise
        except Exception as e:  # noqa: BLE001 - an answer of an unexpected shape; it was paid for all the same
            err = ModelError("the model's answer had an unexpected shape", retryable=True,
                             usage={"prompt_tokens": 0, "completion_tokens": 0})
            err.cost_usd = cost
            raise err from e

    def _result(self, responses, profile, previous, consumer_id, captured_at, t0, image_bytes, cost):
        s = self.settings
        outcomes, reads, identified = [], [], None
        for r in responses:
            a = r["answer"]
            o = checks.evaluate_read(a, self._rule(profile, a), s.min_confidence)
            outcomes.append(o)
            identified = identified or self.registry.identify(a)
            reads.append({"status": a.get("status"), "display": a.get("display_text"), "unit": a.get("unit"),
                          "screenLabel": a.get("screen_label"),
                          "screenType": a.get("screen_type"), "confidence": a.get("confidence"),
                          "meterMake": a.get("meter_make"), "meterModelCode": a.get("meter_model_code"),
                          "bestGuess": a.get("best_guess"), "decimalPlacesGuess": a.get("decimal_places_guess"),
                          "check": "ok" if o.ok else o.reason, "reason": a.get("reason"), "seconds": r.get("seconds")})

        unit_hint = next((o.unit for o in outcomes if o.ok and o.unit), None)
        history_unavailable = False
        if previous is None and self.store and consumer_id:
            try:
                previous = self.store.last_trusted(consumer_id, unit=unit_hint, before=captured_at)
            except Exception:  # noqa: BLE001 - the paid reading is kept; it just cannot be cross-checked
                logger.warning("previous readings of %s could not be read", consumer_id, exc_info=True)
                history_unavailable = True
        decision = checks.decide(outcomes, billing_unit=profile.billing_unit if profile else None, previous=previous,
                                 captured_at=captured_at, max_daily_increase=s.max_daily_increase,
                                 clear_read_confidence=s.clear_read_confidence)
        if history_unavailable and decision.status == checks.ACCEPTED:
            decision.status, decision.reasons = checks.NEEDS_REVIEW, ["HISTORY_UNAVAILABLE"]

        first = responses[0]["answer"]
        hint = (next((REASONS[c][2] for c in decision.reasons if REASONS[c][2]), None)
                if decision.status == checks.RETAKE else None)
        type_id = decision.meter_type_id or (profile.meter_type_id if profile else None) or (identified.id if identified else None)
        # Every result carries a reading when a display was seen: the checked reading, else the model's best guess.
        if decision.reading is not None:
            cand = checks.Candidate(decision.reading, decision.decimal_source)
        else:
            cand = checks.candidate(first, self._rule(profile, first), previous)
        return ReadingResult(
            status=decision.status, reading=cand.reading, value=decision.value,
            unit=decision.unit or unit_hint or checks.normalize_unit(first.get("unit")),
            decimal_source=cand.decimal_source, uncertain_digits=cand.uncertain_digits,
            raw_answers=[{"answer": r["answer"], "usage": r.get("usage"), "model": r.get("model"),
                          "seconds": r.get("seconds")} for r in responses],
            reasons=[{"code": c, "message": REASONS[c][1]} for c in decision.reasons], retake_hint=hint,
            meter={"typeId": type_id, "make": first.get("meter_make"),
                   "modelCode": first.get("meter_model_code"), "faceplateText": first.get("faceplate_text") or []},
            previous={"value": str(previous.value), "unit": previous.unit,
                      "capturedAt": previous.captured_at.isoformat() if previous.captured_at else None} if previous else None,
            reads=reads, notes=decision.notes, model=responses[0].get("model", s.model), prompt_version=PROMPT_VERSION,
            processing_seconds=round(time.time() - t0, 2),
            cost_usd=cost,
            image_sha1=hashlib.sha1(image_bytes).hexdigest())
