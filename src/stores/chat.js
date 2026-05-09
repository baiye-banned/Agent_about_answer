import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { chatAPI, streamChat } from '@/api/chat'

export const useChatStore = defineStore('chat', () => {
  const conversations = ref([])
  const currentId = ref(null)
  const messages = ref([])
  const loading = ref(false)
  const historyManageMode = ref(false)
  const selectedConversationIds = ref([])

  const streaming = ref(false)
  const streamContent = ref('')
  const streamSources = ref([])
  const streamingConversationId = ref(null)
  const pendingRouteConversationId = ref(null)
  const selectedKnowledgeBaseId = ref(null)
  const errorMessage = ref('')
  const abortController = ref(null)
  const streamingHasAttachments = ref(false)
  const streamImageAnalysis = ref(null)
  const evaluationPollTimer = ref(null)
  const evaluationPollStartedAt = ref(0)

  const currentConversation = computed(() =>
    conversations.value.find((conversation) => conversation.id === currentId.value)
  )

  async function fetchConversations() {
    loading.value = true
    try {
      const response = await chatAPI.getConversations()
      conversations.value = Array.isArray(response) ? response : []
      return conversations.value
    } finally {
      loading.value = false
    }
  }

  function upsertConversation(conversation) {
    if (!conversation?.id) return
    const index = conversations.value.findIndex((item) => item.id === conversation.id)
    const next = {
      id: conversation.id,
      title: conversation.title || '未命名对话',
      knowledge_base_id: conversation.knowledge_base_id || null,
      knowledge_base_name: conversation.knowledge_base_name || '',
      created_at: conversation.created_at || new Date().toISOString(),
      updated_at: conversation.updated_at || new Date().toISOString(),
    }
    if (index >= 0) {
      conversations.value[index] = { ...conversations.value[index], ...next }
    } else {
      conversations.value.unshift(next)
    }
  }

  async function selectConversation(id) {
    currentId.value = id
    loading.value = true
    try {
      const response = await chatAPI.getMessages(id)
      messages.value = Array.isArray(response) ? response : []
      const conversation = conversations.value.find((item) => item.id === id)
      if (conversation?.knowledge_base_id) {
        selectedKnowledgeBaseId.value = conversation.knowledge_base_id
      }
      if (hasPendingEvaluation(messages.value)) {
        startEvaluationPolling(id)
      } else {
        stopEvaluationPolling()
      }
    } finally {
      loading.value = false
    }
  }

  function addMessage(message) {
    messages.value.push(normalizeMessage(message))
  }

  function replaceMessages(nextMessages) {
    messages.value = nextMessages.map(normalizeMessage)
  }

  function setCurrentId(id) {
    currentId.value = id || null
  }

  function setSelectedKnowledgeBaseId(id) {
    selectedKnowledgeBaseId.value = id || null
  }

  function enterHistoryManageMode() {
    historyManageMode.value = true
    selectedConversationIds.value = []
  }

  function exitHistoryManageMode() {
    historyManageMode.value = false
    selectedConversationIds.value = []
  }

  function toggleConversationSelection(id) {
    if (!id) return
    const exists = selectedConversationIds.value.includes(id)
    selectedConversationIds.value = exists
      ? selectedConversationIds.value.filter((item) => item !== id)
      : [...selectedConversationIds.value, id]
  }

  function toggleSelectAllConversations() {
    if (selectedConversationIds.value.length === conversations.value.length) {
      selectedConversationIds.value = []
      return
    }
    selectedConversationIds.value = conversations.value.map((conversation) => conversation.id)
  }

  function isConversationSelected(id) {
    return selectedConversationIds.value.includes(id)
  }

  function sendMessage(question, attachments = []) {
    const rawText = question.trim()
    const displayText = rawText || (attachments.length ? '请分析这张图片' : '')
    if ((!rawText && !attachments.length) || streaming.value) return

    errorMessage.value = ''
    streaming.value = true
    streamContent.value = ''
    streamSources.value = []
    streamImageAnalysis.value = null
    streamingConversationId.value = currentId.value
    pendingRouteConversationId.value = null
    streamingHasAttachments.value = attachments.length > 0
    abortController.value = new AbortController()

    addMessage({ role: 'user', content: displayText, attachments })

    streamChat({
      conversationId: currentId.value,
      knowledgeBaseId: selectedKnowledgeBaseId.value,
      question: rawText,
      attachments,
      signal: abortController.value.signal,
      onMessage: (content, event) => {
        if (event?.type === 'conversation') {
          const conversation = event.conversation || {}
          upsertConversation(conversation)
          streamingConversationId.value = conversation.id || streamingConversationId.value
          pendingRouteConversationId.value = conversation.id || null
          if (!currentId.value && conversation.id) {
            currentId.value = conversation.id
          }
          if (conversation.knowledge_base_id) {
            selectedKnowledgeBaseId.value = conversation.knowledge_base_id
          }
          return
        }
        if (event?.type === 'sources') {
          streamSources.value = event.sources || []
          return
        }
        if (event?.type === 'image_analysis') {
          streamImageAnalysis.value = normalizeImageAnalysis(event.analysis || event)
          return
        }
        streamContent.value += content
      },
      onDone: handleStreamDone,
      onError: handleStreamError,
    })
  }

  async function handleStreamDone() {
    const targetConversationId = streamingConversationId.value
    const shouldShowLocalMessage = streamContent.value && currentId.value === targetConversationId
    if (shouldShowLocalMessage) {
      addMessage({
        role: 'assistant',
        content: streamContent.value,
        sources: streamSources.value,
        ...streamImageAnalysisFields(),
        ragas_status: 'pending',
        isLocal: true,
      })
    }

    finishStreaming()
    const nextConversations = await fetchConversations().catch(() => [])
    if (!currentId.value && nextConversations?.[0]?.id) {
      setCurrentId(nextConversations[0].id)
    } else if (targetConversationId && !currentId.value) {
      setCurrentId(targetConversationId)
    }
    if (targetConversationId && currentId.value === targetConversationId) {
      await refreshMessages(targetConversationId)
      startEvaluationPolling(targetConversationId)
    }
  }

  function handleStreamError(error) {
    if (error?.name === 'AbortError') {
      if (streamContent.value && currentId.value === streamingConversationId.value) {
        addMessage({
          role: 'assistant',
          content: `${streamContent.value}\n\n*(已停止生成)*`,
          sources: streamSources.value,
          isLocal: true,
        })
      }
    } else {
      const message = getStreamErrorMessage(error)
      errorMessage.value = message
      if (currentId.value === streamingConversationId.value) {
        const content = streamContent.value
          ? `${streamContent.value}\n\n${message}`
          : message
        addMessage({
          role: 'assistant',
          content,
          sources: streamSources.value,
          stream_error: message,
          ...streamImageAnalysisFields(),
          isLocal: true,
        })
      }
    }
    finishStreaming()
  }

  function stopGeneration() {
    abortController.value?.abort()
  }

  function finishStreaming() {
    streaming.value = false
    streamContent.value = ''
    streamSources.value = []
    streamImageAnalysis.value = null
    streamingConversationId.value = null
    pendingRouteConversationId.value = null
    streamingHasAttachments.value = false
    abortController.value = null
  }

  function getStreamErrorMessage(error) {
    if (error?.message) return error.message
    if (streamingHasAttachments.value) {
      return '图片内容处理失败，请检查视觉模型配置或图片地址是否可访问'
    }
    return '回答生成失败，请稍后重试'
  }

  function normalizeImageAnalysis(analysis = {}) {
    return {
      status: analysis.status || analysis.image_analysis_status || '',
      description: analysis.description || analysis.image_description || '',
      error: analysis.error || analysis.image_analysis_error || '',
    }
  }

  function streamImageAnalysisFields() {
    const analysis = normalizeImageAnalysis(streamImageAnalysis.value || {})
    return {
      image_analysis_status: analysis.status,
      image_analysis_error: analysis.error,
      image_description: analysis.description,
      retrieval_trace: {
        image_analysis_status: analysis.status,
        image_analysis_error: analysis.error,
        image_description: analysis.description,
      },
    }
  }

  async function deleteConversation(id) {
    await chatAPI.deleteConversation(id)
    conversations.value = conversations.value.filter((conversation) => conversation.id !== id)
    selectedConversationIds.value = selectedConversationIds.value.filter((item) => item !== id)
    if (streamingConversationId.value === id) {
      stopGeneration()
    }
    if (currentId.value === id) {
      clearMessages()
    }
  }

  async function refreshMessages(id = currentId.value) {
    if (!id) return []
    const response = await chatAPI.getMessages(id)
    const nextMessages = Array.isArray(response) ? response : []
    const localAssistantMessages = messages.value.filter(
      (message) => message.role === 'assistant' && message.isLocal
    )
    const mergedMessages = nextMessages.map(normalizeMessage)
    const latestBackendAssistant = [...mergedMessages].reverse().find(
      (message) => message.role === 'assistant'
    )
    for (const localMessage of localAssistantMessages) {
      const exists = mergedMessages.some(
        (item) =>
          item.role === localMessage.role &&
          (
            item.content === localMessage.content ||
            localMessage.content.includes(item.content) ||
            item.content.includes(localMessage.content)
          )
      )
      if (!exists && latestBackendAssistant && localMessage.ragas_status === 'pending') {
        continue
      }
      if (!exists) {
        mergedMessages.push(localMessage)
      }
    }
    if (currentId.value === id) {
      messages.value = mergedMessages
    }
    return mergedMessages
  }

  function hasPendingEvaluation(nextMessages = messages.value) {
    return nextMessages.some((message) =>
      message.role === 'assistant' && ['pending', 'running'].includes(message.ragas_status)
    )
  }

  function startEvaluationPolling(conversationId) {
    stopEvaluationPolling()
    evaluationPollStartedAt.value = Date.now()
    let elapsed = 0
    evaluationPollTimer.value = window.setInterval(async () => {
      elapsed += 3000
      if (currentId.value !== conversationId) {
        stopEvaluationPolling()
        return
      }
      const nextMessages = await refreshMessages(conversationId).catch(() => messages.value)
      if (elapsed > 110000) {
        markLocalEvaluationTimeout()
        stopEvaluationPolling()
        return
      }
      if (!hasPendingEvaluation(nextMessages)) {
        stopEvaluationPolling()
      }
    }, 3000)
  }

  function stopEvaluationPolling() {
    if (evaluationPollTimer.value) {
      window.clearInterval(evaluationPollTimer.value)
      evaluationPollTimer.value = null
    }
    evaluationPollStartedAt.value = 0
  }

  function markLocalEvaluationTimeout() {
    messages.value = messages.value.map((message) => {
      if (
        message.role === 'assistant' &&
        message.isLocal &&
        ['pending', 'running'].includes(message.ragas_status)
      ) {
        return {
          ...message,
          ragas_status: 'failed',
          ragas_error: '评测未及时返回，稍后刷新会话可查看最终状态',
        }
      }
      return message
    })
  }

  async function renameConversation(id, title) {
    await chatAPI.renameConversation(id, title)
    const conversation = conversations.value.find((item) => item.id === id)
    if (conversation) {
      conversation.title = title
    }
  }

  function clearMessages() {
    currentId.value = null
    messages.value = []
    stopEvaluationPolling()
  }

  function normalizeMessage(message) {
    const retrievalTrace = message.retrieval_trace || {}
    return {
      id: message.id || null,
      role: message.role,
      content: message.content,
      sources: message.sources || [],
      attachments: message.attachments || [],
      ragas_status: message.ragas_status || '',
      ragas_scores: message.ragas_scores || {},
      ragas_error: message.ragas_error || '',
      stream_error: message.stream_error || '',
      retrieval_trace: retrievalTrace,
      image_analysis_status: message.image_analysis_status || retrievalTrace.image_analysis_status || '',
      image_analysis_error: message.image_analysis_error || retrievalTrace.image_analysis_error || '',
      image_description: message.image_description || retrievalTrace.image_description || '',
      isLocal: Boolean(message.isLocal),
      created_at: message.created_at || new Date().toISOString(),
    }
  }

  return {
    conversations,
    currentId,
    messages,
    loading,
    historyManageMode,
    selectedConversationIds,
    streaming,
    streamContent,
    streamSources,
    streamingConversationId,
    pendingRouteConversationId,
    selectedKnowledgeBaseId,
    errorMessage,
    currentConversation,
    fetchConversations,
    selectConversation,
    addMessage,
    replaceMessages,
    refreshMessages,
    setCurrentId,
    setSelectedKnowledgeBaseId,
    enterHistoryManageMode,
    exitHistoryManageMode,
    toggleConversationSelection,
    toggleSelectAllConversations,
    isConversationSelected,
    sendMessage,
    stopGeneration,
    finishStreaming,
    deleteConversation,
    renameConversation,
    clearMessages,
  }
})
