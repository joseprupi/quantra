module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  ignorePatterns: ['dist', '.eslintrc.cjs'],
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
  },
  plugins: ['react-refresh'],
  rules: {
    // ── BASELINE RULES ────────────────────────────────────────────────────────
    // Rules turned off (or configured permissively) at base for pre-plan-01
    // legacy code; restored at error level in the src/lib/api/** override.

    // Legacy code uses `any` broadly for JSON.parse / Firebase / localStorage
    // payloads that lack type annotations.
    '@typescript-eslint/no-explicit-any': 'off',

    // Legacy pages use underscore-prefixed parameters intentionally (_def,
    // _asset, _) — the TypeScript convention for "intentionally unused".
    // Configured with ignore patterns rather than disabled entirely.
    '@typescript-eslint/no-unused-vars': [
      'error',
      {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
      },
    ],

    // Legacy page components have intentional dep-array elisions across 9+
    // files. Turned off rather than adding eslint-disable in each component.
    'react-hooks/exhaustive-deps': 'off',

    // listStyles.tsx and Matrix2DEditor.tsx export non-component values
    // alongside components — legacy design. Turned off (replaces the Vite
    // template default warn) to avoid --max-warnings 0 failures.
    'react-refresh/only-export-components': 'off',
  },
  overrides: [
    {
      // Plan 01's clean zone: held to full recommended + all baselined rules.
      // No eslint-disable comments are permitted in these files.
      files: ['src/lib/api/**/*.ts', 'src/lib/api/**/*.tsx'],
      rules: {
        '@typescript-eslint/no-explicit-any': 'error',
        '@typescript-eslint/no-unused-vars': [
          'error',
          {
            argsIgnorePattern: '^_',
            varsIgnorePattern: '^_',
            caughtErrorsIgnorePattern: '^_',
          },
        ],
        'react-hooks/exhaustive-deps': 'error',
        'react-refresh/only-export-components': [
          'error',
          { allowConstantExport: true },
        ],
      },
    },
  ],
}
