import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  publicDir: '../packages/ocr-runtime/public',
  plugins: [
    vue({
      template: {
        compilerOptions: {
          // MDUI 使用原生自定义元素，由浏览器注册组件。
          isCustomElement: (tag) => tag.startsWith('mdui-'),
        },
      },
    }),
  ],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:5000',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
