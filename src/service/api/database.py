import os
import sqlalchemy as db
from sqlalchemy import text
from conf.config import Config


class DatabaseService:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        db_config = self.config.find("database", {}) or {}

        username = os.environ.get("FLOWVISION_DB_USERNAME", db_config.get("username", "postgres"))
        password = os.environ.get("FLOWVISION_DB_PASSWORD", db_config.get("password", "postgres"))
        host = os.environ.get("FLOWVISION_DB_HOST", db_config.get("host", "localhost"))
        port = int(os.environ.get("FLOWVISION_DB_PORT", db_config.get("port", 5432)))
        database = os.environ.get("FLOWVISION_DB_NAME", db_config.get("dbname", "flowvision"))

        url_object = db.URL.create(
            drivername="postgresql+psycopg2",
            username=username,
            password=password,
            host=host,
            port=port,
            database=database,
        )

        pool_config = db_config.get("pool", {}) or {}
        self.engine = db.create_engine(
            url_object,
            pool_size=pool_config.get("size", 5),
            max_overflow=pool_config.get("max_overflow", 10),
            pool_timeout=pool_config.get("timeout", 30),
            pool_recycle=pool_config.get("recycle", 1800),
            pool_pre_ping=True,
            future=True,
        )


    def get_connection(self) -> db.Connection:
        return self.engine.connect()

    def release_connection(self, conn: db.Connection):
        conn.close()

    def upsert(self, sql, params):
        conn = self.get_connection()
        try:
            conn.execute(statement=text(sql), parameters=params)
            conn.commit()
        except Exception as e:
            raise e
        finally:
            self.release_connection(conn=conn)

    def update(self):
        pass
