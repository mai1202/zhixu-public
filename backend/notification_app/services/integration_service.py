"""复用来源入库、幂等和联网防护，外部内容始终按资料处理。"""
import hashlib
import hmac
import secrets
import time
from uuid import uuid4

from defusedxml import ElementTree
from flask import current_app
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import NotificationFeed, User, WebhookCredential
from ..schemas.business import CreateSourceInput
from . import business_service, web_knowledge_service as web


def _engine():
    return current_app.extensions["notification_db_engine"]


def _digest(token):
    # 摘要按用途派生，不存明文凭证，也不允许作为网页登录凭证使用。
    return hmac.new(str(current_app.config["AUTH_HMAC_KEY"]).encode(), ("notice-hook:" + token).encode(), hashlib.sha256).hexdigest()


def credential_status(user_id):
    with Session(_engine()) as db:
        item = db.get(WebhookCredential, user_id)
        return {"enabled": item is not None, "endpoint": "/api/v1/integrations/webhook", "created_at_epoch": item.created_at_epoch if item else None}


def issue_credential(user_id):
    token = secrets.token_urlsafe(48)
    with Session(_engine()) as db, db.begin():
        db.execute(delete(WebhookCredential).where(WebhookCredential.user_id == user_id))
        db.add(WebhookCredential(user_id=user_id, token_digest=_digest(token), created_at_epoch=int(time.time())))
    return {**credential_status(user_id), "token": token}


def revoke_credential(user_id):
    with Session(_engine()) as db, db.begin():
        db.execute(delete(WebhookCredential).where(WebhookCredential.user_id == user_id))
    return credential_status(user_id)


def webhook_user(token):
    if not isinstance(token, str) or not 40 <= len(token) <= 100:
        raise ApiProblem("invalid_webhook_token", "投递凭证无效。", 401)
    with Session(_engine()) as db:
        user_id = db.scalar(select(WebhookCredential.user_id).join(User, User.id == WebhookCredential.user_id)
            .where(WebhookCredential.token_digest == _digest(token), User.is_active.is_(True)))
    if not user_id:
        raise ApiProblem("invalid_webhook_token", "投递凭证无效或已撤销。", 401)
    return user_id


def deliver(user_id, payload, *, source_type="webhook", namespace="webhook"):
    source = CreateSourceInput(source_type="text", title=payload.title, text=payload.text)
    return business_service.create_source(user_id, source, source_type_override=source_type,
        external_key=f"{namespace}:" + hashlib.sha256(payload.source_key.encode()).hexdigest(),
        submission_hash=business_service.submission_fingerprint(source))


def list_feeds(user_id):
    with Session(_engine()) as db:
        rows = db.scalars(select(NotificationFeed).where(NotificationFeed.user_id == user_id).order_by(NotificationFeed.created_at_epoch)).all()
        return {"items": [{"id": r.id, "title": r.title, "url": r.url, "last_success_epoch": r.last_success_epoch,
                            "error_code": r.error_code} for r in rows]}


def add_feed(user_id, payload):
    # 保存时与每次拉取时都校验目标，不能通过重定向或 DNS 变化访问内网。
    clean, _, _ = web._address(payload.url)
    try:
        with Session(_engine()) as db, db.begin():
            if db.scalar(select(func.count()).select_from(NotificationFeed).where(NotificationFeed.user_id == user_id)) >= 20:
                raise ApiProblem("feed_limit", "最多订阅20个通知源。", 422)
            db.add(NotificationFeed(id=str(uuid4()), user_id=user_id, title=payload.title.strip(), url=clean,
                created_at_epoch=int(time.time()), next_poll_epoch=int(time.time()), last_success_epoch=None, error_code=None))
    except IntegrityError:
        raise ApiProblem("feed_exists", "已经订阅该通知源。", 409) from None
    return list_feeds(user_id)


def remove_feed(user_id, feed_id):
    with Session(_engine()) as db, db.begin():
        if db.execute(delete(NotificationFeed).where(NotificationFeed.id == feed_id, NotificationFeed.user_id == user_id)).rowcount != 1:
            raise ApiProblem("not_found", "未找到订阅源。", 404)
    return list_feeds(user_id)


def _entries(data):
    """支持 RSS2 和 Atom；禁用实体解析，不跟随条目中的资源或脚本。"""
    try:
        root = ElementTree.fromstring(data)
    except Exception:
        raise ApiProblem("invalid_feed", "订阅源不是有效的 RSS/Atom。", 422) from None
    tag = root.tag.rsplit("}", 1)[-1]
    if tag not in {"rss", "feed", "RDF"}:
        raise ApiProblem("invalid_feed", "订阅源不是有效的 RSS/Atom。", 422)
    for node in [n for n in root.iter() if n.tag.rsplit("}", 1)[-1] in {"item", "entry"}][:50]:
        fields = {child.tag.rsplit("}", 1)[-1]: "".join(child.itertext()) for child in node}
        title = fields.get("title", "通知")[:500]
        raw = fields.get("encoded") or fields.get("content") or fields.get("description") or fields.get("summary") or title
        page = web.PageText(); page.feed(raw)
        text = page.text()[:100000]
        key = fields.get("guid") or fields.get("id") or fields.get("link") or hashlib.sha256((title + text).encode()).hexdigest()
        if text.strip():
            yield key, title, text


def poll_feeds():
    """只调度到独立持久任务，网络拉取不阻塞整轮提醒、邮件与其他订阅源。"""
    from .planning_guard import enqueue
    now = int(time.time())
    with Session(_engine()) as db:
        ids = db.scalars(select(NotificationFeed.id).where(NotificationFeed.next_poll_epoch <= now).limit(100)).all()
    queued = 0
    for identifier in ids:
        with Session(_engine()) as db, db.begin():
            row = db.scalar(select(NotificationFeed).where(NotificationFeed.id == identifier))
            if row is None or db.execute(update(NotificationFeed).where(NotificationFeed.id == identifier,
                    NotificationFeed.next_poll_epoch <= now).values(next_poll_epoch=now + 1800)).rowcount != 1:
                continue
            user = db.get(User, row.user_id)
            if not user or not user.is_active:
                continue
            enqueue(db, row.user_id, "feed.poll", {"feed_id": identifier}, f"feed:{identifier}:{now}")
            queued += 1
    return {"queued": queued}


def handle_feed_job(claim):
    """每个工作只处理一个本人订阅源，删除订阅后尚未执行的工作自动停止。"""
    from ..schemas.integrations import WebhookNotificationInput
    identifier = claim.payload["feed_id"]
    with Session(_engine()) as db:
        row = db.scalar(select(NotificationFeed).where(NotificationFeed.id == identifier, NotificationFeed.user_id == claim.user_id))
        user = db.get(User, claim.user_id)
        if row is None or not user or not user.is_active:
            return {"status": "skipped"}
        url = row.url
    error, count = None, 0
    try:
        web._robots(url, False)
        status, _, data, final_url = web._download(url)
        if final_url != url: web._robots(final_url, False)
        if status != 200: raise ApiProblem("feed_unavailable", "无法读取订阅源。", 502)
        for key, title, text in _entries(data):
            # 内容变化作为新通知入库，重复轮询不会再次付出模型解析成本。
            changed_key = hashlib.sha256((key + "\0" + text).encode()).hexdigest()
            with Session(_engine()) as db:
                if db.get(NotificationFeed, identifier) is None: break
            deliver(claim.user_id, WebhookNotificationInput(source_key=changed_key, title=title, text=text),
                    source_type="feed", namespace="feed:" + identifier)
            count += 1
    except ApiProblem as failure:
        error = failure.code
    except Exception:
        error = "feed_poll_failed"
        current_app.logger.exception("订阅源拉取失败 feed_id=%s", identifier)
    with Session(_engine()) as db, db.begin():
        values = {"error_code": error}
        if error is None: values["last_success_epoch"] = int(time.time())
        db.execute(update(NotificationFeed).where(NotificationFeed.id == identifier, NotificationFeed.user_id == claim.user_id).values(**values))
    return {"status": "failed" if error else "checked", "code": error, "processed": count}
