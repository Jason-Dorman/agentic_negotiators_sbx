// docs/contributing.md 2.2: strict mode, @typescript-eslint/strict-type-checked, Prettier.
import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: [
      '**/dist/**',
      '**/node_modules/**',
      // The Python virtualenv ships vendored JavaScript that is not ours to lint.
      '**/.venv/**',
      '**/__pycache__/**',
      '**/coverage/**',
      '**/playwright-report/**',
      '**/test-results/**',
      'contracts/lib/**',
      'contracts/out/**',
      'contracts/cache/**',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Amounts are bigint internally and base-10 strings on the wire. An implicit coercion of
      // either is the bug class this project cannot afford (docs/contributing.md 2.2).
      '@typescript-eslint/restrict-template-expressions': ['error', { allowNumber: false }],
    },
  },
  {
    files: ['**/*.config.{js,ts,mjs}', '**/*.cjs'],
    extends: [tseslint.configs.disableTypeChecked],
  },
  // Must stay last: turns off the rules Prettier owns.
  prettier,
);
