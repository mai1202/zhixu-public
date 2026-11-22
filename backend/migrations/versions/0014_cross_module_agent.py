"""通知、任务、课程三端关联，以及审计、提醒和日报/周报。"""

from alembic import op
import sqlalchemy as sa

revision = "0014_cross_module_agent"
down_revision = "0013_agent_thinking_memory"
branch_labels = depends_on = None


def upgrade() -> None:
    # 现有候选确认后仍能写入这些列，因此新增字段均使用空值或安全默认值。
    with op.batch_alter_table("notices") as batch_op:
        batch_op.add_column(sa.Column("importance", sa.String(12), server_default="normal", nullable=False))
        batch_op.add_column(sa.Column("info_status", sa.String(20), server_default="complete", nullable=False))
        batch_op.add_column(sa.Column("last_change_set_id", sa.String(36), nullable=True))

    op.drop_index("ix_tasks_owner_status_due", table_name="tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("source_document_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("notice_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("course_series_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("course_occurrence_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("task_kind", sa.String(24), server_default="assignment", nullable=False))
        batch_op.add_column(sa.Column("priority", sa.String(12), server_default="normal", nullable=False))
        batch_op.add_column(sa.Column("due_at_epoch", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("original_requirement", sa.String(1000), nullable=True))
        batch_op.add_column(sa.Column("reminder_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False))
        batch_op.create_foreign_key(
            "fk_tasks_source_document", "source_documents", ["source_document_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_foreign_key(
            "fk_tasks_notice", "notices", ["notice_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_foreign_key(
            "fk_tasks_course_series", "course_series", ["course_series_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_foreign_key(
            "fk_tasks_course_occurrence",
            "course_occurrences",
            ["course_occurrence_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_tasks_owner_status_due",
        "tasks",
        ["user_id", "status", "due_at_epoch"],
    )

    with op.batch_alter_table("course_occurrences") as batch_op:
        batch_op.add_column(sa.Column("room_override", sa.String(500), nullable=True))

    with op.batch_alter_table("course_exceptions") as batch_op:
        batch_op.add_column(sa.Column("notice_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("source_document_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("change_set_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("actor_type", sa.String(20), server_default="user", nullable=False))
        batch_op.add_column(sa.Column("evidence_json", sa.JSON(), nullable=True))
        batch_op.create_foreign_key(
            "fk_course_exceptions_notice", "notices", ["notice_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_foreign_key(
            "fk_course_exceptions_source",
            "source_documents",
            ["source_document_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_course_exceptions_change",
            "change_sets",
            ["change_set_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("digest_preferences") as batch_op:
        batch_op.add_column(sa.Column("weekly_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False))
        batch_op.add_column(sa.Column("weekly_send_day", sa.Integer(), server_default="7", nullable=False))
        batch_op.add_column(sa.Column("weekly_send_time", sa.String(5), server_default="18:00", nullable=False))
        batch_op.add_column(sa.Column("next_weekly_send_epoch", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reminder_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False))
        batch_op.add_column(sa.Column("reminder_offsets_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("notification_flags_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("quiet_hours_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("merge_window_minutes", sa.Integer(), server_default="30", nullable=False))
    # SQLite/PostgreSQL 的 JSON 列新增时不会自动回填；显式写入兼容默认值。
    op.execute("UPDATE digest_preferences SET reminder_offsets_json = '[1440, 120]' WHERE reminder_offsets_json IS NULL")
    op.execute("UPDATE digest_preferences SET notification_flags_json = '{}' WHERE notification_flags_json IS NULL")
    op.execute("UPDATE digest_preferences SET quiet_hours_json = '{}' WHERE quiet_hours_json IS NULL")
    op.execute("UPDATE course_exceptions SET evidence_json = '{}' WHERE evidence_json IS NULL")

    op.create_table(
        "business_links",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("from_type", sa.String(32), nullable=False),
        sa.Column("from_id", sa.String(36), nullable=False),
        sa.Column("to_type", sa.String(32), nullable=False),
        sa.Column("to_id", sa.String(36), nullable=False),
        sa.Column("relation", sa.String(32), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("source_document_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            "relation",
            name="uq_business_links_owner_edge",
        ),
    )
    op.create_index(
        "ix_business_links_owner_from",
        "business_links",
        ["user_id", "from_type", "from_id", "status"],
    )
    op.create_index(
        "ix_business_links_owner_to",
        "business_links",
        ["user_id", "to_type", "to_id", "status"],
    )

    op.create_table(
        "operation_logs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_type", sa.String(32), nullable=False),
        sa.Column("object_id", sa.String(36), nullable=False),
        sa.Column("change_set_id", sa.String(36), nullable=True),
        sa.Column("source_document_id", sa.String(36), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("undoable", sa.Boolean(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["change_set_id"], ["change_sets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operation_logs_owner_created",
        "operation_logs",
        ["user_id", "created_at_epoch"],
    )
    op.create_index(
        "ix_operation_logs_owner_object",
        "operation_logs",
        ["user_id", "object_type", "object_id"],
    )

    op.create_table(
        "activity_reports",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("period_start", sa.String(10), nullable=False),
        sa.Column("period_end", sa.String(10), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("sections_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(250), nullable=False),
        sa.Column("outbox_id", sa.String(36), nullable=True),
        sa.Column("generated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("sent_at_epoch", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["outbox_id"], ["mail_outbox.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_activity_reports_dedupe_key"),
    )
    op.create_index(
        "ix_activity_reports_owner_kind_period",
        "activity_reports",
        ["user_id", "kind", "period_start"],
    )

    op.create_table(
        "reminders",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("notice_id", sa.String(36), nullable=True),
        sa.Column("course_occurrence_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("scheduled_at_epoch", sa.Integer(), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("dedupe_key", sa.String(250), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("outbox_id", sa.String(36), nullable=True),
        sa.Column("sent_at_epoch", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["course_occurrence_id"], ["course_occurrences.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["notice_id"], ["notices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["outbox_id"], ["mail_outbox.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_reminders_dedupe_key"),
    )
    op.create_index(
        "ix_reminders_owner_status_due",
        "reminders",
        ["user_id", "status", "scheduled_at_epoch"],
    )
    op.create_index("ix_reminders_task", "reminders", ["user_id", "task_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_reminders_task", table_name="reminders")
    op.drop_index("ix_reminders_owner_status_due", table_name="reminders")
    op.drop_table("reminders")
    op.drop_index("ix_activity_reports_owner_kind_period", table_name="activity_reports")
    op.drop_table("activity_reports")
    op.drop_index("ix_operation_logs_owner_object", table_name="operation_logs")
    op.drop_index("ix_operation_logs_owner_created", table_name="operation_logs")
    op.drop_table("operation_logs")
    op.drop_index("ix_business_links_owner_to", table_name="business_links")
    op.drop_index("ix_business_links_owner_from", table_name="business_links")
    op.drop_table("business_links")

    with op.batch_alter_table("digest_preferences") as batch_op:
        batch_op.drop_column("merge_window_minutes")
        batch_op.drop_column("quiet_hours_json")
        batch_op.drop_column("notification_flags_json")
        batch_op.drop_column("reminder_offsets_json")
        batch_op.drop_column("reminder_enabled")
        batch_op.drop_column("next_weekly_send_epoch")
        batch_op.drop_column("weekly_send_time")
        batch_op.drop_column("weekly_send_day")
        batch_op.drop_column("weekly_enabled")

    with op.batch_alter_table("course_exceptions") as batch_op:
        batch_op.drop_constraint("fk_course_exceptions_change", type_="foreignkey")
        batch_op.drop_constraint("fk_course_exceptions_source", type_="foreignkey")
        batch_op.drop_constraint("fk_course_exceptions_notice", type_="foreignkey")
        batch_op.drop_column("evidence_json")
        batch_op.drop_column("actor_type")
        batch_op.drop_column("change_set_id")
        batch_op.drop_column("source_document_id")
        batch_op.drop_column("notice_id")

    with op.batch_alter_table("course_occurrences") as batch_op:
        batch_op.drop_column("room_override")

    op.drop_index("ix_tasks_owner_status_due", table_name="tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("fk_tasks_course_occurrence", type_="foreignkey")
        batch_op.drop_constraint("fk_tasks_course_series", type_="foreignkey")
        batch_op.drop_constraint("fk_tasks_notice", type_="foreignkey")
        batch_op.drop_constraint("fk_tasks_source_document", type_="foreignkey")
        batch_op.drop_column("reminder_enabled")
        batch_op.drop_column("original_requirement")
        batch_op.drop_column("due_at_epoch")
        batch_op.drop_column("priority")
        batch_op.drop_column("task_kind")
        batch_op.drop_column("description")
        batch_op.drop_column("course_occurrence_id")
        batch_op.drop_column("course_series_id")
        batch_op.drop_column("notice_id")
        batch_op.drop_column("source_document_id")
    op.create_index("ix_tasks_owner_status_due", "tasks", ["user_id", "status"])

    with op.batch_alter_table("notices") as batch_op:
        batch_op.drop_column("last_change_set_id")
        batch_op.drop_column("info_status")
        batch_op.drop_column("importance")
