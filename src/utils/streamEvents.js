const STREAM_DONE_MESSAGE = '[DONE]'
// reset：后备模型接管前作废已渲染内容的控制事件，正文为空，消费方需清空当前流缓冲后重新累积。
const CONTROL_EVENT_TYPES = new Set(['sources', 'conversation', 'image_analysis', 'trace', 'reset'])

export async function readStreamEvents(body, onMessage) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) return buffer ? dispatchEvent(buffer, onMessage) : false

    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    const events = buffer.split('\n\n')
    buffer = events.pop() || ''

    for (const event of events) {
      if (dispatchEvent(event, onMessage)) return true
    }
  }
}

function dispatchEvent(event, onMessage) {
  for (const data of extractSseDataLines(event)) {
    if (dispatchStreamData(data, onMessage)) return true
  }
  return false
}

export function extractSseDataLines(event) {
  return event
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.replace(/^data:\s?/, ''))
}

export function dispatchStreamData(data, onMessage) {
  if (data === STREAM_DONE_MESSAGE) return true

  let parsed
  try {
    parsed = JSON.parse(data)
  } catch {
    onMessage?.(data)
    return false
  }

  if (parsed.type === 'error') {
    throw new Error(parsed.message || parsed.content || '模型请求失败')
  }

  if (CONTROL_EVENT_TYPES.has(parsed.type)) {
    onMessage?.('', parsed)
  } else {
    onMessage?.(parsed.content ?? '', parsed)
  }
  return false
}
