// ESLint flat config —— Phase 19
//
// 背景：dashboard 先前完全沒有 lint，CI 只跑 `tsc -b && vite build`。
// 型別檢查抓不到 hooks 依賴陣列錯誤、未使用變數、不安全的 any 等問題，
// 而這些正是 React 專案最常見的缺陷來源。
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules'] },

  // ── 應用程式碼 ──────────────────────────────────────────────────────────
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs['recommended-latest'].rules,
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
      // any 一律報錯而非警告——此專案的型別契約（types.ts）是前後端的共同語言，
      // 用 any 繞過等於放棄該契約。目前僅 useAnalysis.ts 的 SSE payload 有一處
      // 明確標註的例外（附 eslint-disable 註解說明理由）。
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },

  // ── 測試碼 ─────────────────────────────────────────────────────────────
  {
    files: ['**/*.test.{ts,tsx}', 'src/test/**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      // 測試裡建構 SSE payload / mock 物件時需要較寬鬆的型別
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
)
