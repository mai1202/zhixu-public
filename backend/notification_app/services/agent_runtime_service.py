"""Agent 运行状态：把额度、密钥和提供方失败翻译成用户能处理的一句话。

界面只需要一个结论和一条入口，因此这里只读取调用失败的错误码、平台额度与已启用的
模型配置，不返回提示词、响应正文或任何密钥。
"""

from __future__ import annotations

import time
from typing import Any

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, LLMRun, ProviderProfile
from . import token_quota_service

# 只对近期失败报警，几天前的偶发错误不应一直挂在首页。
LOOKBACK_SECONDS = 3 * 24 * 3600

QUOTA_CODES = {
    "platform_token_quota_exceeded",
    "platform_pricing_unavailable",
    "provider_insufficient_balance",
    "insufficient_balance",
}
KEY_CODES = {
    "provider_auth_failed",
    "authentication_failed",
    "api_key_invalid",
    "secret_decrypt_failed",
    "llm_vault_unavailable",
}
RATE_CODES = {"provider_rate_limited", "rate_limited"}
UNAVAILABLE_CODES = {
    "provider_timeout",
    "provider_unavailable",
    "provider_http_error",
    "connection_failed",
    "model_unavailable",
    "model_request_failed",
    "provider_request_failed",
    "pi_run_failed",
    "pi_unavailable",
    "pi_stream_interrupted",
}

FAILURE_LABELS = {
    "platform_token_quota_exceeded": "今日平台模型预算不足",
    "platform_pricing_unavailable": "平台模型缺少可信单价",
    "provider_insufficient_balance": "模型账户余额不足",
    "insufficient_balance": "模型账户余额不足",
    "provider_auth_failed": "模型密钥被拒绝",
    "authentication_failed": "模型密钥被拒绝",
    "api_key_invalid": "模型密钥无效",
    "secret_decrypt_failed": "模型密钥无法解密",
    "provider_rate_limited": "模型请求被限流",
    "rate_limited": "模型请求被限流",
    "provider_timeout": "模型请求超时",
    "provider_unavailable": "模型服务不可用",
    "provider_http_error": "模型服务返回错误",
    "connection_failed": "无法连接模型服务",
    "model_unavailable": "模型配置不可用",
    "model_request_failed": "模型请求失败",
    "provider_request_failed": "模型请求失败",
    "pi_run_failed": "助手运行失败",
    "pi_stream_interrupted": "助手连接中断",
}


def _engine():
    return current_app.extensions["notification_db_engine"]


def _latest_failure(user_id: str) -> tuple[int | None, str | None]:
    """取最近一次可解释的失败：后台任务优先，其次逐请求模型账本。"""
    candidates: list[tuple[int, str]] = []
    with Session(_engine()) as db:
        job = db.scalar(
            select(Job)
            .where(
                Job.user_id == user_id,
                Job.kind.in_(("agent.run", "agent.notification")),
                Job.error_code.is_not(None),
            )
            .order_by(Job.updated_at_epoch.desc(), Job.id.desc())
            .limit(1)
        )
        if job is not None and job.error_code:
            candidates.append((int(job.updated_at_epoch or 0), str(job.error_code)))
        run = db.scalar(
            select(LLMRun)
            .where(LLMRun.user_id == user_id, LLMRun.status == "failed", LLMRun.failure_code.is_not(None))
            .order_by(LLMRun.created_at_epoch.desc(), LLMRun.id.desc())
            .limit(1)
        )
        if run is not None and run.failure_code:
            candidates.append((int(run.created_at_epoch or 0), str(run.failure_code)))
    if not candidates:
        return None, None
    return max(candidates)


def _block(
    status: str,
    title: str,
    detail: str,
    *,
    action_label: str = "检查模型设置",
    action_href: str = "/settings/models",
    failure_code: str | None = None,
    failure_epoch: int | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "healthy": False,
        "title": title,
        "detail": detail,
        "action_label": action_label,
        "action_href": action_href,
        "last_failure_code": failure_code,
        "last_failure_epoch": failure_epoch,
        "failure_label": FAILURE_LABELS.get(failure_code or "", None),
    }


def runtime_health(user_id: str) -> dict[str, Any]:
    """按「有没有可用模型 → 预算 → 最近失败」的顺序给出可执行结论。"""
    now = int(time.time())
    with Session(_engine()) as db:
        profiles = list(
            db.scalars(select(ProviderProfile).where(ProviderProfile.user_id == user_id)).all()
        )
    quota = token_quota_service.status(user_id)
    enabled = [profile for profile in profiles if profile.enabled]
    base = {
        "checked_at_epoch": now,
        "quota": {
            "exempt": quota["exempt"],
            "limit_cny": quota["limit_cny"],
            "spent_cny": quota["spent_cny"],
            "remaining_cny": quota["remaining_cny"],
            "resets_at": quota["resets_at"],
        },
        "enabled_profiles": len(enabled),
    }
    healthy = {
        "status": "ok",
        "healthy": True,
        "title": "Agent 可以正常工作",
        "detail": "模型调用未发现额度、密钥或连接问题。",
        "action_label": "检查模型设置",
        "action_href": "/settings/models",
        "last_failure_code": None,
        "last_failure_epoch": None,
        "failure_label": None,
    }
    if not enabled:
        return {
            **base,
            **_block(
                "no_profile",
                "还没有可用的模型配置",
                "Agent 需要至少一个已启用的模型配置才能整理通知和事务。"
                "请到「设置 → 模型连接」填写自己的 API Key，或连接本机模型后重新测试。",
            ),
        }
    if quota["remaining_cny"] is not None and quota["remaining_cny"] <= 0:
        limit = quota["limit_cny"] or 0
        spent = quota["spent_cny"]
        return {
            **base,
            **_block(
                "quota_exhausted",
                "今天的平台模型预算已经用完",
                f"平台默认模型每天最多 {limit:g} 元，今天已用 {spent:.2f} 元。"
                "北京时间零点自动恢复；也可以在「设置 → 模型连接」绑定自己的 API Key 继续使用。",
                action_label="绑定自己的模型",
            ),
        }
    failure_epoch, failure_code = _latest_failure(user_id)
    if failure_code and failure_epoch and now - failure_epoch <= LOOKBACK_SECONDS:
        label = FAILURE_LABELS.get(failure_code, failure_code)
        if failure_code in QUOTA_CODES:
            result = _block(
                "quota_exhausted",
                "模型额度或余额不足，Agent 暂停工作",
                f"最近一次失败：{label}（{failure_code}）。"
                "请给模型服务商充值，或在「设置 → 模型连接」绑定另一个可用模型后重试。",
                action_label="绑定自己的模型",
                failure_code=failure_code,
                failure_epoch=failure_epoch,
            )
        elif failure_code in KEY_CODES:
            result = _block(
                "key_rejected",
                "API Key 被模型服务拒绝",
                f"最近一次失败：{label}（{failure_code}）。"
                "请检查「设置 → 模型连接」里的 API Key 是否填错、过期或已被停用，然后重新测试连接。",
                action_label="检查 API Key",
                failure_code=failure_code,
                failure_epoch=failure_epoch,
            )
        elif failure_code in RATE_CODES:
            result = _block(
                "rate_limited",
                "模型服务正在限流，请求被暂时拒绝",
                f"最近一次失败：{label}（{failure_code}）。后台会自动重试；"
                "如果持续出现，请换一个模型配置或稍后再试。",
                failure_code=failure_code,
                failure_epoch=failure_epoch,
            )
        elif failure_code in UNAVAILABLE_CODES:
            result = _block(
                "unavailable",
                "模型调用失败，Agent 暂时无法完成整理",
                f"最近一次失败：{label}（{failure_code}）。"
                "请检查网络、模型地址（Base URL）与模型 ID，在「设置 → 模型连接」重新测试连接。",
                failure_code=failure_code,
                failure_epoch=failure_epoch,
            )
        else:
            result = _block(
                "failing",
                "Agent 最近一次运行失败",
                f"最近一次失败：{label}（{failure_code}）。"
                "请到「设置 → 模型连接」重新测试模型，或在「运行诊断」查看调用记录。",
                action_label="查看运行诊断",
                action_href="/settings/diagnostics",
                failure_code=failure_code,
                failure_epoch=failure_epoch,
            )
        return {**base, **result}
    return {**base, **healthy}
