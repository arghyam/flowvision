from datetime import datetime, timedelta, timezone

from conftest import answer
from service.vision import ACCEPTED, NEEDS_REVIEW, RETAKE, previous_from

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def stored(store, consumer, reading, when, status="SUCCESS", unit="kWh", cid="c1", accurate=None, actual=None):
    store.store_extraction({"request_id": cid, "correlation_id": cid, "consumer_id": consumer, "captured_at": when,
                            "meter_reading_status": status, "meter_reading": reading, "unit": unit})
    if accurate is not None:
        store.store_feedback(cid, accurate, actual, when)


def test_accepted_reading_and_cost(make_reader, jpeg_bytes):
    reader = make_reader(answer("00197127", model_code="PPSM 01"))
    r = reader.read(jpeg_bytes)
    assert r.status == ACCEPTED and r.reading == "0019712.7" and r.meter["typeId"] == "hpl_ppsm01"
    assert reader.client.calls == 1 and r.cost_usd > 0


def test_two_reads_must_agree(make_reader, jpeg_bytes):
    assert make_reader(answer("00197127"), answer("00197727"), reads=2).read(jpeg_bytes).status == NEEDS_REVIEW
    same = make_reader(answer("00197127"), reads=2)
    assert same.read(jpeg_bytes).status == ACCEPTED and same.client.calls == 2


def test_consumer_profile_sets_decimals_for_unknown_meter(make_reader, store, jpeg_bytes):
    reader = make_reader(answer("00018103", make=None, model_code=None), store=store)
    assert reader.read(jpeg_bytes, consumer_id="C1").status == NEEDS_REVIEW      # decimal position unknown
    store.set_profile("C1", meter_type_id="lt_er300", billing_unit="kWh")
    r = reader.read(jpeg_bytes, consumer_id="C1")
    assert r.status == ACCEPTED and r.reading == "00018103" and r.decimal_source == "consumer"


def test_billing_unit_mismatch_is_retake(make_reader, store, jpeg_bytes):
    store.set_profile("C1b", billing_unit="kVAh")
    r = make_reader(answer("00197127", unit="kWh"), store=store).read(jpeg_bytes, consumer_id="C1b")
    assert r.status == RETAKE and r.reason_codes == ["UNIT_MISMATCH"] and r.retake_hint


def test_stored_previous_reading_is_used(make_reader, store, jpeg_bytes):
    stored(store, "C2", "0019712.7", T0)
    r = make_reader(answer("00196127"), store=store).read(jpeg_bytes, consumer_id="C2", captured_at=T0 + timedelta(days=30))
    assert r.status == NEEDS_REVIEW and r.reason_codes == ["BELOW_PREVIOUS"] and r.previous["value"] == "19712.7"
    ok = make_reader(answer("00197927"), store=store).read(jpeg_bytes, consumer_id="C2", captured_at=T0 + timedelta(days=30))
    assert ok.status == ACCEPTED


def test_readings_marked_wrong_are_not_trusted_but_corrections_are(store):
    stored(store, "C3", "0029712.7", T0, cid="bad", accurate=False, actual="0019712.7")
    prev = store.last_trusted("C3", unit="kWh", before=T0 + timedelta(days=1))
    assert str(prev.value) == "19712.7"
    stored(store, "C4", "0029712.7", T0, cid="bad2", accurate=False)
    assert store.last_trusted("C4", before=T0 + timedelta(days=1)) is None
    stored(store, "C5", "0012345.6", T0, cid="unclear", status="UNCLEAR")
    assert store.last_trusted("C5", before=T0 + timedelta(days=1)) is None


def test_bad_stored_value_does_not_switch_off_previous_check(store):
    stored(store, "C6", "0019712.7", T0, cid="good")
    stored(store, "C6", "0019800.0", T0 + timedelta(days=5), cid="badfb", accurate=False, actual="19,800")
    prev = store.last_trusted("C6", unit="kWh", before=T0 + timedelta(days=30))
    assert prev is not None and str(prev.value) == "19712.7"


def test_reviewed_reading_confirmed_accurate_becomes_trusted(store):
    stored(store, "C7", "0001458.3", T0, cid="held", status="NEEDS_REVIEW")
    assert store.last_trusted("C7", before=T0 + timedelta(days=1)) is None
    store.store_feedback("held", True, None, T0)
    assert str(store.last_trusted("C7", before=T0 + timedelta(days=1)).value) == "1458.3"


def test_consumer_decimals_keep_the_type_digit_count(make_reader, store, jpeg_bytes):
    store.set_profile("C8", meter_type_id="hpl_ppsm01", decimals=1)
    r = make_reader(answer("008365.8", make=None, model_code=None), store=store).read(jpeg_bytes, consumer_id="C8")
    assert r.status == NEEDS_REVIEW and r.reason_codes == ["DIGIT_COUNT_MISMATCH"]


def test_first_reading_of_unknown_meter_is_held_then_cross_checked(make_reader, store, jpeg_bytes):
    assert make_reader(answer("0001458.3", make=None, model_code=None, conf=0.98)).read(jpeg_bytes).status == ACCEPTED
    reader = make_reader(answer("0001458.3", make=None, model_code=None, conf=0.92), store=store)
    first = reader.read(jpeg_bytes, consumer_id="C9", captured_at=T0)
    assert first.status == NEEDS_REVIEW and first.reason_codes == ["NO_CROSS_CHECK"] and first.reading == "0001458.3"
    stored(store, "C9", "0001450.0", T0, cid="c9prev")
    later = reader.read(jpeg_bytes, consumer_id="C9", captured_at=T0 + timedelta(days=20))
    assert later.status == ACCEPTED and later.reading == "0001458.3"


def test_explicit_previous_and_retake_hint(make_reader, jpeg_bytes):
    prev = previous_from("19000", "kWh", datetime(2026, 9, 1))
    r = make_reader(answer("0019712.7", unit=None)).read(jpeg_bytes, previous=prev)
    assert r.status == RETAKE and r.reason_codes == ["UNIT_NOT_VISIBLE"] and r.retake_hint
    assert r.previous["value"] == "19000"
    # a hard-to-read photo goes to review: no retake hint; the model's reading and the previous reading are shown
    u = make_reader(answer("0019?12.7", status="UNREADABLE", best_guess="0019712.7")).read(jpeg_bytes, previous=prev)
    assert u.status == NEEDS_REVIEW and u.reason_codes == ["UNREADABLE"] and u.retake_hint is None
    assert u.reading == "0019712.7" and u.uncertain_digits == [5] and u.previous["value"] == "19000"



def test_every_result_with_a_display_carries_a_reading(make_reader, store, jpeg_bytes):
    # unknown meter, no point visible: held for review, with the decimal point from the previous reading
    stored(store, "C10", "0010414.0", T0, cid="c10prev")
    r = make_reader(answer("00104685", make=None, model_code=None, decimal_guess=1), store=store).read(
        jpeg_bytes, consumer_id="C10", captured_at=T0 + timedelta(days=5))
    assert r.status == NEEDS_REVIEW and r.reason_codes == ["DECIMAL_UNCERTAIN"]
    assert (r.reading, r.decimal_source, r.unit) == ("0010468.5", "previous_reading", "kWh")
    # wrong screen: the number the model saw is still returned, flagged as a retake
    w = make_reader(answer("0001458.3", status="NOT_CUMULATIVE_SCREEN", screen="other_register", unit="kVAh",
                           make=None, model_code=None)).read(jpeg_bytes)
    assert w.status == RETAKE and w.reason_codes == ["WRONG_SCREEN"] and w.reading == "0001458.3"
