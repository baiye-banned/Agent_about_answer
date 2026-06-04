import test from 'node:test'
import assert from 'node:assert/strict'

import { dispatchStreamData, extractSseDataLines, readStreamEvents } from '../src/utils/streamEvents.js'

function streamFromChunks(chunks) {
  const encoder = new TextEncoder()
  const encoded = chunks.map((chunk) => encoder.encode(chunk))
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

test('extractSseDataLines reads data lines and ignores non-data lines', () => {
  assert.deepEqual(
    extractSseDataLines('event: message\ndata: hello\ndata: world\nid: 1'),
    ['hello', 'world']
  )
})

test('dispatchStreamData routes plain text, control events, content events, and done marker', () => {
  const messages = []
  const onMessage = (content, event) => messages.push([content, event])

  assert.equal(dispatchStreamData('plain text', onMessage), false)
  assert.equal(dispatchStreamData('{"type":"sources","items":[]}', onMessage), false)
  assert.equal(dispatchStreamData('{"type":"token","content":"hello"}', onMessage), false)
  assert.equal(dispatchStreamData('[DONE]', onMessage), true)

  assert.deepEqual(messages, [
    ['plain text', undefined],
    ['', { type: 'sources', items: [] }],
    ['hello', { type: 'token', content: 'hello' }],
  ])
})

test('dispatchStreamData throws for error events', () => {
  assert.throws(
    () => dispatchStreamData('{"type":"error","message":"bad stream"}'),
    /bad stream/
  )
})

test('readStreamEvents handles chunked SSE events until done marker', async () => {
  const messages = []
  const body = streamFromChunks([
    'data: {"type":"conversation","id":"c1"}\n\n',
    'data: {"type":"token","content":"hel',
    'lo"}\n\n',
    'data: [DONE]\n\n',
  ])

  const doneReceived = await readStreamEvents(body, (content, event) => messages.push([content, event]))

  assert.equal(doneReceived, true)
  assert.deepEqual(messages, [
    ['', { type: 'conversation', id: 'c1' }],
    ['hello', { type: 'token', content: 'hello' }],
  ])
})

test('readStreamEvents handles CRLF separated SSE events', async () => {
  const messages = []
  const body = streamFromChunks([
    'data: {"type":"token","content":"hello"}\r\n\r\n',
    'data: [DONE]\r\n\r\n',
  ])

  const doneReceived = await readStreamEvents(body, (content, event) => messages.push([content, event]))

  assert.equal(doneReceived, true)
  assert.deepEqual(messages, [['hello', { type: 'token', content: 'hello' }]])
})

test('readStreamEvents dispatches the final buffered event when stream closes', async () => {
  const messages = []
  const body = streamFromChunks([
    'data: {"type":"token","content":"hello"}\n\n',
    'data: [DONE]',
  ])

  const doneReceived = await readStreamEvents(body, (content, event) => messages.push([content, event]))

  assert.equal(doneReceived, true)
  assert.deepEqual(messages, [['hello', { type: 'token', content: 'hello' }]])
})

test('readStreamEvents returns false when stream ends without done marker', async () => {
  const body = streamFromChunks(['data: hello\n\n'])

  assert.equal(await readStreamEvents(body, () => {}), false)
})
