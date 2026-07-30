import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiProxy = {
  '/api': 'http://localhost:8000',
  '/_allauth': 'http://localhost:8000',
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // TERMS.md lives at the repo root, one level above this package, and is
  // imported ?raw into the Terms dialog — the dev server has to be allowed to
  // read it.
  server: { proxy: apiProxy, fs: { allow: ['..'] } },
  preview: { proxy: apiProxy },
})
