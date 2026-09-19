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

// 协议契约（issue #21）：后备模型接管前，后端下发 {"type":"reset"} 控制事件。
// 该事件不携带正文，消费方必须清空当前流缓冲后再追加后备模型的内容。
test('dispatchStreamData 把 reset 作为控制事件投递，且不携带正文', () => {
  const messages = []
  const payload = '{"type":"reset","reason":"text_fallback","message":"已切换到文本后备模型"}'

  assert.equal(dispatchStreamData(payload, (content, event) => messages.push([content, event])), false)
  assert.deepEqual(messages, [
    ['', { type: 'reset', reason: 'text_fallback', message: '已切换到文本后备模型' }],
  ])
  // 旧的解析路径（未知类型落到 parsed.content ?? ''）对该事件同样只会追加空串，天然向后兼容。
  assert.equal(JSON.parse(payload).content ?? '', '')
})

test('readStreamEvents 按序投递「首模型增量 → reset → 后备增量」', async () => {
  const messages = []
  const body = streamFromChunks([
    'data: {"content":"根据《员工手册》考勤管理"}\n\n',
    'data: {"content":"，迟到30分钟以内"}\n\n',
    'data: {"type":"reset","reason":"text_fallback"}\n\n',
    'data: {"content":"根据《员工手册》考勤管理章节，"}\n\n',
    'data: {"content":"迟到30分钟以内罚款50元。"}\n\n',
    'data: [DONE]\n\n',
  ])

  await readStreamEvents(body, (content, event) => messages.push([content, event]))

  assert.deepEqual(
    messages.map(([content, event]) => (event?.type === 'reset' ? 'reset' : content)),
    ['根据《员工手册》考勤管理', '，迟到30分钟以内', 'reset', '根据《员工手册》考勤管理章节，', '迟到30分钟以内罚款50元。']
  )
})
