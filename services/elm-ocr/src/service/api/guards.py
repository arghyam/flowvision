"""
Two limits that protect the OpenAI bill and the service itself:
- DailyBudget: once today's model spend reaches METER_DAILY_BUDGET_USD, new readings are refused (HTTP 429)
  until the next day (India time). Spend is summed from the database, so every instance shares one cap, and readings
  in flight count: each paid read first reserves its estimated cost (MetadataStore.reserve, under a lock shared by
  all instances), so a burst cannot pass the cap together.
- ReadSlots: at most METER_MAX_CONCURRENT_READS readings in progress per worker process; a request that cannot get
  a slot within METER_BUSY_WAIT_SECONDS is refused (HTTP 503, retryable) instead of queueing without limit.
"""
import threading
from datetime import datetime, timedelta, timezone, tzinfo

INDIA = timezone(timedelta(hours=5, minutes=30))     # the budget day runs from midnight to midnight India time


class DailyBudget:
    def __init__(self, limit_usd: float | None, reserve, day_zone: tzinfo = INDIA):
        """reserve(request_id, owner_ts, amount, since, limit) -> (allowed, spent, settled): MetadataStore.reserve."""
        self.limit, self._reserve, self.day_zone = limit_usd, reserve, day_zone

    def day_start(self, now: datetime | None = None) -> datetime:
        local = (now or datetime.now(timezone.utc)).astimezone(self.day_zone)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def reserve(self, request_id: str, owner_ts: datetime, amount: float) -> tuple[bool, bool]:
        """Reserves `amount` for one paid read, if today's spend (reservations included) plus `amount` stays within
        the limit. Returns (allowed, used_up): used_up is True when the finished readings alone leave no room for it
        (nothing frees up until tomorrow), False when only reservations of readings in flight are in the way (retry
        shortly)."""
        allowed, spent, settled = self._reserve(request_id, owner_ts, amount, self.day_start(), self.limit)
        return allowed, (not allowed and (self.limit is None or settled + amount > self.limit + 1e-9))

    def resets_at(self, now: datetime | None = None) -> datetime:
        return self.day_start(now) + timedelta(days=1)


class ReadSlots:
    def __init__(self, size: int, wait_seconds: float):
        self._slots = threading.BoundedSemaphore(size)
        self.size, self.wait = size, wait_seconds
        self._busy = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        if not self._slots.acquire(timeout=self.wait):
            return False
        with self._lock:
            self._busy += 1
        return True

    def release(self):
        with self._lock:
            self._busy -= 1
        self._slots.release()

    @property
    def busy(self) -> int:
        return self._busy
