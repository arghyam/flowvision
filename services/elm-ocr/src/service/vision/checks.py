"""
Safety checks. A reading is ACCEPTED only when every check passes; anything doubtful becomes RETAKE
(the photo shows no display, the wrong screen or the wrong unit: ask for a new one) or NEEDS_REVIEW
(the photo is hard to read, e.g. glare or an unclear digit, or the result failed a consistency check:
a person checks the reading against the photo).
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

ACCEPTED, RETAKE, NEEDS_REVIEW = "ACCEPTED", "RETAKE", "NEEDS_REVIEW"

# code -> (outcome, message for staff, hint for the consumer or None)
REASONS = {
    "NO_DISPLAY": (RETAKE, "No meter display is visible, or it is blank.",
                   "Take the photo straight at the meter's display, with the display switched on."),
    "WRONG_SCREEN": (RETAKE, "The display is showing another screen, not the cumulative energy reading.",
                     "Wait until the display shows the total kWh (or kVAh) reading, then take the photo."),
    # Hard-to-read photos (glare, blur, an unclear digit) go to a person, who checks the model's reading against the
    # photo and confirms or corrects it through /feedback, rather than back to the consumer for a new photo.
    "UNREADABLE": (NEEDS_REVIEW, "The reading could not be read with certainty.", None),
    "DIGITS_UNCLEAR": (NEEDS_REVIEW, "One or more digits are unclear, or a digit may be missing.", None),
    "LOW_CONFIDENCE": (NEEDS_REVIEW, "The model was not confident enough in the reading.", None),
    "UNIT_MISMATCH": (RETAKE, "The display shows a different unit from the one billed for this meter.",
                      "Wait until the display shows the billed unit (kWh or kVAh), then take the photo."),
    "UNIT_NOT_VISIBLE": (RETAKE, "No kWh or kVAh label was seen, so it is not certain which energy total is shown "
                                 "(these meters show both).",
                         "Take the photo so the whole display is visible, including the kWh or kVAh label next to the reading."),
    "BAD_OUTPUT": (NEEDS_REVIEW, "The model's answer was internally inconsistent.", None),
    "DIGIT_COUNT_MISMATCH": (NEEDS_REVIEW, "The number of digits does not match this meter type; a digit may have been dropped.", None),
    "DECIMAL_UNCERTAIN": (NEEDS_REVIEW, "Meter type unknown and no decimal point was seen, so the decimal position is uncertain. "
                                        "Set this consumer's meter type to accept readings automatically.", None),
    "HISTORY_UNAVAILABLE": (NEEDS_REVIEW, "The consumer's previous readings could not be checked (the database did "
                                          "not answer), so the reading was not compared with them.", None),
    "READS_DISAGREE": (NEEDS_REVIEW, "Independent reads of the same photo gave different results.", None),
    "BELOW_PREVIOUS": (NEEDS_REVIEW, "The reading is lower than the previous accepted reading.", None),
    "JUMP_TOO_LARGE": (NEEDS_REVIEW, "The increase since the previous reading is implausibly large.", None),
    "NO_CROSS_CHECK": (NEEDS_REVIEW, "The model was not confident enough to accept this reading on its own, and this "
                                     "meter has no known digit count or previous reading to cross-check it. Set the "
                                     "consumer's meter type, or confirm this reading through feedback.", None),
    "CORRECTED_BEFORE": (NEEDS_REVIEW, "A person marked the reading of this same photo wrong or corrected it, so it is "
                                       "not read or accepted again; the earlier model reading is shown.", None),
}

# Cumulative energy units that can be billed. kVArh (reactive energy) is a cumulative register too, but not the target.
ENERGY_UNITS = ("kWh", "kVAh")
_UNIT_TOKENS = (("kvarh", "kVArh"), ("kvah", "kVAh"), ("kwh", "kWh"))
_READING_RE = re.compile(r"^\d+(\.\d+)?$")
# LCD labels that mark a screen other than the grand total, whatever the model concluded about the screen:
# tariff/time-of-day zones (T1, T01, T.1, kWhT1, TOD, TOU, TARIFF, RATE 1, ZONE, Z1, OBIS 1.8.1...), billing history
# (01 BILL, 01 B, a bare 01 bill index, HIST, PREV, LAST), maximum demand (MD, MDkW, MAX DEMAND) and prepaid
# balance/money (INR, Rs, ₹, BAL, CREDIT, LTR), a history register (H, H1, H-1, H01... up to H12) and a billing
# register (B1, B-1, B01... up to B12). The h of the unit itself (kWh, kW h, kW·h, kVA h) is not an H label, and a B
# without a number is not a B label: it is the phase indicator (R Y B) shown on every screen, alone when a phase is out.
_SEP = r"\s*[-_.:\u2013\u2014]?\s*"            # T1, T 1, T-1, T.1, T_1, T:1, T – 1
NOT_TOTAL_LABEL = re.compile(
    r"(?:(?<![A-Z0-9])|(?<=[WA]H))T" + _SEP + r"0?[1-8](?![0-9])|\bTOD(?![A-Z])|\bTOU(?![A-Z])|\bTZ(?![A-Z])"
    r"|\bTARIFF|\bRATE" + _SEP + r"0?[1-8](?![0-9])|\bZONE|\bZ" + _SEP + r"0?[1-8](?![0-9])|\b1\.8\.[1-9]\b"
    r"|BILL|\bHIST|\bPREV|\bLAST\b|(?<![0-9.])0[1-9](?![0-9.])"
    r"|\bM\.?\s?D(?=I\b|K|[0-9]|\b)|\bDEMAND|\bINR\b|\bRS\b|₹|\bBAL|\bCREDIT|\bLTR\b"
    r"|\bT" + _SEP + r"(?:I{1,3}|IV|VI{0,3})\b|\bTR" + _SEP + r"0?[1-8](?![0-9])|\b(?:OFF" + _SEP + r")?PEAK\b"
    r"|\b2\.8\.[0-9]\b"
    r"|(?<![A-Z0-9])(?<![WA]\s)(?<![WA]\s\s)(?<![WA][\u00b7.\-_])H(?:" + _SEP + r"0?(?:1[0-2]|[1-9]))?(?![A-Z0-9])"
    r"|(?<![A-Z0-9])B" + _SEP + r"0?(?:1[0-2]|[1-9])(?![A-Z0-9])",
    re.IGNORECASE)
# Screens whose number is not an energy register: their reading keeps the point shown, not the kWh format.
NOT_ENERGY_SCREENS = ("max_demand", "voltage_current_pf", "date_time", "meter_id", "test_or_segment_check")
NOT_ENERGY_UNIT = re.compile(r"^(m?V|kV|m?A|Hz|k?W|k?VA|k?VAr|PF|%|INR|Rs\.?|₹)$", re.IGNORECASE)


def normalize_unit(unit):
    """'KWH', 'kWh ', 'CUM kWh' -> 'kWh'. A text naming no unit, or more than one, is returned unchanged."""
    if not unit:
        return None
    key = re.sub(r"[\s·.\-_]", "", str(unit)).lower()           # "kW h", "kW·h", "kW.h" -> "kwh"
    found = [name for token, name in _UNIT_TOKENS if token in key]
    return found[0] if len(found) == 1 else (str(unit).strip() or None)


def parse_reading(value) -> Decimal:
    """A meter reading as a Decimal: digits with an optional decimal point ('0019712.7', 19712.7). Raises ValueError."""
    text = "" if value is None or isinstance(value, bool) else str(value).strip()
    if not _READING_RE.match(text):
        raise ValueError(f"not a meter reading: {value!r}")
    return Decimal(text)


@dataclass
class MeterRule:
    """How the cumulative reading is formatted on a given meter. Either part may be unknown (None)."""
    decimals: int | None   # digits after the decimal point; None: take a clearly visible point
    digits: int | None     # total digits, leading zeros included; None: not fixed (no dropped-digit check)
    source: str            # where `decimals` comes from: "consumer" | "registry"
    type_id: str | None = None


@dataclass
class ReadOutcome:
    ok: bool
    reason: str | None = None           # REASONS code when not ok
    reading: str | None = None          # formatted, e.g. "0019712.7"
    value: Decimal | None = None
    unit: str | None = None
    meter_type_id: str | None = None
    decimal_source: str | None = None   # consumer | registry | visible_point
    digits_fixed: bool = False          # the digit count was checked against a known meter format
    confidence: float = 0.0
    notes: list = field(default_factory=list)

    @property
    def outcome(self):
        return ACCEPTED if self.ok else REASONS[self.reason][0]


def _fail(reason, **kw):
    return ReadOutcome(ok=False, reason=reason, **kw)


def evaluate_read(answer, rule, min_confidence):
    """Check one model answer. `rule` is a MeterRule for this meter, or None if the type is unknown."""
    status = answer.get("status")
    unit = normalize_unit(answer.get("unit"))
    if status == "NO_DISPLAY" or answer.get("screen_type") in ("blank_or_off", "not_visible"):
        return _fail("NO_DISPLAY", unit=unit)
    if status == "NOT_CUMULATIVE_SCREEN" or answer.get("screen_type") != "cumulative_energy":
        return _fail("WRONG_SCREEN", unit=unit)
    label = str(answer.get("screen_label") or "")
    if NOT_TOTAL_LABEL.search(label):                   # a second check on the model's own screen call
        return _fail("WRONG_SCREEN", unit=unit, notes=[f"LCD label {label!r} marks a tariff, bill-history, "
                                                       "demand or balance screen, not the total"])
    if status != "READ":
        if unit is not None and (unit == "kVArh" or NOT_ENERGY_UNIT.match(unit)):
            # an unreadable kW / V / kVArh... screen is still the wrong screen: a retake, not a review
            return _fail("WRONG_SCREEN", unit=unit, notes=[f"unit {unit} is not kWh or kVAh"])
        return _fail("UNREADABLE", unit=unit)
    if unit is not None and unit not in ENERGY_UNITS:     # kW / kVA (max demand), V, kVArh...: not the billed total
        return _fail("WRONG_SCREEN", unit=unit, notes=[f"unit {unit} is not kWh or kVAh"])

    chars = [str(d.get("char", "")).strip() for d in answer.get("digits") or []]
    digit_chars = [c for c in chars if c != "."]
    if not digit_chars:
        return _fail("UNREADABLE", unit=unit)
    if (not answer.get("digit_count_certain") or any(not d.get("legible") for d in answer["digits"])
            or any(len(c) != 1 or not c.isdigit() for c in digit_chars)):
        return _fail("DIGITS_UNCLEAR", unit=unit)
    digits = "".join(digit_chars)
    shown = "".join(ch for ch in (answer.get("display_text") or "") if ch.isdigit())
    if shown != digits:
        return _fail("BAD_OUTPUT", unit=unit, notes=["digit list and display_text disagree"])
    if float(answer.get("confidence") or 0) < min_confidence:
        return _fail("LOW_CONFIDENCE", unit=unit)
    if unit is None:
        return _fail("UNIT_NOT_VISIBLE")

    dots = [i for i, c in enumerate(chars) if c == "."]
    seen_decimals = None
    if len(dots) == 1 and dots[0] < len(chars) - 1 and answer.get("decimal_point_visible"):
        seen_decimals = len(chars) - 1 - dots[0]
    elif len(dots) > 1:
        return _fail("BAD_OUTPUT", unit=unit, notes=["more than one decimal point"])

    notes = []
    if rule is not None and rule.digits is not None and len(digits) != rule.digits:
        return _fail("DIGIT_COUNT_MISMATCH", unit=unit, meter_type_id=rule.type_id,
                     notes=[f"read {len(digits)} digits, this meter shows {rule.digits}"])
    if rule is not None and rule.decimals is not None:
        decimals, source = rule.decimals, rule.source
        if seen_decimals is not None and seen_decimals != decimals:
            notes.append(f"model saw {seen_decimals} decimal digit(s); meter rule of {decimals} applied")
    elif seen_decimals is not None:
        decimals, source = seen_decimals, "visible_point"
    else:
        return _fail("DECIMAL_UNCERTAIN", unit=unit)

    if decimals >= len(digits):
        return _fail("DIGIT_COUNT_MISMATCH", unit=unit, notes=["fewer digits than the decimal rule needs"])
    whole, frac = (digits[:-decimals], digits[-decimals:]) if decimals else (digits, "")
    reading = f"{whole}.{frac}" if decimals else whole
    return ReadOutcome(ok=True, reading=reading, value=Decimal(reading), unit=unit,
                       meter_type_id=rule.type_id if rule else None, decimal_source=source,
                       # cross-checked only when both the digit count and the decimal position come from a rule:
                       # a digit count alone cannot catch a moved or invented decimal point
                       digits_fixed=rule is not None and rule.digits is not None and rule.decimals is not None,
                       confidence=float(answer.get("confidence") or 0), notes=notes)


@dataclass
class Previous:
    value: Decimal
    unit: str | None = None
    captured_at: datetime | None = None


@dataclass
class Candidate:
    """The model's best guess at the reading, for retakes and reviews. Never bill on it."""
    reading: str | None = None                          # e.g. "0010468.5"; may contain "?" if the model left digits blank
    decimal_source: str | None = None                   # consumer | registry | visible_point | previous_reading | model_guess
    uncertain_digits: list = field(default_factory=list)  # 1-based positions among the digits (leading zeros included)


def candidate(answer, rule=None, previous=None):
    """
    The best available reading from one model answer, even when it failed the checks: the model's best_guess (else
    display_text), with the decimal point from the most trustworthy source available: the consumer's or meter
    type's rule, a clearly visible point, the consumer's previous reading, then the model's own guess.
    """
    text = answer.get("best_guess") or answer.get("display_text")
    chars = [c for c in str(text or "") if c.isdigit() or c in "?."]
    digits = [c for c in chars if c != "."]
    if not digits:
        return Candidate()

    shown = [c for c in str(answer.get("display_text") or "") if c.isdigit() or c == "?"]
    entries = [d for d in answer.get("digits") or [] if str(d.get("char", "")).strip() != "."]
    uncertain = ({i + 1 for i, c in enumerate(digits) if c == "?"} | {i + 1 for i, c in enumerate(shown) if c == "?"}
                 | {i + 1 for i, d in enumerate(entries) if not d.get("legible")})

    dots = [i for i, c in enumerate(chars) if c == "."]
    seen = len(chars) - 1 - dots[0] if len(dots) == 1 and dots[0] < len(chars) - 1 else None
    guess = answer.get("decimal_places_guess")
    prev_places = -previous.value.as_tuple().exponent if previous is not None else 0
    unit = str(answer.get("unit") or "").strip()
    if answer.get("screen_type") in NOT_ENERGY_SCREENS or NOT_ENERGY_UNIT.match(unit):
        # volts, Hz, max demand, dates...: neither the meter's kWh format nor the previous kWh reading applies
        rule, prev_places = None, 0
    if rule is not None and rule.decimals is not None:
        decimals, source = rule.decimals, rule.source
    elif seen is not None and answer.get("decimal_point_visible"):
        decimals, source = seen, "visible_point"
    elif prev_places > 0:
        decimals, source = prev_places, "previous_reading"
    elif isinstance(guess, int) and not isinstance(guess, bool) and 0 <= guess <= 3:
        decimals, source = guess, "model_guess"
    elif seen is not None:
        decimals, source = seen, "model_guess"
    else:
        decimals, source = 0, None                      # no idea where a point would go: the digits as shown

    raw = "".join(digits)
    reading = f"{raw[:-decimals]}.{raw[-decimals:]}" if 0 < decimals < len(raw) else raw
    return Candidate(reading, source, sorted(uncertain))


@dataclass
class Decision:
    status: str
    reasons: list                       # REASONS codes, most important first
    reading: str | None = None
    value: Decimal | None = None
    unit: str | None = None
    meter_type_id: str | None = None
    decimal_source: str | None = None
    notes: list = field(default_factory=list)


def decide(outcomes, billing_unit=None, previous=None, captured_at=None, max_daily_increase=300.0,
           clear_read_confidence=0.95):
    """Combine the per-read outcomes and apply the unit and previous-reading checks."""
    oks = [o for o in outcomes if o.ok]
    reviews = [o for o in outcomes if not o.ok and o.outcome == NEEDS_REVIEW]
    if reviews:
        return Decision(NEEDS_REVIEW, list(dict.fromkeys(o.reason for o in reviews)),
                        notes=[n for o in reviews for n in o.notes])
    if len(oks) < len(outcomes):
        retakes = [o.reason for o in outcomes if not o.ok]
        return Decision(RETAKE, list(dict.fromkeys(retakes)))
    if len({(o.reading, o.unit) for o in oks}) > 1:
        return Decision(NEEDS_REVIEW, ["READS_DISAGREE"],
                        notes=[f"read {i + 1}: {o.reading} {o.unit or ''}".strip() for i, o in enumerate(oks)])

    best = oks[0]
    d = Decision(ACCEPTED, [], reading=best.reading, value=best.value, unit=best.unit,
                 meter_type_id=best.meter_type_id, decimal_source=best.decimal_source,
                 notes=list(dict.fromkeys(n for o in oks for n in o.notes)))
    if billing_unit and normalize_unit(billing_unit) != best.unit:
        d.status, d.reasons = RETAKE, ["UNIT_MISMATCH"]
        return d
    comparable = previous is not None and (previous.unit is None or normalize_unit(previous.unit) == best.unit)
    if comparable:
        if best.value < previous.value:
            d.status, d.reasons = NEEDS_REVIEW, ["BELOW_PREVIOUS"]
            d.notes.append(f"previous accepted reading was {previous.value}")
            return d
        days = 1.0
        if previous.captured_at and captured_at:
            days = max(1.0, (captured_at - previous.captured_at).total_seconds() / 86400)
        if best.value - previous.value > Decimal(str(max_daily_increase * days)):
            d.status, d.reasons = NEEDS_REVIEW, ["JUMP_TOO_LARGE"]
            d.notes.append(f"+{best.value - previous.value} in {days:.1f} day(s); limit {max_daily_increase:g}/day")
            return d
    # A dropped digit or shifted decimal point is caught either by the meter's known digit count or by comparing
    # with a previous reading. With neither, a clear read is still accepted: every digit legible, digit count certain,
    # unit and decimal point seen, and confidence >= clear_read_confidence (trial replay: 10/10 Sol v2 and 14/14
    # Astra v2 such reads were right; 18/18 even with the looser v3 prompt at >= 0.95). Anything less, a person confirms.
    if not best.digits_fixed and not comparable:
        if min(o.confidence for o in oks) >= clear_read_confidence:
            d.notes.append("accepted as a clear read: no meter digit count or previous reading to cross-check")
        else:
            d.status, d.reasons = NEEDS_REVIEW, ["NO_CROSS_CHECK"]
    return d
