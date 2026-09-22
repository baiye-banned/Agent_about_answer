import test from 'node:test'
import assert from 'node:assert/strict'

// 覆盖 src/utils/fileListRequest.js：Knowledge.vue 的文件列表（fetchFiles →
// knowledgeAPI.getList）走的就是这里的 load()，切库走 handleKnowledgeBaseChange。
// 锁的是「列表内容必须属于当前选中的知识库」这条时序不变量（issue #83 第 8 项，
// 与 #13 的会话切换同族，这里是不同视图、不同请求）。
import { createFileListRequest } from '../src/utils/fileListRequest.js'

const file = (id, name) => ({ id, name, size: 1024, created_at: '2026-09-01T10:00:00Z' })

// 手动控制的取数桩：每个知识库一条待决队列，用例自己决定谁先返回。
function setup({ pageSize = 50 } = {}) {
  const state = { files: [], loading: false, hasMore: false }
  const calls = []
  const paramsLog = []
  const pending = new Map()
  let currentId = null

  const fetchList = (params) => {
    calls.push(params.knowledge_base_id)
    paramsLog.push(params)
    return new Promise((resolve, reject) => {
      const queue = pending.get(params.knowledge_base_id) || []
      queue.push({ resolve, reject })
      pending.set(params.knowledge_base_id, queue)
    })
  }

  const requests = createFileListRequest({
    getKnowledgeBaseId: () => currentId,
    fetchList,
    pageSize,
    // 与视图同款：append 追加在已有列表后面（这里只断言追加语义本身）。
    applyFiles: (files, { append } = {}) => {
      state.files = append ? [...state.files, ...files] : files
    },
    applyLoading: (value) => {
      state.loading = value
    },
    applyHasMore: (value) => {
      state.hasMore = value
    },
  })

  const take = (id) => {
    const queue = pending.get(id) || []
    const entry = queue.shift()
    pending.set(id, queue)
    assert.ok(entry, `没有在飞的请求：${id}`)
    return entry
  }

  return {
    state,
    requests,
    calls,
    paramsLog,
    select: (id) => {
      currentId = id
    },
    respond(id, files) {
      take(id).resolve(files)
    },
    respondError(id, error) {
      take(id).reject(error)
    },
  }
}

test('慢库 A → 快库 B → A 迟到：迟到的旧库响应不得覆盖当前库的列表', async () => {
  const { state, requests, calls, select, respond } = setup()

  select('A')
  const loadA = requests.load()
  assert.equal(state.loading, true)

  // A 还没回来就切到 B。
  select('B')
  const loadB = requests.load()

  respond('B', [file(2, 'B-员工手册.md')])
  await loadB
  assert.deepEqual(state.files.map((item) => item.id), [2])
  assert.equal(state.loading, false)

  // A 的迟到响应到达：整条丢弃，列表必须仍是 B 的。
  respond('A', [file(1, 'A-年度报告.md')])
  await loadA

  assert.deepEqual(state.files.map((item) => item.id), [2])
  assert.equal(state.files[0].name, 'B-员工手册.md')
  assert.deepEqual(calls, ['A', 'B'])
})

test('陈旧响应不得提前收起 loading', async () => {
  const { state, requests, select, respond } = setup()

  select('A')
  const loadA = requests.load()
  select('B')
  const loadB = requests.load()

  // 陈旧的 A 先回来：列表与 loading 都不能动，界面仍停在加载态等 B。
  respond('A', [file(1, 'A-年度报告.md')])
  await loadA
  assert.deepEqual(state.files, [])
  assert.equal(state.loading, true)

  respond('B', [file(2, 'B-员工手册.md')])
  await loadB
  assert.deepEqual(state.files.map((item) => item.id), [2])
  assert.equal(state.loading, false)
})

test('连续快速切多个库，乱序返回后只保留最后选中的那个库', async () => {
  const { state, requests, select, respond } = setup()
  const ids = ['A', 'B', 'C', 'D']

  const loads = ids.map((id) => {
    select(id)
    return requests.load()
  })

  // 返回顺序与切换顺序完全不同，最后返回的是最早发出的那次。
  for (const index of [3, 1, 0, 2]) {
    respond(ids[index], [file(index + 1, `${ids[index]}.md`)])
    await loads[index]
  }

  assert.deepEqual(state.files.map((item) => item.name), ['D.md'])
})

test('未选中知识库时同步清空，并作废在飞的旧库请求', async () => {
  const { state, requests, select, respond } = setup()

  select('A')
  const loadA = requests.load()

  // 切到「没有知识库」：列表立刻清空（不同步作废的话，A 回来又会把它填回去）。
  select(null)
  await requests.load()
  assert.deepEqual(state.files, [])
  assert.equal(state.loading, false)

  respond('A', [file(1, 'A-年度报告.md')])
  await loadA
  assert.deepEqual(state.files, [])
})

test('invalidate 作废在飞请求：组件卸载后到达的响应不再写状态', async () => {
  const { state, requests, select, respond } = setup()

  select('A')
  const loadA = requests.load()

  // 卸载：此后到达的响应整条丢弃。
  requests.invalidate()
  respond('A', [file(1, 'A-年度报告.md')])
  await loadA

  assert.deepEqual(state.files, [])
  assert.equal(state.loading, false)
})

test('响应不是数组时回落到空列表，不把非列表数据写进表格', async () => {
  const { state, requests, select, respond } = setup()

  select('A')
  const loadA = requests.load()
  respond('A', null)

  assert.deepEqual(await loadA, [])
  assert.deepEqual(state.files, [])
})

test('最新请求失败时复位 loading 并把拒绝交给调用方', async () => {
  const { state, requests, select, respondError } = setup()
  const failure = new Error('接口不可用')

  select('A')
  const loadA = requests.load()
  respondError('A', failure)

  await assert.rejects(loadA, (error) => error === failure)
  assert.equal(state.loading, false)
})

test('陈旧请求的失败不扰动当前列表，也不复位最新请求的 loading', async () => {
  const { state, requests, select, respond, respondError } = setup()

  select('A')
  const loadA = requests.load()
  select('B')
  const loadB = requests.load()

  respondError('A', new Error('A 已失效'))
  await assert.rejects(loadA)
  assert.equal(state.loading, true)

  respond('B', [file(2, 'B-员工手册.md')])
  await loadB
  assert.deepEqual(state.files.map((item) => item.id), [2])
  assert.equal(state.loading, false)
})

// ---------------------------------------------------------------- issue #191：按需翻页

// 造一页数据：后端按「新 -> 旧」返回，多要的那一条在末尾。
const pageOf = (startId, count) =>
  Array.from({ length: count }, (_, index) => file(startId - index, `f-${startId - index}.txt`))

test('第一页取满时给出「还有更早的文件」并带上探测用的多一条', async () => {
  const { state, requests, paramsLog, select, respond } = setup({ pageSize: 3 })

  select('A')
  const loadA = requests.load()
  respond('A', pageOf(9, 4)) // 3 + 1：多出来的那条说明还有更多

  await loadA

  assert.deepEqual(paramsLog[0], { knowledge_base_id: 'A', limit: 4 })
  assert.deepEqual(state.files.map((item) => item.id), [9, 8, 7]) // 收下正好一页，丢掉探测条
  assert.equal(state.hasMore, true)
})

test('取不满一页时不再给出加载入口', async () => {
  const { state, requests, select, respond } = setup({ pageSize: 3 })

  select('A')
  const loadA = requests.load()
  respond('A', pageOf(2, 2))

  await loadA

  assert.deepEqual(state.files.map((item) => item.id), [2, 1])
  assert.equal(state.hasMore, false)
})

test('loadMore 用末位 id 作游标取更早的一页并追加在后面', async () => {
  const { state, requests, paramsLog, select, respond } = setup({ pageSize: 3 })

  select('A')
  const loadA = requests.load()
  respond('A', pageOf(9, 4))
  await loadA

  const moreA = requests.loadMore()
  assert.deepEqual(paramsLog[1], { knowledge_base_id: 'A', limit: 4, before_id: 7 })
  respond('A', [file(6, 'f-6.txt'), file(5, 'f-5.txt')])
  await moreA

  assert.deepEqual(state.files.map((item) => item.id), [9, 8, 7, 6, 5])
  assert.equal(state.hasMore, false) // 这一页没取满：已经到最早一个
})

test('切换知识库后，迟到的旧库翻页结果不得拼进新库的列表', async () => {
  const { state, requests, select, respond } = setup({ pageSize: 2 })

  select('A')
  const loadA = requests.load()
  respond('A', pageOf(9, 3))
  await loadA
  assert.equal(state.hasMore, true)

  const moreA = requests.loadMore()
  // 翻页还没回来就切到 B，并加载出 B 的第一页。
  select('B')
  const loadB = requests.load()
  respond('B', [file(100, 'B-1.txt')])
  await loadB

  respond('A', [file(7, 'A-7.txt'), file(6, 'A-6.txt')])
  await moreA

  assert.deepEqual(state.files.map((item) => item.id), [100])
  assert.equal(state.hasMore, false)
})

test('重新加载（切库/刷新）会把游标重置到第一页', async () => {
  const { requests, paramsLog, select, respond } = setup({ pageSize: 2 })

  select('A')
  const loadA = requests.load()
  respond('A', pageOf(9, 3))
  await loadA

  select('B')
  const loadB = requests.load()
  respond('B', pageOf(5, 3))
  await loadB

  // 新库的第一页请求不带 before_id：游标没有跨库复用。
  assert.deepEqual(paramsLog[1], { knowledge_base_id: 'B', limit: 3 })

  const moreB = requests.loadMore()
  assert.deepEqual(paramsLog[2], { knowledge_base_id: 'B', limit: 3, before_id: 4 })
  respond('B', [])
  await moreB
})

test('没有下一页时 loadMore 不发请求', async () => {
  const { requests, paramsLog, select, respond } = setup({ pageSize: 3 })

  select('A')
  const loadA = requests.load()
  respond('A', pageOf(1, 1))
  await loadA

  assert.equal(await requests.loadMore(), null)
  assert.equal(paramsLog.length, 1)
})
