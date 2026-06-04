import request from './request'
import { assertStreamResponse } from '@/utils/streamResponse'
import { readStreamEvents } from '@/utils/streamEvents'

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

export { assertStreamResponse }

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

    await assertStreamResponse(response)

    const doneReceived = await readStreamEvents(response.body, onMessage)

    if (doneReceived) {
      onDone?.()
    } else {
      throw new Error('流式响应未正常结束')
    }
  } catch (error) {
    onError?.(error)
  }
}
