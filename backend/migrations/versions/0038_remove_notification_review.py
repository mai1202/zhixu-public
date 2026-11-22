"""删除通知核对字段/请求表，保留原文、业务实体、邮箱状态及真实操作审计。"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid5, NAMESPACE_URL

revision = '0038_remove_notification_review'
down_revision = '0037_model_billing'
branch_labels = depends_on = None


def upgrade():
    db = op.get_bind()
    now = int(datetime.now(timezone.utc).timestamp())
    # 在删除旧来源状态前固化忽略/归档分组，不能让旧忽略通知回到收件箱。
    db.execute(sa.text("""INSERT INTO notification_organizations (source_id,user_id,mailbox,version,updated_at_epoch,grouping_locked)
        SELECT s.id,s.user_id,CASE WHEN s.archived_at_epoch IS NOT NULL THEN 'archived' ELSE 'ignored' END,1,:now,0
        FROM source_documents s WHERE (s.status='rejected' OR s.archived_at_epoch IS NOT NULL)
        AND NOT EXISTS (SELECT 1 FROM notification_organizations n WHERE n.source_id=s.id)"""), {'now': now})
    op.add_column('source_documents', sa.Column('analysis_state', sa.String(24), nullable=False, server_default='received'))
    db.execute(sa.text("UPDATE source_documents SET analysis_state=CASE WHEN status='processing' THEN 'processing' ELSE 'processed' END"))
    op.drop_column('source_documents', 'status')
    op.drop_column('notices', 'info_status')
    # 仅独立日程等操作使用事务状态；通知来源绑定的操作列为空，没有核对状态。
    op.add_column('change_sets', sa.Column('operation_state', sa.String(24), nullable=True))
    db.execute(sa.text("UPDATE change_sets SET operation_state=status WHERE source_document_id IS NULL AND kind NOT IN ('normalize','requirements','planning_review')"))
    op.drop_table('clarification_requests')
    # 未执行的旧要求提案不是业务事实，退役时删除提案，不伪装成已应用。
    db.execute(sa.text("DELETE FROM change_sets WHERE kind IN ('requirements','planning_review') AND confirmed_at_epoch IS NULL"))
    db.execute(sa.text("UPDATE jobs SET status='cancelled',finished_at_epoch=:now,updated_at_epoch=:now WHERE kind='requirements.resume' AND status IN ('queued','retry_wait','running','leased')"), {'now': now})
    # 退役机制的自动建议与日邮件开关也不保留在后台。
    db.execute(sa.text("DELETE FROM agent_insights WHERE kind='pending_review'"))
    preferences=sa.table('digest_preferences',sa.column('user_id',sa.String()),sa.column('include_json',sa.JSON()))
    for row in db.execute(sa.select(preferences.c.user_id,preferences.c.include_json)):
        if isinstance(row[1],dict) and 'pending' in row[1]:
            include=dict(row[1]); include.pop('pending')
            db.execute(preferences.update().where(preferences.c.user_id==row[0]).values(include_json=include))
    # 旧有效通知候选交给本地 worker 直接应用，不再等待人工点击或再次收费调用模型。
    jobs = sa.table('jobs', *[sa.column(name, typ) for name, typ in [('id',sa.String()),('user_id',sa.String()),('kind',sa.String()),('status',sa.String()),('priority',sa.Integer()),('payload_json',sa.JSON()),('payload_hash',sa.String()),('run_at_epoch',sa.Integer()),('attempt',sa.Integer()),('max_attempts',sa.Integer()),('idempotency_key',sa.String()),('input_version',sa.Integer()),('lock_version',sa.Integer()),('created_at_epoch',sa.Integer()),('updated_at_epoch',sa.Integer())]])
    for row in db.execute(sa.text("SELECT id,user_id FROM change_sets WHERE kind='normalize' AND status='proposed'")):
        payload = {'change_id': row.id}
        db.execute(jobs.insert().values(id=str(uuid5(NAMESPACE_URL,'retire-notification-review:'+row.id)), user_id=row.user_id,
            kind='notification.apply_legacy',status='queued',priority=80,payload_json=payload,
            payload_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            run_at_epoch=now,attempt=0,max_attempts=1,idempotency_key='review-retired:'+row.id,input_version=1,lock_version=1,created_at_epoch=now,updated_at_epoch=now))
    # 已忽略记录只表示此操作被消费，不再保留“已驳回/已确认”通知状态。
    db.execute(sa.text("UPDATE change_sets SET confirmed_at_epoch=COALESCE(confirmed_at_epoch,updated_at_epoch) WHERE source_document_id IS NOT NULL AND status='rejected'"))
    op.drop_index('ix_change_sets_owner_status_created', table_name='change_sets')
    op.drop_column('change_sets','status')
    op.create_index('ix_change_sets_owner_operation_created','change_sets',['user_id','operation_state','created_at_epoch'])
    # 清除 JSON 快照中的核对字段，原始通知正文与用户历史消息保持原文。
    def clean(value):
        if isinstance(value, list): return [clean(item) for item in value]
        if not isinstance(value, dict): return value
        result = {key: clean(item) for key,item in value.items() if key not in {'info_status','verification','field_verification','pending_changes','pending_count','pending_confirmations','questions','approved','user_confirmed_date'}}
        if result.get('notification_decision') == 'review': result.pop('notification_decision')
        if result.get('decision') == 'review': result.pop('decision')
        if isinstance(result.get('change_set'), dict): result['change_set'].pop('status',None)
        # 历史工具 JSON Schema 也可能含 kind/status 对象，不能假定它们都是字符串。
        if result.get('kind') in ('normalize','requirements','planning_review'):
            result.pop('status',None)
        for name in ('identity','identity_json'):
            if isinstance(result.get(name),dict) and result[name].get('status') in ('confirmed','unverified','pending'):
                result[name].pop('status')
        if 'source_type' in result and ('analysis_mode' in result or 'original_text' in result):
            result.pop('status',None)
        return result
    for table_name, cols in [('revisions',['data_json']),('operation_logs',['before_json','after_json']),('agent_turns',['metadata_json']),('change_sets',['bundle_json','operations_json']),('jobs',['result_json']),('agent_tool_receipts',['result_json']),('agent_run_events',['payload_json']),('activity_reports',['sections_json','metadata_json'])]:
        available = {col['name'] for col in sa.inspect(db).get_columns(table_name)}
        keys=sa.inspect(db).get_pk_constraint(table_name)['constrained_columns']
        for name in cols:
            if name not in available: continue
            table=sa.table(table_name,*[sa.column(key) for key in keys],sa.column(name,sa.JSON()))
            for row in db.execute(sa.select(table)):
                original=row._mapping[name]
                if original is not None and (updated := clean(original)) != original:
                    # 流式事件和工具收据使用复合主键，不能只按 run_id 覆盖整轮。
                    db.execute(table.update().where(sa.and_(*(table.c[key]==row._mapping[key] for key in keys))).values({name:updated}))
    # 身份对象不再携带“已确认”标签。
    events=sa.table('events',sa.column('id',sa.String()),sa.column('identity_json',sa.JSON()))
    for row in db.execute(sa.select(events.c.id,events.c.identity_json)):
        if isinstance(row[1],dict) and row[1].get('status') in ('confirmed','unverified','pending'):
            identity=dict(row[1]); identity.pop('status'); db.execute(events.update().where(events.c.id==row[0]).values(identity_json=identity))


def downgrade():
    """只恢复旧结构，不重建用户已明确删除的核对记录；完整旧数据保留在发布备份。"""
    db=op.get_bind()
    # 回退结构时旧 worker 不认识这个新增任务，已消费记录保留，未执行记录取消。
    db.execute(sa.text("UPDATE jobs SET status='cancelled' WHERE kind='notification.apply_legacy' AND status IN ('queued','retry_wait','running','leased')"))
    op.add_column('source_documents',sa.Column('status',sa.String(24),nullable=False,server_default='processed'))
    db.execute(sa.text('UPDATE source_documents SET status=analysis_state'))
    op.drop_column('source_documents','analysis_state')
    op.add_column('notices',sa.Column('info_status',sa.String(20),nullable=False,server_default='complete'))
    op.add_column('change_sets',sa.Column('status',sa.String(24),nullable=False,server_default='confirmed'))
    db.execute(sa.text("UPDATE change_sets SET status=COALESCE(operation_state,CASE WHEN undone_at_epoch IS NOT NULL THEN 'undone' ELSE 'confirmed' END)"))
    op.drop_index('ix_change_sets_owner_operation_created',table_name='change_sets')
    op.drop_column('change_sets','operation_state')
    op.create_index('ix_change_sets_owner_status_created','change_sets',['user_id','status','created_at_epoch'])
    # 旧迁移负责空表结构，避免复制一份易漂移的外键定义。
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path
    spec=spec_from_file_location('restore_clarification_schema',Path(__file__).with_name('0030_clarification_requests.py'))
    module=module_from_spec(spec); spec.loader.exec_module(module); module.upgrade()
