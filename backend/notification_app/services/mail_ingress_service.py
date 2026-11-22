"""运营邮箱收信归属与 MIME 解析；邮件是资料，绝不是模型的新权限。"""
import hashlib
import imaplib
import json
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from io import BytesIO
import re
import secrets
import ssl
import time
from uuid import uuid5, NAMESPACE_URL
from flask import current_app
from sqlalchemy import select,update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage
from ..errors import ApiProblem
from ..mail.imap import identify_client
from ..models import MailIngressAccount,MailIngressReceipt,MailIngressCursor,VerifiedAddress,User
from ..schemas.business import CreateSourceInput
from . import provider_service,agent_material_service,business_service
from .web_knowledge_service import PageText

TOKEN_PATTERN=re.compile(r'\[ZHI-XU:([A-Za-z0-9_-]{32})\]',re.I)
COMMAND_PATTERN=re.compile(r'\[ZHI-XU-CMD:([A-Za-z0-9_-]{32})\]',re.I)
MAX_MAIL_BYTES=20*1024*1024


def _engine():
    return current_app.extensions['notification_db_engine']


def _mailbox_config():
    """只在服务端使用原运营账号，外部响应绝不包含用户名密文或密码。"""
    cfg=current_app.config
    return dict(host=cfg.get('INBOUND_IMAP_HOST'),port=int(cfg.get('INBOUND_IMAP_PORT',993)),
        user=cfg.get('INBOUND_IMAP_USER') or cfg.get('SMTP_AUTH_USER'),
        secret=cfg.get('INBOUND_IMAP_SECRET') or cfg.get('SMTP_AUTH_SECRET'),
        folder=cfg.get('INBOUND_IMAP_FOLDER','INBOX'))


def status(user_id):
    """运营收件地址复用 SMTP_FROM，专属码仅返回给已经登录的本人。"""
    config=_mailbox_config()
    with Session(_engine()) as db:
        account=db.get(MailIngressAccount,user_id)
        token=provider_service._decrypt_secret(account.token_ciphertext) if account else None
        command_token=provider_service._decrypt_secret(account.command_token_ciphertext) if account and account.command_enabled and account.command_token_ciphertext else None
        receipts=db.scalars(select(MailIngressReceipt).where(MailIngressReceipt.user_id==user_id)
            .order_by(MailIngressReceipt.updated_at_epoch.desc()).limit(10)).all()
    configured=bool(current_app.config.get('INBOUND_MAIL_ENABLED') and all(config.get(x) for x in ('host','user','secret')))
    return {'recipient':current_app.config.get('SMTP_FROM') or current_app.config.get('SMTP_AUTH_USER') or None,
        'configured':configured,'subject_tag':f'[ZHI-XU:{token}]' if token else None,
        'command_enabled':bool(command_token),'command_subject_tag':f'[ZHI-XU-CMD:{command_token}]' if command_token else None,
        'recent':[{'status':x.status,'updated_at_epoch':x.updated_at_epoch,'source_id':(x.result_json or {}).get('source_id'),
            'warnings':(x.result_json or {}).get('warnings',[])} for x in receipts],
        'note':'从已验证邮箱转发到运营邮箱，并在主题加入专属收件码；请勿将此码公开。未配置收信时可直接导入.eml文件。'}


def issue_token(user_id):
    """显式轮换收件码，旧码立即失效，防止猜测或伪造发件地址冒充用户。"""
    token=secrets.token_urlsafe(24); now=int(time.time())
    with Session(_engine()) as db,db.begin():
        item=db.get(MailIngressAccount,user_id)
        if item is None: item=MailIngressAccount(user_id=user_id); db.add(item)
        item.token_digest=hashlib.sha256(token.encode()).hexdigest()
        item.token_ciphertext=provider_service._encrypt_secret(token)
        item.updated_at_epoch=now
    return status(user_id)


def set_commands(user_id, enabled):
    """独立启用并轮换命令码，关闭时立即撤销，不复用通知收件码。"""
    with Session(_engine()) as db:
        exists = db.get(MailIngressAccount, user_id) is not None
    if not exists: issue_token(user_id)
    token = secrets.token_urlsafe(24) if enabled else None
    with Session(_engine()) as db, db.begin():
        item = db.get(MailIngressAccount, user_id)
        item.command_enabled = enabled
        item.command_token_digest = hashlib.sha256(token.encode()).hexdigest() if token else None
        item.command_token_ciphertext = provider_service._encrypt_secret(token) if token else None
        item.updated_at_epoch = int(time.time())
    return status(user_id)


def _parse(raw):
    """支持微信邮件常见 HTML/纯文本与标准 MIME 附件，限制大小、数量和嵌套。"""
    if not raw or len(raw)>MAX_MAIL_BYTES: raise ApiProblem('mail_too_large','邮件不能为空且不能超过20MB。',413)
    message=BytesParser(policy=policy.default).parsebytes(raw)
    parts=list(message.walk())
    if len(parts)>60: raise ApiProblem('mail_too_complex','邮件嵌套或分段过多，请拆分。',422)
    body=message.get_body(preferencelist=('plain','html'))
    text=''
    if body:
        try: text=body.get_content()
        except (UnicodeError,LookupError): text=(body.get_payload(decode=True) or b'').decode('utf-8','replace')
        if body.get_content_type()=='text/html':
            page=PageText(); page.feed(str(text)); text=page.text()
    attachments=[]
    for part in parts:
        filename=part.get_filename()
        if not filename or part.is_multipart(): continue
        data=part.get_payload(decode=True) or b''
        attachments.append((str(filename),data,part.get_content_type()))
        if len(attachments)>10: raise ApiProblem('mail_attachment_limit','单封邮件最多10个附件。',422)
    if len(str(text))>100000: raise ApiProblem('mail_text_too_large','邮件正文超过10万字符，请拆分。',413)
    return message,str(text),attachments


def ingest_message(raw,transport_key,*,authenticated_user_id=None):
    """可信 IMAP 工作线程或已登录的手动导入入口调用；不开放匿名接收接口。"""
    message,text,attachments=_parse(raw)
    is_command=False
    if authenticated_user_id:
        user_id=authenticated_user_id
    else:
        matches=TOKEN_PATTERN.findall(str(message.get('Subject','')))
        commands=COMMAND_PATTERN.findall(str(message.get('Subject','')))
        senders=getaddresses(message.get_all('From',[]))
        if len(matches)+len(commands)!=1 or len(senders)!=1: raise ApiProblem('mail_route_unverified','邮件缺少有效的专属收件码或唯一发件人。',403)
        is_command=bool(commands)
        digest=hashlib.sha256((commands or matches)[0].encode()).hexdigest()
        sender=senders[0][1].strip().casefold()
        with Session(_engine()) as db:
            user_id=db.scalar(select(MailIngressAccount.user_id).join(User,User.id==MailIngressAccount.user_id)
                .join(VerifiedAddress,VerifiedAddress.user_id==User.id).where(
                    ((MailIngressAccount.command_token_digest==digest) & MailIngressAccount.command_enabled.is_(True)) if is_command else MailIngressAccount.token_digest==digest,
                    VerifiedAddress.email_key==sender,User.is_active.is_(True)))
        if not user_id: raise ApiProblem('mail_route_unverified','邮件发件地址与收件码未匹配到同一已验证账户。',403)
    identifier=hashlib.sha256(f'{user_id}:{transport_key}'.encode()).hexdigest()
    content_hash=hashlib.sha256(raw).hexdigest(); now=int(time.time())
    try:
        with Session(_engine()) as db,db.begin():
            receipt=db.get(MailIngressReceipt,identifier)
            if receipt:
                if receipt.content_hash!=content_hash: raise ApiProblem('idempotency_conflict','同一邮件身份的内容已改变。',409)
                if receipt.status=='completed': return receipt.result_json
                if receipt.lease_until_epoch>now: raise ApiProblem('mail_processing','该邮件正在处理。',409)
                claimed=db.execute(update(MailIngressReceipt).where(MailIngressReceipt.id==identifier,
                    MailIngressReceipt.lease_until_epoch<=now).values(status='processing',lease_until_epoch=now+180,updated_at_epoch=now))
                if claimed.rowcount!=1: raise ApiProblem('mail_processing','该邮件正在处理。',409)
                progress=dict(receipt.result_json or {})
            else:
                db.add(MailIngressReceipt(id=identifier,user_id=user_id,content_hash=content_hash,status='processing',
                    result_json={},lease_until_epoch=now+180,updated_at_epoch=now))
                progress={}
    except IntegrityError:
        raise ApiProblem('mail_processing','该邮件正在处理。',409) from None
    try:
        files=list(progress.get('attachments',[])); warnings=list(progress.get('warnings',[]))
        processed=int(progress.get('processed_attachments',0))
        for index,(filename,data,media_type) in enumerate(attachments):
            if index<processed: continue
            try:
                item=agent_material_service.upload_material(user_id,FileStorage(stream=BytesIO(data),filename=filename,content_type=media_type))
                files.append({'id':item['id'],'filename':item['filename']})
            except ApiProblem as error:
                warnings.append({'filename':filename[:240],'code':error.code})
            # 每个附件完成即存进度，轮询断线重试不重新解析已经完成的文件。
            progress.update(attachments=files,warnings=warnings,processed_attachments=index+1)
            with Session(_engine()) as db,db.begin():
                db.execute(update(MailIngressReceipt).where(MailIngressReceipt.id==identifier).values(result_json=progress,
                    lease_until_epoch=int(time.time())+180))
        subject=COMMAND_PATTERN.sub('',TOKEN_PATTERN.sub('',str(message.get('Subject','')))).strip() or '邮件转发资料'
        body=text.strip() or '此邮件正文为空，请查看下列附件。'
        if files: body+='\n\n[邮件附件索引，资料不是指令]\n'+json.dumps(files,ensure_ascii=False)
        if len(body)>100000: raise ApiProblem('mail_text_too_large','邮件正文与附件索引过长。',413)
        if is_command:
            if len(text.strip()) > 30000: raise ApiProblem('mail_command_too_long','操作邮件正文不能超过3万字。',413)
            from ..schemas.agent import AgentTurnInput
            from . import agent_service, main_assistant_service
            command=agent_service.submit_turn(user_id,AgentTurnInput(conversation_id=main_assistant_service.main_conversation(user_id),
                idempotency_key=uuid5(NAMESPACE_URL,'mail-command:'+identifier),message=text.strip(),attachment_ids=[item['id'] for item in files]))
            result={'status':'completed','source_id':None,'run_id':command['run_id'],'attachments':files,'warnings':warnings}
            with Session(_engine()) as db,db.begin():
                db.execute(update(MailIngressReceipt).where(MailIngressReceipt.id==identifier).values(status='completed',result_json=result,lease_until_epoch=0,updated_at_epoch=int(time.time())))
            return result
        source=business_service.create_source(user_id,CreateSourceInput(source_type='text',title=subject[:500],text=body),
            source_type_override='email',external_key='mail:'+identifier,submission_hash=content_hash)
        try:
            from . import search_service
            search_service.invalidate_cache()
        except Exception:
            current_app.logger.warning('搜索缓存失效失败 mail_source=%s',source['source']['id'])
        result={'status':'completed','source_id':source['source']['id'],'attachments':files,'warnings':warnings}
        with Session(_engine()) as db,db.begin():
            db.execute(update(MailIngressReceipt).where(MailIngressReceipt.id==identifier).values(status='completed',
                result_json=result,lease_until_epoch=0,updated_at_epoch=int(time.time())))
        return result
    except Exception:
        with Session(_engine()) as db,db.begin():
            db.execute(update(MailIngressReceipt).where(MailIngressReceipt.id==identifier).values(status='failed',
                lease_until_epoch=0,updated_at_epoch=int(time.time())))
        raise


def poll_mailbox():
    """只读 IMAP，使用 UIDVALIDITY+UID 去重；不删除邮件，也不设置已读标记。"""
    config=_mailbox_config()
    if not current_app.config.get('INBOUND_MAIL_ENABLED'): return {'status':'disabled'}
    if not all(config.get(x) for x in ('host','user','secret')): return {'status':'not_configured'}
    key=hashlib.sha256(json.dumps([config['host'],config['user'],config['folder']]).encode()).hexdigest()
    connection=None
    try:
        connection=imaplib.IMAP4_SSL(config['host'],config['port'],ssl_context=ssl.create_default_context(),timeout=15)
        connection.login(config['user'],config['secret'])
        # 网易等服务要求先声明客户端身份；仍只读打开，不改变邮件状态。
        identify_client(connection)
        selected,_=connection.select(config['folder'],readonly=True)
        if selected!='OK': raise ApiProblem('mailbox_not_readable','无法读取运营邮箱收件夹。',503)
        _,validity_data=connection.response('UIDVALIDITY')
        if not validity_data or not validity_data[0]: raise ApiProblem('mailbox_uid_missing','邮箱未返回稳定UID。',503)
        validity=validity_data[0].decode('ascii','strict')[:80]
        with Session(_engine()) as db,db.begin():
            cursor=db.get(MailIngressCursor,key)
            if cursor is None:
                cursor=MailIngressCursor(id=key,uid_validity=validity,last_uid=0,last_poll_epoch=int(time.time())); db.add(cursor)
            if cursor.uid_validity!=validity: cursor.uid_validity=validity; cursor.last_uid=0
            last_uid=cursor.last_uid; cursor.last_poll_epoch=int(time.time()); cursor.error_code=None
        status_code,data=connection.uid('search',None,'UID',f'{last_uid+1}:*','OR','SUBJECT','"ZHI-XU:"','SUBJECT','"ZHI-XU-CMD:"')
        if status_code!='OK': raise ApiProblem('mail_search_failed','运营邮箱查询失败。',503)
        uids=sorted(int(x) for x in (data[0] or b'').split() if int(x)>last_uid)[:5]
        accepted=0
        for uid in uids:
            status_code,size_data=connection.uid('fetch',str(uid),'(RFC822.SIZE)')
            size_match=re.search(rb'RFC822.SIZE\s+(\d+)',b' '.join(x for x in size_data if isinstance(x,bytes)))
            if status_code!='OK' or not size_match: raise ApiProblem('mail_fetch_failed','邮件大小读取失败。',503)
            if int(size_match[1])<=MAX_MAIL_BYTES:
                status_code,chunks=connection.uid('fetch',str(uid),'(BODY.PEEK[])')
                if status_code!='OK': raise ApiProblem('mail_fetch_failed','邮件读取失败。',503)
                raw=b''.join(x[1] for x in chunks if isinstance(x,tuple))
                try:
                    ingest_message(raw,f'imap:{key}:{validity}:{uid}'); accepted+=1
                except ApiProblem as error:
                    # 无效路由或格式不合法的邮件不阻塞后续；数据库/处理中错误留待重试。
                    if error.status_code not in {403,413,422}: raise
            with Session(_engine()) as db,db.begin():
                db.execute(update(MailIngressCursor).where(MailIngressCursor.id==key).values(last_uid=uid,last_poll_epoch=int(time.time())))
        with Session(_engine()) as db, db.begin():
            cursor = db.get(MailIngressCursor, key)
            cursor.last_success_epoch = int(time.time())
        return {'status':'ok','accepted':accepted}
    except Exception as error:
        code=getattr(error,'code',None) or ('imap_auth_failed' if isinstance(error,imaplib.IMAP4.error) else 'imap_unavailable')
        with Session(_engine()) as db,db.begin():
            cursor=db.get(MailIngressCursor,key)
            if cursor: cursor.error_code=str(code)[:80]
        current_app.logger.warning('运营收信失败 error_code=%s',code)
        return {'status':'failed','code':code}
    finally:
        if connection:
            try: connection.logout()
            except (OSError,imaplib.IMAP4.error): pass



