"""
Database access, following FlowVision's DatabaseService (SQLAlchemy). Tables are defined with SQLAlchemy Core so the
same code runs on PostgreSQL (production, e.g. RDS; see meter_reader_db_ddl.sql) and SQLite (local runs and tests).
"""
import logging
import os
import time

import sqlalchemy as db
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url

metadata = db.MetaData()
# JSONB on PostgreSQL (indexable, compact), JSON elsewhere; Python None is stored as SQL NULL.
JSON = db.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")

extraction_data = db.Table(
    "meter_reader_extraction_data", metadata,
    db.Column("request_id", db.String, primary_key=True),
    db.Column("correlation_id", db.String, index=True),
    db.Column("consumer_id", db.String, index=True),
    db.Column("image_url", db.String),
    db.Column("image_sha1", db.String, index=True),           # finds a photo that was already read
    db.Column("request_fingerprint", db.String),              # consumer + photo: a reused id must match it
    db.Column("duplicate_of", db.String),                     # answered from this earlier request (same photo)
    db.Column("image_path", db.String),       # saved photo, relative to the photo store (storage.images_dir)
    db.Column("metadata", JSON),
    db.Column("request_timestamp", db.DateTime(timezone=True), index=True),   # server time the request arrived
    db.Column("captured_at", db.DateTime(timezone=True)),
    db.Column("meter_reading_status", db.String),   # SUCCESS | UNCLEAR | NOMETER | NEEDS_REVIEW | ERROR | PROCESSING
    db.Column("meter_reading", db.String),
    db.Column("unit", db.String),
    db.Column("meter_type", db.String),
    db.Column("reasons", JSON),
    db.Column("result", JSON),
    db.Column("model", db.String),
    db.Column("processing_time", db.Float),
    db.Column("cost_usd", db.Float),
    db.Column("response_timestamp", db.DateTime(timezone=True)),
    db.Column("extracted_reading_accurate", db.Boolean),
    db.Column("actual_reading", db.String),
    db.Column("feedback_timestamp", db.DateTime(timezone=True)),
    db.Column("request_json", JSON),          # the full request as received (image data left out), extra details included
    db.Column("response_json", JSON),         # the full response as sent, errors included
    # the previous-reading lookup: newest trusted reading of one consumer
    db.Index("ix_meter_reader_extraction_consumer_captured", "consumer_id", "captured_at"),
)

meter_profiles = db.Table(
    "meter_reader_profiles", metadata,
    db.Column("consumer_id", db.String, primary_key=True),
    db.Column("meter_type_id", db.String),
    db.Column("decimals", db.Integer),
    db.Column("digits", db.Integer),
    db.Column("billing_unit", db.String),
    db.Column("updated_at", db.DateTime(timezone=True)),
)

_MIGRATION_LOCK = 72_531_001        # PostgreSQL advisory lock id: one process at a time creates/changes tables
logger = logging.getLogger(__name__)


def _int_env(name, default):
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


class DatabaseService:
    def __init__(self, url: str, create_tables: bool = True):
        u = make_url(url)
        backend = u.get_backend_name()
        if backend == "sqlite" and u.database and u.database != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(u.database)), exist_ok=True)
            self.engine = db.create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})
        elif backend == "sqlite":
            self.engine = db.create_engine(url, connect_args={"check_same_thread": False}, poolclass=db.pool.StaticPool)
        else:
            connect_args = {}
            if backend == "postgresql":
                # fail fast when the database is unreachable, and never let one query hang a worker thread: the
                # server cancels slow statements (statement_timeout), and TCP keepalives plus tcp_user_timeout drop a
                # connection whose server has gone (failover, network loss) within about 15-25 s
                connect_args = {"connect_timeout": _int_env("METER_DB_CONNECT_TIMEOUT", 5),
                                "keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5,
                                "keepalives_count": 3,
                                "tcp_user_timeout": _int_env("METER_DB_TCP_USER_TIMEOUT_MS", 15000),
                                "options": f"-c statement_timeout={_int_env('METER_DB_STATEMENT_TIMEOUT_MS', 15000)} "
                                           "-c timezone=UTC"}           # times come back in UTC everywhere
            if os.getenv("METER_DB_NULL_POOL", "").lower() == "true":      # tests: no connections kept open
                self.engine = db.create_engine(url, poolclass=db.pool.NullPool, connect_args=connect_args)
            else:
                self.engine = db.create_engine(
                    url, pool_pre_ping=True, connect_args=connect_args,
                    pool_size=_int_env("METER_DB_POOL_SIZE", 5), max_overflow=_int_env("METER_DB_MAX_OVERFLOW", 10),
                    pool_timeout=_int_env("METER_DB_POOL_TIMEOUT", 10),
                    pool_recycle=_int_env("METER_DB_POOL_RECYCLE", 1800))
        if create_tables:
            self._with_retries(self._migrate)

    @staticmethod
    def _with_retries(fn, attempts=5):
        for attempt in range(attempts):
            try:
                return fn()
            except db.exc.OperationalError:
                if attempt == attempts - 1:
                    raise
                logger.warning("database not ready, retrying table setup (%d/%d)", attempt + 1, attempts)
                time.sleep(2 * (attempt + 1))

    def _migrate(self):
        """Creates missing tables, columns and indexes. On PostgreSQL an advisory lock makes concurrent workers and
        instances take turns, so they never race on the same DDL."""
        with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(db.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MIGRATION_LOCK})
            metadata.create_all(conn, checkfirst=True)
            inspector = db.inspect(conn)
            for table in metadata.sorted_tables:
                existing = {c["name"] for c in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name not in existing:
                        kind = column.type.compile(dialect=conn.dialect)
                        conn.execute(db.text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {kind}'))
                for index in table.indexes:
                    index.create(conn, checkfirst=True)

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(db.text("SELECT 1"))
        return True

    def execute(self, statement):
        with self.engine.begin() as conn:
            return conn.execute(statement)

    def fetch_one(self, statement):
        with self.engine.connect() as conn:
            row = conn.execute(statement).mappings().first()
        return dict(row) if row else None

    def fetch_all(self, statement):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(statement).mappings().all()]
