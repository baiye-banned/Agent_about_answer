import test from 'node:test'
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import { readFileSync } from 'node:fs'

const srcRoot = new URL('../src/', import.meta.url)
const messageUrl = `data:text/javascript,${encodeURIComponent(`
  export const messages = []
  export const ElMessage = {
    warning: (message) => messages.push(message),
    error: (message) => messages.push(message),
  }
`)}`
const routerUrl = `data:text/javascript,${encodeURIComponent(`
  export const redirects = []
  export default {
    currentRoute: { value: { path: '/chat' } },
    replace: (path) => redirects.push(path),
  }
`)}`

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'element-plus') return { url: messageUrl, shortCircuit: true }
    if (specifier === '@/router') return { url: routerUrl, shortCircuit: true }
    if (specifier.startsWith('@/')) {
      return { url: new URL(`${specifier.slice(2)}.js`, srcRoot).href, shortCircuit: true }
    }
    if (specifier === './request' && context.parentURL === new URL('api/chat.js', srcRoot).href) {
      return { url: new URL('api/request.js', srcRoot).href, shortCircuit: true }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url === new URL('api/request.js', srcRoot).href || url === new URL('api/chat.js', srcRoot).href) {
      return {
        format: 'module',
        source: readFileSync(new URL(url), 'utf8').replaceAll('import.meta.env', '({})'),
        shortCircuit: true,
      }
    }
    return nextLoad(url, context)
  },
})

const { default: request } = await import('../src/api/request.js')
const { chatAPI } = await import('../src/api/chat.js')
const { messages } = await import(messageUrl)
const { redirects } = await import(routerUrl)

function storage(t) {
  const original = globalThis.localStorage
  const removed = []
  globalThis.localStorage = {
    getItem: () => 'test-login-token',
    removeItem: (key) => removed.push(key),
  }
  t.after(() => {
    if (original === undefined) delete globalThis.localStorage
    else globalThis.localStorage = original
  })
  return removed
}

test('导出 API：只请求一次，复用登录头并保留 Blob 和 Content-Disposition；普通接口仍返回 data', async (t) => {
  storage(t)
  const received = []
  const blob = new Blob(['# 文件'], { type: 'text/markdown' })
  request.defaults.adapter = async (config) => {
    received.push(config)
    return {
      data: config.url.endsWith('/export') ? blob : [{ id: 'c1' }],
      status: 200,
      headers: { 'content-disposition': 'attachment; filename="answer.md"' },
      config,
    }
  }
  const response = await chatAPI.exportConversation('c1')
  assert.equal(received.length, 1)
  assert.equal(received[0].url, '/chat/conversations/c1/export')
  assert.equal(received[0].responseType, 'blob')
  assert.equal(received[0].returnFullResponse, true)
  assert.equal(received[0].silent, true)
  assert.equal(received[0].headers.Authorization, 'Bearer test-login-token')
  assert.strictEqual(response.data, blob)
  assert.equal(response.headers['content-disposition'], 'attachment; filename="answer.md"')
  assert.deepEqual(await chatAPI.getConversations(), [{ id: 'c1' }])
})

test('导出 API：HTTP 错误仍 reject；silent 不重复 toast；401 仍清登录并跳转', async (t) => {
  const removed = storage(t)
  messages.length = 0
  redirects.length = 0
  let status = 500
  request.defaults.adapter = async (config) => {
    const error = new Error('Request failed')
    error.config = config
    error.response = { status, data: new Blob(['{"detail":"导出失败"}'], { type: 'application/json' }), config }
    throw error
  }
  await assert.rejects(() => chatAPI.exportConversation('c1'), /Request failed/)
  assert.deepEqual(messages, [])
  status = 401
  await assert.rejects(() => chatAPI.exportConversation('c1'), /Request failed/)
  assert.deepEqual(messages, [])
  assert.deepEqual(removed, ['token', 'username'])
  assert.deepEqual(redirects, ['/login'])
})
