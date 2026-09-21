import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

// 作用域与 static-checks.yml 的前端门禁一致：src/。这也是原来 node --check 覆盖的范围，
// 换上 eslint 后多出 .vue 与真正的规则检查，只强不弱。
// 范围外的 tests/ 与 scripts/ 本次不动，留作后续单独一轮。实测 `npx eslint .` 全仓 71 处，
// 全部落在 src/ 之外（tests/ 33、scripts/ 38）：no-undef 62（process / Buffer / document 这类
// 环境全局量）、no-irregular-whitespace 5、no-useless-escape 2、no-unused-vars 2。

// eslint-plugin-vue 的 flat/recommended 比 flat/essential 多出 33 条规则，其中 26 条在本仓库
// 一次都没触发。真正会命中的 7 条里，5 条只管空白与排版（singleline-html-element-content-newline
// 150 次、max-attributes-per-line 144 次、html-self-closing 3 次、html-indent 2 次、
// attributes-order 1 次，合计 300 次 = 全部命中的 89%）。
// static-checks.yml 写明「这个 workflow 不做『风格即门禁』的事」，因此这里只把管正确性与组件
// API 契约的那部分补回来，排版类一律不启用。
const vueCorrectnessRules = {
  // 组件对外契约：props 的类型、默认值与命名，以及 emits 必须显式声明。
  'vue/require-prop-types': 'error',
  'vue/require-default-prop': 'error',
  'vue/prop-name-casing': 'error',
  'vue/require-explicit-emits': 'error',
  'vue/component-definition-name-casing': 'error',
  // 模板语义正确性。
  'vue/no-template-shadow': 'error',
  'vue/no-lone-template': 'error',
  'vue/no-multiple-slot-args': 'error',
  'vue/no-required-prop-with-default': 'error',
  'vue/this-in-template': 'error',
  'vue/one-component-per-file': 'error',
  // v-html 是 XSS 入口，保持开启，唯一例外在 MarkdownRenderer.vue 里就地豁免（详见该文件注释）。
  'vue/no-v-html': 'error',
}

export default [
  js.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    rules: {
      ...vueCorrectnessRules,
      // views 下的组件按路由命名（Chat / Knowledge / Layout / Login），是单文件路由视图的既定
      // 约定；改名会连带改动 router 与各处引用，不属于本次引入 lint 的范围。只豁免这四个名字，
      // 其余组件仍受该规则约束。
      'vue/multi-word-component-names': [
        'error',
        { ignores: ['Chat', 'Knowledge', 'Layout', 'Login'] },
      ],
    },
  },
  {
    // 前端源码直接使用 localStorage / document / navigator / fetch / FormData /
    // AbortController / TextDecoder 等浏览器全局量，不声明的话 no-undef 会误报。
    files: ['src/**/*.{js,vue}'],
    languageOptions: { globals: { ...globals.browser } },
  },
  {
    ignores: ['dist/**', 'coverage/**', 'playwright-report/**', 'test-results/**'],
  },
]
