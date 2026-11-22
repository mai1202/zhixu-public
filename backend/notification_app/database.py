from collections.abc import Mapping

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def create_engine_for_app(config: Mapping[str, object]) -> Engine:
    """创建当前应用独享的 SQLAlchemy 2 引擎并启用 SQLite 约束。"""
    database_url = str(config["DATABASE_URL"])
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 5}

    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_sqlite_safety(dbapi_connection, _connection_record) -> None:
            # SQLite 默认不强制外键；每条连接都开启外键和有限等待。
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine
