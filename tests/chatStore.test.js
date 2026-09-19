import test from 'node:test'
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'

// 桩模块：只替换网络层，跑的是真实的 src/stores/chat.js。
const stubSource = [
  'const pending = new Map()',
  'export const streams = []',
  'export const chatAPI = {',
  '  getConversations: async () => [],',
  '  getMessages: (id) => new Promise((resolve) => pending.set(id, resolve)),',
  '  deleteConversation: async () => {},',
  '  renameConversation: async () => {},',
  '}',
  'export function respond(id, messages) {',
  '  const resolve = pending.get(id)',
  '  pending.delete(id)',
  '  resolve(messages)',
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
globalThis.window = {
  setInterval: (fn) => {
    pollingStarts.push(fn)
    return pollingStarts.length
  },
  clearInterval: () => {},
}

const { createPinia, setActivePinia } = await import('pinia')
const { useChatStore } = await import('../src/stores/chat.js')
const { respond, streams } = await import(chatApiStub)

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

function createStore(conversations) {
  setActivePinia(createPinia())
  streams.length = 0
  pollingStarts.length = 0
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

test('loading 只由最新一次加载复位', async () => {
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
