import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // Local API for `npm run dev`; override with REMEMBRA_API_PROXY=http://host:port
        target: process.env.REMEMBRA_API_PROXY || 'http://localhost:8787',
        changeOrigin: true,
      },
      '/ws': {
        target: process.env.REMEMBRA_API_PROXY || 'http://localhost:8787',
        changeOrigin: true,
        ws: true,
      },
      '/health': {
        target: process.env.REMEMBRA_API_PROXY || 'http://localhost:8787',
        changeOrigin: true,
      },
    },
  },
})
