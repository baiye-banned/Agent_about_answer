<template>
  <div class="flex h-full flex-col bg-slate-50">
    <header class="flex min-h-14 items-center justify-between gap-4 border-b border-slate-200 bg-white px-5 py-3">
      <div class="min-w-0">
        <div v-if="chatStore.currentId" class="flex items-center gap-2">
          <el-input
            v-if="renaming"
            ref="renameRef"
            v-model.trim="renameTitle"
            size="small"
            class="w-72"
            maxlength="40"
            @keyup.enter="confirmRename"
            @blur="confirmRename"
          />
          <h2 v-else class="truncate text-sm font-medium text-slate-800">
            {{ chatStore.currentConversation?.title || '未命名对话' }}
          </h2>
          <el-tooltip v-if="!renaming" content="重命名" placement="bottom">
            <el-button link :icon="Edit" @click="startRename" />
          </el-tooltip>
        </div>
        <h2 v-else class="text-sm font-medium text-slate-800">新对话</h2>
      </div>

      <div class="flex flex-wrap items-center justify-end gap-3">
        <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-2">
          <p class="text-[11px] font-medium uppercase tracking-[0.08em] text-slate-400">当前知识库</p>
          <div class="mt-1 flex flex-wrap items-center gap-3">
            <span class="max-w-[220px] truncate text-sm font-medium text-slate-700">
              {{ currentKnowledgeBaseName }}
            </span>
            <el-select
              v-model="selectedKnowledgeBaseId"
              class="w-52"
              size="small"
              :disabled="Boolean(chatStore.currentId) || chatStore.streaming"
              placeholder="选择知识库"
              @change="changeKnowledgeBase"
            >
              <el-option
                v-for="base in knowledgeBases"
                :key="base.id"
                :label="base.name"
                :value="base.id"
              />
            </el-select>
          </div>
        </div>

        <el-button :icon="Plus" @click="createNewConversation">新建对话</el-button>
      </div>
    </header>

    <main ref="chatRef" class="min-h-0 flex-1 overflow-y-auto px-6 py-5">
      <section
        v-if="!chatStore.messages.length && !isCurrentConversationStreaming"
        class="flex h-full items-center justify-center"
      >
        <div class="w-full max-w-2xl text-center">
          <div class="mx-auto flex h-14 w-14 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
            <el-icon :size="30"><ChatDotSquare /></el-icon>
          </div>
          <h1 class="mt-5 text-2xl font-semibold text-slate-900">今天想了解什么？</h1>
          <p class="mt-2 text-sm text-slate-500">向企业知识库提问，系统会结合已上传资料生成答案。</p>
          <div class="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <button
              v-for="suggestion in suggestions"
              :key="suggestion"
              type="button"
              class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-left text-sm text-slate-700 transition-colors hover:border-brand-500 hover:text-brand-700"
              @click="askSuggestion(suggestion)"
            >
              {{ suggestion }}
            </button>
          </div>
        </div>
      </section>

      <section v-else class="mx-auto max-w-5xl space-y-5">
        <article
          v-for="(message, index) in chatStore.messages"
          :key="`${message.role}-${index}-${message.created_at || index}`"
          :class="['flex', message.role === 'user' ? 'justify-end' : 'justify-start']"
        >
          <div
            :class="[
              'group relative max-w-[82%] rounded-lg px-4 py-3 shadow-sm',
              message.role === 'user'
                ? 'bg-brand-600 text-white'
                : 'border border-slate-200 bg-white text-slate-700',
            ]"
          >
            <div v-if="message.role === 'assistant'" class="mb-2 flex items-center gap-2 text-xs text-slate-500">
              <el-icon><MagicStick /></el-icon>
              <span>AI 助手</span>
            </div>
            <template v-if="message.role === 'assistant'">
              <MarkdownRenderer :content="message.content" />
              <p
                v-if="message.stream_error"
                class="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-700"
              >
                {{ message.stream_error }}
              </p>
              <p
                v-if="shouldShowImageAnalysisWarning(message)"
                class="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-700"
              >
                {{ imageAnalysisWarningText(message) }}
              </p>
              <button
                v-if="message.sources?.length"
                type="button"
                class="mt-3 inline-flex items-center gap-1 rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-500 hover:border-brand-500 hover:text-brand-700"
                @click="openSources(message.sources)"
              >
                <el-icon><Document /></el-icon>
                参考 {{ message.sources.length }} 篇资料
              </button>
              <button
                v-if="hasTrace(message)"
                type="button"
                class="ml-2 mt-3 inline-flex items-center gap-1 rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-500 hover:border-brand-500 hover:text-brand-700"
                @click="openTrace(message)"
              >
                <el-icon><Document /></el-icon>
                流程
              </button>
              <div
                v-if="message.ragas_status"
                class="mt-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600"
              >
                <div class="mb-1 flex items-center justify-between">
                  <span class="font-medium text-slate-700">RAG Evaluation</span>
                  <span :class="ragasStatusClass(message.ragas_status)">
                    {{ ragasStatusText(message.ragas_status) }}
                  </span>
                </div>
                <div v-if="message.ragas_status === 'done'" class="grid grid-cols-3 gap-2">
                  <div v-for="metric in ragasMetrics" :key="metric.key">
                    <p class="text-slate-400">{{ metric.label }}</p>
                    <p class="mt-0.5 font-medium text-slate-800">{{ formatScore(message.ragas_scores?.[metric.key]) }}</p>
                  </div>
                </div>
                <p v-else-if="message.ragas_status === 'failed'" class="leading-5 text-red-500">
                  {{ message.ragas_error || '评测失败' }}
                </p>
                <p v-else class="text-slate-400">评测中...</p>
              </div>
            </template>
            <div v-else class="space-y-2">
              <div v-if="message.content" class="whitespace-pre-wrap text-sm leading-6">{{ message.content }}</div>
              <div v-if="message.attachments?.length" class="grid max-w-sm grid-cols-2 gap-2">
                <img
                  v-for="attachment in message.attachments"
                  :key="attachment.object_key || attachment.url"
                  :src="attachment.url"
                  :alt="attachment.name || '上传图片'"
                  class="h-28 w-full rounded-md border border-white/30 object-cover"
                />
              </div>
            </div>

            <div
              v-if="message.role === 'assistant'"
              class="pointer-events-none absolute right-2 top-2 flex rounded-md border border-slate-200 bg-white/95 opacity-0 shadow-sm transition-opacity group-hover:pointer-events-auto group-hover:opacity-100 focus-within:pointer-events-auto focus-within:opacity-100"
            >
              <el-tooltip content="复制" placement="bottom">
                <el-button link :icon="CopyDocument" @click="copyMessage(message.content)" />
              </el-tooltip>
              <el-tooltip content="重新生成" placement="bottom">
                <el-button link :icon="Refresh" @click="regenerate(index)" />
              </el-tooltip>
            </div>
          </div>
        </article>

        <article v-if="isCurrentConversationStreaming" class="flex justify-start">
          <div class="max-w-[82%] rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-sm">
            <div class="mb-2 flex items-center gap-2 text-xs text-slate-500">
              <el-icon><Loading /></el-icon>
              <span>AI 正在生成</span>
            </div>
            <MarkdownRenderer :content="chatStore.streamContent || '正在检索知识库...'" />
            <button
              v-if="chatStore.streamSources.length"
              type="button"
              class="mt-3 inline-flex items-center gap-1 rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-500 hover:border-brand-500 hover:text-brand-700"
              @click="openSources(chatStore.streamSources)"
            >
              <el-icon><Document /></el-icon>
              参考 {{ chatStore.streamSources.length }} 篇资料
            </button>
            <button
              v-if="chatStore.streamTrace.events?.length"
              type="button"
              class="ml-2 mt-3 inline-flex items-center gap-1 rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-500 hover:border-brand-500 hover:text-brand-700"
              @click="openTrace({ learning_trace: chatStore.streamTrace })"
            >
              <el-icon><Document /></el-icon>
              流程
            </button>
          </div>
        </article>

        <el-alert
          v-if="chatStore.errorMessage"
          :title="chatStore.errorMessage"
          type="error"
          show-icon
          :closable="true"
          @close="chatStore.errorMessage = ''"
        />
      </section>
    </main>

    <footer class="border-t border-slate-200 bg-white p-4">
      <div class="mx-auto max-w-5xl">
        <el-input
          v-model="question"
          type="textarea"
          :rows="3"
          resize="none"
          :disabled="chatStore.streaming"
          :placeholder="chatStore.streaming ? 'AI 正在回答...' : '输入问题，Enter 发送，Shift + Enter 换行'"
          @keydown.enter="handleInputEnter"
        />
        <div v-if="attachments.length" class="mt-3 flex flex-wrap gap-2">
          <div
            v-for="attachment in attachments"
            :key="attachment.object_key"
            class="group relative h-16 w-16 overflow-hidden rounded-md border border-slate-200 bg-slate-50"
          >
            <img :src="attachment.url" :alt="attachment.name" class="h-full w-full object-cover" />
            <button
              type="button"
              class="absolute right-1 top-1 hidden rounded bg-slate-900/70 p-0.5 text-white group-hover:block"
              @click="removeAttachment(attachment)"
            >
              <el-icon><CloseBold /></el-icon>
            </button>
          </div>
        </div>
        <div class="mt-3 flex items-center justify-between gap-3">
          <span class="text-xs text-slate-400">答案由知识库检索增强生成，请结合业务资料复核。</span>
          <div class="flex items-center gap-2">
            <el-upload
              :show-file-list="false"
              :before-upload="handleImageUpload"
              accept="image/png,image/jpeg,image/webp"
              :disabled="chatStore.streaming || uploadingAttachment"
            >
              <el-button :icon="Picture" :loading="uploadingAttachment" :disabled="chatStore.streaming">
                图片
              </el-button>
            </el-upload>
            <el-button v-if="chatStore.streaming" type="warning" :icon="CloseBold" @click="stopGeneration">
              停止生成
            </el-button>
            <el-button
              type="primary"
              :disabled="chatStore.streaming || (!question.trim() && !attachments.length)"
              @click="sendMessage"
            >
              发送
            </el-button>
          </div>
        </div>
      </div>
    </footer>

    <el-drawer v-model="sourcesVisible" title="参考资料" size="360px" append-to-body>
      <div v-if="activeSources.length" class="space-y-4">
        <article
          v-for="source in activeSources"
          :key="`${source.file_id}-${source.index}`"
          class="rounded-lg border border-slate-200 bg-white p-3"
        >
          <div class="flex items-start gap-2">
            <span class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-medium text-brand-700">
              {{ source.index }}
            </span>
            <div class="min-w-0">
              <h3 class="truncate text-sm font-medium text-slate-900">{{ source.file_name || '未命名资料' }}</h3>
              <p class="mt-1 whitespace-pre-wrap text-xs leading-5 text-slate-500">{{ source.content || source.excerpt }}</p>
              <div class="mt-2 flex flex-wrap gap-1 text-[11px] text-slate-400">
                <span
                  v-if="source.rerank_score !== null && source.rerank_score !== undefined"
                  class="rounded bg-slate-100 px-1.5 py-0.5"
                >
                  rerank {{ formatScore(source.rerank_score) }}
                </span>
                <span v-if="source.rrf_score" class="rounded bg-slate-100 px-1.5 py-0.5">
                  RRF {{ Number(source.rrf_score).toFixed(3) }}
                </span>
                <span
                  v-for="route in source.routes || []"
                  :key="`${source.index}-${route.route}-${route.rank}`"
                  class="rounded bg-slate-100 px-1.5 py-0.5"
                >
                  {{ route.route }} #{{ route.rank }}
                </span>
              </div>
              <p v-if="source.rerank_reason" class="mt-1 text-[11px] leading-4 text-slate-400">
                {{ source.rerank_reason }}
              </p>
            </div>
          </div>
        </article>
      </div>
      <el-empty v-else description="暂无参考资料" />
    </el-drawer>

    <el-drawer v-model="traceVisible" title="本次回答执行流程" size="720px" append-to-body>
      <div v-loading="traceLoading" class="space-y-4">
        <div class="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
          Trace ID：{{ activeTrace.trace_id || '--' }} · 状态：{{ activeTrace.status || '--' }}
        </div>
        <el-tabs v-model="traceTab">
          <el-tab-pane label="时间线" name="timeline">
            <el-timeline v-if="traceEvents.length">
              <el-timeline-item
                v-for="event in traceEvents"
                :key="event.index"
                :timestamp="event.time"
                placement="top"
              >
                <div class="rounded-md border border-slate-200 bg-white p-3">
                  <div class="flex items-center justify-between gap-3">
                    <p class="text-sm font-medium text-slate-800">{{ event.index }}. {{ event.stage }}</p>
                    <span class="text-xs text-slate-400">{{ event.function }}</span>
                  </div>
                  <p class="mt-1 text-xs leading-5 text-slate-500">{{ event.note || '暂无说明' }}</p>
                </div>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-else description="暂无流程事件" />
          </el-tab-pane>

          <el-tab-pane label="变量流" name="variables">
            <el-table :data="traceVariableRows" size="small" border>
              <el-table-column prop="name" label="变量" width="180" />
              <el-table-column prop="stage" label="创建/使用位置" width="180" />
              <el-table-column prop="value" label="值摘要" />
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="检索过程" name="retrieval">
            <el-collapse v-if="retrievalRoutes.length">
              <el-collapse-item
                v-for="route in retrievalRoutes"
                :key="route.route"
                :title="`${route.route} · ${route.count || 0} 条`"
              >
                <p class="mb-2 whitespace-pre-wrap text-xs text-slate-500">Query：{{ route.query }}</p>
                <div class="space-y-2">
                  <div
                    v-for="item in route.items || []"
                    :key="`${route.route}-${item.file_id}-${item.chunk_id}`"
                    class="rounded-md border border-slate-200 bg-white p-2 text-xs"
                  >
                    <p class="font-medium text-slate-700">{{ item.file_name || '未命名资料' }} #{{ item.chunk_id }}</p>
                    <p class="mt-1 whitespace-pre-wrap text-slate-500">{{ item.excerpt }}</p>
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
            <el-empty v-else description="暂无检索明细" />
          </el-tab-pane>

          <el-tab-pane label="记忆管理" name="memory">
            <div class="space-y-3 text-xs leading-5 text-slate-600">
              <p>滑动窗口：最近 4 轮；摘要触发：完整回答轮数 &gt; 8 且未摘要轮数 &gt;= 4。</p>
              <div
                v-for="event in memoryEvents"
                :key="event.index"
                class="rounded-md border border-slate-200 bg-white p-3"
              >
                <p class="font-medium text-slate-800">{{ event.stage }}</p>
                <p class="mt-1">{{ event.note }}</p>
                <pre class="mt-2 max-h-56 overflow-auto rounded bg-slate-50 p-2">{{ formatJson(event.creates || event.result || event.params) }}</pre>
              </div>
              <el-empty v-if="!memoryEvents.length" description="暂无记忆事件" />
            </div>
          </el-tab-pane>

          <el-tab-pane label="RAGAS评估" name="ragas">
            <div class="space-y-3 text-xs leading-5 text-slate-600">
              <div
                v-for="event in ragasEvents"
                :key="event.index"
                class="rounded-md border border-slate-200 bg-white p-3"
              >
                <p class="font-medium text-slate-800">{{ event.stage }}</p>
                <p class="mt-1">{{ event.note }}</p>
                <pre class="mt-2 max-h-56 overflow-auto rounded bg-slate-50 p-2">{{ formatJson(event.result || event.params || event.creates) }}</pre>
              </div>
              <el-empty v-if="!ragasEvents.length" description="暂无 RAGAS 事件" />
            </div>
          </el-tab-pane>

          <el-tab-pane label="原始JSON" name="json">
            <pre class="max-h-[70vh] overflow-auto rounded-md bg-slate-950 p-3 text-xs leading-5 text-slate-100">{{ prettyTrace }}</pre>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatDotSquare,
  CloseBold,
  CopyDocument,
  Document,
  Edit,
  Loading,
  MagicStick,
  Picture,
  Plus,
  Refresh,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import { useChatStore } from '@/stores/chat'
import { useKnowledgeStore } from '@/stores/knowledge'
import { chatAPI } from '@/api/chat'

const route = useRoute()
const router = useRouter()
const chatStore = useChatStore()
const knowledgeStore = useKnowledgeStore()

const chatRef = ref(null)
const question = ref('')
const renaming = ref(false)
const renameTitle = ref('')
const renameRef = ref(null)
const sourcesVisible = ref(false)
const activeSources = ref([])
const traceVisible = ref(false)
const traceLoading = ref(false)
const traceTab = ref('timeline')
const activeTrace = ref({ trace_id: '', status: '', events: [] })
const attachments = ref([])
const uploadingAttachment = ref(false)
const knowledgeBases = computed(() => knowledgeStore.knowledgeBases)
const selectedKnowledgeBaseId = ref(null)

const isCurrentConversationStreaming = computed(() => {
  if (!chatStore.streaming) return false
  if (!chatStore.streamingConversationId) return !route.params.id && !chatStore.currentId
  return chatStore.streamingConversationId === chatStore.currentId
})

const currentKnowledgeBaseName = computed(() => {
  if (chatStore.currentConversation?.knowledge_base_name) {
    return chatStore.currentConversation.knowledge_base_name
  }
  const selectedBase = knowledgeBases.value.find((item) => item.id === selectedKnowledgeBaseId.value)
  return selectedBase?.name || '未选择知识库'
})

const suggestions = [
  '请总结企业知识库中的核心制度',
  '知识库中有哪些项目交付规范？',
  '根据已有资料生成一份培训提纲',
  '帮我提炼文档里的常见问题',
]

const ragasMetrics = [
  { key: 'faithfulness', label: 'Faithfulness' },
  { key: 'response_relevancy', label: 'Relevancy' },
  { key: 'context_precision_without_reference', label: 'Context precision' },
]

const traceEvents = computed(() => activeTrace.value?.events || [])

const traceVariableRows = computed(() => {
  const rows = []
  for (const event of traceEvents.value) {
    for (const groupName of ['creates', 'uses', 'params', 'result']) {
      const group = event[groupName] || {}
      for (const [name, value] of Object.entries(group)) {
        rows.push({
          name,
          stage: `${event.stage} / ${groupName}`,
          value: stringifyBrief(value),
        })
      }
    }
  }
  return rows
})

const retrievalRoutes = computed(() => {
  const event = [...traceEvents.value].reverse().find((item) => item.stage === 'retrieval_completed')
  return event?.creates?.routes || []
})

const memoryEvents = computed(() =>
  traceEvents.value.filter((event) => event.stage?.includes('memory'))
)

const ragasEvents = computed(() =>
  traceEvents.value.filter((event) => event.stage?.includes('ragas'))
)

const prettyTrace = computed(() => formatJson(activeTrace.value))

onMounted(async () => {
  await refreshKnowledgeBases()
  await chatStore.fetchConversations().catch(() => {})
  if (route.params.id && route.params.id !== chatStore.currentId) {
    await chatStore.selectConversation(route.params.id).catch(() => router.replace('/chat'))
    syncSelectedKnowledgeBase()
  }
})

watch(
  () => route.params.id,
  async (id) => {
    await refreshKnowledgeBases()
    if (!id) {
      if (!chatStore.streaming) {
        chatStore.clearMessages()
      }
      syncSelectedKnowledgeBase()
      return
    }
    if (id !== chatStore.currentId) {
      await chatStore.selectConversation(id).catch(() => {
        ElMessage.error('对话加载失败')
        router.replace('/chat')
      })
      syncSelectedKnowledgeBase()
    }
  }
)

watch(
  () => chatStore.streaming,
  (value) => {
    if (!value) {
      scrollToBottom()
    }
  }
)

watch(
  () => chatStore.currentId,
  (id) => {
    if (id && !route.params.id && !chatStore.streaming) {
      router.replace(`/chat/${id}`)
    }
  }
)

watch(
  () => chatStore.pendingRouteConversationId,
  (id) => {
    if (id && !route.params.id) {
      router.replace(`/chat/${id}`)
    }
  }
)

watch(
  () => chatStore.errorMessage,
  (message) => {
    if (message) {
      ElMessage.error(message)
    }
  }
)

function askSuggestion(text) {
  question.value = text
  sendMessage()
}

function createNewConversation() {
  chatStore.clearMessages()
  router.push('/chat')
  syncSelectedKnowledgeBase()
}

function sendMessage() {
  const text = question.value.trim()
  if ((!text && !attachments.value.length) || chatStore.streaming) return

  const sendingAttachments = [...attachments.value]
  question.value = ''
  attachments.value = []
  chatStore.sendMessage(text, sendingAttachments)
  scrollToBottom()
}

function handleInputEnter(event) {
  if (event.shiftKey) return
  event.preventDefault()
  sendMessage()
}

function stopGeneration() {
  chatStore.stopGeneration()
}

async function copyMessage(content) {
  try {
    await navigator.clipboard.writeText(content)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择文本')
  }
}

async function regenerate(index) {
  const messages = chatStore.messages
  const previousUserMessage = [...messages]
    .slice(0, index)
    .reverse()
    .find((message) => message.role === 'user')

  if (!previousUserMessage || chatStore.streaming) return

  chatStore.replaceMessages(messages.slice(0, index))
  question.value = previousUserMessage.content
  await sendMessage()
}

function startRename() {
  renaming.value = true
  renameTitle.value = chatStore.currentConversation?.title || ''
  nextTick(() => renameRef.value?.focus())
}

async function confirmRename() {
  if (!renaming.value) return

  renaming.value = false
  const title = renameTitle.value.trim()
  if (!title || title === chatStore.currentConversation?.title) return

  try {
    await chatStore.renameConversation(chatStore.currentId, title)
    ElMessage.success('已重命名')
  } catch {
    ElMessage.error('重命名失败')
  }
}

function scrollToBottom() {
  nextTick(() => {
    chatRef.value?.scrollTo({ top: chatRef.value.scrollHeight, behavior: 'smooth' })
  })
}

function openSources(sources) {
  activeSources.value = sources || []
  sourcesVisible.value = true
}

function hasTrace(message) {
  const trace = message?.learning_trace || message?.retrieval_trace?.learning_trace || {}
  return Boolean(message?.trace_id || trace.trace_id || trace.events?.length)
}

async function openTrace(message) {
  traceVisible.value = true
  traceTab.value = 'timeline'
  const embeddedTrace = message?.learning_trace || message?.retrieval_trace?.learning_trace || {}
  activeTrace.value = embeddedTrace || { trace_id: '', status: '', events: [] }
  const traceId = message?.trace_id || embeddedTrace.trace_id
  if (!traceId && !message?.id) return

  traceLoading.value = true
  try {
    const response = message?.id
      ? await chatAPI.getMessageTrace(message.id)
      : await chatAPI.getTrace(traceId)
    activeTrace.value = normalizeTrace(response)
  } catch (error) {
    if (!activeTrace.value?.events?.length) {
      ElMessage.warning('暂无可加载的流程详情')
    }
  } finally {
    traceLoading.value = false
  }
}

function normalizeTrace(trace = {}) {
  return {
    trace_id: trace.trace_id || '',
    status: trace.status || '',
    events: Array.isArray(trace.events) ? trace.events : [],
  }
}

function formatJson(value) {
  try {
    return JSON.stringify(value || {}, null, 2)
  } catch {
    return String(value || '')
  }
}

function stringifyBrief(value) {
  const text = typeof value === 'string' ? value : formatJson(value)
  return text.length > 220 ? `${text.slice(0, 220)}...` : text
}

function formatScore(score) {
  if (score === null || score === undefined || score === '') return '--'
  const numeric = Number(score)
  if (Number.isNaN(numeric)) return '--'
  return numeric.toFixed(2)
}

function ragasStatusText(status) {
  const textMap = {
    pending: '等待中',
    running: '评测中',
    done: '已完成',
    failed: '失败',
  }
  return textMap[status] || status
}

function ragasStatusClass(status) {
  return {
    pending: 'text-slate-400',
    running: 'text-brand-600',
    done: 'text-emerald-600',
    failed: 'text-red-500',
  }[status] || 'text-slate-400'
}

function shouldShowImageAnalysisWarning(message) {
  return ['partial', 'failed'].includes(message?.image_analysis_status)
}

function imageAnalysisWarningText(message) {
  if (message?.image_analysis_error) {
    return message.image_analysis_error
  }
  if (message?.image_analysis_status === 'failed') {
    return '图片部分未能识别，当前回答可能仅基于文字问题生成。'
  }
  return '图片内容仅部分识别，已结合可识别信息继续回答。'
}

async function refreshKnowledgeBases() {
  await knowledgeStore.refreshKnowledgeBases().catch(() => [])
  syncSelectedKnowledgeBase()
}

function syncSelectedKnowledgeBase() {
  const preferredId = chatStore.selectedKnowledgeBaseId
  const hasPreferred = knowledgeBases.value.some((item) => item.id === preferredId)
  const nextId = hasPreferred ? preferredId : knowledgeBases.value[0]?.id || null
  selectedKnowledgeBaseId.value = nextId
  chatStore.setSelectedKnowledgeBaseId(nextId)
}

function changeKnowledgeBase(id) {
  chatStore.setSelectedKnowledgeBaseId(id)
  syncSelectedKnowledgeBase()
}

async function handleImageUpload(file) {
  const allowedTypes = ['image/png', 'image/jpeg', 'image/webp']
  if (!allowedTypes.includes(file.type)) {
    ElMessage.error('仅支持 png、jpg、jpeg、webp 图片')
    return false
  }
  if (file.size > 5 * 1024 * 1024) {
    ElMessage.error('图片不能超过 5MB')
    return false
  }

  uploadingAttachment.value = true
  try {
    const response = await chatAPI.uploadAttachment(file)
    attachments.value.push(response)
    ElMessage.success('图片已添加')
  } finally {
    uploadingAttachment.value = false
  }
  return false
}

function removeAttachment(attachment) {
  attachments.value = attachments.value.filter((item) => item !== attachment)
}
</script>
