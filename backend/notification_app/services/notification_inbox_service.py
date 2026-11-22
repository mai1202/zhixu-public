"""以原文为单位的通知邮箱，不依赖候选是否有日期或已确认事务。"""

import time
import re
import json
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from flask import current_app
from ..errors import ApiProblem
from ..models import SourceDocument, NotificationOrganization, ChangeSet, Notice, SyncItem
from ..schemas.assistant_workflow import OrganizationInput, MailboxInput
from ..text_sanitize import clean_display_text, clean_title


def _engine():
    return current_app.extensions['notification_db_engine']


def _owned(db, user_id, source_id):
    source = db.scalar(select(SourceDocument).where(SourceDocument.id == source_id, SourceDocument.user_id == user_id))
    if source is None:
        raise ApiProblem('not_found', '未找到该通知。', 404)
    return source


def _default_mailbox(source):
    if source.archived_at_epoch:
        return 'archived'
    return 'inbox'


def _state(db, source):
    """旧通知首次操作时补齐邮箱状态，不改动候选和原文。"""
    # 用户打开原文与 Agent 首次整理可能同时到达，原子补齐避免首次插入互相冲突。
    insert = sqlite_insert if db.get_bind().dialect.name == 'sqlite' else postgresql_insert
    db.execute(insert(NotificationOrganization).values(source_id=source.id, user_id=source.user_id,
        mailbox=_default_mailbox(source), version=1, updated_at_epoch=int(time.time()))
        .on_conflict_do_nothing(index_elements=['source_id']))
    return db.get(NotificationOrganization, source.id, populate_existing=True, with_for_update=True)


def _canonical(db, user_id, source_id):
    """成员深链接解析到阅读入口，读取原始证据仍使用各自来源 ID。"""
    source = _owned(db, user_id, source_id)
    item = db.get(NotificationOrganization, source.id)
    return _owned(db, user_id, item.group_source_id) if item and item.group_source_id else source


def _members(db, user_id, root_id):
    """归组指针保持一层，避免递归和历史链接失效。"""
    return db.scalars(select(SourceDocument).outerjoin(NotificationOrganization,
        NotificationOrganization.source_id == SourceDocument.id).where(SourceDocument.user_id == user_id,
        (SourceDocument.id == root_id) | (NotificationOrganization.group_source_id == root_id))
        .order_by(SourceDocument.created_at_epoch, SourceDocument.id)).all()


def _public(source, organization, source_count=1, latest_at=None):
    title = organization.title if organization and organization.title else source.title
    return dict(id=source.id, source_id=source.id, source_count=source_count, title=clean_title(title, '未命名通知'),
        summary=clean_display_text(organization.summary, limit=4000) if organization else None,
        advice=clean_display_text(organization.advice, limit=4000) if organization else None,
        organized=bool(organization and organization.title),
        mailbox=organization.mailbox if organization else _default_mailbox(source),
        read=bool(organization and organization.read_at_epoch),
        version=organization.version if organization else 1,
        created_at_epoch=latest_at if latest_at is not None else source.created_at_epoch, source_type=source.source_type)


def list_messages(user_id, mailbox='inbox', query='', offset=0, limit=50):
    """收件箱包含已读和未读，忽略和归档互不影响原业务事项。"""
    if mailbox not in {'inbox', 'archived', 'ignored'}:
        raise ApiProblem('invalid_mailbox', '通知分组无效。', 422)
    fallback = case((SourceDocument.archived_at_epoch.is_not(None), 'archived'),
        else_='inbox')
    # 在数据库聚合数量和最新来源时间，避免逐卡加载整组正文及漏看后续补充。
    group_key = func.coalesce(NotificationOrganization.group_source_id, SourceDocument.id)
    stats = select(group_key.label('root_id'), func.count().label('total'),
        func.max(SourceDocument.created_at_epoch).label('latest_at')).select_from(SourceDocument).outerjoin(
        NotificationOrganization, NotificationOrganization.source_id == SourceDocument.id).where(
        SourceDocument.user_id == user_id).group_by(group_key).subquery()
    statement = select(SourceDocument, NotificationOrganization, stats.c.total, stats.c.latest_at).outerjoin(NotificationOrganization,
        NotificationOrganization.source_id == SourceDocument.id).join(stats, stats.c.root_id == SourceDocument.id).where(SourceDocument.user_id == user_id,
        func.coalesce(NotificationOrganization.mailbox, fallback) == mailbox,
        NotificationOrganization.group_source_id.is_(None))
    if query.strip():
        needle = query.strip()[:200]
        # 搜索任一成员都能找到汇总入口，不能因折叠而漏掉补充要求。
        member, organization = aliased(SourceDocument), aliased(NotificationOrganization)
        matching_roots = select(func.coalesce(organization.group_source_id, member.id)).select_from(member).outerjoin(
            organization, organization.source_id == member.id).where(member.user_id == user_id,
            member.original_text.contains(needle, autoescape=True) | member.title.contains(needle, autoescape=True))
        statement = statement.where(SourceDocument.id.in_(matching_roots)
            | SourceDocument.original_text.contains(needle, autoescape=True)
            | SourceDocument.title.contains(needle, autoescape=True)
            | NotificationOrganization.title.contains(needle, autoescape=True)
            | NotificationOrganization.summary.contains(needle, autoescape=True))
    with Session(_engine()) as db:
        rows = db.execute(statement.order_by(stats.c.latest_at.desc(), SourceDocument.id)
            .offset(offset).limit(limit + 1)).all()
        items = [_public(source, item, total, latest_at) for source, item, total, latest_at in rows[:limit]]
    return {'items': items, 'has_more': len(rows) > limit}


def mailbox_counts(user_id):
    """按阅读入口根来源统计三个邮箱及收件箱未读数，不随搜索词变化。"""
    fallback = case((SourceDocument.archived_at_epoch.is_not(None), 'archived'),
        else_='inbox')
    group_key = func.coalesce(NotificationOrganization.group_source_id, SourceDocument.id)
    stats = select(group_key.label('root_id')).select_from(SourceDocument).outerjoin(
        NotificationOrganization, NotificationOrganization.source_id == SourceDocument.id).where(
        SourceDocument.user_id == user_id).group_by(group_key).subquery()
    mailbox = func.coalesce(NotificationOrganization.mailbox, fallback)
    with Session(_engine()) as db:
        rows = db.execute(select(mailbox.label('mailbox'), func.count().label('total')).select_from(
            SourceDocument).outerjoin(NotificationOrganization,
            NotificationOrganization.source_id == SourceDocument.id).join(
            stats, stats.c.root_id == SourceDocument.id).where(
            SourceDocument.user_id == user_id,
            NotificationOrganization.group_source_id.is_(None)).group_by(mailbox)).all()
        counts = {'inbox': 0, 'archived': 0, 'ignored': 0, 'unread': 0}
        for row in rows:
            if row.mailbox in counts:
                counts[row.mailbox] = int(row.total)
        counts['unread'] = int(db.scalar(
            select(func.count()).select_from(SourceDocument).outerjoin(
                NotificationOrganization,
                NotificationOrganization.source_id == SourceDocument.id).join(
                stats, stats.c.root_id == SourceDocument.id).where(
                SourceDocument.user_id == user_id,
                NotificationOrganization.group_source_id.is_(None),
                mailbox == 'inbox',
                NotificationOrganization.read_at_epoch.is_(None))
        ) or 0)
    return counts


def get_message(user_id, source_id):
    """原文直接可读，审计与结构化事项作为次级详情按需展开。"""
    with Session(_engine()) as db:
        source = _canonical(db, user_id, source_id)
        members = _members(db, user_id, source.id)
        result = _public(source, db.get(NotificationOrganization, source.id), len(members))
        result['sources'] = [{'id': part.id, 'title': clean_title(part.title, '通知原文'), 'original_text': part.original_text,
            'created_at_epoch': part.created_at_epoch} for part in members]
        change = db.scalar(select(ChangeSet).where(ChangeSet.source_document_id == source.id,
            ChangeSet.user_id == user_id).order_by(ChangeSet.created_at_epoch.desc()).limit(1))
        result.update(original_text=source.original_text, original_title=clean_title(source.title, '通知原文'),
            change_set={'id': change.id, 'summary': change.summary} if change else None)
        # 邮箱只是阅读入口；提供同源业务通知索引，让界面复用既有证据与三端关系详情。
        notices = db.scalars(select(Notice).where(Notice.user_id == user_id,
            Notice.source_document_id.in_([part.id for part in members])).order_by(Notice.created_at_epoch, Notice.id).limit(51)).all()
        result['notices'] = [{'id': item.id, 'title': clean_title(item.title), 'status': item.status} for item in notices[:50]]
        result['notices_has_more'] = len(notices) > 50
    return result


def update_mailbox(user_id, source_id, payload: MailboxInput):
    """已读标记和邮箱移动均按账户及版本校验，不删除原始内容。"""
    with Session(_engine()) as db, db.begin():
        source = _canonical(db, user_id, source_id)
        item = _state(db, source)
        if payload.expected_version is not None and item.version != payload.expected_version:
            raise ApiProblem('version_conflict', '通知状态已变化，请刷新。', 409)
        if payload.mailbox is not None:
            item.mailbox = payload.mailbox
        if payload.read is not None:
            item.read_at_epoch = int(time.time()) if payload.read else None
        item.version += 1
        item.updated_at_epoch = int(time.time())
        db.flush()
        return _public(source, item, len(_members(db, user_id, source.id)))


def organize(user_id, source_id, payload: OrganizationInput):
    """仅保存简短标题、备注摘要和建议；缺日期不影响通知整理。"""
    with Session(_engine()) as db, db.begin():
        source = _canonical(db, user_id, source_id)
        item = _state(db, source)
        if payload.expected_version is not None and item.version != payload.expected_version:
            raise ApiProblem('version_conflict', '通知摘要已变化，请重新读取后整理。', 409)
        members = {part.id: part for part in _members(db, user_id, source.id)}
        for entry in payload.evidence:
            part = members.get(str(entry.source_id))
            if part is None or len(entry.quote.strip()) < 2 or entry.quote.strip() not in part.original_text:
                raise ApiProblem('invalid_evidence', '摘要依据必须来自本组真实原文。', 422)
        item.title = clean_title(payload.title, '')
        item.summary = clean_display_text(payload.summary, limit=4000)
        item.advice = clean_display_text(payload.advice, limit=4000)
        if not item.title or not item.summary:
            raise ApiProblem('invalid_summary', '整理标题和摘要不能为空。', 422)
        item.version += 1
        item.updated_at_epoch = int(time.time())
        db.flush()
        return _public(source, item, len(_members(db, user_id, source.id)))


def source_for_notice(user_id, notice_id):
    """兼容历史通知详情链接，统一落到原文邮箱详情。"""
    with Session(_engine()) as db:
        notice = db.scalar(select(Notice).where(Notice.id == notice_id, Notice.user_id == user_id))
        if notice is None:
            raise ApiProblem('not_found', '未找到该通知。', 404)
        return notice.source_document_id


def group_messages(user_id, payload, *, anchor_id=None):
    """原子归组，保留原文；后台必须包含触发来源且尊重用户拆分保护。"""
    identifiers = {str(value) for value in payload.source_ids}
    evidence = {str(value.source_id): value for value in payload.evidence}
    if len(identifiers) < 2 or set(evidence) != identifiers or len(evidence) != len(payload.evidence):
        raise ApiProblem('invalid_group', '每条来源必须提供唯一的归组证据。', 422)
    if anchor_id is not None and anchor_id not in identifiers:
        raise ApiProblem('notification_scope', '后台归组必须包含当前触发来源。', 403)
    with Session(_engine()) as db, db.begin():
        sources = [_owned(db, user_id, key) for key in sorted(identifiers)]
        # 摘录仅证明资料存在，关联语义仍由 Agent 判断并记录理由供用户核对。
        for source in sources:
            if len(evidence[source.id].quote.strip()) < 2 or evidence[source.id].quote.strip() not in source.original_text:
                raise ApiProblem('invalid_evidence', '归组摘录必须存在于对应原文。', 422)
        roots = {_canonical(db, user_id, source.id).id for source in sources}
        if set(payload.expected_versions) != roots:
            raise ApiProblem('version_conflict', '请先读取所有归组入口及其版本。', 409)
        # 统一锁顺序防止并发归组互相覆盖，SQLite 首次写入同时取得写锁。
        states = {key: _state(db, _owned(db, user_id, key)) for key in sorted(roots)}
        if any(states[key].version != payload.expected_versions[key] or states[key].group_source_id for key in roots):
            raise ApiProblem('version_conflict', '归组已发生变化，请重新查证。', 409)
        members = {part.id: part for key in roots for part in _members(db, user_id, key)}
        if len(members) > 100:
            raise ApiProblem('group_too_large', '同一归组最多100条来源，请核对是否混入独立事项。', 422)
        member_states = {key: _state(db, members[key]) for key in sorted(members)}
        if anchor_id is not None:
            if any(item.grouping_locked for item in member_states.values()):
                raise ApiProblem('grouping_locked', '用户已拆分保护该来源，后台不能重新合并。', 409)
            if len({item.mailbox for item in states.values()}) != 1:
                raise ApiProblem('mailbox_conflict', '后台不能合并不同邮箱状态的通知。', 409)
        root = min((members[key] for key in roots), key=lambda part: (part.created_at_epoch, part.id))
        now = int(time.time())
        for key, item in member_states.items():
            item.group_source_id = None if key == root.id else root.id
            if anchor_id is None:
                # 用户对话明确要求重新合并时，可以解除此前人工拆分保护。
                item.grouping_locked = 0
            if key in evidence:
                item.group_reason = json.dumps({'reason': payload.reason, 'quote': evidence[key].quote,
                    'relation': evidence[key].relation}, ensure_ascii=False)
            item.version += 1
            item.updated_at_epoch = now
        # 新补充重新置为未读；摘要由后续 organize_notification 依据整组证据更新。
        member_states[root.id].read_at_epoch = None
        if len(roots) > 1:
            # 等待整组摘要时不沿用可能已被修订的旧结论。
            member_states[root.id].title = member_states[root.id].summary = member_states[root.id].advice = None
        db.flush()
        return _public(root, member_states[root.id], len(members))


def detach_message(user_id, root_id, source_id, expected_version):
    """拆开误合并并保护该来源，恢复独立阅读后按原文重新整理摘要。"""
    with Session(_engine()) as db, db.begin():
        root = _canonical(db, user_id, root_id)
        state = _state(db, root)
        if root.id != root_id or state.version != expected_version:
            raise ApiProblem('version_conflict', '通知已变化，请刷新后拆分。', 409)
        members = _members(db, user_id, root.id)
        if source_id not in {part.id for part in members} or len(members) < 2:
            raise ApiProblem('invalid_group', '只能拆分当前归组中的来源。', 422)
        # 拆分入口来源时为剩余成员平移指针，所有历史来源链接仍然有效。
        remaining = [part for part in members if part.id != source_id]
        replacement = next((part for part in remaining if part.id == root.id), remaining[0])
        for part in members:
            item = _state(db, part)
            item.group_source_id = None if part.id in {source_id, replacement.id} else replacement.id
            item.version += 1
            item.updated_at_epoch = int(time.time())
            item.read_at_epoch = None
            # 原汇总可能包含移出的要求，宁可等待重整也不能展示错误汇总。
            item.title = item.summary = item.advice = None
            if part.id == source_id:
                item.grouping_locked = 1
                item.group_reason = None
        db.flush()
        return {'detached_source_id': source_id, 'remaining_source_id': replacement.id}


def notification_context(user_id, source_id, query=''):
    """为后台提供同群邻文、相同链接与关键词候选；这些信号均不直接决定归组。"""
    current = get_message(user_id, source_id)
    # 上下文响应有界；成员索引用于继续分页查证，列表详情仍保留完整原文。
    current['source_ids'] = [part['id'] for part in current['sources']]
    current['sources_has_more'] = len(current['sources']) > 8
    current['sources'] = [{**part, 'original_text': part['original_text'][:2000],
        'truncated': len(part['original_text']) > 2000} for part in current['sources'][:8]]
    current['original_text'] = current['original_text'][:2000]
    with Session(_engine()) as db:
        source = _owned(db, user_id, source_id)
        anchor = db.scalar(select(SyncItem).where(SyncItem.user_id == user_id,
            SyncItem.source_document_id == source_id).order_by(SyncItem.created_at_epoch.desc()).limit(1))
        chats = select(SyncItem.chat_id).where(SyncItem.user_id == user_id,
            SyncItem.source_document_id == source_id, SyncItem.chat_id.is_not(None))
        neighbors = db.scalars(select(SyncItem.source_document_id).where(SyncItem.user_id == user_id,
            SyncItem.chat_id.in_(chats), SyncItem.source_document_id.is_not(None))
            .order_by(func.abs(SyncItem.created_at_epoch - source.created_at_epoch),
                case((SyncItem.batch_id == anchor.batch_id, func.abs(SyncItem.ordinal - anchor.ordinal)), else_=1000000) if anchor is not None else SyncItem.ordinal)
            .limit(16)).all()
        urls = set(re.findall(r'https?://[^\s<>"，。]+', source.original_text))
        statement = select(SourceDocument).where(SourceDocument.user_id == user_id,
            SourceDocument.id != source_id)
        signals = [SourceDocument.id.in_(neighbors)]
        signals.extend(SourceDocument.original_text.contains(url, autoescape=True) for url in sorted(urls)[:5])
        if query.strip():
            signals.append(SourceDocument.original_text.contains(query.strip()[:100], autoescape=True))
        from sqlalchemy import or_
        candidates = db.scalars(statement.where(or_(*signals))
            .order_by(SourceDocument.created_at_epoch.desc(), SourceDocument.id).limit(21)).all()
        result = []
        # 总量和单条上限同时约束，截断明确告知，Agent 可再读原文工具继续查证。
        budget = 20000
        for part in candidates[:20]:
            canonical = _canonical(db, user_id, part.id)
            item = db.get(NotificationOrganization, canonical.id)
            excerpt = part.original_text[:min(2000, budget)]
            if not excerpt:
                break
            budget -= len(excerpt)
            result.append({'source_id': part.id, 'group_id': canonical.id,
                'version': item.version if item else 1, 'title': part.title,
                'text': excerpt, 'truncated': len(excerpt) < len(part.original_text),
                'same_chat': part.id in neighbors, 'shared_url': bool(urls.intersection(
                    re.findall(r'https?://[^\s<>"，。]+', part.original_text))),
                'grouping_locked': bool(item and item.grouping_locked),
                'mailbox': item.mailbox if item else _default_mailbox(canonical)})
        return {'current': current, 'candidates': result,
            'has_more': len(candidates) > len(result),
            'guidance': '候选只是检索线索。读原文确认具体事项、届次、受众及关系；同群、时间接近或通用链接不能单独证明应合并。'}
