"""测试账号复用正式会话、隐私告知及数据库限流；密码只保存加盐摘要。"""
import re
import time
from uuid import uuid4
from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash, check_password_hash
from ..errors import ApiProblem
from ..models import TestAccountAccess, User
from . import auth_service
from .rate_limit import _increment_bucket

HASH_METHOD = 'scrypt:16384:8:1'
DUMMY_HASH = generate_password_hash('not-an-account-password', method=HASH_METHOD)


def provision(email, password, *, daily_budget_micros=4_000_000):
    """只由维护端执行；不能给已有普通用户额外开通密码，也不赋管理员权限。"""
    email = email.strip().lower()
    if not re.fullmatch(r'[a-z0-9._+-]+@[a-z0-9.-]+\.[a-z]{2,}', email) or not 12 <= len(password) <= 256:
        raise ValueError('测试登录地址或密码格式不正确')
    if type(daily_budget_micros) is not int or not 1 <= daily_budget_micros <= 1_000_000_000:
        raise ValueError('测试预算超出范围')
    engine = current_app.extensions['notification_db_engine']
    now = int(time.time())
    with Session(engine) as db, db.begin():
        user = db.scalar(select(User).where(User.email_key == email))
        created = user is None
        if created:
            user = User(id=str(uuid4()), primary_email=email, email_key=email, is_active=True, is_admin=False,
                verified_at_epoch=now, created_at_epoch=now)
            db.add(user); db.flush()
        access = db.get(TestAccountAccess, user.id)
        if access is None and not created:
            raise ApiProblem('account_exists', '此地址已有普通账号，不能改为测试密码账号。', 409)
        if access is None:
            access = TestAccountAccess(user_id=user.id, created_at_epoch=now)
            db.add(access)
        access.password_hash = generate_password_hash(password, method=HASH_METHOD)
        access.enabled, access.daily_budget_micros = True, daily_budget_micros
        # 不创建 VerifiedAddress；这是管理员预置的登录标识，不是假称已经验证过的收件邮箱。
        db.query(auth_service.WebSession).filter_by(user_id=user.id).update({'revoked_at_epoch': now})
        return user.id


def login(payload, client_ip):
    if not current_app.config.get('TEST_PASSWORD_LOGIN_ENABLED', False):
        raise ApiProblem('password_login_disabled', '当前未开放测试账号密码登录。', 404)
    from .privacy_service import POLICY_VERSION
    if current_app.config.get('PRIVACY_CONSENT_REQUIRED', True) and payload.policy_version != POLICY_VERSION:
        raise ApiProblem('privacy_consent_required', '请先阅读并同意资料处理告知。', 422)
    email = payload.email.strip().lower()
    engine = current_app.extensions['notification_db_engine']
    now = int(time.time())
    # 限流在查找账户之前执行，未知地址也计数；只保存身份的HMAC，不保存密码。
    with Session(engine) as db, db.begin():
        counts = [_increment_bucket(db, auth_service._keyed_digest('password-login-' + scope, value), now, 900)[0]
            for scope, value in (('email', email), ('ip', client_ip or 'unknown'))]
    if counts[0] > 10 or counts[1] > 50:
        raise ApiProblem('password_login_rate_limited', '密码尝试次数过多，请15分钟后重试。', 429, retry_after=900)
    with Session(engine) as db:
        row = db.execute(select(User, TestAccountAccess).join(TestAccountAccess, User.id == TestAccountAccess.user_id)
            .where(User.email_key == email)).first()
        digest = row[1].password_hash if row else DUMMY_HASH
        valid = check_password_hash(digest, payload.password)
        if not row or not valid or not row[0].is_active or not row[1].enabled:
            raise ApiProblem('invalid_credentials', '邮箱或密码不正确。', 401)
        return auth_service._create_web_session(db, email, email, auth_service._auth_key(), now, policy_version=payload.policy_version)
