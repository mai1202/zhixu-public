from collections.abc import Callable, Mapping
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from .queue import (
    ClaimedJob,
    claim_next_job,
    complete_job,
    fail_job,
    mark_job_running,
)

JobHandler = Callable[[ClaimedJob], dict[str, Any]]


def run_once(
    engine,
    *,
    worker_id: str,
    handlers: Mapping[str, JobHandler],
    now_epoch: int | None = None,
    lease_seconds: int = 60,
) -> bool:
    """领取并执行一项白名单任务；异常只保存受控错误代码，不保存正文。"""
    claim = claim_next_job(
        engine,
        worker_id=worker_id,
        now_epoch=now_epoch,
        lease_seconds=lease_seconds,
        allowed_kinds=tuple(handlers),
    )
    if claim is None:
        return False
    if not mark_job_running(
        engine,
        claim,
        worker_id=worker_id,
        now_epoch=now_epoch,
    ):
        return True

    handler = handlers.get(claim.kind)
    if handler is None:
        fail_job(
            engine,
            claim,
            worker_id=worker_id,
            error_code="handler_unavailable",
            retry_delay_seconds=30,
            now_epoch=now_epoch,
        )
        return True

    try:
        result = handler(claim)
        if not isinstance(result, dict):
            raise TypeError("任务处理器必须返回 JSON 对象")
        complete_job(
            engine,
            claim,
            worker_id=worker_id,
            result_json=result,
            now_epoch=now_epoch,
        )
    except Exception as error:
        fail_job(
            engine,
            claim,
            worker_id=worker_id,
            error_code=str(getattr(error, "code", None) or type(error).__name__),
            # 缺浏览器等确定的配置错误直接终止，其他异常仍使用有限重试。
            retryable=getattr(error, "retryable", True),
            now_epoch=now_epoch,
        )
    return True


def main() -> None:
    """独立 worker 入口；各业务阶段注册自己的固定任务处理器。"""
    from .. import create_app

    app = create_app()
    engine = app.extensions["notification_db_engine"]
    # Playwright 驱动在启动浏览器前就需要创建临时目录；使用已配置的数据目录，
    # 避免系统 TEMP 权限异常使所有 OCR 任务反复失败。仅影响当前 worker 及其子进程。
    runtime_temp = Path(str(app.config["DATA_DIR"])) / "private" / "runtime-temp"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    for variable in ("TEMP", "TMP", "TMPDIR"):
        os.environ[variable] = str(runtime_temp)
    worker_id = os.environ.get("WORKER_ID") or f"worker-{uuid4().hex[:12]}"
    # 处理器拿到可信的租约 owner 和 job id，不从任务正文猜归属。
    from ..services.agent_service import handle_agent_job
    from ..services.main_assistant_service import handle_notification_job
    from ..services.mail_service import (
        dispatch_due_digests,
        handle_digest_compose_job,
        handle_mail_send_job,
    )
    from ..services.ocr_service import handle_server_ocr_job
    from ..services.reminder_service import dispatch_due_reminders, sync_all_reminders
    from ..services.report_service import dispatch_due_weekly_reports, handle_report_job
    from ..services.insight_service import scan_all_users
    from ..services.agent_schedule_service import dispatch_due_tasks
    from ..services.mail_ingress_service import poll_mailbox
    from ..services.integration_service import poll_feeds, handle_feed_job
    from ..services.trusted_source_service import distribute as distribute_journey
    from ..services.retrieval_service import handle_knowledge_index_job
    from ..services.device_attachment_service import handle_process_job as handle_attachment_job
    from ..services.onboarding_service import dispatch_reminders as dispatch_onboarding_reminders

    from ..services import business_service
    from ..services.innovation_status_service import handle_reminders
    from ..services.planning_service import handle_replan
    from ..services.operations_alert_service import dispatch_pending_alerts, handle_alert_job, schedule_balance_check, handle_balance_check
    handlers: dict[str, JobHandler] = {
        "operations.alert": handle_alert_job,
        "operations.balance_check": handle_balance_check,
        "feed.poll": handle_feed_job,
        "journey.distribute": distribute_journey,
        "notification.apply_legacy": business_service.handle_legacy_notification,
        "requirements.reminders": handle_reminders,
        "planning.replan": handle_replan,
        "ocr.server": handle_server_ocr_job,
        "agent.run": handle_agent_job,
        "agent.notification": handle_notification_job,
        "digest.compose": handle_digest_compose_job,
        "mail.send": handle_mail_send_job,
        "report.generate": handle_report_job,
        "knowledge.index": handle_knowledge_index_job,
        "attachment.process": handle_attachment_job,
    }
    lease_seconds = max(
        int(app.config.get("WORKER_LEASE_SECONDS", 600)),
        int(app.config["OCR_RUNNER_TIMEOUT_SECONDS"]) + 60,
    )
    last_digest_scan = 0
    last_reminder_sync = 0
    last_insight_scan = 0
    last_agent_schedule_scan = 0
    last_mail_poll = 0
    last_feed_poll = 0
    while True:
        with app.app_context():
            now = int(time.time())
            if now - last_feed_poll >= 60:
                try:
                    poll_feeds()
                except Exception as error:
                    app.logger.error('通知订阅源调度失败 error_type=%s', type(error).__name__)
                last_feed_poll = now
            if now - last_agent_schedule_scan >= 15:
                try:
                    dispatch_due_tasks(engine, now_epoch=now)
                except Exception as error:
                    app.logger.error('助手定时唤醒调度失败 error_type=%s', type(error).__name__)
                last_agent_schedule_scan = now
            if now - last_digest_scan >= 60:
                try:
                    dispatch_due_digests(engine, now_epoch=now)
                    dispatch_due_weekly_reports(engine, now_epoch=now)
                    dispatch_due_reminders(engine, now_epoch=now)
                    dispatch_onboarding_reminders(now_epoch=now)
                    dispatch_pending_alerts()
                    schedule_balance_check()
                except Exception as error:
                    app.logger.error("主动服务定时调度失败 error_type=%s", type(error).__name__)
                last_digest_scan = now
            if now - last_reminder_sync >= 300:
                try:
                    sync_all_reminders(engine, now_epoch=now)
                except Exception as error:
                    app.logger.error("提醒计划同步失败 error_type=%s", type(error).__name__)
                last_reminder_sync = now
            # 收信只在 worker 执行；IMAP 未配置时立即返回，不阻塞其他任务。
            if now - last_mail_poll >= 60:
                try:
                    result = poll_mailbox()
                    if result.get('status') == 'failed':
                        app.logger.warning('运营邮箱收信失败 error_code=%s', result.get('code'))
                except Exception as error:
                    app.logger.error('运营邮箱收信调度失败 error_type=%s', type(error).__name__)
                last_mail_poll = now
            if now - last_insight_scan >= 600:
                try:
                    scan_all_users(engine, now_epoch=now)
                except Exception as error:
                    app.logger.error("主动洞察扫描失败 error_type=%s", type(error).__name__)
                last_insight_scan = now
            processed = run_once(
                engine,
                worker_id=worker_id,
                handlers=handlers,
                lease_seconds=lease_seconds,
            )
        if not processed:
            time.sleep(1)


if __name__ == "__main__":
    main()
