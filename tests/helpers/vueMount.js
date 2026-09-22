// 单文件组件（SFC）挂载工具：让 `node --test` 能真正 mount 一个 .vue 视图。
//
// 仓库既有的前端用例做两件事：纯函数级行为断言（submit 到 src/utils 下的模块），
// 以及静态读文件断言接线（knowledgeViewWiring.test.js 那种）。两者都执行不到 SFC 的
// <script setup>：胶水层（调哪个 helper、toast 级别、失败时状态复位）至今只靠阅读。
//
// 这里不引入 vitest / @vue/test-utils 之类的新体系，全部走 package.json 里已有的依赖：
//   @vue/compiler-sfc   把 SFC 编译成模块代码（由 vue 自己的 dependencies 带来，
//                       不是本仓库的直接依赖，package.json 里查不到它）
//   jsdom               提供 document（已有的 devDependency）
//   vue / element-plus  已有的运行时依赖
//
// 依赖顺序有硬约束：vue 的 runtime-dom 在模块求值时就抓走 document
// （`const doc = typeof document !== 'undefined' ? document : null`），
// 所以 jsdom 的全局变量必须在 import('vue') 之前装好。本模块在顶层做这件事，
// 调用方只要 `import { mountSfc } from './helpers/vueMount.js'` 即自动满足顺序；
// 但**不要**在同一个文件里再静态 import vue，那样会抢在前面。

import { fileURLToPath, pathToFileURL } from 'node:url'
import { createRequire, register } from 'node:module'
import { JSDOM } from 'jsdom'

const SRC_ROOT = fileURLToPath(new URL('../../src/', import.meta.url))
const LOADER_URL = pathToFileURL(fileURLToPath(new URL('./vueSfcLoader.js', import.meta.url))).href

// 站 Vite 构建期 import.meta.env 的位置，见 vueSfcLoader.js 的说明。
globalThis.__VITE_ENV__ = Object.fromEntries(
  Object.entries(process.env).filter(([key]) => key.startsWith('VITE_'))
)

// ---------------------------------------------------------------------------
// jsdom 环境（必须在任何 vue import 之前执行）
// ---------------------------------------------------------------------------

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/',
  pretendToBeVisual: true,
})

// 列表按需增长，**只补真的缺过的**：Element Plus 在事件回调里直接 new 的构造器、以及它做
// 可聚焦性判断时直接 instanceof 的元素类，jsdom 的 window 上有、Node 全局没有，必须逐个
// 搬过来才在 Node 全局里可见。打开 el-dialog 这条路径就会踩到 —— 输入框的 focus/blur
// 处理器 new FocusEvent(...)，focus-trap 的 isSelectable 又 instanceof HTMLInputElement，
// 缺哪个都会在回调里抛 ReferenceError（既有的挂载用例都不开对话框，所以这个缺口此前没被
// 踩到），表现为「instanceof 抛 ReferenceError」与「new 构造器抛 ReferenceError」两种形态。
// 凡是 jsdom 没有的名字这里的 `continue` 会直接跳过，所以多写几个名字不会让别的用例变红。
for (const key of [
  'window',
  'document',
  'navigator',
  'HTMLElement',
  'HTMLInputElement',
  'SVGElement',
  'Element',
  'Node',
  'Event',
  'MouseEvent',
  'KeyboardEvent',
  'FocusEvent',
  'InputEvent',
  'ClipboardEvent',
  'CustomEvent',
  'MutationObserver',
  'requestAnimationFrame',
  'cancelAnimationFrame',
  'getComputedStyle',
  // vue-router 的 finalizeNavigation 直接读裸全局 `history`（jsdom 的 window 上有、
  // Node 全局没有），缺了它每次导航都以 `ReferenceError: history is not defined` 收场。
  'history',
  'location',
  'FormData',
  'File',
  'Blob',
  'localStorage',
  'sessionStorage',
  // el-dialog 挂载起来会走 focus-trap：它按元素类型做 instanceof 判定，
  // 并给 overlay 里的元素依次聚焦，因此下面这批元素类必须有。
  'HTMLInputElement',
  'HTMLTextAreaElement',
  'HTMLButtonElement',
  'HTMLSelectElement',
  'HTMLAnchorElement',
  'HTMLDivElement',
  'HTMLSpanElement',
  'HTMLFormElement',
  'HTMLImageElement',
  'HTMLLabelElement',
  'HTMLParagraphElement',
  'HTMLPreElement',
  'HTMLHeadingElement',
  'HTMLUListElement',
  'HTMLOListElement',
  'HTMLLIElement',
  'HTMLTableElement',
  'HTMLTableRowElement',
  'HTMLTableCellElement',
  'HTMLStyleElement',
  'HTMLTemplateElement',
  'NodeList',
  'Range',
  'Selection',
]) {
  if (dom.window[key] === undefined) continue
  // Node 22 把 navigator 定义成只取的全局属性（直接赋值抛 TypeError），
  // 所以一律走 defineProperty 覆盖。
  Object.defineProperty(globalThis, key, {
    value: dom.window[key],
    writable: true,
    configurable: true,
  })
}

// jsdom 不实现 Element.prototype.scrollTo（浏览器里是有的），而 Chat.vue 的
// scrollToBottom / loadOlderMessages 会调 `chatRef.value?.scrollTo(...)` —— 可选链挡不住
// 「方法不存在」，于是变成一条 unhandledRejection，而且往往在用例**结束之后**才冒出来，
// 记在整套输出上（看起来像别人的错）。滚动位置在 jsdom 里没有任何断言面，补个空实现。
if (typeof dom.window.Element.prototype.scrollTo !== 'function') {
  dom.window.Element.prototype.scrollTo = () => {}
}

// ---------------------------------------------------------------------------
// 模块替换 seam
// ---------------------------------------------------------------------------

// 装一次加载钩子。registry 随 data 进钩子线程，所以每次换 registry 都得重新注册；
// 多次注册是叠加的，**后注册的先被问到**，于是最近一次 mount 的替换表优先命中。
// 默认替换表：把 async-validator 的 CJS 入口换成拆掉一层包装的垫片，
// 让 Element Plus 的表单校验在 Node 下真的生效（详见该文件里的说明）。
// 调用方自己的 modules 优先级更高。
function defaultModules() {
  const shim = pathToFileURL(fileURLToPath(new URL('./asyncValidatorInterop.js', import.meta.url))).href
  const targets = {}
  try {
    const entry = createRequire(import.meta.url).resolve('async-validator').replace(/\\/g, '/')
    targets[entry] = shim
  } catch {
    // 依赖树里没有 async-validator 时无事可做，挂载用例自己会暴露症状。
  }
  return targets
}

function registerLoader(modules) {
  // data 要过结构化克隆进钩子线程：URL 实例克隆不了（DataCloneError），
  // 统一降成字符串再传。
  const registry = Object.fromEntries(
    Object.entries({ ...defaultModules(), ...(modules || {}) }).map(([key, value]) => [
      key,
      value instanceof URL ? value.href : value,
    ])
  )
  register(LOADER_URL, {
    data: { registry, srcRoot: SRC_ROOT },
  })
}

/**
 * 真正挂载一个 SFC。
 *
 * SFC 模板里 <el-switch> 这类标签靠 unplugin-vue-components 在构建期自动解析；
 * 单独用 compiler-sfc 编译时它们退化成 resolveComponent('el-switch')，
 * 所以这里 `app.use(ElementPlus)` 装上真实组件，而不是塞桩件——
 * v-model、禁用态走的是 Element Plus 自己的实现，不是我们造的假货。
 *
 * @param {string} filePath 相对 src/ 的路径（如 'components/TraceVariableFlow.vue'）
 * @param {{
 *   props?: object,
 *   modules?: Record<string, string>,  // 相对 src 的模块路径 -> 替代源码
 *   pinia?: boolean,
 *   stubs?: Record<string, any>,
 *   plugins?: any[],  // 额外装的 app 级插件（如 vue-router 实例）；按传入顺序 use
 * }} options
 */
export async function mountSfc(filePath, options = {}) {
  // 必须在 import('vue') 之前，见文件头的顺序说明。
  registerLoader(options.modules)

  const url = pathToFileURL(SRC_ROOT + filePath).href
  const [vue, component] = await Promise.all([import('vue'), import(url)])
  const elementPlus = await import('element-plus')
  const ElementPlus = elementPlus.default

  const host = document.createElement('div')
  document.body.appendChild(host)

  // 用一层极薄的反应式宿主传 props，而不是 createApp(Component, props)——
  // 后者把 props 焊死，改不动；本仓库的挂载用例要覆盖「props 变了之后组件怎么复位」。
  // 宿主只做 h(Component, props) 转发，不含任何被测逻辑。
  const props = vue.reactive({ ...(options.props || {}) })
  const Host = {
    name: 'SfcHost',
    setup() {
      return () => vue.h(component.default ?? component, { ...props })
    },
  }

  const app = vue.createApp(Host)
  app.use(ElementPlus)
  for (const [name, impl] of Object.entries(options.stubs || {})) app.component(name, impl)
  // vue-router 这类要提供 inject 的插件必须在这里装：Chat.vue / Layout.vue / Login.vue
  // 用 useRoute() / useRouter() 取路由，缺了它拿到的是 undefined 而不是报错，
  // 症状会漂成「读 route.path 时抛」——看不出是路由没装。
  for (const plugin of options.plugins || []) app.use(plugin)

  let pinia = null
  if (options.pinia !== false) {
    // createPinia 在 pinia 包里，vue 不导出它；动态 import 是为了走上面的加载钩子，
    // 而不是可选链兜底。
    pinia = (await import('pinia')).createPinia()
    app.use(pinia)
  }

  app.mount(host)

  // ElMessage 必须在 element-plus 已经加载之后才拿得到与组件同一个实例；
  // 由本函数交出去，调用方就不必自己 import('element-plus')——
  // 那样会抢在加载钩子注册之前把 element-plus 及其 async-validator 冻进模块缓存。
  const messages = []
  const messageTargets = ['success', 'error', 'warning', 'info']
  const originals = {}
  for (const level of messageTargets) {
    originals[level] = elementPlus.ElMessage[level]
    elementPlus.ElMessage[level] = (message) => messages.push({ level, message })
  }

  return {
    app,
    vue,
    pinia,
    props,
    host,
    elementPlus,
    // 记录到的 toast：{ level, message }，level 即 success / error / warning。
    messages,
    elementPlusUnpatch() {
      for (const level of messageTargets) elementPlus.ElMessage[level] = originals[level]
    },
    get html() {
      return host.innerHTML
    },
    // 挂载后改 props：等价于 @vue/test-utils 的 setProps。
    async setProps(patch) {
      Object.assign(props, patch)
      await vue.nextTick()
    },
    async nextTick() {
      await vue.nextTick()
    },
    // 让挂载后 new 的 microtask / promise 链（上传、请求替身）跑完。
    async flush(times = 3) {
      for (let index = 0; index < times; index += 1) {
        await vue.nextTick()
        await new Promise((resolve) => setTimeout(resolve, 0))
      }
    },
    text() {
      return host.textContent
    },
    query(selector) {
      return host.querySelector(selector)
    },
    queryAll(selector) {
      return [...host.querySelectorAll(selector)]
    },
    buttonByText(label) {
      return [...host.querySelectorAll('button')].find((node) => node.textContent.trim() === label)
    },
    async unmount() {
      app.unmount()
      host.remove?.()
    },
  }
}

export { dom }
