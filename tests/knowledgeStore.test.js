import test from 'node:test'
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'

// 桩模块：只替换网络层（@/api/knowledge），跑的是真实的 src/stores/knowledge.js。
// 口径与 tests/chatStore.test.js 一致：请求进队列，用例自己决定什么时候返回、返回什么。
// 只有这样才能断言「第二次 fetch 到底有没有再打网络」「翻页带过去的游标是不是那一个」，
// 而不是只数调用次数。
const stubSource = [
  'const pending = []',
  'export const getBasesCalls = []',
  'export const knowledgeAPI = {',
  '  getBases: (params) => new Promise((resolve, reject) => {',
  '    getBasesCalls.push(params)',
  '    pending.push({ resolve, reject, params })',
  '  }),',
  '}',
  'function take() {',
  '  const entry = pending.shift()',
  "  if (!entry) throw new Error('没有在飞的知识库请求')",
  '  return entry',
  '}',
  'export function respond(data) { take().resolve(data) }',
  'export function respondError(error) { take().reject(error) }',
  'export function pendingCount() { return pending.length }',
  'export function resetStub() {',
  '  pending.length = 0',
  '  getBasesCalls.length = 0',
  '}',
  '',
].join('\n')

export const knowledgeApiStub = `data:text/javascript,${encodeURIComponent(stubSource)}`

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@/api/knowledge') {
      return { url: knowledgeApiStub, shortCircuit: true }
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
const { useKnowledgeStore } = await import('../src/stores/knowledge.js')
const { respond, respondError, resetStub, getBasesCalls, pendingCount } =
  await import(knowledgeApiStub)

// 与 src/stores/knowledge.js 的常量对齐：一次取一页，且多要一条用来判断「还有没有」。
const PAGE_SIZE = 50
const FETCH_LIMIT = PAGE_SIZE + 1

const kb = (id, overrides = {}) => ({
  id,
  name: `知识库 ${id}`,
  file_count: 1,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-02T00:00:00Z',
  ...overrides,
})

const range = (from, to) => Array.from({ length: to - from + 1 }, (_, index) => from + index)
const rows = (from, to) => range(from, to).map((id) => kb(`kb-${id}`))
const idsOf = (store) => store.knowledgeBases.map((item) => item.id)

function createStore() {
  setActivePinia(createPinia())
  resetStub()
  return useKnowledgeStore()
}

// 发一次取数并立刻让它返回指定数据。取数在第 0 个微任务前就把请求压进桩队列，
// 所以这里「先发起、再 respond」不会出现竞态。
function fetchWith(store, data, { force = false } = {}) {
  const request = store.fetchKnowledgeBases(force)
  respond(data)
  return request
}

test('取数只取一页：多要的那条不入列，只用来点亮「还有更多」', async () => {
  const store = createStore()

  const request = store.fetchKnowledgeBases()
  assert.equal(store.loading, true)
  assert.deepEqual(getBasesCalls, [{ limit: FETCH_LIMIT }], '请求要带页大小，且首屏不带游标')

  respond(rows(1, PAGE_SIZE + 1))
  const result = await request

  assert.equal(result.length, PAGE_SIZE)
  assert.deepEqual(idsOf(store), range(1, PAGE_SIZE).map((id) => `kb-${id}`))
  assert.equal(store.hasMoreKnowledgeBases, true)
  assert.equal(store.loading, false)
  assert.equal(store.loaded, true)
})

test('第二次取数命中缓存：不再打网络，也不把 loading 置起来', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1))
  const snapshot = store.knowledgeBases
  const callsAfterFirst = getBasesCalls.length

  const second = store.fetchKnowledgeBases()
  // 短路发生在 loading 置位之前：这一行在「先置 loading 再判缓存」的写法下会红。
  assert.equal(store.loading, false)

  assert.deepEqual(await second, snapshot)
  assert.equal(getBasesCalls.length, callsAfterFirst, '第二次不该有新请求')
  assert.equal(pendingCount(), 0, '也不该留下悬着的请求')
})

test('force=true 绕过缓存，并整体替换列表与「还有更多」', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1))
  assert.equal(store.hasMoreKnowledgeBases, true)

  const request = store.fetchKnowledgeBases(true)
  assert.equal(store.loading, true)
  respond(rows(1, 3))
  await request

  assert.deepEqual(idsOf(store), ['kb-1', 'kb-2', 'kb-3'])
  assert.equal(store.hasMoreKnowledgeBases, false, '不足一页即没有更多')
  assert.equal(getBasesCalls.length, 2)
})

test('refreshKnowledgeBases 走的就是 force 路径：已有缓存照打网络', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, 2))
  assert.equal(store.loaded, true)

  const request = store.refreshKnowledgeBases()
  assert.deepEqual(getBasesCalls.at(-1), { limit: FETCH_LIMIT })
  respond(rows(5, 6))
  await request

  assert.deepEqual(idsOf(store), ['kb-5', 'kb-6'])
  assert.equal(getBasesCalls.length, 2)
})

test('取数失败：loading 复位、loaded 不置位，下一次仍会打网络', async () => {
  const store = createStore()

  const request = store.fetchKnowledgeBases()
  respondError(new Error('接口不可用'))
  await assert.rejects(request, /接口不可用/)

  assert.equal(store.loading, false)
  assert.equal(store.loaded, false)
  assert.equal(store.hasKnowledgeBases, false)

  const retry = store.fetchKnowledgeBases()
  assert.equal(getBasesCalls.length, 2, '失败不该被缓存挡住')
  respond(rows(1, 1))
  await retry
  assert.deepEqual(idsOf(store), ['kb-1'])
})

test('setKnowledgeBases 过滤脏数据，并给缺省字段补齐默认值', async () => {
  const store = createStore()

  // null / undefined / 字符串 / 数字都不是对象，归一化后是 null，必须被丢掉。
  store.setKnowledgeBases([kb('kb-1'), null, 'kb-2', undefined, 42, { id: 'kb-3' }])

  assert.deepEqual(idsOf(store), ['kb-1', 'kb-3'])
  assert.deepEqual(store.knowledgeBases[1], {
    id: 'kb-3',
    name: '',
    file_count: 0,
    created_at: '',
    updated_at: '',
  })
  assert.equal(store.loaded, true)
})

test('setKnowledgeBases 收到非数组时回落到空列表', async () => {
  const store = createStore()
  store.setKnowledgeBases([kb('kb-1')])
  assert.equal(store.hasKnowledgeBases, true)

  store.setKnowledgeBases(null)

  assert.deepEqual(store.knowledgeBases, [])
  assert.equal(store.hasKnowledgeBases, false)
})

test('hasKnowledgeBases 跟着列表走：空 -> 有 -> 再空', async () => {
  const store = createStore()
  assert.equal(store.hasKnowledgeBases, false)

  await fetchWith(store, rows(1, 2))
  assert.equal(store.hasKnowledgeBases, true)

  store.setKnowledgeBases([])
  assert.equal(store.hasKnowledgeBases, false)
})

test('upsertKnowledgeBase 同 id 就地合并：长度与顺序都不变', async () => {
  const store = createStore()
  store.setKnowledgeBases([kb('kb-1'), kb('kb-2'), kb('kb-3')])

  store.upsertKnowledgeBase(kb('kb-2', { name: '改过名的库' }))

  assert.deepEqual(idsOf(store), ['kb-1', 'kb-2', 'kb-3'], '不能变成追加出第四条')
  assert.equal(store.knowledgeBases[1].name, '改过名的库')
})

test('upsertKnowledgeBase 的合并以归一化结果为准：没传的字段回落默认值', async () => {
  const store = createStore()
  store.setKnowledgeBases([kb('kb-1', { file_count: 7, created_at: '2026-01-01T00:00:00Z' })])
  assert.equal(store.knowledgeBases[0].file_count, 7)

  store.upsertKnowledgeBase({ id: 'kb-1', name: '只改名字' })

  assert.equal(store.knowledgeBases[0].name, '只改名字')
  // 归一化后的对象五个字段总是齐的，所以「合并」在可观察层面就是「就地替换」：
  // 调用方没传的 file_count / created_at 会被重置成默认值，而不是保留旧值。
  assert.equal(store.knowledgeBases[0].file_count, 0)
  assert.equal(store.knowledgeBases[0].created_at, '')
})

test('upsertKnowledgeBase 新 id 追加到末尾', async () => {
  const store = createStore()
  store.setKnowledgeBases([kb('kb-1')])

  store.upsertKnowledgeBase(kb('kb-9', { name: '新建的库' }))

  assert.deepEqual(idsOf(store), ['kb-1', 'kb-9'])
})

test('upsertKnowledgeBase 脏数据不写状态', async () => {
  const store = createStore()
  store.setKnowledgeBases([kb('kb-1')])

  store.upsertKnowledgeBase(null)
  store.upsertKnowledgeBase('kb-2')
  store.upsertKnowledgeBase(42)

  assert.deepEqual(idsOf(store), ['kb-1'])
})

test('loadMoreKnowledgeBases：没有更多时是纯 no-op，不发请求也不动 loadingMore', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE)) // 正好一页 -> 没有更多
  assert.equal(store.hasMoreKnowledgeBases, false)
  const callsAfterFetch = getBasesCalls.length

  await store.loadMoreKnowledgeBases()

  assert.equal(getBasesCalls.length, callsAfterFetch, '到底之后不该再发请求')
  assert.equal(pendingCount(), 0)
  assert.equal(store.loadingMore, false)
})

test('loadMoreKnowledgeBases：按取回那一页的末位推进游标，取不满一页即到底', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1))
  assert.equal(store.hasMoreKnowledgeBases, true)

  const first = store.loadMoreKnowledgeBases()
  assert.equal(store.loadingMore, true)
  assert.deepEqual(getBasesCalls.at(-1), { limit: FETCH_LIMIT, after_id: 'kb-50' })
  respond(rows(PAGE_SIZE + 1, 2 * PAGE_SIZE + 1))
  await first

  assert.equal(store.knowledgeBases.length, 2 * PAGE_SIZE)
  assert.equal(store.hasMoreKnowledgeBases, true, '取满一页 -> 还有更多')
  assert.equal(store.loadingMore, false)

  const second = store.loadMoreKnowledgeBases()
  assert.deepEqual(getBasesCalls.at(-1), { limit: FETCH_LIMIT, after_id: 'kb-100' })
  respond(rows(2 * PAGE_SIZE + 1, 2 * PAGE_SIZE + 30))
  await second

  assert.equal(store.knowledgeBases.length, 2 * PAGE_SIZE + 30)
  assert.equal(store.hasMoreKnowledgeBases, false, '取不满一页 -> 到底')

  const callsAtBottom = getBasesCalls.length
  await store.loadMoreKnowledgeBases()
  assert.equal(getBasesCalls.length, callsAtBottom)
})

test('整页都与已加载内容重复时，游标照常前进而不是原地空转', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1)) // 游标 kb-50，还有更多

  // 列表被别的路径整体换成了更长的内容（setKnowledgeBases 不动游标），
  // 于是下一页整页都是已知 id：追加不会前进，游标必须跟着「取回的那一页」走。
  store.setKnowledgeBases(rows(1, 300))

  const request = store.loadMoreKnowledgeBases()
  respond(rows(PAGE_SIZE + 1, 2 * PAGE_SIZE + 1)) // kb-51..kb-101
  await request

  assert.equal(store.knowledgeBases.length, 300, '重复的一条都不该追加')
  assert.equal(new Set(idsOf(store)).size, 300, '也不该产生重复行')

  const next = store.loadMoreKnowledgeBases()
  assert.deepEqual(
    getBasesCalls.at(-1),
    { limit: FETCH_LIMIT, after_id: 'kb-100' },
    '游标必须前进到这一页的末位'
  )
  respond([])
  await next
})

test('与已加载内容重叠的返回只追加新的那些，不产生重复行', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1)) // kb-1..kb-50

  const request = store.loadMoreKnowledgeBases()
  respond(rows(40, 90)) // 51 条，前 11 条与已加载内容重叠
  await request

  const ids = idsOf(store)
  assert.equal(new Set(ids).size, ids.length, '不能有重复 id')
  assert.equal(ids.length, 89) // kb-1..kb-50 + kb-51..kb-89
  assert.equal(ids.at(-1), 'kb-89')
})

test('末位没有 id 时不给「加载更多」：宁可不给入口，也不给一个点了没反应的按钮', async () => {
  const store = createStore()
  const page = rows(1, PAGE_SIZE)
  page[PAGE_SIZE - 1] = { name: '没有 id 的脏数据' } // 归一化后 id 是 undefined

  await fetchWith(store, [...page, kb('kb-51')]) // 51 条 -> 本来「还有更多」

  assert.equal(store.hasMoreKnowledgeBases, false)

  const calls = getBasesCalls.length
  await store.loadMoreKnowledgeBases()
  assert.equal(getBasesCalls.length, calls)
})

test('在途时重复触发不叠加请求', async () => {
  const store = createStore()
  await fetchWith(store, rows(1, PAGE_SIZE + 1))

  const first = store.loadMoreKnowledgeBases()
  const second = store.loadMoreKnowledgeBases()

  assert.equal(pendingCount(), 1, '第二次触发必须被 loadingMore 挡住')
  assert.equal(getBasesCalls.length, 2, '只有首屏那一次与这一次翻页')

  respond(rows(PAGE_SIZE + 1, PAGE_SIZE + 2))
  await Promise.all([first, second])

  assert.equal(store.loadingMore, false)
  assert.equal(store.knowledgeBases.length, PAGE_SIZE + 2)
})
