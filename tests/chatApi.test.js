import test from 'node:test'
import assert from 'node:assert/strict'

import { assertStreamResponse } from '../src/utils/streamResponse.js'

test('assertStreamResponse formats object error bodies through the shared error helper', async () => {
  const response = {
    ok: false,
    body: null,
    async json() {
      return { detail: { field: 'question' } }
    },
  }

  await assert.rejects(
    () => assertStreamResponse(response),
    (error) => error.message === '{"field":"question"}'
  )
})

test('assertStreamResponse falls back when error body is unreadable', async () => {
  const response = {
    ok: false,
    body: null,
    async json() {
      throw new Error('bad json')
    },
  }

  await assert.rejects(
    () => assertStreamResponse(response),
    /流式请求失败/
  )
})

test('assertStreamResponse reports missing stream body on successful responses', async () => {
  await assert.rejects(
    () => assertStreamResponse({ ok: true, body: null }),
    /流式响应缺少响应体/
  )
})
