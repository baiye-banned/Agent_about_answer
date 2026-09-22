import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

// 作用域与 static-checks.yml 的前端门禁一致：全仓（`npx eslint .`）——src/（全部 .js 与 .vue）、
// tests/、scripts/，外加仓库根的构建配置。这也是 node --check 覆盖的范围，换上 eslint 后
// 多出 .vue 与真正的规则检查，只强不弱。
//
// 仓库根的构建配置（eslint / postcss / tailwind / vite.config.js）是 issue #208 补上的。它们决定
// 「别的文件怎么被检查、怎么被构建」，此前却是全仓唯一不在任何门禁覆盖面里的一类文件。
// 它们失守时**不一定报红**：配置语法坏掉那种会让配置加载失败，谈不上隐蔽——旧命令与 `eslint .`
// 都是 exit 2（配置加载失败），且改成 `eslint .` 之后，连不依赖 npm ci 的 node --check 也会先
// 拦住它（实测 exit 1）。真正危险的是「能加载但被改松」——摘掉某个规则集、把目录塞进 ignores、
// 放宽某条规则——此时前端 lint 静默变弱而 CI 依旧全绿。
// **这道门禁守不住「改松」**：实测给本文件加一条 `ignores: ['src/**']` 之后，`eslint .` 依旧
// exit 0，只是检查文件数从 80 静默掉到 45。纳入根级配置买到的是「配置被写坏会立刻报红」与
// 「根级不再有覆盖空洞」这两件事，改松与否仍然只能靠人看 diff，不假装 lint 能替人做这件事。
// 把作用域从 `eslint src tests scripts` 换成 `eslint .` 是等价的扩法而不是重划：实测检查文件数
// 76 → 80，新增的恰好是这 4 个文件，**无一处丢失覆盖**（差集逐文件比对过）。
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
    // 仓库根的构建配置（eslint/postcss/tailwind/vite.config.js）是 Node ESM：由各自的 CLI 在
    // Node 里加载，写成函数时还能拿到 process.env 这类 Node 全局量。不声明的话，vite.config.js
    // 里最常见的 `process.env.X` 会被报成 no-undef——实测把 `strictPort: true` 改成
    // `strictPort: process.env.VITE_STRICT_PORT !== '0'` 后就是 exit=1，那是环境全局量声明缺失，
    // 不是代码问题，与上面 tests/、scripts/ 两块同理。
    // 与 scripts/ 一样**只**声明 Node 全局量：构建配置里出现 document / localStorage 一定是笔误。
    // 模式不含斜杠，按 flat config 的语义锚在**本配置所在目录（仓库根）**，不会漏进 src/：
    // 实测在 src/ 下放一个同名的 `_probe.config.js`，它不受本块影响（仍按 src 的 browser 全局量判）。
    files: ['*.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    ignores: ['dist/**', 'coverage/**', 'playwright-report/**', 'test-results/**'],
  },
]
