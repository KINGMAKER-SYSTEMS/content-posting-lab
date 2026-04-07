import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [tailwindcss(), react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  css: {
    transformer: 'lightningcss',
    lightningcss: {
      targets: { chrome: 100 << 16 },
    },
  },
  build: {
    cssMinify: 'lightningcss',
  },
  server: {
    proxy: {
      '/api/captions/ws': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
      '/api/burn/ws': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
      '/api/recreate/ws': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
      '/api/clipper/ws': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        timeout: 0,
        configure: (proxy) => {
          // Disable proxy timeouts for long-running SSE streams (clipper process-batch)
          // and raise body size limit for large video uploads
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('connection', 'keep-alive');
            proxyReq.socket?.setTimeout(0);
          });
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.socket?.setTimeout(0);
          });
        },
      },
      '/fonts': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/projects': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/output': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/caption-output': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/burn-output': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
