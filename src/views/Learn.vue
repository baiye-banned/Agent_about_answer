<template>
  <div class="space-y-5">
    <section class="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div class="min-w-0">
          <div class="text-2xl font-semibold text-slate-900">真实符号驱动的学习中心</div>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-slate-500">
            这里不讲抽象概念，只把项目里真实存在的变量名、函数名、API 字段名和 trace 字段串成一条完整链路。
          </p>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <el-radio-group v-model="mode" size="large">
            <el-radio-button label="demo">示例演示</el-radio-button>
            <el-radio-button label="trace">真实回放</el-radio-button>
          </el-radio-group>

          <el-button :icon="demoPlaying ? VideoPause : VideoPlay" @click="toggleDemoPlay">
            {{ demoPlaying ? '暂停演示' : '播放演示' }}
          </el-button>
          <el-button :icon="RefreshRight" @click="resetDemo">重置演示</el-button>
        </div>
      </div>

      <div class="mt-4 grid gap-3 md:grid-cols-3">
        <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
          <div class="text-xs font-medium uppercase tracking-[0.12em] text-slate-400">当前节点</div>
          <div class="mt-1 text-sm font-semibold text-slate-800">{{ selectedNode?.name || '--' }}</div>
          <div class="mt-1 text-xs leading-5 text-slate-500">{{ selectedNode?.why || '--' }}</div>
        </div>
        <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
          <div class="text-xs font-medium uppercase tracking-[0.12em] text-slate-400">演示进度</div>
          <div class="mt-1 text-sm font-semibold text-slate-800">
            {{ demoIndex + 1 }} / {{ demoPath.length }}
          </div>
          <div class="mt-1 text-xs leading-5 text-slate-500">
            {{ currentDemoNode?.name || '--' }}：{{ currentDemoNode?.value || '--' }}
          </div>
        </div>
        <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
          <div class="text-xs font-medium uppercase tracking-[0.12em] text-slate-400">页面状态</div>
          <div class="mt-1 text-sm font-semibold text-slate-800">
            {{ mode === 'demo' ? '示例演示' : '真实回放' }}
          </div>
          <div class="mt-1 text-xs leading-5 text-slate-500">
            {{ mode === 'demo' ? '默认播放固定流程链路' : '输入 trace_id 后加载真实事件' }}
          </div>
        </div>
      </div>

      <div v-if="mode === 'trace'" class="mt-4 space-y-3">
        <div class="flex flex-col gap-2 lg:flex-row lg:items-center">
          <el-input
            v-model="traceIdInput"
            clearable
            placeholder="输入 trace_id，例如 6f3b2a1c4d..."
            class="max-w-2xl"
            @keyup.enter="loadTrace"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>

          <el-button type="primary" :loading="traceLoading" @click="loadTrace">加载真实 Trace</el-button>
          <el-button :icon="RefreshRight" @click="clearTrace">清空</el-button>

          <span v-if="traceLoading" class="text-xs text-slate-500">正在读取后端 trace 数据...</span>
        </div>

        <el-alert
          v-if="traceError"
          :title="traceError"
          type="error"
          :closable="false"
          show-icon
        />
      </div>
    </section>

    <div class="grid gap-4 xl:grid-cols-[minmax(0,1fr)_440px]">
      <section class="space-y-4">
        <div class="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <div class="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <div class="text-sm font-semibold text-slate-800">
                {{ selectedNode?.name || '请选择一个节点' }}
              </div>
              <div class="mt-1 text-xs text-slate-500">
                {{ selectedNode?.file || '--' }}
              </div>
            </div>
            <div class="text-xs text-slate-400">
              {{ selectedNode?.kind === 'variable' ? '气泡 = 变量' : '方框 = 方法' }}
            </div>
          </div>

          <p class="mt-3 text-sm leading-6 text-slate-600">
            {{ selectedNode?.why || '点击画布上的任意节点，右侧会显示它来自哪个文件、它的上游和下游是谁，以及为什么它在链路里存在。' }}
          </p>
        </div>

        <LearnCanvas
          :nodes="flowSpec.nodes"
          :edges="flowSpec.edges"
          :selected-node-id="selectedNodeId"
          :active-node-ids="activeNodeIds"
          :width="flowSpec.width"
          :height="flowSpec.height"
          @select="handleNodeSelect"
        />
      </section>

      <aside class="space-y-4">
        <section class="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <div class="flex items-center justify-between gap-3">
            <div>
              <div class="text-sm font-semibold text-slate-800">节点详情</div>
              <div class="mt-1 text-xs text-slate-400">只展示真实符号，不编造概念名。</div>
            </div>
            <el-tag v-if="selectedNode" type="info" effect="plain">
              {{ selectedNode.kind === 'variable' ? '变量' : '方法' }}
            </el-tag>
          </div>

          <div v-if="selectedNode" class="mt-4 space-y-3">
            <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
              <div class="text-xs font-medium uppercase tracking-[0.12em] text-slate-400">符号名</div>
              <div class="mt-1 break-words text-sm font-semibold text-slate-800">
                {{ selectedNode.name }}
              </div>
            </div>

            <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
              <div class="text-xs font-medium uppercase tracking-[0.12em] text-slate-400">值</div>
              <div class="mt-1 whitespace-pre-wrap break-words text-sm leading-6 text-slate-700">
                {{ selectedNode.value }}
              </div>
            </div>

            <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
              <div class="text-xs font-medium uppercase tracking-[0.12em] text-slate-400">来源文件</div>
              <div class="mt-1 break-words text-sm text-slate-700">{{ selectedNode.file }}</div>
            </div>

            <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
              <div class="text-xs font-medium uppercase tracking-[0.12em] text-slate-400">为什么存在</div>
              <div class="mt-1 text-sm leading-6 text-slate-700">
                {{ selectedNode.why }}
              </div>
            </div>
          </div>

          <el-empty v-else description="先点一个节点" />
        </section>

        <section class="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <div class="text-sm font-semibold text-slate-800">上游 / 下游</div>
          <div class="mt-4 grid gap-4 sm:grid-cols-2">
            <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
              <div class="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.12em] text-slate-400">
                <el-icon><ArrowLeft /></el-icon>
                <span>上游</span>
              </div>
              <div class="mt-3 space-y-2">
                <div v-for="item in upstreamRelations" :key="`up-${item.id}`" class="rounded-lg border border-slate-200 bg-white px-3 py-2">
                  <div class="text-sm font-medium text-slate-800">{{ item.name }}</div>
                  <div class="mt-1 text-xs text-slate-500">{{ item.label }}</div>
                </div>
                <el-empty v-if="!upstreamRelations.length" description="没有上游节点" />
              </div>
            </div>

            <div class="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
              <div class="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.12em] text-slate-400">
                <el-icon><ArrowRight /></el-icon>
                <span>下游</span>
              </div>
              <div class="mt-3 space-y-2">
                <div v-for="item in downstreamRelations" :key="`down-${item.id}`" class="rounded-lg border border-slate-200 bg-white px-3 py-2">
                  <div class="text-sm font-medium text-slate-800">{{ item.name }}</div>
                  <div class="mt-1 text-xs text-slate-500">{{ item.label }}</div>
                </div>
                <el-empty v-if="!downstreamRelations.length" description="没有下游节点" />
              </div>
            </div>
          </div>
        </section>

        <section class="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <div class="flex items-center justify-between gap-3">
            <div>
              <div class="text-sm font-semibold text-slate-800">分支槽</div>
              <div class="mt-1 text-xs text-slate-400">把失败分支收在这里，避免主线画布过满。</div>
            </div>
            <el-tag type="warning" effect="plain">5 个分支</el-tag>
          </div>

          <div class="mt-4 space-y-3">
            <details
              v-for="branch in branchSpecs"
              :key="branch.title"
              class="group rounded-xl border border-slate-200 bg-slate-50 px-4 py-3"
              :open="isBranchRelevant(branch)"
            >
              <summary class="cursor-pointer list-none">
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0">
                    <div class="truncate text-sm font-semibold text-slate-800">
                      {{ branch.title }}
                    </div>
                    <div class="mt-1 text-xs text-slate-500">{{ branch.symbol }}</div>
                  </div>
                  <el-tag size="small" :type="isBranchRelevant(branch) ? 'danger' : 'info'" effect="plain">
                    {{ branch.file }}
                  </el-tag>
                </div>
              </summary>
              <p class="mt-3 text-sm leading-6 text-slate-600">
                {{ branch.text }}
              </p>
            </details>
          </div>
        </section>

        <section v-if="mode === 'trace'" class="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <LearnTracePanel
            :trace="normalizedTrace"
            :selected-index="selectedTraceIndex"
            @select-event="handleTraceEventSelect"
          />
        </section>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ArrowRight, RefreshRight, Search, VideoPause, VideoPlay } from '@element-plus/icons-vue'

import LearnCanvas from './LearnCanvas.vue'
import LearnTracePanel from './LearnTracePanel.vue'
import { branchSpecs, demoPath, flowSpec, getNodeById, getNodeIdBySymbol, symbolToNodeId } from './learnFlowSpec'
import { chatAPI } from '@/api/chat'

const route = useRoute()

const mode = ref('demo')
const demoIndex = ref(0)
const demoPlaying = ref(false)
const demoTimer = ref(null)
const selectedNodeId = ref(demoPath[0] || '')

const traceIdInput = ref(typeof route.query.trace_id === 'string' ? route.query.trace_id : '')
const traceLoading = ref(false)
const traceError = ref('')
const normalizedTrace = ref({ trace_id: '', status: '', events: [] })
const selectedTraceIndex = ref(0)

const nodeMap = computed(() => new Map(flowSpec.nodes.map((node) => [node.id, node])))
const currentDemoNode = computed(() => getNodeById(demoPath[demoIndex.value]) || null)
const selectedNode = computed(() => nodeMap.value.get(selectedNodeId.value) || currentDemoNode.value)

const activeNodeIds = computed(() => {
  if (mode.value === 'demo') {
    return demoPath.slice(0, demoIndex.value + 1)
  }
  return Array.from(
    new Set(
      (normalizedTrace.value.events || [])
        .map((event) => getNodeIdBySymbol(event.function) || getNodeIdBySymbol(event.stage))
        .filter(Boolean)
    )
  )
})

const upstreamRelations = computed(() => {
  const targetId = selectedNode.value?.id
  if (!targetId) return []
  return flowSpec.edges
    .filter(([from, to]) => to === targetId)
    .map(([from, , label]) => {
      const node = getNodeById(from)
      return node ? { id: node.id, name: node.name, label } : null
    })
    .filter(Boolean)
})

const downstreamRelations = computed(() => {
  const sourceId = selectedNode.value?.id
  if (!sourceId) return []
  return flowSpec.edges
    .filter(([from]) => from === sourceId)
    .map(([, to, label]) => {
      const node = getNodeById(to)
      return node ? { id: node.id, name: node.name, label } : null
    })
    .filter(Boolean)
})

function startDemoTimer() {
  stopDemoTimer()
  demoPlaying.value = true
  demoTimer.value = window.setInterval(() => {
    if (demoIndex.value >= demoPath.length - 1) {
      stopDemoTimer()
      return
    }
    demoIndex.value += 1
    selectedNodeId.value = demoPath[demoIndex.value]
  }, 1400)
}

function stopDemoTimer() {
  if (demoTimer.value) {
    window.clearInterval(demoTimer.value)
    demoTimer.value = null
  }
  demoPlaying.value = false
}

function toggleDemoPlay() {
  if (mode.value !== 'demo') {
    mode.value = 'demo'
  }
  if (demoPlaying.value) {
    stopDemoTimer()
  } else {
    startDemoTimer()
  }
}

function resetDemo() {
  stopDemoTimer()
  demoIndex.value = 0
  selectedNodeId.value = demoPath[0] || ''
}

function handleNodeSelect(node) {
  selectedNodeId.value = node.id
  const stepIndex = demoPath.indexOf(node.id)
  if (stepIndex >= 0) {
    demoIndex.value = stepIndex
  }
}

function normalizeTrace(trace) {
  return {
    trace_id: trace?.trace_id || '',
    status: trace?.status || '',
    conversation_id: trace?.conversation_id || '',
    message_id: trace?.message_id || '',
    events: Array.isArray(trace?.events) ? trace.events : [],
  }
}

function clearTrace() {
  traceError.value = ''
  normalizedTrace.value = { trace_id: '', status: '', events: [] }
  selectedTraceIndex.value = 0
}

async function loadTrace() {
  const traceId = traceIdInput.value.trim()
  if (!traceId) {
    traceError.value = '请先输入 trace_id'
    ElMessage.warning('请先输入 trace_id')
    return
  }

  traceLoading.value = true
  traceError.value = ''
  try {
    const response = await chatAPI.getTrace(traceId)
    normalizedTrace.value = normalizeTrace(response)
    selectedTraceIndex.value = normalizedTrace.value.events[0]?.index || 0
    const firstEvent = normalizedTrace.value.events[0]
    const mappedNodeId = firstEvent
      ? getNodeIdBySymbol(firstEvent.function) || getNodeIdBySymbol(firstEvent.stage)
      : ''
    if (mappedNodeId) {
      selectedNodeId.value = mappedNodeId
    }
    mode.value = 'trace'
    ElMessage.success('真实 Trace 已加载')
  } catch (error) {
    traceError.value = error?.response?.data?.detail || error?.response?.data?.message || error?.message || '加载 Trace 失败'
  } finally {
    traceLoading.value = false
  }
}

function handleTraceEventSelect(index) {
  selectedTraceIndex.value = Number(index)
  const event = normalizedTrace.value.events.find((item) => Number(item.index) === Number(index))
  if (!event) return
  const mappedNodeId =
    getNodeIdBySymbol(event.function) ||
    getNodeIdBySymbol(event.stage) ||
    symbolToNodeId[event.function] ||
    symbolToNodeId[event.stage] ||
    ''
  if (mappedNodeId) {
    selectedNodeId.value = mappedNodeId
  }
}

function isBranchRelevant(branch) {
  const mappedNodeId = getNodeIdBySymbol(branch.symbol)
  return mappedNodeId && selectedNodeId.value === mappedNodeId
}

watch(mode, (nextMode) => {
  stopDemoTimer()
  if (nextMode === 'demo') {
    demoIndex.value = 0
    selectedNodeId.value = demoPath[0] || ''
  }
})

watch(
  () => route.query.trace_id,
  (traceId) => {
    if (typeof traceId === 'string' && traceId.trim()) {
      traceIdInput.value = traceId.trim()
      mode.value = 'trace'
      loadTrace()
    }
  },
  { immediate: true }
)

onMounted(() => {
  selectedNodeId.value = demoPath[0] || ''
})

onBeforeUnmount(() => {
  stopDemoTimer()
})
</script>
