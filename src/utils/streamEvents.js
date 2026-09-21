const STREAM_DONE_MESSAGE = '[DONE]'
// reset：后备模型接管前作废已渲染内容的控制事件，正文为空，消费方需清空当前流缓冲后重新累积。
const CONTROL_EVENT_TYPES = new Set(['sources', 'conversation', 'image_analysis', 'trace', 'reset'])

export async function readStreamEvents(body, onMessage) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) {
      // 收尾要 flush 解码器：多字节字符的残尾若留在解码器里，会随着流结束无声消失。
      // 之后仍要派发最后一个「没有空行收尾」的帧——data: [DONE] 常常不带尾随空行，
      // 而它能否被认出来，决定上层拿到的是「正常结束」还是「连接断在半途」。
      buffer += decoder.decode()
      return buffer ? dispatchEvent(buffer, onMessage) : false
    }

    // 换行归一化放在拼进缓冲之后：\r\n 正好被 chunk 边界切开时（本片以 \r 结尾、
    // 下一片以 \n 开头），逐片替换会漏掉，残留的 \r 会让 [DONE] 认不出来。
    buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
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

// 本项目 data: 帧只有两种协议形状：json.dumps(...) 出来的对象字面量，和结束标记 [DONE]
// （见 backend/service/chat_service.py、backend/service/trace_service.py）。
// 「像协议帧却解析不出来」只可能是被截断或损坏的帧，不能按纯文本增量降级成正文（issue #134）。
function looksLikeProtocolFrame(data) {
  const payload = data.trim()
  return payload.startsWith('{') || (payload !== '' && STREAM_DONE_MESSAGE.startsWith(payload))
}

export function dispatchStreamData(data, onMessage) {
  if (data === STREAM_DONE_MESSAGE) return true

  let parsed
  try {
    parsed = JSON.parse(data)
  } catch {
    // 纯文本 data: 载荷是有意支持的，所以这里保留兜底——但它只适用于「不像协议帧」的载荷。
    // 截断/损坏的帧直接丢弃：不上屏为正文，也不当控制事件派发。它的可观测面是
    // readStreamEvents 返回 false，src/api/chat.js 据此抛「流式响应未正常结束」。
    if (!looksLikeProtocolFrame(data)) onMessage?.(data)
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
