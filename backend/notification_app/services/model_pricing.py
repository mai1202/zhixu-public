"""人民币价格快照：只对核对过的官方端点自动定价，不能把未知单价当成零。"""
from decimal import Decimal, ROUND_CEILING
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
VERIFIED = "2026-11-22"
ZONE = ZoneInfo("Asia/Shanghai")
FLASH = {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}
# 国办发明电〔2025〕7号；周末即便调休上班也遵循 DeepSeek 的周末空闲规则。
HOLIDAYS_2026 = ((1, 1, 3), (2, 15, 23), (4, 4, 6), (5, 1, 5), (6, 19, 21), (9, 25, 27), (10, 1, 7))


def includes_peak(start_epoch, end):
    """跨时段调用按高价保护预算，不因开始与结束恰巧都在空闲期而漏掉中间高峰。"""
    start = datetime.fromtimestamp(start_epoch, timezone.utc).astimezone(ZONE)
    stop = end.astimezone(ZONE)
    if stop < start or stop - start > timedelta(days=7):
        return True  # 时钟回拨或异常长请求不能释放可能已消费的高峰预留。
    day = start.date()
    while day <= stop.date():
        holiday = day.year == 2026 and any(day.month == month and first <= day.day <= last for month, first, last in HOLIDAYS_2026)
        if day.weekday() < 5 and not holiday:
            for begin, finish in ((9, 12), (14, 18)):
                low = datetime(day.year, day.month, day.day, begin, tzinfo=ZONE)
                high = datetime(day.year, day.month, day.day, finish, tzinfo=ZONE)
                if start < high and stop >= low:
                    return True
        day += timedelta(days=1)
    return False


def snapshot(profile, now, *, platform_paid=False):
    """冻结调用开始时的单价；跨时段预留始终采用高峰上界。"""
    local = now.astimezone(ZONE)
    holiday = local.year == 2026 and any(local.month == m and first <= local.day <= last for m, first, last in HOLIDAYS_2026)
    peak = local.weekday() < 5 and not holiday and (9 <= local.hour < 12 or 14 <= local.hour < 18)
    info = {"model_id": profile.model_id, "label": getattr(profile, "label", profile.model_id),
        "provider": profile.provider, "source": None, "verified_at": VERIFIED, "period": "peak" if peak else "off_peak",
        "rates": None, "max_rates": None, "calendar_estimated": local.year != 2026}
    official = urlsplit(profile.base_url).hostname == "api.deepseek.com"
    if official and (profile.model_id in FLASH or profile.model_id == "deepseek-v4-pro"):
        high = ["0.04", "2", "8"] if profile.model_id in FLASH else ["0.30", "9", "27"]
        info.update(source=SOURCE, max_rates=high, rates=high if peak else [str(Decimal(n) / 2) for n in high])
    elif not platform_paid and getattr(profile, "billing_prices_json", None):
        # 手填价格只影响自有密钥账单，复制平台密钥不能通过填零绕过限制。
        values = profile.billing_prices_json
        rates = [str(values[key]) for key in ("cache_hit", "cache_miss", "output")]
        info.update(source="user_configured", max_rates=rates, rates=rates, period="custom", calendar_estimated=False)
    return info


def cost_micros(rates, hit, miss, output):
    """每百万 token 单价乘 token 数即微元；每次向上取整到微元保护上限。"""
    return int(sum(Decimal(rate) * count for rate, count in zip(rates, (hit, miss, output))).to_integral_value(rounding=ROUND_CEILING))


def usage_parts(usage):
    """缓存属于输入，reasoning 属于输出；缺缓存数据时保守按未命中输入计费。"""
    if not isinstance(usage, dict):
        return None
    prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
    valid = lambda n: type(n) is int and 0 <= n <= 100_000_000
    if not valid(prompt) or not valid(output):
        return None
    total = usage.get("total_tokens")
    if total is not None and (not valid(total) or total != prompt + output):
        return None
    details = usage.get("prompt_tokens_details")
    hit = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0) if isinstance(details, dict) else 0)
    if not valid(hit) or hit > prompt:
        return None
    miss = usage.get("prompt_cache_miss_tokens", prompt - hit)
    if not valid(miss) or hit + miss != prompt:
        return None
    return {"prompt_tokens": prompt, "cache_hit_tokens": hit, "cache_miss_tokens": miss, "completion_tokens": output}
