from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import answer
from service.vision.checks import (ACCEPTED, NEEDS_REVIEW, RETAKE, MeterRule, Previous, candidate, decide,
                                   evaluate_read, normalize_unit, parse_reading)
from service.vision.meter_types import MeterTypeRegistry

REG = MeterTypeRegistry()


def rule_for(a):
    t = REG.identify(a)
    return MeterRule(t.decimals, t.digits, "registry", t.id) if t else None


def check(a, min_conf=0.9):
    return evaluate_read(a, rule_for(a), min_conf)


def test_hpl_dropped_decimal_point_is_restored_by_meter_rule():
    # trial failure: display 0004457.5 read as 00044575 -> the HPL rule puts the point back
    o = check(answer("00551215", make="HPL", model_code="PPSM 01"))
    assert o.ok and o.reading == "0055121.5" and o.meter_type_id == "hpl_ppsm01" and o.decimal_source == "registry"


def test_er300_invented_decimal_point_is_removed():
    # trial failure: ER300 shows 00018103, v1 read 0001810.3
    o = check(answer("0001810.3", make="L&T", model_code="ER300P"))
    assert o.ok and o.reading == "00018103" and o.value == Decimal("18103")
    assert any("rule of 0 applied" in n for n in o.notes)


def test_er300_matched_from_faceplate_code():
    o = check(answer("00007696", make="Schneider", model_code=None, faceplate=["WR300BC53ASSAM1"]))
    assert o.ok and o.reading == "00007696"


def test_genus_saksham_type():
    # the smoke run's one held reading: the model read 0032740.9 on a Genus SAKSHAM-345
    o = check(answer("0032740.9", make="Genus", model_code="SAKSHAM-345"))
    assert o.ok and o.meter_type_id == "genus_saksham345" and o.digits_fixed
    assert check(answer("003274.9", make="Genus", model_code="SAKSHAM-345")).reason == "DIGIT_COUNT_MISMATCH"


def test_brand_alone_does_not_decide_decimals():
    # L&T also makes the EM101+ with a decimal digit: brand without a model code stays unknown
    o = check(answer("23447", make="L&T", model_code=None))
    assert not o.ok and o.reason == "DECIMAL_UNCERTAIN"


def test_hpl_dropped_digit_caught_by_digit_count():
    # trial failure: 0083655.8 read as 008365.8
    o = check(answer("008365.8", model_code="PPSM 01"))
    assert not o.ok and o.reason == "DIGIT_COUNT_MISMATCH" and o.outcome == NEEDS_REVIEW


def test_unknown_meter_with_visible_point_is_accepted():
    o = check(answer("0001458.3", make="Adani", model_code=None))
    assert o.ok and o.reading == "0001458.3" and o.decimal_source == "visible_point"


def test_unknown_meter_without_point_needs_review():
    o = check(answer("00044575", make=None, model_code=None))
    assert not o.ok and o.reason == "DECIMAL_UNCERTAIN"


def test_photo_problems_are_retakes():
    assert check(answer(None, status="NO_DISPLAY", screen="not_visible")).reason == "NO_DISPLAY"
    assert check(answer("23.4", status="NOT_CUMULATIVE_SCREEN", screen="max_demand")).reason == "WRONG_SCREEN"
    assert check(answer("0019712.7", screen="max_demand")).reason == "WRONG_SCREEN"   # READ but wrong screen
    assert check(answer("0019?12.7", status="UNREADABLE")).reason == "UNREADABLE"
    assert check(answer("0019712.7", illegible={3})).reason == "DIGITS_UNCLEAR"
    assert check(answer("0019712.7", count_certain=False)).reason == "DIGITS_UNCLEAR"
    o = check(answer("0019712.7", conf=0.8))
    assert o.reason == "LOW_CONFIDENCE" and o.outcome == RETAKE


def test_inconsistent_answer_needs_review():
    a = answer("0019712.7")
    a["display_text"] = "0019772.7"
    assert check(a).reason == "BAD_OUTPUT"


def test_unit_must_be_a_visible_energy_unit():
    # these meters show both kWh and kVAh totals: without a unit the reading is ambiguous
    o = check(answer("0019712.7", unit=None))
    assert o.reason == "UNIT_NOT_VISIBLE" and o.outcome == RETAKE
    assert check(answer("0019712.7", unit="kW")).reason == "WRONG_SCREEN"       # max demand
    assert check(answer("0019712.7", unit="kVArh")).reason == "WRONG_SCREEN"    # reactive energy
    assert check(answer("0019712.7", unit="CUM KWH")).unit == "kWh"
    assert normalize_unit("kVA h") == "kVAh" and normalize_unit("kWh/kVAh") == "kWh/kVAh"


def test_label_of_a_non_total_screen_overrides_the_model():
    # the model said READ on the cumulative screen, but the LCD label it copied says otherwise
    for label in ("T2 kVAh", "t1 kVAh", "01 Bill", "BILL 02 kWh", "TOD 3", "Zone 2 kWh", "1.8.1", "MD kW",
                  "LTR INR", "Rs", "BAL", "Hist 1", "PREV kWh"):
        a = answer("0019712.7")
        a["screen_label"] = label
        o = check(a)
        assert o.reason == "WRONG_SCREEN" and o.outcome == RETAKE, label
    for label in ("CUM kWh", "kWh", "kVAh", "Cum. kVAh", "1.8.0", "TOTAL kWh", "IMPORT kWh", None, ""):
        a = answer("0019712.7")
        a["screen_label"] = label
        assert check(a).ok, label


def test_parse_reading():
    assert parse_reading(" 0019712.7 ") == Decimal("19712.7") and parse_reading(19712) == Decimal("19712")
    for bad in ("19,800", "abc", "-5", "NaN", "Infinity", "", None, True, "1e5"):
        with pytest.raises(ValueError):
            parse_reading(bad)


def test_clear_read_is_accepted_without_cross_check():
    # unknown meter, no history: a clear read (all checks pass, point seen) at >= 0.95 confidence is accepted
    d = decide([check(answer("0010468.5", make="Adani", model_code=None, conf=0.98))])
    assert d.status == ACCEPTED and d.reading == "0010468.5" and any("clear read" in n for n in d.notes)
    assert decide([check(answer("2344.7", make="L&T", model_code="EM101+", conf=0.95))]).status == ACCEPTED


def test_reading_without_cross_check_needs_review():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    unknown = check(answer("0001458.3", make="Adani", model_code=None, conf=0.92))   # passes, but not a clear read
    d = decide([unknown])
    assert d.status == NEEDS_REVIEW and d.reasons == ["NO_CROSS_CHECK"] and d.reading == "0001458.3"
    prev = Previous(Decimal("1400"), "kWh", now - timedelta(days=30))
    assert decide([unknown], previous=prev, captured_at=now).status == ACCEPTED
    em101 = check(answer("2344.7", make="L&T", model_code="EM101+", conf=0.92))   # known type, digit count varies
    assert decide([em101]).reasons == ["NO_CROSS_CHECK"]
    assert decide([em101], clear_read_confidence=0.9).status == ACCEPTED
    # a previous reading in another unit cannot cross-check
    assert decide([unknown], previous=Previous(Decimal("1400"), "kVAh", now), captured_at=now).reasons == ["NO_CROSS_CHECK"]
    # a dropped digit on an unknown meter is caught by the previous reading, even on a confident read
    dropped = check(answer("000145.3", make=None, model_code=None, conf=0.99))
    assert decide([dropped], previous=prev, captured_at=now).reasons == ["BELOW_PREVIOUS"]


def test_candidate_decimal_sources():
    prev = Previous(Decimal("10414.0"), "kWh")
    unknown = dict(make=None, model_code=None)
    # meter rule first: an HPL display read without its point gets it back
    c = candidate(answer("00104685", status="UNREADABLE"), MeterRule(1, 8, "registry", "hpl_ppsm01"))
    assert (c.reading, c.decimal_source) == ("0010468.5", "registry")
    # a clearly visible point
    assert candidate(answer("0010468.5", **unknown)).decimal_source == "visible_point"
    # no point seen: the consumer's previous reading, then the model's own guess
    a = answer("00104685", status="UNREADABLE", decimal_guess=1, **unknown)
    assert (candidate(a, None, prev).reading, candidate(a, None, prev).decimal_source) == ("0010468.5", "previous_reading")
    assert (candidate(a).reading, candidate(a).decimal_source) == ("0010468.5", "model_guess")
    assert candidate(answer("00018103", status="UNREADABLE", decimal_guess=0, **unknown)).reading == "00018103"
    # no idea: the digits as shown, without a point
    assert (candidate(answer("00104685", **unknown)).reading, candidate(answer("00104685", **unknown)).decimal_source) \
        == ("00104685", None)
    # "?" digits are filled from best_guess and reported as unsure
    c = candidate(answer("00123?12", status="UNREADABLE", best_guess="00123512", decimal_guess=1, **unknown))
    assert c.reading == "0012351.2" and c.uncertain_digits == [6]
    assert candidate(answer(None, status="NO_DISPLAY", screen="not_visible")).reading is None


def ok(reading, unit="kWh"):
    return evaluate_read(answer(reading, unit=unit), MeterRule(1, 8, "registry", "hpl_ppsm01"), 0.9)


def test_decide_requires_reads_to_agree():
    assert decide([ok("0019712.7"), ok("0019712.7")]).status == ACCEPTED
    d = decide([ok("0019712.7"), ok("0019772.7")])
    assert d.status == NEEDS_REVIEW and d.reasons == ["READS_DISAGREE"]


def test_decide_one_unclear_read_means_retake():
    unclear = check(answer("0019712.7", illegible={2}))
    assert decide([ok("0019712.7"), unclear]).status == RETAKE


def test_decide_previous_reading_checks():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    prev = Previous(Decimal("19700.0"), "kWh", now - timedelta(days=10))
    assert decide([ok("0019712.7")], previous=prev, captured_at=now).status == ACCEPTED
    below = decide([ok("0019612.7")], previous=prev, captured_at=now)
    assert below.status == NEEDS_REVIEW and below.reasons == ["BELOW_PREVIOUS"]
    jump = decide([ok("0029712.7")], previous=prev, captured_at=now, max_daily_increase=300)
    assert jump.reasons == ["JUMP_TOO_LARGE"]
    other_unit = Previous(Decimal("99999"), "kVAh", now - timedelta(days=1))
    assert decide([ok("0019712.7")], previous=other_unit, captured_at=now).status == ACCEPTED


def test_decide_billing_unit_mismatch_is_retake():
    d = decide([ok("0019712.7", unit="kWh")], billing_unit="kVAh")
    assert d.status == RETAKE and d.reasons == ["UNIT_MISMATCH"]
