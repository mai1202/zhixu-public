import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';
import { useAuthStore } from '../stores/auth';

const routes: RouteRecordRaw[] = [
  // 按页面加载业务代码，首次打开无需同时下载课表、OCR 和模型设置页面。
  { path: '/', redirect: '/today' },
  { path: '/login', component: () => import('../views/LoginView.vue'), meta: { public: true, title: '邮箱验证' } },
  { path: '/today', component: () => import('../views/TodayView.vue'), meta: { title: '今天' } },
  { path: '/onboarding', component: () => import('../views/OnboardingView.vue'), meta: { title: '首次使用引导' } },
  { path: '/mobile', component: () => import('../views/MobileView.vue'), meta: { title: '手机使用' } },
  { path: '/agent/workspace', component: () => import('../views/ActivityView.vue'), meta: { title: 'Agent 工作台' } },
  { path: '/activity', redirect: '/agent/workspace' },
  { path: '/tasks/:id', component: () => import('../views/TaskDetailView.vue'), meta: { title: '任务详情' } },
  // 通知详情使用独立地址，浏览器返回可恢复列表与筛选。
  { path: '/inbox/:sourceId', component: () => import('../views/NotificationsView.vue'), meta: { title: '通知详情' } },
  { path: '/inbox', component: () => import('../views/NotificationsView.vue'), meta: { title: '通知', phase: 'P2' } },
  // 保留两个旧入口及查询参数，统一呈现课表、个人日程和任务截止提醒。
  { path: '/schedule', component: () => import('../views/ScheduleView.vue'), meta: { title: '日程', phase: 'P3' } },
  { path: '/courses', component: () => import('../views/ScheduleView.vue'), meta: { title: '日程', phase: 'P3' } },
  { path: '/matters', component: () => import('../views/MattersView.vue'), meta: { title: '事务', phase: 'P2' } },
  // 办理、可完成性与机会发现收在事务下，保留五项主导航。
  { path: '/matters/journeys', component: () => import('../views/JourneysView.vue'), meta: { title: '办理路线' } },
  // 旧核对书签回到通知列表，退役的状态与确认页面不再加载。
  { path: '/matters/review', redirect: '/inbox' },
  { path: '/matters/planning', component: () => import('../views/PlanningView.vue'), meta: { title: '统一安排事务' } },
  { path: '/matters/opportunities', component: () => import('../views/OpportunitiesView.vue'), meta: { title: '发现机会' } },
  // 上传与核对各有独立地址，日程主页只负责查看和编辑安排。
  { path: '/schedule/import/:id?', component: () => import('../views/TimetableImportView.vue'), meta: { title: '导入课表' } },
  { path: '/agent', component: () => import('../views/AgentView.vue'), meta: { title: '助手', phase: 'P7' } },
  // 旧书签统一重定向到规范设置路径，不再保留第二套编辑入口。
  { path: '/agent/settings', redirect: to => '/settings/' + (({ memory: 'memory', schedules: 'schedules' } as Record<string, string>)[String(to.query.tab)] ?? 'assistant') },
  { path: '/mail', redirect: '/settings/mail' },
  { path: '/settings/mail', component: () => import('../views/MailView.vue'), meta: { title: '邮件提醒' } },
  { path: '/settings/integrations', component: () => import('../views/IntegrationsView.vue'), meta: { title: '通知接入' } },
  { path: '/knowledge', component: () => import('../views/KnowledgeView.vue'), meta: { title: '知识库' } },
  { path: '/settings', component: () => import('../views/SettingsIndexView.vue'), meta: { title: '设置' } },
  ...(['devices', 'models', 'diagnostics'] as const).map(section => ({
    path: '/settings/' + section, component: () => import('../views/SettingsView.vue'),
    meta: {title: {devices: '设备与采集', models: '模型连接', diagnostics: '运行诊断'}[section], settingsSection: section === 'diagnostics' ? 'runs' : section},
  })),
  ...(['notifications', 'assistant', 'memory', 'schedules'] as const).map(section => ({
    path: '/settings/' + section, component: () => import('../views/AgentSettingsView.vue'),
    meta: {title: {notifications: '通知处理', assistant: '助手推理', memory: '长期记忆', schedules: '定时任务'}[section], settingsSection: section},
  })),
  { path: '/settings/account', component: () => import('../views/ProfileView.vue'), meta: { title: '账户' } },
  // 登录设备统一从安全页管理，不与采集器设备入口混用。
  { path: '/settings/security', component: () => import('../views/SecurityView.vue'), meta: { title: '安全' } },
  { path: '/profile', redirect: '/settings' },
  { path: '/:pathMatch(.*)*', redirect: '/today' },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(to, _from, savedPosition) {
    // 前进切页回到顶部，浏览器后退时恢复原来的阅读位置；深链锚点保留。
    if (to.hash.startsWith('#invite=')) return { top: 0 };
    if (to.hash) return { el: to.hash, top: 88 };
    return savedPosition ?? { top: 0 };
  },
});

router.afterEach((to) => {
  document.title = to.meta.public ? '登录 · 知序' : `${String(to.meta.title ?? '知序')} · 知序`;
});

router.beforeEach(async (to) => {
  // 旧查询式设置链接即使从目录内部打开，也必须变成唯一的规范地址。
  if (to.path === '/settings') {
    const section = ({devices: 'devices', processing: 'notifications', models: 'models', runs: 'diagnostics'} as Record<string, string>)[String(to.query.tab)];
    if (section) return {path: '/settings/' + section, hash: to.hash === '#attachment-processing' ? '' : to.hash};
  }
  const auth = useAuthStore();
  if (!auth.initialized) {
    try {
      await auth.restore();
    } catch {
      return { path: '/login' };
    }
  }

  if (to.path !== '/login' && !auth.user) {
    // 邀请凭证继续留在片段，避免登录页刷新时进入服务器查询日志。
    if (to.hash.startsWith('#invite=')) return { path: '/login', query: { returnTo: to.path }, hash: to.hash };
    return { path: '/login', query: { returnTo: to.fullPath } };
  }
  if (to.path === '/login' && auth.user) {
    return '/today';
  }
  return true;
});
