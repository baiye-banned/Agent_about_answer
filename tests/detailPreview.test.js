import test from 'node:test'
import assert from 'node:assert/strict'
import { effect, nextTick, reactive } from 'vue'

// 覆盖 src/utils/detailPreview.js：Knowledge.vue 的详情弹窗（showDetail → knowledgeAPI.getContent）
// 走的就是这里的 open/close。锁的是「标题与正文必须来自同一份文件」这条时序不变量（#63）：
// 先点慢文件、再点快文件时，迟到的旧响应不得改写正文。
import {
  DETAIL_PREVIEW_EMPTY_TEXT,
  DETAIL_PREVIEW_ERROR_TEXT,
  createDetailPreview,
  createDetailPreviewState,
} from '../src/utils/detailPreview.js'

const file = (id, name) => ({ id, name, size: 1024, created_at: '2026-09-01T10:00:00Z' })

// 手动控制的取数桩：每个 id 一条待决队列，用例自己决定谁先返回。
// fetchContent 在 open() 的同步段内就被调用，因此 open() 返回时队列里已经有这一笔。
function setup({ state = createDetailPreviewState() } = {}) {
  const calls = []
  const pending = new Map()

  const fetchContent = (id) => {
    calls.push(id)
    return new Promise((resolve, reject) => {
      const queue = pending.get(id) || []
      queue.push({ resolve, reject })
      pending.set(id, queue)
    })
  }

  const take = (id) => {
    const queue = pending.get(id) || []
    const entry = queue.shift()
    pending.set(id, queue)
    assert.ok(entry, `没有在飞的请求：${id}`)
    return entry
  }

  const preview = createDetailPreview({ state, fetchContent })

  return {
    state,
    preview,
    calls,
    respond(id, content) {
      take(id).resolve({ id, name: `文件 ${id}`, content })
    },
    respondEmpty(id) {
      take(id).resolve({ id, name: `文件 ${id}` })
    },
    respondError(id, error) {
      take(id).reject(error)
    },
  }
}

const a = file(101, 'A-年度报告.md')
const b = file(102, 'B-员工手册.md')
const c = file(103, 'C-合同模板.md')
const d = file(104, 'D-入职指引.md')

test('先点慢文件再点快文件：迟到的响应不得改写正文，标题与正文始终同一份文件', async () => {
  const { state, preview, calls, respond } = setup()

  // 点开 A（慢，3000ms），弹窗先进入骨架屏。
  const openA = preview.open(a)
  assert.equal(state.file, a)
  assert.equal(state.content, '')
  assert.equal(state.loading, true)
  assert.equal(state.visible, true)

  // A 还没返回就点开 B（快，100ms）。标题立刻切到 B，正文清空。
  const openB = preview.open(b)
  assert.equal(state.file, b)
  assert.equal(state.content, '')

  respond(b.id, 'B 文件的正文（员工手册）')
  await openB
  assert.equal(state.content, 'B 文件的正文（员工手册）')
  assert.equal(state.loading, false)

  // A 的迟到响应到达：整条丢弃，不得把正文改写成 A 的。
  respond(a.id, 'A 文件的正文（年度报告全文）')
  await openA

  assert.equal(state.file, b)
  assert.equal(state.file.name, 'B-员工手册.md')
  assert.equal(state.content, 'B 文件的正文（员工手册）')
  assert.deepEqual(calls, [a.id, b.id])
})

test('连续快速点开多份文件，乱序返回后只保留最后一份的正文', async () => {
  const { state, preview, respond } = setup()
  const files = [a, b, c, d]

  const openings = files.map((item) => preview.open(item))
  assert.equal(state.file, d)

  // 返回顺序与点击顺序完全不同，最后返回的是最早发出的那次。
  for (const index of [3, 1, 0, 2]) {
    respond(files[index].id, `${files[index].name} 的正文`)
    await openings[index]
  }

  assert.equal(state.file, d)
  assert.equal(state.content, 'D-入职指引.md 的正文')
  assert.equal(state.loading, false)
})

test('骨架屏只由最新一次请求收起，陈旧响应不得提前复位 loading', async () => {
  const { state, preview, respond } = setup()

  const openA = preview.open(a)
  const openB = preview.open(b)

  // 陈旧的 A 先回来：正文与 loading 都不能动，界面仍停在骨架屏上等 B。
  respond(a.id, 'A 的正文')
  await openA
  assert.equal(state.content, '')
  assert.equal(state.loading, true)

  respond(b.id, 'B 的正文')
  await openB
  assert.equal(state.content, 'B 的正文')
  assert.equal(state.loading, false)
})

test('关闭弹窗后到达的响应不再写入正文，也不改动任何预览状态', async () => {
  const { state, preview, respond } = setup()

  const openA = preview.open(a)
  preview.close()
  assert.equal(state.visible, false)
  assert.equal(state.loading, false)

  respond(a.id, 'A 的正文（迟到）')
  await openA

  assert.equal(state.visible, false)
  assert.equal(state.content, '')
  assert.equal(state.loading, false)
})

test('关闭后再打开新文件：迟到的旧响应不得顶掉新文件待加载的空正文', async () => {
  const { state, preview, respond } = setup()

  const openA = preview.open(a)
  preview.close()

  // 关掉 A 之后打开 B，B 还在飞。
  const openB = preview.open(b)
  assert.equal(state.file, b)
  assert.equal(state.loading, true)

  // A 的迟到响应先到：既不能写正文，也不能收起 B 的骨架屏。
  respond(a.id, 'A 的正文（迟到）')
  await openA
  assert.equal(state.content, '')
  assert.equal(state.loading, true)

  respond(b.id, 'B 的正文')
  await openB
  assert.equal(state.file, b)
  assert.equal(state.content, 'B 的正文')
})

test('读取失败写进 error 而不是空态，且不向调用方抛出拒绝', async () => {
  const { state, preview, respondError } = setup()

  const error = new Error('Request failed with status code 500')
  error.response = { status: 500, data: { detail: '文件解析失败' } }

  const openA = preview.open(a)
  respondError(a.id, error)
  await openA // 不 reject：调用方是模板事件处理器，抛出去就是 unhandledrejection

  assert.equal(state.error, '文件解析失败')
  assert.equal(state.content, '')
  assert.equal(state.loading, false)
  // 空态文案只在「真的没有内容」时出现，读取失败不能落到它上面。
  assert.notEqual(state.error, DETAIL_PREVIEW_EMPTY_TEXT)
})

test('读取失败且后端没给文案时回落到统一的失败文案', async () => {
  const { state, preview, respondError } = setup()

  const openA = preview.open(a)
  respondError(a.id, {})
  await openA

  assert.equal(state.error, DETAIL_PREVIEW_ERROR_TEXT)
})

test('陈旧请求的失败不污染当前预览', async () => {
  const { state, preview, respond, respondError } = setup()

  const openA = preview.open(a)
  const openB = preview.open(b)

  respond(b.id, 'B 的正文')
  await openB

  respondError(a.id, new Error('A 读取失败'))
  await openA

  assert.equal(state.file, b)
  assert.equal(state.content, 'B 的正文')
  assert.equal(state.error, '')
})

test('最新请求成功后清掉上一次的错误', async () => {
  const { state, preview, respond, respondError } = setup()

  const openA = preview.open(a)
  respondError(a.id, new Error('A 读取失败'))
  await openA
  assert.notEqual(state.error, '')

  const openB = preview.open(b)
  assert.equal(state.error, '') // 换文件即清错误，不能把上一次的失败挂在新文件上
  respond(b.id, 'B 的正文')
  await openB

  assert.equal(state.error, '')
  assert.equal(state.content, 'B 的正文')
})

test('控制器写入的状态经 reactive() 包装后能触发 Vue 的更新（视图接线契约）', async () => {
  // createDetailPreview 是直接写 state 的属性，视图侧必须把响应式代理传进去；
  // 传普通对象时写入不触发任何更新（界面会一直停在打开前的内容），所以这里把契约钉死。
  const state = reactive(createDetailPreviewState())
  const renders = []
  effect(() => {
    renders.push(`${state.file?.name ?? '-'}:${state.content}`)
  })
  const { preview, respond } = setup({ state })

  const openA = preview.open(a)
  await nextTick() // 打开瞬间：标题已经是 A，正文还没回来
  respond(a.id, 'A 的正文')
  await openA
  await nextTick()

  preview.close()
  await nextTick() // 关闭只复位弹窗状态，不扰动已渲染的正文

  assert.deepEqual(renders, ['-:', 'A-年度报告.md:', 'A-年度报告.md:A 的正文'])
})

test('正文为空的文件停在空态：content 为空且没有 error', async () => {
  const { state, preview, respondEmpty } = setup()

  const openA = preview.open(a)
  respondEmpty(a.id)
  await openA

  assert.equal(state.content, '')
  assert.equal(state.error, '')
  assert.equal(state.loading, false)
})
