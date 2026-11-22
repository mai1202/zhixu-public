import { defineConfig } from 'vite';

export default defineConfig({
  publicDir: false,
  build: {
    lib: {
      entry: 'src/runtime.ts',
      formats: ['es'],
      fileName: () => 'runtime.js',
    },
    outDir: 'public/ocr-runtime',
    emptyOutDir: false,
    // 固定 runner 需要单个运行时模块，使用当前打包器支持的选项。
    rolldownOptions: {
      output: { codeSplitting: false },
    },
  },
});
