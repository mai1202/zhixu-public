import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL


class MigrationTest(unittest.TestCase):
    def test_model_billing_downgrade_preserves_accounts_profiles_and_token_ledger(self) -> None:
        """人民币列回退再升级不能级联删掉用户、模型配置或原有调用记录。"""
        from helpers import make_harness, login
        from sqlalchemy.orm import Session
        from notification_app.models import ProviderProfile, PlatformTokenDay, PlatformTokenReservation
        from sqlalchemy import select
        harness = make_harness(DEFAULT_LLM_API_KEY="synthetic-migration-key-only")
        try:
            client = harness.app.test_client()
            user, _ = login(client, harness.mail, "billing-migrate@gdufs.edu.cn")
            engine = harness.app.extensions["notification_db_engine"]
            with Session(engine) as db, db.begin():
                profile = db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == user["id"]))
                db.add(PlatformTokenDay(user_id=user["id"], day="2026-10-22", used=123, reserved=0))
                db.add(PlatformTokenReservation(id="migration-call", user_id=user["id"], day="2026-10-22", amount=1000,
                    charged=123, status="settled", profile_id=profile.id, created_at_epoch=1))
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            url = engine.url.render_as_string(hide_password=False)
            config.set_main_option("sqlalchemy.url", url)
            with patch.dict(os.environ, {"DATABASE_URL": url}):
                command.downgrade(config, "0036_course_actual_times")
                command.upgrade(config, "head")
            with engine.connect() as db:
                self.assertEqual(db.execute(text("select count(*) from users where id=:id"), {"id": user["id"]}).scalar(), 1)
                self.assertEqual(db.execute(text("select count(*) from provider_profiles where user_id=:id"), {"id": user["id"]}).scalar(), 2)
                self.assertEqual(db.execute(text("select charged from platform_token_reservations where id='migration-call'")).scalar(), 123)
                self.assertEqual(db.execute(text("pragma foreign_key_check")).all(), [])
        finally:
            harness.close()

    def test_parallel_login_and_planning_migrations_preserve_existing_sessions(self) -> None:
        """旧库先升级规划分支再合并登录分支，原会话与业务表必须同时保留。"""
        from alembic.script import ScriptDirectory

        backend_dir = Path(__file__).resolve().parents[1]
        config = Config(str(backend_dir / 'alembic.ini'))
        # 后续可继续增加迁移；要求仍然只有一个头，且包含已合并的两条父链。
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        self.assertEqual(len(heads), 1)
        self.assertIn('0033_merge_login_security', {revision.revision for revision in script.walk_revisions()})
        with tempfile.TemporaryDirectory(prefix='合并迁移测试_') as temp_dir:
            database_url = URL.create('sqlite', database=str(Path(temp_dir) / 'merge.sqlite3')).render_as_string(hide_password=False)
            config.set_main_option('sqlalchemy.url', database_url)
            command.upgrade(config, '0028_notification_groups')
            engine = create_engine(database_url)
            try:
                # 保留一条旧的撤销会话，确认合并不会复活令牌或清空历史记录。
                with engine.begin() as connection:
                    connection.execute(text("INSERT INTO users (id,primary_email,email_key,verified_at_epoch,created_at_epoch) VALUES ('merge-owner','merge@gdufs.edu.cn','merge@gdufs.edu.cn',1,1)"))
                    connection.execute(text("INSERT INTO web_sessions (id,user_id,token_digest,created_at_epoch,expires_at_epoch,revoked_at_epoch) VALUES ('legacy-session','merge-owner',:digest,1,2,3)"), {'digest': 'a' * 64})
                command.upgrade(config, '0031_planning_runs')
                command.upgrade(config, 'head')
                with engine.connect() as connection:
                    session = connection.execute(text('SELECT token_digest,expires_at_epoch,revoked_at_epoch,persistent FROM web_sessions WHERE id=:id'), {'id': 'legacy-session'}).one()
                    self.assertEqual(tuple(session), ('a' * 64, 2, 3, 0))
                    self.assertEqual(connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all(), heads)
                    self.assertEqual(connection.execute(text('PRAGMA quick_check')).scalar(), 'ok')
                self.assertIn('user_planning_states', inspect(engine).get_table_names())
            finally:
                engine.dispose()

    def test_data_dir_matches_application_database(self) -> None:
        """只配置 DATA_DIR 时，迁移与应用必须使用同一新建数据库。"""
        from notification_app.config import load_config

        with tempfile.TemporaryDirectory(prefix="迁移数据目录_") as temp_dir:
            data_dir = Path(temp_dir) / "新数据目录"
            with patch.dict(os.environ, {"DATA_DIR": str(data_dir)}):
                # 清除继承的连接串，验证仅配置目录的真实启动流程。
                os.environ.pop("DATABASE_URL", None)
                config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                command.upgrade(config, "head")
                engine = create_engine(load_config()["DATABASE_URL"])
                try:
                    self.assertIn("users", inspect(engine).get_table_names())
                finally:
                    engine.dispose()

    def test_database_url_takes_priority_over_data_dir(self) -> None:
        """显式 DATABASE_URL 必须覆盖 DATA_DIR，避免迁移错误的目标库。"""
        with tempfile.TemporaryDirectory(prefix="迁移连接优先级_") as temp_dir:
            database_path = Path(temp_dir) / "explicit.sqlite3"
            database_url = URL.create("sqlite", database=str(database_path)).render_as_string(hide_password=False)
            data_dir = Path(temp_dir) / "unused"
            with patch.dict(os.environ, {"DATABASE_URL": database_url, "DATA_DIR": str(data_dir)}):
                config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                command.upgrade(config, "head")
                engine = create_engine(database_url)
                try:
                    self.assertIn("users", inspect(engine).get_table_names())
                    self.assertFalse(data_dir.exists())
                finally:
                    engine.dispose()

    def test_all_migrations_run_on_unicode_windows_path(self) -> None:
        backend_dir = Path(__file__).resolve().parents[1]
        config_path = backend_dir / "alembic.ini"

        # 中文目录名用于验证 Unicode 路径，但位置放在系统临时目录。
        with tempfile.TemporaryDirectory(
            prefix="通知管理迁移_",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            database_path = Path(temp_dir) / "test.sqlite3"
            database_url = URL.create(
                "sqlite",
                database=str(database_path),
            ).render_as_string(hide_password=False)
            config = Config(str(config_path))
            config.set_main_option("sqlalchemy.url", database_url)

            command.upgrade(config, "head")

            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                self.assertIn("app_metadata", inspector.get_table_names())
                self.assertTrue(
                    {
                        "users",
                        "verified_addresses",
                        "web_sessions",
                        "jobs",
                        "source_documents",
                        "source_segments",
                        "notices",
                        "events",
                        "tasks",
                        "revisions",
                        "change_sets",
                        "academic_terms",
                        "timetable_imports",
                        "course_series",
                        "course_occurrences",
                        "course_exceptions",
                        "schedule_entries",
                        "provider_profiles",
                        "llm_runs",
                        "ocr_artifacts",
                        "devices",
                        "device_pairings",
                        "device_credentials",
                        "sync_batches",
                        "sync_items",
                        "digest_preferences",
                        "digest_cursors",
                        "mail_outbox",
                        "mail_credentials",
                        "agent_memories",
                        "business_links",
                        "operation_logs",
                        "reminders",
                        "activity_reports",
                        "agent_insights",
                        "device_attachments",
                        "user_onboarding",
                        "student_journeys",
                        "journey_members",
                        "student_intents",
                    }.issubset(set(inspector.get_table_names()))
                )
                # 新增表和偏好列必须随完整迁移链建立，旧库升级后才能持久保存记忆与档位。
                memory_columns = {
                    column["name"]
                    for column in inspector.get_columns("agent_memories")
                }
                self.assertTrue({"user_id", "content", "normalized_content"}.issubset(memory_columns))
                preference_columns = {
                    column["name"]
                    for column in inspector.get_columns("agent_preferences")
                }
                self.assertIn("thinking_level", preference_columns)
                # 附件父消息的范围字段与提醒调度索引也必须来自迁移，不能只存在于 ORM。
                self.assertIn('chat_id', {column['name'] for column in inspector.get_columns('sync_items')})
                self.assertIn('ix_user_onboarding_reminder_due', {index['name'] for index in inspector.get_indexes('user_onboarding')})
                with engine.connect() as connection:
                    foundation = connection.execute(
                        text("SELECT foundation_version FROM app_metadata WHERE id = 1")
                    ).scalar_one()
                    self.assertEqual(foundation, "p0-1")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
