from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import event, engine_from_config, pool

from notification_app.models import Base
from notification_app.config import load_config
from pathlib import Path

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """让迁移命令与应用读取同一数据库配置。"""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if os.environ.get("DATA_DIR"):
        # 自定义数据目录首次运行时可能不存在，迁移前按应用配置创建目录。
        app_config = load_config()
        Path(app_config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
        return str(app_config["DATABASE_URL"])
    # 保留 Alembic 显式指定连接串的能力，供隔离测试和运维工具使用。
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    url = database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    if connectable.dialect.name == "sqlite":
        @event.listens_for(connectable, "connect")
        def enable_sqlite_safety(dbapi_connection, _connection_record) -> None:
            # 在 SQLAlchemy 开启事务前配置 SQLite，避免迁移写入随连接关闭回滚。
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
