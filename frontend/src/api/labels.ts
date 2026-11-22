// 面向学生显示中文状态，内部枚举仍保持接口兼容，未知值留给诊断详情。
export function statusLabel(value: string): string {
  return ({ proposed: '待核验', confirmed: '已确认', rejected: '已驳回', undone: '已撤销',
    active: '有效', archived: '已归档', queued: '排队中', leased: '准备中', running: '处理中',
    retry_wait: '等待重试', succeeded: '已完成', failed: '失败', cancelled: '已取消', expired: '已过期',
    success: '成功', pending: '等待处理', open: '待完成', completed: '已完成' } as Record<string, string>)[value] ?? '状态待确认';
}

export function precisionLabel(value?: string): string {
  return ({ unknown: '时间待核验', year: '仅年份', month: '仅月份', day: '日期', minute: '精确到分钟',
    second: '精确到秒' } as Record<string, string>)[value ?? 'unknown'] ?? '时间待核验';
}

export function taskKindLabel(value: string): string {
  // 调用记录保留提供方和模型名，任务用途用中文解释。
  return ({ connection_test: '连接测试', extract: '通知提取', agent_plan: '对话规划', repair: '结构修正' } as Record<string, string>)[value] ?? '模型调用';
}

export function evidenceLabel(value: string): string {
  return ({ deadline: '截止时间', time_range: '活动时间', activity_start: '活动开始时间', actor: '责任主体',
    stage: '阶段', scope: '适用范围', materials: '所需材料', submission: '提交渠道', title: '事项标题' } as Record<string, string>)[value] ?? '字段';
}
