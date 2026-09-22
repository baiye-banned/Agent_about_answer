import test from 'node:test'
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'

// 会话列表分页（issue #191 引入、评审条件③点名缺用例）：fetchConversations /
// loadMoreConversations / hasMoreConversations。
//
// 只替换网络层（@/api/chat），跑的是真实的 src/stores/chat.js，口径同 tests/chatStore.test.js：
// 请求进队列，用例自己决定什么时候返回、返回什么，因此可以断言「翻页带过去的
// (updated_at, id) 复合游标是不是那一个」，而不只是数调用次数。
//
// 与 chatStore.test.js 分开成文件，是因为那边的桩把 getConversations 的入参丢掉了
// （只回一个 resolve），而分页要断言的恰恰是入参；两边的桩口径相同，只是这一个记参数。
const stubSource = [
  'const pending = []',
  'export const getConversationsCalls = []',
  'export const chatAPI = {',
  '  getConversations: (params) => new Promise((resolve, reject) => {',
  '    getConversationsCalls.push(params)',
  '    pending.push({ resolve, reject, params })',
  '  }),',
  '}',
  // chat.js 在模块层 import 了 streamChat，缺了它整个模块加载不起来。
  'export function streamChat() {}',
  'function take() {',
  '  const entry = pending.shift()',
  "  if (!entry) throw new Error('没有在飞的会话列表请求')",
  '  return entry',
  '}',
  'export function respond(data) { take().resolve(data) }',
  'export function respondError(error) { take().reject(error) }',
  'export function pendingCount() { return pending.length }',
  'export function resetStub() {',
  '  pending.length = 0',
  '  getConversationsCalls.length = 0',
  '}',
  '',
].join('\n')

export const chatApiStub = `data:text/javascript,${encodeURIComponent(stubSource)}`

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@/api/chat') {
      return { url: chatApiStub, shortCircuit: true }
    }
    if (specifier.startsWith('@/')) {
      return {
        url: new URL(`../src/${specifier.slice(2)}.js`, import.meta.url).href,
        shortCircuit: true,
      }
    }
    return nextResolve(specifier, context)
  },
})

const { createPinia, setActivePinia } = await import('pinia')
const { useChatStore } = await import('../src/stores/chat.js')
const { respond, respondError, resetStub, getConversationsCalls, pendingCount } =
  await import(chatApiStub)

// 与 src/stores/chat.js 的常量对齐：一次取一页，多要一条判断「还有没有更早的」。
const PAGE_SIZE = 50
const FETCH_LIMIT = PAGE_SIZE + 1

const stamp = (n) => new Date(Date.UTC(2026, 8, 1, 0, 0, n)).toISOString()
const conv = (id, updatedAt) => ({ id, title: id, updated_at: updatedAt })
const range = (from, to) => Array.from({ length: to - from + 1 }, (_, index) => from + index)
const rows = (from, to) => range(from, to).map((n) => conv(`c-${n}`, stamp(n)))
const idsOf = (store) => store.conversations.map((item) => item.id)

function createStore() {
  setActivePinia(createPinia())
  resetStub()
  return useChatStore()
}

function fetchWith(store, data) {
  const request = store.fetchConversations()
  respond(data)
  return request
}

test('初始没有「还有更多」：还没取过数就不该出现加载入口', async () => {
  const store = createStore()
  assert.equal(store.hasMoreConversations, false)
})

test('首屏只取一页，游标取该页末位（用翻页请求的入参反证）', async () => {
  const store = createStore()

  const request = store.fetchConversations()
  assert.equal(store.loading, true)
  assert.deepEqual(getConversationsCalls, [{ limit: FETCH_LIMIT }], '首屏不带游标')

  respond(rows(1, PAGE_SIZE + 1))
  await request

  assert.deepEqual(idsOf(store), range(1, PAGE_SIZE).map((n) => `c-${n}`))
  assert.equal(store.hasMoreConversations, true)
  assert.equal(store.loading, false)

  const load = store.loadMoreConversations()
  assert.deepEqual(getConversationsCalls.at(-1), {
    limit: FETCH_LIMIT,
    before_updated_at: stamp(PAGE_SIZE),
    before_id: `c-${PAGE_SIZE}`,
  })
  respond([])
  await load
})

test('取不满一页时没有更多，翻页是纯 no-op', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, 10))
  assert.equal(store.hasMoreConversations, false)
  const callsAfterFetch = getConversationsCalls.length

  await store.loadMoreConversations()

  assert.equal(getConversationsCalls.length, callsAfterFetch, '到底之后不该再发请求')
  assert.equal(pendingCount(), 0)
  assert.equal(store.conversations.length, 10)
})

test('翻页：追加到列表末尾、复合游标前进、取不满一页即到底', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1))
  assert.equal(store.hasMoreConversations, true)

  const first = store.loadMoreConversations()
  assert.deepEqual(getConversationsCalls.at(-1), {
    limit: FETCH_LIMIT,
    before_updated_at: stamp(PAGE_SIZE),
    before_id: `c-${PAGE_SIZE}`,
  })
  respond(rows(PAGE_SIZE + 1, 2 * PAGE_SIZE + 1))
  await first

  assert.equal(store.conversations.length, 2 * PAGE_SIZE, '更早的一页接在末尾')
  assert.equal(store.hasMoreConversations, true)

  const second = store.loadMoreConversations()
  assert.deepEqual(
    getConversationsCalls.at(-1),
    {
      limit: FETCH_LIMIT,
      before_updated_at: stamp(2 * PAGE_SIZE),
      before_id: `c-${2 * PAGE_SIZE}`,
    },
    '游标必须换成这一页的末位'
  )
  respond(rows(2 * PAGE_SIZE + 1, 2 * PAGE_SIZE + 30))
  await second

  assert.equal(store.conversations.length, 2 * PAGE_SIZE + 30)
  assert.equal(store.hasMoreConversations, false, '取不满一页 -> 到底')

  const callsAtBottom = getConversationsCalls.length
  await store.loadMoreConversations()
  assert.equal(getConversationsCalls.length, callsAtBottom)
})

test('与已加载内容重叠的返回逐条去重，不产生重复行', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1)) // c-1..c-50

  const request = store.loadMoreConversations()
  respond(rows(40, 90)) // 51 条，前 11 条与已加载内容重叠
  await request

  const ids = idsOf(store)
  assert.equal(new Set(ids).size, ids.length, '不能有重复会话')
  assert.equal(ids.length, 89) // c-1..c-50 + c-51..c-89
  assert.equal(ids.at(-1), 'c-89')
})

test('末位缺 updated_at 时不给「还有更多」：给不出游标就不给入口', async () => {
  const store = createStore()
  const page = rows(1, PAGE_SIZE)
  page[PAGE_SIZE - 1] = { id: `c-${PAGE_SIZE}`, title: '没有 updated_at' }

  await fetchWith(store, [...page, conv(`c-${PAGE_SIZE + 1}`, stamp(PAGE_SIZE + 1))])

  assert.equal(store.hasMoreConversations, false)

  const calls = getConversationsCalls.length
  await store.loadMoreConversations()
  assert.equal(getConversationsCalls.length, calls)
})

test('响应不是数组时回落到空列表，且没有更多', async () => {
  const store = createStore()
  await fetchWith(store, null)

  assert.deepEqual(store.conversations, [])
  assert.equal(store.hasMoreConversations, false)
})

test('取数失败：loading 复位并把拒绝交给调用方', async () => {
  const store = createStore()

  const request = store.fetchConversations()
  respondError(new Error('会话接口不可用'))
  await assert.rejects(request, /会话接口不可用/)

  assert.equal(store.loading, false)
  assert.deepEqual(store.conversations, [])
})

test('重新取数会把列表换回最新一页，并重置游标与「还有更多」', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1))
  const load = store.loadMoreConversations()
  respond(rows(PAGE_SIZE + 1, 2 * PAGE_SIZE + 1))
  await load
  assert.equal(store.conversations.length, 2 * PAGE_SIZE)

  const refresh = store.fetchConversations()
  respond(rows(1, PAGE_SIZE + 1))
  await refresh

  assert.equal(store.conversations.length, PAGE_SIZE, '刷新只取最新一页，不保留已翻出的更早会话')
  assert.equal(store.hasMoreConversations, true)

  const next = store.loadMoreConversations()
  assert.deepEqual(
    getConversationsCalls.at(-1),
    {
      limit: FETCH_LIMIT,
      before_updated_at: stamp(PAGE_SIZE),
      before_id: `c-${PAGE_SIZE}`,
    },
    '游标要回到新一页的末位，不能用刷新前的旧游标'
  )
  respond([])
  await next
})

test('在途时重复触发不叠加请求', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1))

  const first = store.loadMoreConversations()
  const second = store.loadMoreConversations()

  assert.equal(pendingCount(), 1, '第二次触发必须被 loadingMoreConversations 挡住')
  assert.equal(getConversationsCalls.length, 2, '只有首屏那一次与这一次翻页')

  respond(rows(PAGE_SIZE + 1, PAGE_SIZE + 2))
  await Promise.all([first, second])

  assert.equal(store.conversations.length, PAGE_SIZE + 2)
})
