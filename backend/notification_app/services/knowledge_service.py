"""联合检索现有私人资料与显式知识文档，避免复制或公开个人数据。"""
from datetime import date
import hashlib
import json
import re
import time
from uuid import uuid5, NAMESPACE_URL
from flask import current_app
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from ..errors import ApiProblem
from ..models import (KnowledgeDocument, User, AgentMemory, AgentTurn, AgentAttachment,
                      SourceDocument, NotificationOrganization, OcrArtifact)
from ..schemas.knowledge import KnowledgeInput

KINDS = {'document','notification','memory','conversation','attachment'}


def _engine():
    return current_app.extensions['notification_db_engine']


def _admin(db,user_id):
    user = db.get(User,user_id)
    return bool(user and user.is_active and user.is_admin)


def _terms(query):
    """首版用中文双字词和完整关键词召回，不谎称已接入语义向量库。"""
    words = re.findall(r'[a-zA-Z0-9_]+|[\u4e00-\u9fff]+',query.casefold())
    terms = []
    for word in words[:8]:
        terms.append(word)
        if re.fullmatch(r'[\u4e00-\u9fff]{3,}',word):
            terms.extend(word[index:index+2] for index in range(min(len(word)-1,12)))
    return list(dict.fromkeys(terms))[:24]


def _matched(columns,terms):
    return or_(*(column.contains(term,autoescape=True) for column in columns for term in terms))


def _entry(kind,identifier,title,text,scope,updated,version=1,source_url=None,href=None,fingerprint=None,metadata=None):
    return dict(kind=kind,id=identifier,title=title,scope=scope,updated_at_epoch=updated,
        version=version,source_url=source_url,href=href,content=text,metadata=metadata or {},
        fingerprint=fingerprint or hashlib.sha256(text.encode()).hexdigest())


def _rows(user_id,query,scope,kind,per_kind=100,watch=False):
    """所有 SQL 先过滤 owner；公共库只包含被明确发布的 document。"""
    if scope not in {'all','private','public'} or kind not in KINDS | {'all'}:
        raise ApiProblem('invalid_knowledge_filter','资料范围无效。',422)
    terms = _terms(query)
    output,limited = [],False
    with Session(_engine()) as db:
        def take(model,statement,columns,order):
            nonlocal limited
            if terms: statement = statement.where(_matched(columns,terms))
            rows = db.scalars(statement.order_by(order.desc(),model.id).limit(per_kind+1)).all()
            limited = limited or len(rows)>per_kind
            return rows[:per_kind]
        if kind in {'all','document'}:
            visible = (KnowledgeDocument.scope=='public') if scope=='public' else (
                (KnowledgeDocument.scope=='private') & (KnowledgeDocument.user_id==user_id))
            if scope=='all': visible = (KnowledgeDocument.scope=='public') | ((KnowledgeDocument.scope=='private') & (KnowledgeDocument.user_id==user_id))
            docs = take(KnowledgeDocument,select(KnowledgeDocument).where(visible,KnowledgeDocument.status=='active'),
                [KnowledgeDocument.title,KnowledgeDocument.content],KnowledgeDocument.updated_at_epoch)
            output.extend(_entry('document',x.id,x.title,x.content,x.scope,x.updated_at_epoch,x.version,x.source_url,
                fingerprint=x.content_hash,metadata=x.metadata_json) for x in docs)
        if scope!='public':
            if kind in {'all','notification'}:
                sources = take(SourceDocument,select(SourceDocument).where(SourceDocument.user_id==user_id),
                    [SourceDocument.title,SourceDocument.original_text],SourceDocument.created_at_epoch)
                for x in sources:
                    summary=db.get(NotificationOrganization,x.id)
                    output.append(_entry('notification',x.id,summary.title if summary and summary.title else x.title,
                        x.original_text,'private',x.created_at_epoch,source_url=None,href=f'/inbox?source={x.id}',fingerprint=x.content_hash))
            if not watch and kind in {'all','memory'}:
                memories=take(AgentMemory,select(AgentMemory).where(AgentMemory.user_id==user_id),
                    [AgentMemory.content],AgentMemory.updated_at_epoch)
                output.extend(_entry('memory',x.id,'长期记忆 · '+x.category,x.content,'private',x.updated_at_epoch,
                    href='/agent/settings') for x in memories)
            if not watch and kind in {'all','conversation'}:
                turns=take(AgentTurn,select(AgentTurn).where(AgentTurn.user_id==user_id),[AgentTurn.content],AgentTurn.created_at_epoch)
                output.extend(_entry('conversation',x.id,('你' if x.role=='user' else '助手')+'的对话记录',x.content,'private',x.created_at_epoch,
                    href=f'/agent?conversation={x.conversation_id}') for x in turns)
            if kind in {'all','attachment'}:
                # 有解析文本的文件参与全文召回，图片可通过文件名查到并在详情读取 OCR。
                files=take(AgentAttachment,select(AgentAttachment).where(AgentAttachment.user_id==user_id),
                    [AgentAttachment.filename,AgentAttachment.extracted_text],AgentAttachment.created_at_epoch)
                output.extend(_entry('attachment',x.id,x.filename,x.extracted_text or '', 'private',x.created_at_epoch,
                    href=f'/api/v1/agent/attachments/{x.id}/download') for x in files)
    return output,limited


def search_method():
    """诊断页展示当前真实检索方式，不把关键词检索说成向量检索。"""
    return 'keyword'


def search(user_id,query='',scope='all',kind='all',limit=30):
    """返回可回查的片段和范围，不把搜索结果当成已经核实的学校规定。"""
    if len(query)>300: raise ApiProblem('query_too_long','关键词最多300字。',422)
    rows,limited = _rows(user_id,query,scope,kind)
    terms=_terms(query)
    for row in rows:
        content=row.pop('content')
        row['content_hash']=row.pop('fingerprint')
        lowered=content.casefold()
        positions=[lowered.find(term) for term in terms if term in lowered]
        start=max(0,min(positions)-80) if positions else 0
        row['snippet']=content[start:start+450]
        row['score']=sum(4*(term in row['title'].casefold())+int(term in lowered) for term in terms)
    rows.sort(key=lambda x:(x['score'],x['updated_at_epoch'],x['id']),reverse=True)
    cap=max(1,min(limit,100))
    return {'items':rows[:cap],'has_more':limited or len(rows)>cap,'method':'keyword',
        'note':'私人范围联合检索通知、长期记忆、对话和文件；公共规定请以出处及当前有效版本为准。'}


DATE_PATTERN=re.compile(r'(?:\d{4}\s*[年./-]\s*)?\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?|\d{4}\s*[年./-]\s*\d{1,2}\s*月?')
TIME_PATTERN=re.compile(r'\d{1,2}\s*[:：]\s*\d{2}')
MATERIAL_WORDS=('身份证','学生证','复印件','原件','照片','表格','报名表','推荐信','成绩单','证明','材料','盖章','签字')
LOCATION_WORDS=('地点','教室','线上','腾讯会议','会议室','图书馆','教学楼','实验楼','报到')


def _normalized_text(text):
    """去掉空白和重复标点，用于识别仅排版/空格变化。"""
    value=re.sub(r'\s+','',text or '')
    return re.sub(r'[，。；：、,.!?！？;:]+','，',value)


def _facts(text):
    """只提取可比较的事实线索，不把整段正文写入关注快照。"""
    dates=sorted({re.sub(r'\s+','',item) for item in DATE_PATTERN.findall(text or '')})
    times=sorted({re.sub(r'\s+','',item) for item in TIME_PATTERN.findall(text or '')})
    materials=sorted({word for word in MATERIAL_WORDS if word in (text or '')})
    locations=sorted({word for word in LOCATION_WORDS if word in (text or '')})
    return {'dates':dates,'times':times,'materials':materials,'locations':locations}


def watch_state(user_id,query,scope):
    """生成可比较的关注快照；不包含长期记忆和会话，避免自身回复触发关注。"""
    rows,limited=_rows(user_id,query,scope,'all',per_kind=1000,watch=True)
    snapshot=[]
    for row in sorted(rows,key=lambda item:(item['kind'],item['id'])):
        normalized=_normalized_text(row['content'])
        snapshot.append({
            'kind':row['kind'],'id':row['id'],'title':(row['title'] or '')[:200],
            'version':row.get('version',1),'updated_at_epoch':row.get('updated_at_epoch'),
            'content_hash':row.get('fingerprint'),
            'normalized_hash':hashlib.sha256(normalized.encode()).hexdigest(),
            'facts':_facts(row['content']),
        })
    raw=json.dumps([[item['kind'],item['id'],item['content_hash']] for item in snapshot],ensure_ascii=False)
    return snapshot,hashlib.sha256(raw.encode()).hexdigest(),limited


def watch_delta(previous,current):
    """区分新增、事实变化、标题变化、正文变化、仅排版和重复转发。"""
    previous=previous or []
    current=current or []
    before={f"{item.get('kind')}:{item.get('id')}":item for item in previous}
    after={f"{item.get('kind')}:{item.get('id')}":item for item in current}
    changes=[]
    for key,item in after.items():
        old=before.get(key)
        if old is None:
            changes.append({'type':'added','key':key,'kind':item.get('kind'),'id':item.get('id'),'title':item.get('title')})
            continue
        if old.get('content_hash')==item.get('content_hash'):
            continue
        if old.get('normalized_hash')==item.get('normalized_hash'):
            changes.append({'type':'cosmetic','key':key,'kind':item.get('kind'),'id':item.get('id'),'title':item.get('title')})
            continue
        old_facts=old.get('facts') or {}; new_facts=item.get('facts') or {}
        fields=[name for name in ('dates','times','materials','locations') if old_facts.get(name)!=new_facts.get(name)]
        if fields:
            changes.append({'type':'fact_changed','key':key,'kind':item.get('kind'),'id':item.get('id'),
                'title':item.get('title'),'fields':fields})
        elif old.get('title')!=item.get('title'):
            changes.append({'type':'title_changed','key':key,'kind':item.get('kind'),'id':item.get('id'),'title':item.get('title')})
        else:
            changes.append({'type':'content_changed','key':key,'kind':item.get('kind'),'id':item.get('id'),'title':item.get('title')})
    for key,item in before.items():
        if key not in after:
            changes.append({'type':'removed','key':key,'kind':item.get('kind'),'id':item.get('id'),'title':item.get('title')})
    # 同内容重复转发不算新证据；保留记录但不触发唤醒。
    hashes={item.get('normalized_hash') for item in current}
    seen=set()
    for change in changes:
        if change['type']!='added': continue
        item=after.get(change['key']) or {}
        digest=item.get('normalized_hash')
        if digest in seen or sum(1 for other in current if other.get('normalized_hash')==digest)>1:
            change['type']='duplicate'
        seen.add(digest)
    meaningful=[change for change in changes if change['type'] in {'added','fact_changed','title_changed','content_changed','removed'}]
    labels={'added':'新增资料','fact_changed':'日期/时间/材料/地点要求变化','title_changed':'标题变化',
        'content_changed':'正文实质变化','removed':'资料撤下','duplicate':'重复转发','cosmetic':'仅排版变化'}
    summary='；'.join(f"{labels.get(change['type'],change['type'])}：{change.get('title') or change.get('key')}" for change in meaningful[:8])
    return {'meaningful':bool(meaningful),'count':len(meaningful),'changes':changes[:20],'summary':summary,
        'ignored':len(changes)-len(meaningful)}


def watch_fingerprint(user_id,query,scope):
    """不索引自动生成的聊天和记忆，防止关注任务被自己的回复反复唤醒。"""
    _,fingerprint,limited=watch_state(user_id,query,scope)
    return fingerprint,limited


def save_document(user_id,payload:KnowledgeInput,*,identifier=None,expected_version=None):
    """发布公共文档仅限管理员，私人资料不会因检索自动升级为公共内容。"""
    from urllib.parse import urlsplit
    if payload.source_url:
        url=urlsplit(payload.source_url)
        if url.scheme!='https' or not url.hostname or url.username or url.password:
            raise ApiProblem('invalid_source_url','出处需为不含凭证的 HTTPS 地址。',422)
    key=identifier or str(uuid5(NAMESPACE_URL,f'knowledge:{user_id}:{payload.idempotency_key}'))
    now=int(time.time())
    digest=hashlib.sha256(payload.content.strip().encode()).hexdigest()
    incoming_metadata={'publisher':payload.publisher,'published_date':payload.published_date.isoformat() if payload.published_date else None,
        'effective_status':payload.effective_status,'audience':payload.audience,
        'supersedes_id':str(payload.supersedes_id) if payload.supersedes_id else None}
    with Session(_engine()) as db,db.begin():
        admin=_admin(db,user_id)
        if payload.scope=='public' and not admin: raise ApiProblem('admin_required','公共库发布需管理员审核。',403)
        item=db.get(KnowledgeDocument,key)
        if item:
            if item.user_id!=user_id and not (admin and item.scope=='public'):
                raise ApiProblem('not_found','未找到该文档。',404)
            stored_metadata=item.metadata_json or {}
            same=(item.content_hash==digest and item.title==payload.title.strip() and item.scope==payload.scope
                and item.source_url==payload.source_url
                and all(stored_metadata.get(key)==value for key,value in incoming_metadata.items()))
            if not same and expected_version is None:
                raise ApiProblem('idempotency_conflict','同次保存的资料已改变，请新建或按版本编辑。',409)
            if expected_version is not None and item.version!=expected_version:
                raise ApiProblem('version_conflict','文档已变化，请刷新。',409)
            if not same:
                item.content=payload.content.strip(); item.content_hash=digest; item.title=payload.title.strip()
                item.source_url=payload.source_url; item.scope=payload.scope; item.version+=1; item.updated_at_epoch=now
                item.metadata_json={**incoming_metadata,'fetched_at_epoch':now}
        else:
            item=KnowledgeDocument(id=key,user_id=user_id,scope=payload.scope,title=payload.title.strip(),content=payload.content.strip(),
                source_url=payload.source_url,content_hash=digest,metadata_json={**incoming_metadata,'fetched_at_epoch':now},
                status='active',version=1,created_at_epoch=now,updated_at_epoch=now)
            db.add(item)
        db.flush()
        saved={'id':item.id,'kind':'document','title':item.title,'scope':item.scope,'version':item.version,
            'status':item.status,'source_url':item.source_url,'owner_id':item.user_id,'metadata':item.metadata_json or {}}
    # 先提交原文再更新派生索引；索引失败不能回滚或阻断资料保存。
    try:
        from .retrieval_service import schedule_index
        schedule_index(saved['owner_id'],'document',saved['id'],version=saved['version'])
    except Exception:
        current_app.logger.warning('知识文档索引调度失败 document_id=%s',saved['id'])
    saved.pop('owner_id',None)
    try:
        from . import search_service
        search_service.invalidate_cache()
    except Exception:
        current_app.logger.warning('搜索缓存失效失败 document_id=%s',saved['id'])
    return saved


def read_entry(user_id,kind,identifier,offset=0):
    """按页读取原材料，账户判断不可由检索结果或模型参数替代。"""
    offset=max(0,int(offset))
    with Session(_engine()) as db:
        models={'document':KnowledgeDocument,'notification':SourceDocument,'memory':AgentMemory,'conversation':AgentTurn,'attachment':AgentAttachment}
        if kind not in models: raise ApiProblem('not_found','未找到该资料。',404)
        item=db.get(models[kind],identifier)
        if not item or not (item.user_id==user_id or (kind=='document' and item.scope=='public' and item.status=='active')):
            raise ApiProblem('not_found','未找到该资料。',404)
        if kind=='attachment':
            from .agent_material_service import material_info
            info=material_info(user_id,identifier,offset=offset)
            # 统一知识库读取形状，同时保留原附件分页字段供下载和预览使用。
            return {**info,'kind':'attachment','id':identifier,'title':info['filename'],'scope':'private',
                'content':info['text'],'version':1,'status':info['status'],'source_url':None,'metadata':{},
                'download_href':f'/api/v1/agent/attachments/{identifier}/download'}
        text=item.original_text if kind=='notification' else item.content
        result=dict(id=identifier,kind=kind,title=getattr(item,'title',kind),scope=getattr(item,'scope','private'),
            content=text[offset:offset+12000],offset=offset,total_characters=len(text),
            next_offset=offset+12000 if offset+12000<len(text) else None,
            version=getattr(item,'version',1),status=getattr(item,'status','active'),source_url=getattr(item,'source_url',None),
            metadata=getattr(item,'metadata_json',None) or {})
        if kind=='conversation': result['conversation_id']=item.conversation_id
        return result


def verify_citation(user_id,kind,identifier,quote):
    """三层校验的前两层：引用确实存在、日期/状态适用于当前使用；结论支持留人工。"""
    quote=(quote or '').strip()
    if len(quote)<2: raise ApiProblem('invalid_citation','引文过短，无法定位。',422)
    with Session(_engine()) as db:
        models={'document':KnowledgeDocument,'notification':SourceDocument,'attachment':AgentAttachment}
        item=db.get(models.get(kind),identifier) if kind in models else None
        if not item or not (item.user_id==user_id or (kind=='document' and item.scope=='public' and item.status=='active')):
            raise ApiProblem('not_found','未找到被引用的资料。',404)
        text=item.content if kind=='document' else (item.original_text if kind=='notification' else (item.extracted_text or ''))
        metadata=(getattr(item,'metadata_json',None) or {}) if kind=='document' else {}
    exists=quote in text
    reasons=[]
    effective=metadata.get('effective_status') or 'unknown'
    if effective=='expired': reasons.append('资料标记为已失效')
    published=metadata.get('published_date')
    if published:
        try:
            if published>date.today().isoformat(): reasons.append('发布日期晚于当前日期')
        except TypeError:
            reasons.append('发布日期格式无法核对')
    if kind!='document': reasons.append('非知识文档没有发布单位与生效状态，需人工核对')
    return {
        'kind':kind,'id':identifier,'exists':exists,
        'quote':quote,'matched_text':quote if exists else None,
        'effective_status':effective,'published_date':published,'publisher':metadata.get('publisher'),
        'audience':metadata.get('audience'),
        'applicable':exists and not reasons,
        'applicable_reasons':reasons,
        'conclusion_supported':None,
        'manual_review_required':True,
        'note':'已校验引用定位与基本时效；结论是否被原文支持仍需人工或金标样本核对。',
    }


def set_state(user_id,identifier,payload):
    """归档可恢复，不物理删除知识文档；管理员不能操作别人的私人文档。"""
    with Session(_engine()) as db,db.begin():
        item=db.get(KnowledgeDocument,identifier,with_for_update=True)
        if not item or not (item.user_id==user_id or (item.scope=='public' and _admin(db,user_id))):
            raise ApiProblem('not_found','未找到该文档。',404)
        if item.scope=='public' and not _admin(db,user_id): raise ApiProblem('admin_required','公共库维护需管理员权限。',403)
        if item.version!=payload.expected_version: raise ApiProblem('version_conflict','文档已变化，请刷新。',409)
        item.status=payload.status; item.version+=1; item.updated_at_epoch=int(time.time())
        result={'id':item.id,'status':item.status,'version':item.version,'owner_id':item.user_id}
    try:
        from . import retrieval_service
        if payload.status=='archived':
            retrieval_service.remove_source(result['owner_id'],'document',result['id'])
        else:
            retrieval_service.schedule_index(result['owner_id'],'document',result['id'],version=result['version'])
    except Exception:
        current_app.logger.warning('知识文档索引状态同步失败 document_id=%s',result['id'])
    result.pop('owner_id',None)
    try:
        from . import search_service
        search_service.invalidate_cache()
    except Exception:
        current_app.logger.warning('搜索缓存失效失败 document_id=%s',result['id'])
    return result
