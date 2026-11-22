"""平台人民币额度：先原子预留上界，再按输入、缓存和输出分别结算。"""
from datetime import datetime, timedelta, timezone
import hmac
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import User, ProviderProfile, PlatformTokenDay, PlatformTokenReservation, TestAccountAccess
from . import model_pricing as pricing

# 评审源码不附带任何私人账号的费用豁免。
EXEMPT_EMAILS = set()
ZONE = ZoneInfo("Asia/Shanghai")
DAILY_MICROS = 3_000_000


def _engine():
    return current_app.extensions["notification_db_engine"]


def _now():
    return datetime.now(timezone.utc)


def _period():
    local = _now().astimezone(ZONE)
    reset = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), ZONE)
    return local.date().isoformat(), reset.isoformat()


def is_platform_profile(profile):
    """默认配置及沿用同一平台密钥的配置均计费；真实自配密钥不计入。"""
    if getattr(profile, "is_default", False):
        return True
    key = str(current_app.config.get("DEFAULT_LLM_API_KEY") or "")
    if key and getattr(profile, "secret_ciphertext", None):
        from .provider_service import _decrypt_secret
        return hmac.compare_digest((_decrypt_secret(profile.secret_ciphertext) or "").encode(), key.encode())
    return False


def _legacy(db, user_id, day):
    """升级前只记录总 token；按最高输出单价补上保守估算，避免部署当天重新获得额度。"""
    for item in db.scalars(select(PlatformTokenReservation).where(PlatformTokenReservation.user_id == user_id,
            PlatformTokenReservation.day == day, PlatformTokenReservation.pricing_json.is_(None))):
        profile = db.get(ProviderProfile, item.profile_id)
        # 无法恢复输入输出拆分，不能伪造精确账单；未知模型取已支持模型中的最高单价。
        rate = 8 if profile and profile.model_id in pricing.FLASH else 27
        held, spent = item.amount * rate, (item.charged or 0) * rate
        info = {"model_id": profile.model_id if profile else "历史模型", "label": profile.label if profile else "历史配置",
            "rates": None, "max_rates": None, "legacy_estimate": True, "source": pricing.SOURCE}
        changed = db.execute(update(PlatformTokenReservation).where(PlatformTokenReservation.id == item.id,
            PlatformTokenReservation.pricing_json.is_(None)).values(pricing_json=info, reserved_micros=held,
                charged_micros=spent if item.status != "reserved" else None).returning(PlatformTokenReservation.id)).first()
        if changed:
            db.execute(update(PlatformTokenDay).where(PlatformTokenDay.user_id == user_id, PlatformTokenDay.day == day)
                .values(spent_micros=PlatformTokenDay.spent_micros + spent,
                    held_micros=PlatformTokenDay.held_micros + (held if item.status == "reserved" else 0)))


def _account_budget(db, user):
    """管理员预置的测试预算优先，不能用豁免邮箱或自填模型价格绕开测试上限。"""
    access = db.get(TestAccountAccess, user.id)
    if access:
        return False, access.daily_budget_micros, True
    return user.primary_email.strip().lower() in EXEMPT_EMAILS, DAILY_MICROS, False


def status(user_id):
    day, reset = _period()
    with Session(_engine()) as db, db.begin():
        user = db.get(User, user_id)
        if not user:
            raise ApiProblem("not_found", "未找到账户。", 404)
        _legacy(db, user_id, day)
        row = db.get(PlatformTokenDay, (user_id, day))
        exempt, limit, all_profiles = _account_budget(db, user)
        used, reserved = (row.used, row.reserved) if row else (0, 0)
        spent, held = (row.spent_micros, row.held_micros) if row else (0, 0)
    return {"day": day, "timezone": "Asia/Shanghai", "resets_at": reset, "exempt": exempt,
        "currency": "CNY", "limit_cny": None if exempt else limit / 1e6, "spent_cny": spent / 1e6,
        "reserved_cny": held / 1e6, "remaining_cny": None if exempt else max(0, limit - spent - held) / 1e6,
        # 旧 token 字段保留作诊断，2M token 上限已由人民币预算替代。
        "limit": None, "used": used, "reserved": reserved, "remaining": None,
        "self_configured_excluded": not all_profiles, "bind_model_href": "/settings/models"}


def reserve(profile, prompt_bytes, max_output_tokens, *, identifier=None, run_id=None):
    """数据库条件更新避免多个API/worker并发超支；未知调用不提前释放预留。"""
    platform = is_platform_profile(profile)
    if not platform and (profile.provider == "lm_studio" or profile.execution_target == "device"):
        return None
    if type(prompt_bytes) is not int or not 0 <= prompt_bytes <= 20_000_000:
        raise ApiProblem("invalid_quota_request", "输入预算无效。", 422)
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= profile.max_output_tokens:
        raise ApiProblem("invalid_quota_request", "输出预算无效。", 422)
    # UTF-8字节数是文本token的保守估计，另预留协议/工具标记开销；输出显式封顶。
    amount = prompt_bytes + 16_384 + max_output_tokens
    identifier = identifier or str(uuid4())
    day, _ = _period()
    now = _now()
    with Session(_engine()) as db, db.begin():
        user = db.get(User, profile.user_id)
        if user is None:
            raise ApiProblem("not_found", "未找到账户。", 404)
        exempt, limit, all_profiles = _account_budget(db, user)
        budgeted = platform or all_profiles
        info = pricing.snapshot(profile, now, platform_paid=budgeted)
        info['account_budgeted'] = all_profiles
        hold = pricing.cost_micros(info["max_rates"], 0, amount - max_output_tokens, max_output_tokens) if info["rates"] else 0
        previous = db.get(PlatformTokenReservation, identifier)
        if previous:
            if (previous.user_id, previous.profile_id, previous.run_id, previous.amount) != (profile.user_id, profile.id, run_id, amount):
                raise ApiProblem("quota_reservation_conflict", "调用预算标识冲突。", 409)
            if previous.status != "reserved":
                raise ApiProblem("quota_reservation_settled", "该调用已经结算，不能重复请求模型。", 409)
            return identifier
        if budgeted and not exempt and not info["rates"]:
            error = ApiProblem("platform_pricing_unavailable", "平台模型暂未配置可信单价，请绑定自己的模型并向服务商充值。", 503)
            error.retryable = False
            raise error
        _legacy(db, profile.user_id, day)
        dialect = db.get_bind().dialect.name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert if dialect == "postgresql" else None
        if insert is None:
            raise RuntimeError("token额度仅支持SQLite/PostgreSQL")
        db.execute(insert(PlatformTokenDay).values(user_id=profile.user_id, day=day, used=0, reserved=0, spent_micros=0, held_micros=0)
            .on_conflict_do_nothing(index_elements=[PlatformTokenDay.user_id, PlatformTokenDay.day]))
        if budgeted:
            conditions = [PlatformTokenDay.user_id == profile.user_id, PlatformTokenDay.day == day]
            if not exempt:
                conditions.append(PlatformTokenDay.spent_micros + PlatformTokenDay.held_micros + hold <= limit)
            updated = db.execute(update(PlatformTokenDay).where(*conditions).values(reserved=PlatformTokenDay.reserved + amount,
                held_micros=PlatformTokenDay.held_micros + hold).returning(PlatformTokenDay.user_id)).first()
            if not updated:
                message = f"今日模型预算不足（每日最多 {limit / 1e6:g} 元，含调用中预留）。"
                message += '请等待北京时间零点重置。' if all_profiles else '请绑定自己的模型，并向模型服务商付费充值后继续使用。'
                error = ApiProblem("platform_token_quota_exceeded", message,
                    429, details={"limit_cny": limit / 1e6, "bind_model_href": "/settings/models"})
                error.retryable = False  # 确定的日额度不足不让后台工作连续重试。
                raise error
        db.add(PlatformTokenReservation(id=identifier, user_id=profile.user_id, day=day, amount=amount,
            status="reserved", profile_id=profile.id, run_id=run_id, created_at_epoch=int(now.timestamp()),
            platform_paid=platform, reserved_micros=hold, pricing_json=info))
    return identifier


def usage_total(usage):
    """缓存输入已在prompt_tokens中，思考已在completion_tokens中，不能重复相加。"""
    if not isinstance(usage, dict):
        return None
    prompt, completion, total = (usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
    valid = lambda n: type(n) is int and n >= 0
    if valid(prompt) and valid(completion):
        return max(prompt + completion, total if valid(total) else 0)
    return total if valid(total) and total > 0 else None


def settle(identifier, user_id, usage, *, run_id=None):
    """未知用量按预留额保守计入；重复结算不会二次扣减，跨日仍归属原预留日。"""
    if identifier is None:
        return
    with Session(_engine()) as db, db.begin():
        item = db.get(PlatformTokenReservation, identifier)
        if item is None or item.user_id != user_id or item.run_id != run_id:
            raise ApiProblem("not_found", "未找到本次调用预算。", 404)
        if item.status != "reserved":
            return
        _legacy(db, user_id, item.day)
        db.refresh(item)
        measured = usage_total(usage)
        charge = item.amount if measured is None else measured
        # 异常提供方若返回超上界用量，完整记账并阻止后续调用，不能截短真实用量。
        if charge > item.amount:
            current_app.logger.error("平台token回执超过预留上界 reservation=%s", identifier)
        parts = pricing.usage_parts(usage)
        info = dict(item.pricing_json or {})
        rates = info.get("rates")
        if rates and info.get("period") == "off_peak" and pricing.includes_peak(item.created_at_epoch, _now()):
            # 供应商不回传具体计价时点；跨高峰按高价保守结算，防止日预算低估。
            rates = info["max_rates"]
            info.update(rates=rates, boundary_estimated=True, period="peak")
        money = pricing.cost_micros(rates, parts["cache_hit_tokens"], parts["cache_miss_tokens"], parts["completion_tokens"]) if parts and rates else item.reserved_micros
        # 无单价的自配云模型仍记用量，费用显示未知；不能显示虚假的 0 元账单。
        if not rates and not (item.pricing_json or {}).get("legacy_estimate"):
            money = None
        changed = db.execute(update(PlatformTokenReservation).where(PlatformTokenReservation.id == identifier,
            PlatformTokenReservation.status == "reserved").values(charged=charge, charged_micros=money, usage_json=parts,
            pricing_json=info,
            status="settled" if parts and rates and not info.get("boundary_estimated") and not info.get("calendar_estimated") else "estimated").returning(PlatformTokenReservation.id)).first()
        if changed and (item.platform_paid or info.get('account_budgeted')):
            db.execute(update(PlatformTokenDay).where(PlatformTokenDay.user_id == user_id, PlatformTokenDay.day == item.day)
                .values(used=PlatformTokenDay.used + charge, reserved=PlatformTokenDay.reserved - item.amount,
                    spent_micros=PlatformTokenDay.spent_micros + (money or 0), held_micros=PlatformTokenDay.held_micros - (item.reserved_micros or 0)))


def billing(user_id):
    """只返回本人今日账单，按模型配置聚合；不返回密钥或提示词。"""
    quota = status(user_id)
    with Session(_engine()) as db:
        rows = db.scalars(select(PlatformTokenReservation).where(PlatformTokenReservation.user_id == user_id,
            PlatformTokenReservation.day == quota["day"]).order_by(PlatformTokenReservation.created_at_epoch.desc(), PlatformTokenReservation.id)).all()
        groups, recent = {}, []
        for item in rows:
            info, usage = item.pricing_json or {}, item.usage_json or {}
            key = (item.profile_id, info.get("model_id"), item.platform_paid)
            group = groups.setdefault(key, {"profile_id": item.profile_id, "model_id": info.get("model_id", "历史模型"),
                "label": info.get("label", "历史配置"), "platform_paid": item.platform_paid, "spent_cny": 0,
                "reserved_cny": 0, "unknown_calls": 0, "estimated_calls": 0, "calls": 0,
                "prompt_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0, "completion_tokens": 0, "pricing": info})
            group["calls"] += 1
            group["spent_cny"] += item.charged_micros or 0
            group["reserved_cny"] += (item.reserved_micros or 0) if item.status == "reserved" else 0
            group["unknown_calls"] += int(item.charged_micros is None and item.status != "reserved")
            group["estimated_calls"] += int(item.status == "estimated" or bool(info.get("legacy_estimate")))
            for field in ("prompt_tokens", "cache_hit_tokens", "cache_miss_tokens", "completion_tokens"):
                group[field] += usage.get(field, 0)
            if len(recent) < 30:
                recent.append({"id": item.id, "profile_id": item.profile_id, "model_id": info.get("model_id", "历史模型"),
                    "created_at_epoch": item.created_at_epoch, "status": item.status, "platform_paid": item.platform_paid,
                    "cost_cny": item.charged_micros / 1e6 if item.charged_micros is not None else None, "usage": usage, "pricing": info})
        for group in groups.values():
            group["spent_cny"] /= 1e6
            group["reserved_cny"] /= 1e6
    return {"quota": quota, "models": list(groups.values()), "recent": recent, "pricing_source": pricing.SOURCE,
        "note": "按官方或自填单价与模型回执估算，供应商账单为准。跨高峰、回执缺失及升级前历史按高价上界估算；调用归属开始时的北京时间自然日，未知单价单独标记。"}


def complete_request(profile, body, callback):
    """抽取、修复与连接测试复用同一关口，发送前拒绝而非超支后仅记日志。"""
    output = body.get("max_completion_tokens", body.get("max_tokens"))
    size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    # 压缩图片的字节数不能约束视觉 token；带图请求按整个 1M 上下文上界预留。
    if any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict)) for m in body.get("messages", [])):
        size = max(size, 1_000_000)
    reservation = reserve(profile, size, output)
    result = None
    try:
        result = callback()
        return result
    except Exception as error:
        if getattr(error, 'code', None) in {'insufficient_balance', 'provider_insufficient_balance'}:
            from .operations_alert_service import notify_balance_safely
            notify_balance_safely(profile)
        raise
    finally:
        if reservation:
            settle(reservation, profile.user_id, result.get("usage") if isinstance(result, dict) else None)
