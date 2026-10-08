import js from '@eslint/js'
import prettier from 'eslint-config-prettier'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'

import noEmojiRule from './eslint-rules/no-emoji.js'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**', '*.min.js', 'src/api/schema.d.ts'] },

  js.configs.recommended,
  ...tseslint.configs.recommended,

  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },

  {
    files: ['**/*.{js,ts,tsx}'],
    plugins: { kylab: { rules: { 'no-emoji': noEmojiRule } } },
    // 与旧前端同一条硬性禁令（《前端设计规范》§3、§8）
    rules: { 'kylab/no-emoji': 'error' },
  },

  {
    files: ['tests/**/*.{ts,tsx}', 'eslint-rules/**/*.js'],
    rules: { '@typescript-eslint/no-explicit-any': 'off' },
  },

  prettier,
)
