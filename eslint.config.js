import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

// 作用域与 static-checks.yml 的前端门禁一致：src/（全部 .js 与 .vue）、tests/、scripts/。
// 这也是原来 node --check 覆盖的范围，换上 eslint 后多出 .vue 与真正的规则检查，只强不弱。
//
// tests/ 与 scripts/ 是 issue #203 补上的。此前这两处不在任何静态检查范围内——包括
// scripts/check_*.mjs 这几个门禁实现体自己。纳入前实测（eslint 10.11.0）共 155 处：
// tests/ 117、scripts/ 38。其中 145 处是 no-undef，且**全部**是环境全局量
// （tests/ 114：document / localStorage / Event / MouseEvent / File / URL / setTimeout /
// process / Buffer ...；scripts/ 31：console / process），属于声明缺失而非代码问题，
// 由下面两个 files 块的 globals 解决。
// 剩下 10 处是真问题，已就地最小修复：tests/ 3 处 no-unused-vars（去掉多余绑定与未用导入），
// scripts/ 7 处 no-irregular-whitespace + no-useless-escape（正则里的 U+3000 / U+FEFF 隐形
// 字符与冗余 \[ 转义，改写成语义等价的 \uXXXX 转义；改写前后用 30 条语料做过匹配行为比对，
// 差异为 0）。
// 这条待办到此关闭，不存在「以后再说」的残留。

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
    // 用例跑在 Node 里（node --test + node:module），但挂载类用例会把 jsdom 的一批浏览器
    // 全局量装到 globalThis 上再跑（见 tests/helpers/vueMount.js 的说明与它注入的键），
    // 所以两边的全局量都要声明：少声明哪一边，都会把合法引用报成 no-undef。
    files: ['tests/**/*.{js,mjs}'],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
  {
    // 门禁脚本（scripts/check_*.mjs 等）是纯粹的命令行 Node 程序。这里**只**声明 Node
    // 全局量、不给浏览器全局量：脚本里出现 document / localStorage 一定是笔误，
    // 放行反而会把真错藏起来。
    files: ['scripts/**/*.{js,mjs}'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    ignores: ['dist/**', 'coverage/**', 'playwright-report/**', 'test-results/**'],
  },
]
