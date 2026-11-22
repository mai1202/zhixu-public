<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue';

// 安装能力由浏览器确认；未提供安装事件时始终展示真实的系统菜单步骤。
interface InstallPrompt extends Event { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> }
const installPrompt = ref<InstallPrompt | null>(null);
const installed = ref(window.matchMedia('(display-mode: standalone)').matches);
const notice = ref('');
function captureInstall(event: Event) { event.preventDefault(); installPrompt.value = event as InstallPrompt; }
function installedNow() { installed.value = true; installPrompt.value = null; }
onMounted(() => { window.addEventListener('beforeinstallprompt', captureInstall); window.addEventListener('appinstalled', installedNow); });
onBeforeUnmount(() => { window.removeEventListener('beforeinstallprompt', captureInstall); window.removeEventListener('appinstalled', installedNow); });
async function install() {
  const event = installPrompt.value;
  if (!event) return;
  try {
    await event.prompt(); const result = await event.userChoice;
    notice.value = result.outcome === 'accepted' ? '已接受安装，请按浏览器提示完成。' : '已取消安装，仍可直接使用网页。';
  } catch { notice.value = '浏览器未完成安装，可以通过菜单添加到主屏幕。'; }
  installPrompt.value = null;
}
</script>
<template>
  <section class="page-section mobile-use-page">
    <p class="eyebrow">手机也能管理校园信息</p><h1>把知序放进口袋</h1>
    <p>查看通知和日程、录入原文、上传截图，和电脑使用同一个账户。</p>
    <nav class="mobile-quick-actions" aria-label="手机快捷操作">
      <RouterLink to="/inbox?compose=1">粘贴通知原文</RouterLink>
      <RouterLink to="/agent">上传截图或拍照</RouterLink>
      <RouterLink to="/schedule">查看课程和日程</RouterLink>
    </nav>
    <section class="mobile-guide-card"><h2>添加到手机主屏幕</h2>
      <p v-if="installed">当前已在独立应用窗口中打开。</p>
      <mdui-button v-else-if="installPrompt" variant="filled" @click="install">安装知序</mdui-button>
      <p>Android：使用 Chrome 或 Edge 打开网站，从浏览器菜单选择“安装应用”或“添加到主屏幕”。</p>
      <p>iPhone / iPad：使用 Safari 打开网站，点“分享”→“添加到主屏幕”。微信内置浏览器没有对应选项时，先选择在系统浏览器打开。</p>
      <p v-if="notice" role="status">{{ notice }}</p>
    </section>
    <section class="mobile-guide-card"><h2>从文字和截图开始</h2>
      <p>支持系统分享目标的浏览器安装后，可在系统分享菜单中把文字或链接交给知序；内容先进入通知编辑框，由你核对后提交。没有分享选项时，复制原文并打开“粘贴通知原文”即可。</p>
      <p>截图：打开助手，点“添加资料”选择相册图片。拍照：点“拍照上传”使用后置相机，再发送给助手；图片仍可在发送前移除。</p>
    </section>
    <section class="mobile-guide-card"><h2>手机与电脑怎样配合</h2>
      <p>手机网页不能直接读取微信聊天数据库。自动同步微信群通知仍需要已配对的 Windows 采集端运行；电脑离线时，手机可查看已经同步的内容，也能手动提交新资料。</p>
      <p>断网时会显示离线说明，不会假装已上传。读取本地数据库涉及文件格式、权限和平台规则，不能保证“微信检测不到”或绝无账号风险，也不应把微信数据库描述为未加密。</p>
    </section>
  </section>
</template>
