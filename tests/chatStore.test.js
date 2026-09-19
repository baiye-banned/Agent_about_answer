import test from 'node:test'
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'

// 桩模块：只替换网络层，跑的是真实的 src/stores/chat.js。
const stubSource = [
  'const pending = new Map()',
  'const conversationResolvers = []',
  'export const streams = []',
  'export const getMessagesCalls = []',
  'export const chatAPI = {',
  '  getConversations: () => new Promise((resolve) => conversationResolvers.push(resolve)),',
  '  getMessages: (id) => new Promise((resolve, reject) => {',
  '    getMessagesCalls.push(id)',
  '    const queue = pending.get(id) || []',
  '    queue.push({ resolve, reject })',
  '    pending.set(id, queue)',
  '  }),',
  '  deleteConversation: async () => {},',
  '  renameConversation: async () => {},',
  '}',
  'export function respondConversations(list) {',
  '  conversationResolvers.splice(0).forEach((resolve) => resolve(list))',
  '}',
  'export function respond(id, messages) {',
  '  const queue = pending.get(id) || []',
  '  const entry = queue.shift()',
  '  pending.set(id, queue)',
  '  if (entry) entry.resolve(messages)',
  '}',
  'export function respondError(id, error) {',
  '  const queue = pending.get(id) || []',
  '  const entry = queue.shift()',
  '  pending.set(id, queue)',
  '  if (entry) entry.reject(error)',
  '}',
  // 每条用例开始前清空桩状态：某条用例提前断言失败时可能留下没人消费的 resolver，
  // 若不清理，下一条用例的 respond() 会把它消费掉，导致该用例永远等不到自己的响应。
  'export function resetStub() {',
  '  pending.clear()',
  '  conversationResolvers.length = 0',
  '}',
  'export function streamChat(options) {',
  '  streams.push(options)',
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

// 评测轮询依赖 window.setInterval；这里只记录调用，不真正起定时器。
const pollingStarts = []
const pollingStops = []
globalThis.window = {
  setInterval: (fn) => {
    pollingStarts.push(fn)
    return pollingStarts.length
  },
  clearInterval: (timer) => {
    pollingStops.push(timer)
  },
}

const { createPinia, setActivePinia } = await import('pinia')
const { useChatStore } = await import('../src/stores/chat.js')
const { respond, respondError, respondConversations, resetStub, streams, getMessagesCalls } =
  await import(chatApiStub)

const conversation = (id, knowledgeBaseId = null) => ({
  id,
  title: id,
  knowledge_base_id: knowledgeBaseId,
})

const message = (id, content, ragasStatus = '') => ({
  id,
  role: 'assistant',
  content,
  ragas_status: ragasStatus,
})

const contents = (store) => store.messages.map((item) => item.content)

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

// 让流的收尾（fetchConversations → 可能的消息刷新）跑完，避免用例留下未决 Promise。
async function settleTail(ids = []) {
  respondConversations([])
  await flush()
  ids.forEach((id) => respond(id, []))
  await flush()
}

function createStore(conversations) {
  setActivePinia(createPinia())
  resetStub()
  streams.length = 0
  getMessagesCalls.length = 0
  pollingStarts.length = 0
  pollingStops.length = 0
  const store = useChatStore()
  store.conversations = conversations
  return store
}

test('selectConversation 丢弃先发出但后返回的陈旧响应', async () => {
  const store = createStore([conversation('a'), conversation('b')])

  const requestA = store.selectConversation('a')
  const requestB = store.selectConversation('b')

  respond('b', [message(2, 'B 的回答')])
  await requestB
  respond('a', [message(1, 'A 的回答')])
  await requestA

  assert.equal(store.currentId, 'b')
  assert.deepEqual(contents(store), ['B 的回答'])
})

test('连续快速切换多个会话，乱序返回后仍只保留最后一个会话的消息', async () => {
  const ids = ['a', 'b', 'c', 'd']
  const store = createStore(ids.map((id) => conversation(id)))

  // 依次点击 a→b→c→d，四个请求同时在空中。
  const pending = ids.map((id) => store.selectConversation(id))

  // 返回顺序与点击顺序完全相反，最后返回的是最早发出的那次。
  for (const index of [3, 1, 0, 2]) {
    respond(ids[index], [message(index, `${ids[index]} 的回答`)])
    await pending[index]
  }

  assert.equal(store.currentId, 'd')
  assert.deepEqual(contents(store), ['d 的回答'])
  assert.equal(store.loading, false)
})

test('selectConversation 的 loading 只由最新一次加载复位', async () => {
  const store = createStore([conversation('a'), conversation('b')])

  const requestA = store.selectConversation('a')
  const requestB = store.selectConversation('b')

  respond('a', [message(1, 'A 的回答')])
  await requestA
  assert.equal(store.loading, true)

  respond('b', [])
  await requestB
  assert.equal(store.loading, false)
})

test('陈旧响应不改动 selectedKnowledgeBaseId，也不启动评测轮询', async () => {
  const store = createStore([
    conversation('a', 'kb-a'),
    conversation('b'),
  ])

  const requestA = store.selectConversation('a')
  const requestB = store.selectConversation('b')

  respond('b', [])
  await requestB
  const pollingBefore = pollingStarts.length

  respond('a', [message(1, 'A 的回答', 'pending')])
  await requestA

  assert.equal(store.currentId, 'b')
  assert.deepEqual(contents(store), [])
  assert.equal(store.selectedKnowledgeBaseId, null)
  assert.equal(pollingStarts.length, pollingBefore)
})

test('最新响应带待评测消息时仍会启动评测轮询', async () => {
  const store = createStore([conversation('a'), conversation('b')])

  const requestB = store.selectConversation('b')
  respond('b', [message(2, 'B 的回答', 'pending')])
  await requestB

  assert.equal(pollingStarts.length, 1)
})

test('加载在飞时清空会话，返回后不得把旧消息写回当前视图', async () => {
  const store = createStore([conversation('a')])

  const requestA = store.selectConversation('a')
  store.clearMessages() // 例如用户在加载期间点了「新对话」
  respond('a', [message(1, 'A 的回答')])
  await requestA

  assert.equal(store.currentId, null)
  assert.deepEqual(contents(store), [])
  assert.equal(store.loading, false)
})

test('被新流式请求取代的旧回调不再写入状态', async () => {
  const store = createStore([conversation('a')])
  store.setCurrentId('a')

  store.sendMessage('第一个问题')
  const firstStream = streams.at(-1)
  firstStream.onMessage('第一段增量')
  assert.equal(store.streamContent, '第一段增量')

  firstStream.onDone()
  await flush()
  assert.equal(store.streaming, false)

  store.sendMessage('第二个问题')
  const secondStream = streams.at(-1)

  firstStream.onMessage('迟到的增量')
  firstStream.onDone()
  firstStream.onError(new Error('迟到的错误'))

  assert.equal(store.streamContent, '')
  assert.equal(store.streaming, true)

  secondStream.onMessage('第二段增量')
  assert.equal(store.streamContent, '第二段增量')

  await settleTail(['a'])
})

test('切换会话后，旧流的收尾不会写入当前会话的消息', async () => {
  const store = createStore([conversation('a'), conversation('b')])
  store.setCurrentId('a')

  store.sendMessage('问题')
  const stream = streams.at(-1)
  stream.onMessage('A 的回答')

  const requestB = store.selectConversation('b')
  respond('b', [message(2, 'B 的回答')])
  await requestB

  stream.onDone()
  respondConversations([conversation('a'), conversation('b')])
  await flush()

  assert.equal(store.currentId, 'b')
  assert.deepEqual(contents(store), ['B 的回答'])
})

test('当前流式请求的错误回调仍然正常收尾', async () => {
  const store = createStore([conversation('a')])
  store.setCurrentId('a')

  store.sendMessage('问题')
  streams.at(-1).onError(new Error('模型请求失败'))

  assert.equal(store.streaming, false)
  assert.equal(store.errorMessage, '模型请求失败')
  assert.deepEqual(contents(store), ['问题', '模型请求失败'])
})

test('回答刚结束又立刻追问时，仍在当前会话的收尾仍要刷新消息并启动评测轮询', async () => {
  const store = createStore([conversation('a')])
  store.setCurrentId('a')

  store.sendMessage('第一个问题')
  const firstStream = streams.at(-1)
  firstStream.onMessage('', { type: 'trace', trace_id: 't1', event: { index: 0, stage: 'retrieval_completed' } })
  firstStream.onMessage('第一段回答')
  firstStream.onDone() // 同步部分已收尾，随后 await 会话列表
  assert.equal(store.streaming, false)

  // 收尾还挂在 fetchConversations() 上时用户继续追问（streaming 已复位，允许发送）。
  store.sendMessage('第二个问题')
  const secondStream = streams.at(-1)

  respondConversations([])
  await flush()
  assert.deepEqual(getMessagesCalls, ['a']) // 收尾没有被新请求作废，仍为当前会话刷新

  respond('a', [message(9, '第一段回答', 'pending')])
  await flush()

  assert.equal(store.currentId, 'a')
  assert.equal(pollingStarts.length, 1) // 刚生成的回答仍会进入评测轮询

  secondStream.onMessage('第二段回答')
  assert.equal(store.streamContent, '第二段回答')
})

test('收尾刷新期间切走再返回，不得改动已切走会话的评测轮询', async () => {
  const store = createStore([conversation('a'), conversation('b')])
  store.setCurrentId('a')

  store.sendMessage('问题')
  const stream = streams.at(-1)
  stream.onMessage('回答')
  stream.onDone()

  respondConversations([])
  await flush()
  assert.deepEqual(getMessagesCalls, ['a']) // 收尾已进入 refreshMessages('a')，尚未返回

  // 用户在收尾刷新在飞时切到 B，B 有带评测消息 → 启动轮询。
  const requestB = store.selectConversation('b')
  respond('b', [message(2, 'B 的回答', 'pending')])
  await requestB
  assert.equal(pollingStarts.length, 1)

  // 收尾的旧刷新这时才返回：它属于已切走的会话 a，不得停掉 B 的轮询。
  respond('a', [message(1, 'A 的回答')])
  await flush()

  assert.equal(store.currentId, 'b')
  assert.equal(pollingStops.length, 0)
  assert.deepEqual(contents(store), ['B 的回答'])
})

test('收尾刷新在飞时又追问，旧刷新不得抹掉新提问', async () => {
  const store = createStore([conversation('a')])
  store.setCurrentId('a')

  store.sendMessage('第一个问题')
  const firstStream = streams.at(-1)
  firstStream.onMessage('第一段回答')
  firstStream.onDone() // 收尾挂在 fetchConversations 上

  respondConversations([])
  await flush()
  assert.deepEqual(getMessagesCalls, ['a']) // 收尾已进入 refreshMessages('a')，尚未返回

  // 旧刷新还没回来，用户又提了第二个问题：新提问的乐观消息已进入列表。
  store.sendMessage('第二个问题')
  const secondStream = streams.at(-1)
  assert.deepEqual(contents(store), ['第一个问题', '第一段回答', '第二个问题'])

  // 收尾的旧刷新这时才返回：它属于上一个请求，不得用后端快照覆盖掉在飞的新提问。
  respond('a', [message(9, '第一段回答', 'pending')])
  await flush()

  assert.deepEqual(contents(store), ['第一个问题', '第一段回答', '第二个问题'])

  secondStream.onMessage('第二段回答')
  assert.equal(store.streamContent, '第二段回答')
})

test('切走后，旧流的会话事件不得再改写当前会话的知识库选择', async () => {
  const store = createStore([conversation('a', 'kb-a'), conversation('b', 'kb-b')])
  store.setCurrentId('a')
  store.setSelectedKnowledgeBaseId('kb-a')

  store.sendMessage('问题')
  const stream = streams.at(-1)

  // 回答生成期间用户切到会话 B：知识库选择应随 B 变成 kb-b。
  const requestB = store.selectConversation('b')
  respond('b', [])
  await requestB
  assert.equal(store.selectedKnowledgeBaseId, 'kb-b')

  // 旧流此刻才投递 conversation 事件（带着 A 的知识库），不得把当前会话的选择改回 kb-a。
  stream.onMessage('', { type: 'conversation', conversation: conversation('a', 'kb-a') })

  assert.equal(store.currentId, 'b')
  assert.equal(store.selectedKnowledgeBaseId, 'kb-b')
})

test('流式期间点了「新对话」，旧流不得把会话认领回来', async () => {
  const store = createStore([conversation('a', 'kb-a')])
  store.setCurrentId('a')

  store.sendMessage('问题')
  const stream = streams.at(-1)
  stream.onMessage('回答')
  stream.onDone() // 收尾挂在 fetchConversations 上

  // 回答还在收尾时用户点了「新对话」：视图被显式清空。
  store.clearMessages()
  assert.equal(store.currentId, null)

  // 旧流此刻才投递 conversation 事件与收尾结果，都不得把已清空的视图认领回会话 a。
  stream.onMessage('', { type: 'conversation', conversation: conversation('a', 'kb-a') })
  respondConversations([conversation('a', 'kb-a')])
  await flush()

  assert.equal(store.currentId, null)
  assert.equal(store.selectedKnowledgeBaseId, null)
  assert.deepEqual(contents(store), [])
})

test('收尾刷新失败时，仍要启动评测轮询而不是中断收尾', async () => {
  const store = createStore([conversation('a')])
  store.setCurrentId('a')

  store.sendMessage('问题')
  const stream = streams.at(-1)
  stream.onMessage('', { type: 'trace', trace_id: 't1', event: { index: 0, stage: 'retrieval_completed' } })
  stream.onMessage('回答')
  stream.onDone()

  respondConversations([])
  await flush()
  assert.deepEqual(getMessagesCalls, ['a']) // 收尾已进入 refreshMessages('a')

  // 收尾的 GET /messages 失败（500 / 超时）：不得把异常抛进 onDone（streamChat 丢弃该 Promise，
  // 会变成未处理的 rejection），待评测的本地消息仍要进入轮询，靠轮询重试收敛。
  respondError('a', new Error('消息接口失败'))
  await flush()

  assert.equal(pollingStarts.length, 1)
  assert.deepEqual(contents(store), ['问题', '回答'])
})
