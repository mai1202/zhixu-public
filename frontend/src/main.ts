import { createApp } from 'vue';
import { createPinia } from 'pinia';
import 'mdui/mdui.css';
import { setColorScheme } from 'mdui/functions/setColorScheme.js';
// 按需注册实际使用的 MDUI 自定义元素，避免整包加载时某个组件异常导致导航等元素完全不升级。
import 'mdui/components/badge.js';
import 'mdui/components/button.js';
import 'mdui/components/button-icon.js';
import 'mdui/components/card.js';
import 'mdui/components/checkbox.js';
import 'mdui/components/chip.js';
import 'mdui/components/circular-progress.js';
import 'mdui/components/collapse.js';
import 'mdui/components/collapse-item.js';
import 'mdui/components/dialog.js';
import 'mdui/components/divider.js';
import 'mdui/components/dropdown.js';
import 'mdui/components/fab.js';
import 'mdui/components/linear-progress.js';
import 'mdui/components/list.js';
import 'mdui/components/list-item.js';
import 'mdui/components/list-subheader.js';
import 'mdui/components/menu.js';
import 'mdui/components/menu-item.js';
import 'mdui/components/navigation-bar.js';
import 'mdui/components/navigation-bar-item.js';
import 'mdui/components/navigation-rail.js';
import 'mdui/components/navigation-rail-item.js';
// 行级可点击卡片复用 MDUI 的涟漪与状态层，保证点击反馈与按钮一致。
import 'mdui/components/ripple.js';
import 'mdui/components/select.js';
import 'mdui/components/segmented-button.js';
import 'mdui/components/segmented-button-group.js';
import 'mdui/components/snackbar.js';
import 'mdui/components/switch.js';
import 'mdui/components/tab.js';
import 'mdui/components/tabs.js';
import 'mdui/components/text-field.js';
import 'mdui/components/tooltip.js';
import App from './App.vue';
import { router } from './router';
import { useAuthStore } from './stores/auth';
import { startSessionMaintenance } from './utils/sessionMaintenance';
import './style.css';
import './agent-insight.css';
import './matters.css';
import './agent-mobile.css';
// 本轮界面整理集中放在最后，保证组件样式与旧页面规则冲突时以新布局为准。
import './ui-refresh.css';
import './agent-workspace.css';
import './feedback-accessibility.css';
import './material-layout.css';

// 从品牌蓝生成整套 Material You 配色，让 MDUI 组件与产品主色保持一致。
setColorScheme('#315da8');

const pinia = createPinia();
createApp(App).use(pinia).use(router).mount('#app');
// 多页签各自核验同一服务端会话，撤销后保留当前地址供重新登录返回。
const stopSessionMaintenance = startSessionMaintenance(useAuthStore(pinia), () => {
  if (router.currentRoute.value.path !== '/login') {
    void router.replace({path: '/login', query: {returnTo: router.currentRoute.value.fullPath}});
  }
});
if (import.meta.hot) import.meta.hot.dispose(stopSessionMaintenance);
// 生产 HTTPS 和本机验收环境注册最小离线说明；失败不影响正常在线业务。
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => { void navigator.serviceWorker.register('/sw.js').catch(() => {}); });
}
