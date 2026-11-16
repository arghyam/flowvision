import os
import sqlalchemy as db
from sqlalchemy import text

class DatabaseService:
    def __init__(self):
        url_object = db.URL.create(
            drivername="postgresql+psycopg2",
            username=os.environ.get("FLOWVISION_DB_USERNAME", "postgres"),
            password=os.environ.get("FLOWVISION_DB_PASSWORD", "postgres"),
            host=os.environ.get("FLOWVISION_DB_HOST", "localhost"),
            port=int(os.environ.get("FLOWVISION_DB_PORT", 5432)),
            database=os.environ.get("FLOWVISION_DB_NAME", "flowvision"),
        )
        # Without time limits a database that hangs would hold every request's background write (and its thread)
        # indefinitely: give up connecting after 3 s and on any statement after 10 s; check a pooled connection
        # is still alive before using it.
        self.engine = db.create_engine(url_object, pool_pre_ping=True,
                                       connect_args={"connect_timeout": 3, "options": "-c statement_timeout=10000"})


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
