import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'
import type { Plugin } from 'vite'

/**
 * Serves the Mol* pre-built IIFE bundle (molstar/build/viewer/molstar.{js,css})
 * as static assets at /molstar.js and /molstar.css.
 *
 * Using the pre-built bundle sidesteps Vite 8 (rolldown) chunk-splitting issues
 * that cause module initialisation order errors in Mol*'s PluginUIContext.
 */
function molstarBundlePlugin(): Plugin {
  const molstarDir = path.resolve(
    import.meta.dirname,
    'node_modules/molstar/build/viewer',
  )

  return {
    name: 'molstar-bundle',

    // Dev: serve the files via custom middleware
    configureServer(server) {
      server.middlewares.use('/molstar.js', (_req, res) => {
        res.setHeader('Content-Type', 'application/javascript')
        fs.createReadStream(path.join(molstarDir, 'molstar.js')).pipe(res)
      })
      server.middlewares.use('/molstar.css', (_req, res) => {
        res.setHeader('Content-Type', 'text/css')
        fs.createReadStream(path.join(molstarDir, 'molstar.css')).pipe(res)
      })
    },

    // Build: emit the files as assets so they land in dist/
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'molstar.js',
        source: fs.readFileSync(path.join(molstarDir, 'molstar.js')),
      })
      this.emitFile({
        type: 'asset',
        fileName: 'molstar.css',
        source: fs.readFileSync(path.join(molstarDir, 'molstar.css')),
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), molstarBundlePlugin()],
  server: {
    port: 5173,
    proxy: {
      // Proxy API calls in dev so CORS isn't needed during local development
      '/auth': 'http://localhost:8000',
      '/projects': 'http://localhost:8000',
      '/runs': 'http://localhost:8000',
      '/structures': 'http://localhost:8000',
      '/campaigns': 'http://localhost:8000',
      '/binders': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
  worker: {
    format: 'es',
  },
})
