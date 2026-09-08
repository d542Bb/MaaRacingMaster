import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// 构建产物直接输出到 tools/navkit/static/（server.py 静态伺服目录）。
// emptyOutDir=false：不触碰同目录下的 calibrator.html / app.js / style.css 等既有文件。
export default defineConfig({
  plugins: [react()],
  // Semi 2.103.0 的 package.json exports 拦了 dist/css/semi.min.css 深路径，
  // 用 alias 显式打通（官方组件主入口正常按需引入）
  resolve: {
    alias: {
      '@semi-css': resolve(__dirname, 'node_modules/@douyinfe/semi-ui/dist/css/semi.min.css'),
    },
  },
  server: {
    port: 8801,
    proxy: {
      '/api': 'http://localhost:8765',
      '/calibrator.html': 'http://localhost:8765',
      '/app.js': 'http://localhost:8765',
      '/history.js': 'http://localhost:8765',
      '/style.css': 'http://localhost:8765',
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: false,
  },
});