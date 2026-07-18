import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import pkg from './package.json'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  // Build-time constant: the web-client (portal) version, sourced from
  // package.json. Surfaced in the About panel so anyone looking at the screen
  // knows exactly which portal build is running. Declared for TS in
  // src/globals.d.ts.
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  server: {
    port: 5173,
    host: true,
    watch: {
      ignored: ['**/scripts/.venv/**', '**/node_modules/**', '**/dist/**', '**/tmp/**'],
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    exclude: ['**/node_modules/**', 'e2e/**'],
  },
})
