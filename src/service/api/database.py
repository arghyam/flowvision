import sqlalchemy as db
from sqlalchemy import text
from conf.config import Config

class DatabaseService:
    def __init__(self, config: Config):
        self.config = Config
        url_object = db.URL.create(
            drivername="postgresql+psycopg2",
            username=config.find("database.username"),
            password=config.find("database.password"),
            host=config.find("database.host"),
            port=config.find("database.port"),
            database=config.find("database.dbname"),
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
