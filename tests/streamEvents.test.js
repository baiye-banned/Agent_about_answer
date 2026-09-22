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

function streamFromBytes(chunks) {
  return new ReadableStream({
    pull(controller) {
      if (!chunks.length) {
        controller.close()
        return
      }
      controller.enqueue(chunks.shift())
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

// issue #134：连接在某个 data: 帧中途断开时，残帧不得当作正文追加进回答。
// 注意与「有意的纯文本 data: 载荷」区分：纯文本兜底本身是受支持的（见上一个用例），
// 被丢弃的只是「长得像协议帧却解析不出来」的那一类。
test('readStreamEvents 丢弃 JSON 中途被截断的末帧，不把协议原文当正文（#134）', async () => {
  const messages = []
  const body = streamFromChunks([
    'data: {"type":"token","content":"答案第一句。"}\n\n',
    'data: {"type":"token","content":"答案第二句，然后服务器在帧',
  ])

  const doneReceived = await readStreamEvents(body, (content, event) => messages.push([content, event]))

  assert.equal(doneReceived, false)
  assert.deepEqual(messages, [['答案第一句。', { type: 'token', content: '答案第一句。' }]])

  // 同一用例内锁定：flush 尾帧这件事本身是有意为之，不能被一起改掉——
  // 正常收尾的 data: [DONE] 也常常不带尾随空行。
  const doneMessages = []
  const doneBody = streamFromChunks([
    'data: {"type":"token","content":"答案"}\n\n',
    'data: [DONE]',
  ])

  assert.equal(
    await readStreamEvents(doneBody, (content, event) => doneMessages.push([content, event])),
    true
  )
  assert.deepEqual(doneMessages, [['答案', { type: 'token', content: '答案' }]])
})

test('readStreamEvents 丢弃被截断的控制帧，既不降级为正文也不投递控制事件（#134）', async () => {
  const fragments = [
    'data: {"type":"sources","sources":[',
    'data: {"type":"error","mess',
    'data: {"type":"reset","rea',
  ]

  for (const fragment of fragments) {
    const messages = []
    const doneReceived = await readStreamEvents(
      streamFromChunks([fragment]),
      (content, event) => messages.push([content, event])
    )

    assert.equal(doneReceived, false, `${fragment} 不得被当作流结束标记`)
    assert.deepEqual(messages, [], `${fragment} 不得进入正文或控制事件`)
  }
})

test('readStreamEvents 丢弃已成帧却 JSON 非法的协议帧，后续帧照常派发（#134）', async () => {
  const messages = []
  const body = streamFromChunks([
    'data: {"type":"token","content":"前半"}\n\n',
    'data: {"type":"token","content":"坏帧}\n\n',
    'data: {"type":"token","content":"后半"}\n\n',
    'data: [DONE]\n\n',
  ])

  const doneReceived = await readStreamEvents(body, (content, event) => messages.push([content, event]))

  assert.equal(doneReceived, true)
  assert.deepEqual(messages, [
    ['前半', { type: 'token', content: '前半' }],
    ['后半', { type: 'token', content: '后半' }],
  ])
})

test('readStreamEvents 跨 chunk 边界切开的汉字被完整还原（流式解码状态不能丢）', async () => {
  const encoder = new TextEncoder()
  const messages = []
  const frame = encoder.encode('data: {"type":"token","content":"答案好好"}\n\n')
  // 切点落在第一个「好」的三个字节中间：解码器必须把半个字符留到下一片再拼。
  const cut = frame.indexOf(encoder.encode('好')[0]) + 1

  const doneReceived = await readStreamEvents(
    streamFromBytes([frame.slice(0, cut), frame.slice(cut), encoder.encode('data: [DONE]\n\n')]),
    (content, event) => messages.push([content, event])
  )

  assert.equal(doneReceived, true)
  assert.deepEqual(messages, [['答案好好', { type: 'token', content: '答案好好' }]])
})

test('readStreamEvents 在流结束时 flush 解码器，末端多字节字符不再被静默吞掉（#134）', async () => {
  const encoder = new TextEncoder()
  const messages = []
  const bytes = encoder.encode('data: 你好')
  // 末字符「好」共 3 字节，只到达前 2 字节：不 flush 时解码器会把这两个字节留在内部丢掉。
  const body = streamFromBytes([bytes.slice(0, bytes.length - 1)])

  const doneReceived = await readStreamEvents(body, (content) => messages.push(content))

  assert.equal(doneReceived, false)
  // 完整字符「你」保留下来，残字节按解码器约定显形为替换字符（可见的丢失信号），而不是无声消失。
  assert.deepEqual(messages, ['你�'])
})

test('readStreamEvents 在 CRLF 被分片切开时仍能识别 [DONE] 收尾（#134）', async () => {
  const encoder = new TextEncoder()
  const messages = []
  const body = streamFromBytes([
    encoder.encode('data: {"type":"token","content":"答案"}\r\n\r\n'),
    encoder.encode('data: [DONE]\r'),
    encoder.encode('\n'),
  ])

  const doneReceived = await readStreamEvents(body, (content, event) => messages.push([content, event]))

  assert.equal(doneReceived, true)
  assert.deepEqual(messages, [['答案', { type: 'token', content: '答案' }]])
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
