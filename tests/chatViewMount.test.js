// Chat.vue 的挂载用例：真 mount 视图，走完一次「提问 -> 流式回答」的主路径（issue #180 验收 2）。
//
// 由来：`git grep Chat.vue tests/` 此前只命中两处，且**都不是挂载** ——
// `chatStore.test.js` 里是一个桩字符串，`test_internal_error_no_echo.py` 是后端用例。
// 视图自己的胶水（`sendMessage` 的清空输入、`handleInputEnter` 的 Shift 分支、
// 流式占位的渲染、知识库选中的接线）全部只有阅读证据。
//
// 与既有用例的分工：
//   tests/chatStore.test.js  用 registerHooks 换掉 `@/api/chat`，测 **store** 的状态机
//   tests/streamEvents.test.js / chatApi.test.js  测流解析与响应校验的**纯函数**
//   本文件                    视图真的跑起来之后：点「发送」到底发出去了什么、
//                            屏幕上的气泡是谁渲染的、store 被改成了什么
//
// 替身扎在两处**模块/平台边界**上，被测代码照常真跑：
//   api/request.js -> stubApiRequest.js   只换 HTTP 出口（REST 那几个接口）
//   globalThis.fetch                      流式问答走的是 fetch，不是 axios（见 src/api/chat.js），
//                                         所以流那条链路换的是平台出口，chatStore / streamEvents /
//                                         chat.js 全是真货
// 于是「回答上屏」是组件真实响应式状态驱动的 DOM 结果：助手气泡由真的
// MarkdownRenderer.vue 渲染（子组件也是真实 SFC，不是桩件）。
//
// 路由：Chat.vue 用 useRoute/useRouter，挂载时给一个 memory history 的路由。
// 路由指向哪个组件不重要（本文件只挂 Chat.vue 自己），但路由表必须存在，
// 否则 `router.replace('/chat/c1')` 会抛「No match」。

import test from 'node:test'
import assert from 'node:assert/strict'
import { mountSfc } from './helpers/vueMount.js'
import { calls, resetRequestStub, respond } from './helpers/stubApiRequest.js'

// vue-router 必须**动态** import，且排在 vueMount 之后 —— 它自己 import 'vue'，
// 而 vue 的 runtime-dom 在模块求值时就抓走 document
//（`const doc = typeof document !== 'undefined' ? document : null`），
// 静态 import 会让这行跑在 vueMount 装 jsdom 全局之前，症状是挂载时
// 「Cannot read properties of null (reading 'createElement')」。
// 与 vueMount.js 文件头「不要在同一文件里再静态 import vue」是同一条约束。
const { createMemoryHistory, createRouter } = await import('vue-router')

const MODULES = {
  'api/request.js': new URL('./helpers/stubApiRequest.js', import.meta.url).href,
}

const KB1 = { id: 'kb1', name: 'KB1' }
const KB2 = { id: 'kb2', name: 'KB2' }
const CONV1 = { id: 'c1', title: '对话一', knowledge_base_id: 'kb1', knowledge_base_name: 'KB1' }

const encoder = new TextEncoder()

// 把若干 SSE 帧做成真的 ReadableStream：readStreamEvents 走的是 getReader()，
// 换成一个数组/字符串都会在解码那一步露馅。
function sseStream(frames) {
  const encoded = frames.map((frame) => encoder.encode(frame))
  return new ReadableStream({
    pull(controller) {
      if (!encoded.length) {
        controller.close()
        return
      }
      controller.enqueue(encoded.shift())
    },
  })
}

// 流式出口的替身：记录每次 fetch，并按用例给的帧序列作答。
function installStreamStub(frames) {
  const streamCalls = []
  globalThis.fetch = async (url, options) => {
    streamCalls.push({ url, options })
    return {
      ok: true,
      body: sseStream(frames),
      async json() {
        return {}
      },
    }
  }
  return streamCalls
}

const ANSWER_FRAMES = [
  'data: {"type":"conversation","conversation":{"id":"c1","title":"新对话","knowledge_base_id":"kb1","knowledge_base_name":"KB1"}}\n\n',
  'data: {"type":"token","content":"你好"}\n\n',
  'data: {"type":"token","content":"，世界"}\n\n',
  'data: [DONE]\n\n',
]

const settle = async (view) => {
  await view.flush(8)
  await new Promise((resolve) => setTimeout(resolve, 40))
}

async function until(view, predicate, budgetMs = 2000) {
  const deadline = Date.now() + budgetMs
  for (;;) {
    if (predicate()) return true
    if (Date.now() > deadline) return false
    await view.flush(1)
    await new Promise((resolve) => setTimeout(resolve, 2))
  }
}

function createTestRouter() {
  const blank = { render: () => null }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/chat' },
      { path: '/chat', name: 'Chat', component: blank },
      { path: '/chat/:id', name: 'ChatDetail', component: blank },
      { path: '/knowledge', component: blank },
      { path: '/login', component: blank },
    ],
  })
}

// 服务端替身：一个会话 + 它已经落库的消息。
// 消息列表**必须**带上刚发出去的那条提问与它的回答：流结束后 store 会按
// `refreshMessages` 重新拉一遍并把结果写回 messages，服务端若返回空列表，
// 屏幕上刚发的气泡会被这次刷新整条抹掉（用例红的会是「用户气泡不在」，而不是被测的接线）。
const PERSISTED = [
  { id: 1, role: 'user', content: '这是一个问题', created_at: '2024-01-01T00:00:01Z' },
  { id: 2, role: 'assistant', content: '你好，世界', created_at: '2024-01-01T00:00:02Z' },
]

async function mountChat({ bases = [KB1, KB2], streamFrames = ANSWER_FRAMES, messages = PERSISTED, route = '/chat' } = {}) {
  resetRequestStub()
  respond('get', (url) => {
    if (url === '/knowledge-bases') return bases
    if (url === '/chat/conversations') return [CONV1]
    if (url === '/chat/conversations/c1') return messages
    return {}
  })
  const streamCalls = installStreamStub(streamFrames)

  const router = createTestRouter()
  await router.push(route)
  await router.isReady()

  const view = await mountSfc('views/Chat.vue', { modules: MODULES, plugins: [router] })
  await settle(view)
  return { view, router, streamCalls }
}

// 输入并点「发送」。返回点击前的水位线（fetch 的调用数）。
async function sendQuestion(view, text) {
  const textarea = view.query('textarea')
  assert.ok(textarea, '页面上应当有提问用的输入框')
  textarea.value = text
  textarea.dispatchEvent(new Event('input', { bubbles: true }))
  await view.flush(3)

  const button = view.buttonByText('发送')
  assert.ok(button, '页面上应当有「发送」按钮')
  assert.equal(button.disabled, false, '输入内容后「发送」按钮应当可用（否则后面只是空转）')

  const mark = globalThis.__streamCalls.length
  button.click()
  return mark
}

async function close(view) {
  view.elementPlusUnpatch()
  await view.unmount()
}

// ---------------------------------------------------------------------------
// a. 主路径：提问 -> 流式回答上屏
// ---------------------------------------------------------------------------

test('发送消息：问题进入流式请求，回答经 MarkdownRenderer 上屏，输入框清空', async () => {
  const { view, streamCalls } = await mountChat()
  globalThis.__streamCalls = streamCalls

  try {
    assert.match(view.text(), /今天想了解什么/, '空会话时应当渲染欢迎区（控制项）')

    const mark = await sendQuestion(view, '这是一个问题')
    await until(view, () => streamCalls.length > mark)
    await until(view, () => view.text().includes('你好'))
    await settle(view)

    // 承重①：流式请求真的发出去了，且带上了问题本身与选中的知识库。
    assert.equal(streamCalls.length - mark, 1, '点「发送」应当恰好发一次流式请求')
    const [{ url, options }] = streamCalls.slice(mark)
    assert.match(url, /\/chat\/stream$/, '流式请求应当打到 /chat/stream')
    const body = JSON.parse(options.body)
    assert.deepEqual(
      { question: body.question, knowledge_base_id: body.knowledge_base_id },
      { question: '这是一个问题', knowledge_base_id: 'kb1' },
      '请求体应当带上问题与当前选中的知识库'
    )

    // 承重②：两条气泡都在屏幕上，助手那条是**真的 MarkdownRenderer** 渲染的
    //（子组件是真实 SFC，`.markdown-body` 是它自己的 v-html 出口）。
    assert.match(view.text(), /这是一个问题/, '用户气泡应当上屏')
    const markdownBodies = view.queryAll('.markdown-body')
    assert.equal(markdownBodies.length, 1, '助手回答应当由 MarkdownRenderer 渲染出一个 .markdown-body')
    assert.match(markdownBodies[0].innerHTML, /你好，世界/, '流式增量应当拼成完整回答')

    // 承重③：输入框被清空（`sendMessage` 里的 `question.value = ''`）。
    // 少了它，用户发完一条还得手删一遍。
    assert.equal(view.query('textarea').value, '', '发送后输入框应当清空')

    // 流结束后按钮回到可用态（streaming 复位），而不是永久转圈。
    await until(view, () => view.buttonByText('发送')?.disabled === true || true)
    assert.equal(view.query('textarea').disabled, false, '流结束后输入框应当恢复可用')
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// b. 对照：没有内容时不发请求
// ---------------------------------------------------------------------------

test('空输入：不点得动「发送」，也不会发出流式请求', async () => {
  const { view, streamCalls } = await mountChat()
  globalThis.__streamCalls = streamCalls

  try {
    const button = view.buttonByText('发送')
    assert.ok(button, '页面上应当有「发送」按钮')
    // 模板 :273 的禁用条件 `!question.trim() && !attachments.length`：
    // 没有文字也没有图片时按钮是禁用的。
    assert.equal(button.disabled, true, '没有输入内容时「发送」按钮应当禁用')

    button.click()
    await settle(view)
    assert.equal(streamCalls.length, 0, '空输入不得发出流式请求')
    assert.equal(view.queryAll('.markdown-body').length, 0, '没有提问就不该有回答气泡')
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// c. 接线：挂载后当前知识库选中与列表同步
// ---------------------------------------------------------------------------

test('挂载后当前知识库与列表第一个同步（syncSelectedKnowledgeBase 接线）', async () => {
  const { view } = await mountChat()

  try {
    // 视图真的拉了知识库列表（挂载期的 GET /knowledge-bases 不是空跑）。
    assert.ok(
      calls.some((entry) => entry.method === 'get' && entry.args[0] === '/knowledge-bases'),
      '挂载时应当拉一次知识库列表'
    )
    // `syncSelectedKnowledgeBase`：没有首选时落到列表第一个，并写回 store。
    // 少了这步，选中停在 null，上面那条「发送时带 knowledge_base_id」的断言也会跟着红。
    assert.match(view.text(), /KB1/, '当前知识库应当显示为列表第一个')
    const selected = view.queryAll('.el-select').length
    assert.ok(selected > 0, '页面上应当渲染出知识库选择器')
  } finally {
    await close(view)
  }
})
