import os
import sqlalchemy as db
from sqlalchemy import text
from conf.config import Config

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
        self.engine = db.create_engine(url_object)


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
