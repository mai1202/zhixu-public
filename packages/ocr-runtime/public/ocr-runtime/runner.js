import { recognizeImage } from './runtime.js';

const imageInput = document.querySelector('#image');
const state = document.querySelector('#state');

// 只执行固定 OCR 模块，不解析用户传入的网址、脚本或浏览器命令。
imageInput.addEventListener('change', async () => {
  try {
    const file = imageInput.files?.[0];
    if (!file) throw new Error('missing_image');
    window.__ocrResult = null;
    window.__ocrError = null;
    state.textContent = 'running';
    window.__ocrResult = await recognizeImage(file, 'tiny', 'server', (progress) => {
      window.__ocrProgress = progress;
      state.textContent = progress.phase;
    });
    state.textContent = 'succeeded';
  } catch (error) {
    window.__ocrError = error instanceof Error ? error.message : 'ocr_failed';
    state.textContent = 'failed';
  } finally {
    // 清除输入控件对图片文件的引用，浏览器上下文只缓存固定模型资源。
    imageInput.value = '';
  }
});
