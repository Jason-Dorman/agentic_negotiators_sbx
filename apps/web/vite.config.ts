import react from '@vitejs/plugin-react';
// `vitest/config` re-exports Vite's defineConfig with the `test` key typed.
import { defineConfig } from 'vitest/config';

// docs/architecture.md 4: everything binds to localhost by default.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
