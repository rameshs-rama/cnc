import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: {
    port: 5173,
    proxy: {
      // Development convenience: the app talks to a relative /v1 so the same
      // build works behind the production reverse proxy without rewriting URLs.
      '/v1': { target: process.env.VITE_API_BASE ?? 'http://localhost:8000', changeOrigin: true },
      '/health': { target: process.env.VITE_API_BASE ?? 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true, chunkSizeWarningLimit: 1200 },
})
