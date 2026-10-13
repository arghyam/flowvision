"""
Storage for requests, results, feedback and per-consumer meter settings (FlowVision's MetadataStore pattern).

Every request gets one row, keyed by its request id:
- claim() inserts it as PROCESSING before any paid work, which makes the id an idempotency key: a request sent again
  with the same id gets the stored result (or "still in progress") instead of a second model call;
- store_extraction() completes it before the response is sent.

The previous-reading check only trusts readings that were accepted (SUCCESS, not marked wrong) or confirmed by a
person: an `actual` value, or `accurate: true` on a SUCCESS or NEEDS_REVIEW reading.

Two steps before a paid model call run under a lock that every worker and instance shares (a PostgreSQL advisory
lock; a process lock on SQLite), so concurrent requests cannot both pass them:
- claim_photo(): the same photo of the same consumer is answered from an earlier result, or refused while it is
  being read under another id, instead of being paid for twice;
- reserve(): the daily budget counts every reading in flight: each one reserves its estimated cost on its own row
  before the call, replaced by the real cost afterwards (and kept if the worker dies).
"""
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import sqlalchemy as db

from conf.config import Config
from service.api.database import DatabaseService, extraction_data, meter_profiles
from service.vision.checks import Previous, parse_reading

PROCESSING, ERROR = "PROCESSING", "ERROR"
FINAL = ("SUCCESS", "UNCLEAR", "NOMETER", "NEEDS_REVIEW")
_BUDGET_LOCK = 72_531_002           # PostgreSQL advisory lock ids (the migration uses 72_531_001)


def _copy_of_earlier(t):
    """True for a row that repeats an earlier request's answer: reused results share the original's correlation id
    (also those written by older versions, which carry no duplicate_of mark). A real re-read has its own."""
    earlier = extraction_data.alias("copied")
    return db.exists().where(earlier.c.correlation_id == t.c.correlation_id, earlier.c.correlation_id.is_not(None),
                             earlier.c.request_timestamp < t.c.request_timestamp)


def _is_correction(row) -> bool:
    """Feedback that says the reading was wrong: accurate=false, or an `actual` that differs from the reading.
    An `actual` equal to the returned reading is a confirmation."""
    if row.get("extracted_reading_accurate") is False:
        return True
    actual = row.get("actual_reading")
    if actual is None:
        return False
    try:
        return parse_reading(actual) != parse_reading(row.get("meter_reading"))
    except ValueError:
        return True


def _photo_lock(consumer_id, image_sha1) -> int:
    """A PostgreSQL advisory lock id (signed 64-bit) for one consumer's photo."""
    digest = hashlib.sha1(f"{consumer_id or ''}|{image_sha1}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MetadataStore:
    def __init__(self, config: Config, database_service: DatabaseService):
        self.config = config
        self.database_service = database_service
        self.base_logger = logging.getLogger(config.find("logs.api_logger.name"))
        # SQLite has no time zones: store UTC without an offset there, timezone-aware UTC elsewhere.
        self._naive = database_service.engine.dialect.name == "sqlite"
        self._postgres = database_service.engine.dialect.name == "postgresql"
        self._budget_lock = threading.Lock()      # within one process; PostgreSQL locks across processes too
        self._photo_lock = threading.Lock()

    def ts(self, dt):
        if dt is None:
            return None
        dt = _aware(dt).astimezone(timezone.utc)
        return dt.replace(tzinfo=None) if self._naive else dt

    def _row(self, record: dict) -> dict:
        return {k: self.ts(v) if isinstance(v, datetime) else v for k, v in record.items()}

    # ---- requests ----
    def claim(self, record: dict, stale_after: float):
        """Starts a request. Returns (state, row):
        'new'         no request with this id yet; a PROCESSING row now holds the id
        'retry'       the earlier attempt failed (ERROR) or was abandoned (PROCESSING for longer than stale_after
                      seconds, e.g. a worker restart): this request takes it over
        'in_progress' another request with this id is being processed right now
        'replay'      the id already has a result: row holds it
        Raises sqlalchemy errors if the database is unavailable."""
        t = extraction_data
        record = self._row({**record, "meter_reading_status": PROCESSING})
        try:
            with self.database_service.engine.begin() as conn:
                conn.execute(db.insert(t).values(**record))
            return "new", None
        except db.exc.IntegrityError:
            pass
        row = self.database_service.fetch_one(db.select(t).where(t.c.request_id == record["request_id"]))
        if row is None:                                   # deleted in between: try once more
            return self.claim(record, stale_after)
        status = row["meter_reading_status"]
        started = _aware(row["request_timestamp"])
        stale = started is None or started < datetime.now(timezone.utc) - timedelta(seconds=stale_after)
        if status in FINAL:
            return "replay", row
        if status == PROCESSING and not stale:
            return "in_progress", row
        if row.get("consumer_id") is not None and row.get("consumer_id") != record.get("consumer_id"):
            return "conflict", row                        # another consumer's failed request: never taken over
        # ERROR, or an abandoned PROCESSING row: take it over, unless another request just did. What the earlier
        # attempt cost is kept (a failed call can still be billed), so the daily budget never loses it.
        values = self._blank(record) | {"cost_usd": row.get("cost_usd")}
        with self.database_service.engine.begin() as conn:
            taken = conn.execute(db.update(t).where(
                t.c.request_id == record["request_id"], t.c.meter_reading_status == status,
                t.c.request_timestamp == row["request_timestamp"]).values(**values)).rowcount
        return ("retry", row) if taken else ("in_progress", row)

    @staticmethod
    def _blank(record: dict) -> dict:
        return {c.name: None for c in extraction_data.c if c.name != "request_id"} | \
            {k: v for k, v in record.items() if k != "request_id"}

    def store_extraction(self, record: dict) -> bool:
        """Completes this request's claimed row. The row is only written while this request still owns it (same
        request_timestamp as its claim): a request that was taken over as abandoned cannot overwrite the newer one.
        Returns False if it no longer owned the row. Raises on database errors."""
        t = extraction_data
        values = self._blank(self._row(record))
        with self.database_service.engine.begin() as conn:
            owned = conn.execute(db.update(t).where(
                t.c.request_id == record["request_id"],
                t.c.request_timestamp == values["request_timestamp"]).values(**values)).rowcount
            if owned:
                return True
            exists = conn.execute(db.select(t.c.request_id).where(t.c.request_id == record["request_id"])).first()
            if exists is None:                                  # never claimed (e.g. claim lost): write it
                conn.execute(db.insert(t).values(request_id=record["request_id"], **values))
                return True
        return False

    def claim_photo(self, request_id: str, owner_ts: datetime, consumer_id, image_sha1: str, since: datetime | None,
                    stale_after: float):
        """Decides, atomically, whether this request's photo must be read. Returns (kind, row):
        'corrected'  a person marked the reading of this same photo (same bytes, same consumer) wrong or corrected
                     it: it is not read again (the same photo would be misread the same way) and goes to review
        'duplicate'  answered recently (since `since`), and the consumer's meter settings have not changed since:
                     that result is reused
        'in_flight'  being read right now under another request id: retry shortly, the result will be reused
        'new'        read it; the photo is marked on this request's row so the others see it from now on
        since=None: earlier results are not reused (only the photo is marked); a corrected photo is still never
        read again."""
        t = extraction_data
        consumer = t.c.consumer_id.is_(None) if consumer_id is None else t.c.consumer_id == consumer_id
        same = db.and_(t.c.image_sha1 == image_sha1, consumer, t.c.request_id != request_id)
        with self._photo_lock, self.database_service.engine.begin() as conn:
            if self._postgres:
                conn.execute(db.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _photo_lock(consumer_id, image_sha1)})
            reviewed = conn.execute(db.select(t).where(
                same, t.c.response_json.is_not(None),
                db.or_(t.c.extracted_reading_accurate.is_(False), t.c.actual_reading.is_not(None)))
                .order_by(t.c.request_timestamp.desc()).limit(20)).mappings().all()
            corrected = next((dict(r) for r in reviewed if _is_correction(r)), None)
            if corrected:                                       # whatever the duplicate setting: never re-read
                return "corrected", corrected
            if since is not None:
                earlier = conn.execute(db.select(t).where(          # the original read, not a reused copy: the
                    same, t.c.meter_reading_status.in_(FINAL), t.c.response_json.is_not(None),   # window counts
                    t.c.duplicate_of.is_(None), ~_copy_of_earlier(t),                             # from the read
                    t.c.request_timestamp >= self.ts(since))
                    .order_by(t.c.request_timestamp.desc()).limit(1)).mappings().first()
                if earlier:
                    changed = None
                    if consumer_id is not None:
                        changed = conn.execute(db.select(meter_profiles.c.updated_at).where(
                            meter_profiles.c.consumer_id == consumer_id)).scalar()
                    if changed is None or _aware(changed) <= _aware(earlier["request_timestamp"]):
                        return "duplicate", dict(earlier)
                busy = conn.execute(db.select(t).where(
                    same, t.c.meter_reading_status == PROCESSING,
                    t.c.request_timestamp >= self.ts(datetime.now(timezone.utc) - timedelta(seconds=stale_after)))
                    .limit(1)).mappings().first()
                if busy:
                    return "in_flight", dict(busy)
            conn.execute(db.update(t).where(t.c.request_id == request_id, t.c.request_timestamp == self.ts(owner_ts))
                         .values(image_sha1=image_sha1))
        return "new", None

    def reserve(self, request_id: str, owner_ts: datetime, amount: float, since: datetime, limit: float | None,
                in_flight_seconds: float = 600):
        """Before a paid call: if the spend recorded since `since` (readings in flight included, by their
        reservations) plus `amount` stays within `limit`, adds `amount` to this request's row and returns
        (True, spent, settled);
        otherwise (False, spent, settled). settled: the spend without the reservations of readings in flight
        (PROCESSING rows younger than in_flight_seconds; an older one was abandoned, and its reservation stays spent).
        limit=None: no cap, the reservation is still recorded."""
        t = extraction_data
        with self._budget_lock, self.database_service.engine.begin() as conn:
            if self._postgres:
                conn.execute(db.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BUDGET_LOCK})
            today = t.c.request_timestamp >= self.ts(since)
            spent = float(conn.execute(db.select(db.func.coalesce(db.func.sum(t.c.cost_usd), 0.0))
                                       .where(today)).scalar() or 0.0)
            if limit is not None and spent + amount > limit + 1e-9:
                recent = self.ts(datetime.now(timezone.utc) - timedelta(seconds=in_flight_seconds))
                in_flight = float(conn.execute(db.select(db.func.coalesce(db.func.sum(t.c.cost_usd), 0.0)).where(
                    today, t.c.meter_reading_status == PROCESSING, t.c.request_timestamp >= recent,
                    t.c.request_id != request_id))           # this request's own row: its earlier attempts' cost
                    .scalar() or 0.0)
                settled = spent - in_flight
                return False, spent, settled
            conn.execute(db.update(t).where(t.c.request_id == request_id, t.c.request_timestamp == self.ts(owner_ts))
                         .values(cost_usd=db.func.coalesce(t.c.cost_usd, 0.0) + amount))
        return True, spent, spent

    def store_rejected(self, record: dict):
        """Writes the record of a refused request, unless that id already has one (it is never overwritten)."""
        try:
            with self.database_service.engine.begin() as conn:
                conn.execute(db.insert(extraction_data).values(**self._row(record)))
        except db.exc.IntegrityError:
            pass

    def store_feedback(self, correlation_id: str, accurate: bool, actual, ts: datetime) -> int:
        """Records feedback on a reading; returns the number of readings updated (0: unknown correlation id)."""
        result = self.database_service.execute(
            db.update(extraction_data).where(extraction_data.c.correlation_id == correlation_id).values(
                extracted_reading_accurate=accurate,
                actual_reading=None if actual is None else str(actual),
                feedback_timestamp=self.ts(ts)))
        return result.rowcount

    def spend_since(self, since: datetime) -> float:
        """Model cost (USD) of the requests that arrived since `since`."""
        t = extraction_data
        row = self.database_service.fetch_one(
            db.select(db.func.coalesce(db.func.sum(t.c.cost_usd), 0.0).label("total"))
            .where(t.c.request_timestamp >= self.ts(since)))
        return float(row["total"] or 0.0)

    def ping(self) -> bool:
        return self.database_service.ping()

    # ---- meter settings ----
    def get_profile(self, consumer_id: str):
        row = self.database_service.fetch_one(db.select(meter_profiles).where(meter_profiles.c.consumer_id == consumer_id))
        return SimpleNamespace(**row) if row else None

    def set_profile(self, consumer_id, meter_type_id=None, decimals=None, digits=None, billing_unit=None):
        current = self.get_profile(consumer_id)
        if current is not None and (current.meter_type_id, current.decimals, current.digits, current.billing_unit) \
                == (meter_type_id, decimals, digits, billing_unit):
            return current               # nothing changed: updated_at stays, so earlier results stay reusable
        values = dict(meter_type_id=meter_type_id, decimals=decimals, digits=digits, billing_unit=billing_unit,
                      updated_at=self.ts(datetime.now(timezone.utc)))
        for attempt in range(2):
            try:
                with self.database_service.engine.begin() as conn:
                    updated = conn.execute(db.update(meter_profiles).where(meter_profiles.c.consumer_id == consumer_id)
                                           .values(**values)).rowcount
                    if not updated:
                        conn.execute(db.insert(meter_profiles).values(consumer_id=consumer_id, **values))
                break
            except db.exc.IntegrityError:          # another request created it at the same moment: update it now
                if attempt:
                    raise
        return self.get_profile(consumer_id)

    # ---- history ----
    def last_trusted(self, consumer_id: str, unit=None, before=None):
        """The newest trusted reading of the consumer taken at or before `before`. A reused answer (a copy of an
        earlier request's result) is not a new reading and is never used, including copies made by older versions of
        the service, which carry no duplicate_of mark."""
        t = extraction_data
        trusted = db.or_(
            db.and_(t.c.meter_reading_status == "SUCCESS",
                    db.or_(t.c.extracted_reading_accurate.is_(None), t.c.extracted_reading_accurate.is_(True))),
            # a person confirmed the returned reading; for RETAKE results only an explicit `actual` counts
            db.and_(t.c.extracted_reading_accurate.is_(True), t.c.meter_reading.is_not(None),
                    t.c.meter_reading_status.in_(("SUCCESS", "NEEDS_REVIEW"))),
            t.c.actual_reading.is_not(None))
        q = db.select(t.c.request_id, t.c.captured_at, t.c.unit, t.c.meter_reading, t.c.actual_reading).where(
            t.c.consumer_id == consumer_id, t.c.captured_at.is_not(None), t.c.duplicate_of.is_(None), trusted,
            ~_copy_of_earlier(t))
        if unit:
            q = q.where(db.or_(t.c.unit == unit, t.c.unit.is_(None)))
        if before:
            q = q.where(t.c.captured_at <= self.ts(before))
        # Newest first. A row whose value cannot be parsed is skipped, so it cannot switch the check off.
        for row in self.database_service.fetch_all(q.order_by(t.c.captured_at.desc()).limit(20)):
            raw = row["actual_reading"] if row["actual_reading"] is not None else row["meter_reading"]
            try:
                value = parse_reading(raw)
            except ValueError:
                self.base_logger.warning("skipping stored reading %s: not a number", row["request_id"])
                continue
            return Previous(value=value, unit=row["unit"], captured_at=_aware(row["captured_at"]))
        return None

    def get_record(self, request_or_correlation_id: str):
        if "\x00" in request_or_correlation_id:          # no stored id contains it (PostgreSQL would refuse it)
            return None
        t = extraction_data
        return self.database_service.fetch_one(db.select(t).where(db.or_(
            t.c.request_id == request_or_correlation_id, t.c.correlation_id == request_or_correlation_id)))
