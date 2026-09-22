import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // Fixture tests read the same JSON as pytest and forge
    // (docs/test_strategy.md section 5). They arrive in stage 1.
    include: ['tests/**/*.test.ts'],
    environment: 'node',
  },
});
