import request from './request'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'

export const chatAPI = {
  getConversations() {
    return request.get('/chat/conversations')
  },
  getMessages(conversationId) {
    return request.get(`/chat/conversations/${conversationId}`)
  },
  deleteConversation(id) {
    return request.delete(`/chat/conversations/${id}`)
  },
  renameConversation(id, title) {
    return request.put(`/chat/conversations/${id}`, { title })
  },
  getTrace(traceId) {
    return request.get(`/chat/traces/${traceId}`)
  },
  getMessageTrace(messageId) {
    return request.get(`/chat/messages/${messageId}/trace`)
  },
  uploadAttachment(file, onProgress) {
    const formData = new FormData()
    formData.append('file', file)

    return request.post('/chat/attachments', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: onProgress,
    })
  },
}

export async function streamChat({
  conversationId,
  knowledgeBaseId,
  question,
  attachments = [],
  signal,
  onMessage,
  onDone,
  onError,
}) {
  const token = localStorage.getItem('token')

  try {
    const response = await fetch(`${API_BASE_URL}/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        conversation_id: conversationId,
        knowledge_base_id: knowledgeBaseId,
        question,
        attachments,
      }),
      signal,
    })

    if (!response.ok || !response.body) {
      const errorBody = await response.json().catch(() => ({}))
      throw new Error(errorBody.detail || errorBody.message || '娴佸紡璇锋眰澶辫触')
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let doneReceived = false

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split('\n\n')
      buffer = events.pop() || ''

      for (const event of events) {
        const dataLines = event
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.replace(/^data:\s?/, ''))

        for (const data of dataLines) {
          if (data === '[DONE]') {
            doneReceived = true
            onDone?.()
            return
          }

          let parsed
          try {
            parsed = JSON.parse(data)
          } catch {
            onMessage?.(data)
            continue
          }

          if (parsed.type === 'error') {
            throw new Error(parsed.message || parsed.content || '模型请求失败')
          }
          if (['sources', 'conversation', 'image_analysis', 'trace'].includes(parsed.type)) {
            onMessage?.('', parsed)
          } else {
            onMessage?.(parsed.content ?? '', parsed)
          }
        }
      }
    }

    if (doneReceived) {
      onDone?.()
    } else {
      throw new Error('流式响应未正常结束')
    }
  } catch (error) {
    onError?.(error)
  }
}

