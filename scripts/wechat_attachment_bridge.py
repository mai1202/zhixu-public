"""为已核实的 wechat-cli 0.2.4 提供精确附件索引，不采用模糊媒体路径。"""
from datetime import datetime
from hashlib import md5, sha256
import importlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

MAX_BYTES = 10 * 1024 * 1024


def resolve_attachment(*, content, local_type, timestamp, chat_id, db_dir, identity):
    """只按原生消息类型、XML 标识和账户目录定位，普通聊天正文不参与路径解析。"""
    kind = 'image' if local_type & 0xFFFFFFFF == 3 else 'file'
    if local_type & 0xFFFFFFFF not in {3, 49}:
        return None
    if not isinstance(content, str) or len(content) > 20000 or re.search(r'<!DOCTYPE|<!ENTITY', content, re.I):
        return None
    try:
        xml = ET.fromstring(content[content.index('<'):])
    except (ValueError, ET.ParseError):
        return None
    app = xml.find('.//appmsg')
    if kind == 'file' and (app is None or app.findtext('type') != '6'):
        return None
    key = sha256(json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()
    result = {'attachment_key': key, 'kind': kind, 'filename': '图片.jpg' if kind == 'image' else '附件',
              'path': None, 'content_md5': None, 'unavailable_reason': 'identity_unavailable'}
    month = datetime.fromtimestamp(timestamp).strftime('%Y-%m')
    account = Path(db_dir).parent.resolve()
    result['storage_root'] = str(account / 'msg')
    if kind == 'file':
        title = (app.findtext('title') or '').strip()
        if not title or '/' in title or '\\' in title or ':' in title or len(title) > 240:
            result['unavailable_reason'] = 'path_rejected'
            return result
        result['filename'] = title
        digest = next((node.text.strip().lower() for node in app.iter()
                       if node.tag.lower() in {'md5', 'filemd5'} and node.text and re.fullmatch(r'[0-9a-fA-F]{32}', node.text.strip())), None)
        candidates = [account / 'msg' / 'file' / month / title]
    else:
        image = xml if xml.tag == 'img' else xml.find('.//img')
        digest = (image.get('md5') or '').lower() if image is not None else ''
        image_dir = account / 'msg' / 'attach' / md5(chat_id.encode('utf-8')).hexdigest() / month / 'Img'
        candidates = [image_dir / f'{digest}{suffix}' for suffix in ('.dat', '.jpg', '.jpeg', '.png')]
    if not digest or not re.fullmatch(r'[0-9a-f]{32}', digest):
        return result
    result['content_md5'] = digest
    # 仅保存在本机加密恢复索引：文件稍后下载时仍能按精确候选重新核验，不扩大文本回看范围。
    result['candidate_paths'] = [str(candidate) for candidate in candidates]
    for candidate in candidates:
        path = candidate.resolve()
        # 拒绝目录跳转与符号链接逃逸；不回退到其他群或相似文件名。
        if not path.is_relative_to(account / 'msg'):
            result['unavailable_reason'] = 'path_rejected'
            return result
        if path.is_file():
            if path.stat().st_size > MAX_BYTES:
                result['unavailable_reason'] = 'too_large'
                return result
            result.update(path=str(path), unavailable_reason=None)
            return result
    result['unavailable_reason'] = 'not_downloaded'
    return result


class AttachmentLine(str):
    """保留原字符串排序和分页语义，附加的本地索引不会混进聊天正文。"""
    def __new__(cls, value, attachment, is_self=None):
        item = super().__new__(cls, value)
        item.attachment = attachment
        item.is_self = is_self
        return item


def run(args, collect_attachments=True):
    """只在本进程包装已核实函数，不修改安装目录或读取未指定的聊天。"""
    from wechat_cli.main import cli
    core = importlib.import_module('wechat_cli.core.messages')
    history = importlib.import_module('wechat_cli.commands.history')
    from click import get_current_context
    from wechat_cli.core.contacts import get_self_username
    original_builder, original_output = core._build_history_line, history.output

    def build(row, ctx, names, id_to_username, display_name_fn, resolve_media=False, db_dir=None):
        stamp, line = original_builder(row, ctx, names, id_to_username, display_name_fn, resolve_media=False, db_dir=db_dir)
        local_id, local_type, timestamp, _sender, content, compression = row
        content = core.decompress_content(content, compression)
        attachment = resolve_attachment(content=content, local_type=local_type, timestamp=timestamp,
            chat_id=ctx['username'], db_dir=db_dir,
            identity=[ctx.get('db_path'), ctx.get('table_name'), local_id, timestamp]) if db_dir and collect_attachments else None
        # 使用原生稳定账号 ID 比对本人身份，不按昵称或正文中的“我”推断。
        click_context = get_current_context(silent=True)
        app = click_context.obj if click_context is not None else None
        own_id = get_self_username(db_dir, app.cache, app.decrypted_dir) if app is not None and db_dir else ''
        content_sender, _text = core._parse_message_content(content, local_type, ctx.get('is_group', False))
        sender_id = id_to_username.get(_sender) or content_sender
        is_self = sender_id == own_id if sender_id and own_id else None
        return stamp, AttachmentLine(line, attachment, is_self)

    def output(payload, fmt):
        if isinstance(payload, dict) and isinstance(payload.get('messages'), list):
            payload = {**payload, 'attachment_records': [getattr(line, 'attachment', None) for line in payload['messages']],
                       'message_metadata': [{'is_self': getattr(line, 'is_self', None)} for line in payload['messages']]}
        return original_output(payload, fmt)

    core._build_history_line, history.output = build, output
    try:
        cli(args=args)
    finally:
        core._build_history_line, history.output = original_builder, original_output
