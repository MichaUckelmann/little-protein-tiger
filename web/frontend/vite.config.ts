import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Proxy API calls in dev so CORS isn't needed during local development
      '/auth': 'http://localhost:8000',
      '/projects': 'http://localhost:8000',
      '/runs': 'http://localhost:8000',
      '/structures': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
