<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <div class="min-w-0">
        <p class="text-sm font-semibold text-slate-800">变量流泳道</p>
        <p class="mt-0.5 text-xs text-slate-500">
          按 trace stage 分组展示，左侧是输入变量，中间是方法，右侧是输出变量。
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-4 text-xs text-slate-500">
        <el-switch v-model="onlyMainLine" size="small" active-text="只看主线" />
        <el-switch
          v-model="showBranches"
          size="small"
          active-text="展开分支"
          :disabled="onlyMainLine || !branchRows.length"
        />
      </div>
    </div>

    <el-empty v-if="!events.length" description="暂无变量流数据" />

    <div v-else class="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div class="min-w-0 space-y-3">
        <section class="space-y-3">
          <div class="flex items-center justify-between gap-3">
            <h3 class="text-sm font-semibold text-slate-800">主线</h3>
            <span class="text-xs text-slate-400">{{ mainRows.length }} 个 stage</span>
          </div>

          <article
            v-for="row in displayedMainRows"
            :key="row.id"
            class="rounded-lg border border-slate-200 bg-white p-3 shadow-sm"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="rounded-full bg-brand-50 px-2 py-0.5 text-[11px] font-semibold text-brand-700">
                    {{ row.index }}
                  </span>
                  <h4 class="truncate text-sm font-semibold text-slate-800">{{ row.stage }}</h4>
                </div>
                <p class="mt-1 text-xs text-slate-400">{{ row.function || '--' }} · {{ row.time || '--' }}</p>
              </div>
              <span class="rounded-full bg-emerald-50 px-2 py-1 text-[11px] font-medium text-emerald-700">主线</span>
            </div>

            <div class="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_120px_minmax(0,1fr)]">
              <VariableGroup
                title="输入变量"
                empty-text="无 params / uses"
                :items="row.inputs"
                :selected-id="selectedVariableId"
                @select="selectVariable"
              />

              <div class="flex items-center justify-center">
                <div class="w-full rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-center">
                  <div class="truncate text-xs font-semibold text-slate-700">{{ row.function || row.stage }}</div>
                  <div class="mt-1 text-[11px] text-slate-400">stage 处理</div>
                  <el-icon class="mt-2 text-slate-400"><ArrowRight /></el-icon>
                </div>
              </div>

              <VariableGroup
                title="输出变量"
                empty-text="无 creates / result"
                :items="row.outputs"
                :selected-id="selectedVariableId"
                @select="selectVariable"
              />
            </div>

            <p v-if="row.note" class="mt-3 rounded-md bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-500">
              {{ row.note }}
            </p>
          </article>
        </section>

        <section v-if="branchRows.length && !onlyMainLine" class="space-y-3 rounded-lg border border-dashed border-slate-300 bg-slate-50 p-3">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 class="text-sm font-semibold text-slate-800">分支</h3>
              <p class="mt-0.5 text-xs text-slate-500">记忆、RAGAS、图片、失败等事件默认收起。</p>
            </div>
            <el-button size="small" text @click="showBranches = !showBranches">
              {{ showBranches ? '收起分支' : `展开 ${branchRows.length} 个分支 stage` }}
            </el-button>
          </div>

          <div v-if="!showBranches" class="flex flex-wrap gap-2">
            <span
              v-for="group in branchSummary"
              :key="group.label"
              class="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600"
            >
              {{ group.label }} · {{ group.count }}
            </span>
          </div>

          <article
            v-for="row in showBranches ? branchRows : []"
            :key="row.id"
            class="rounded-lg border border-amber-200 bg-white p-3 shadow-sm"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
                    {{ row.index }}
                  </span>
                  <h4 class="truncate text-sm font-semibold text-slate-800">{{ row.stage }}</h4>
                </div>
                <p class="mt-1 text-xs text-slate-400">{{ row.function || '--' }} · {{ row.time || '--' }}</p>
              </div>
              <span class="rounded-full bg-amber-50 px-2 py-1 text-[11px] font-medium text-amber-700">
                {{ row.branchLabel }}
              </span>
            </div>

            <div class="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_120px_minmax(0,1fr)]">
              <VariableGroup
                title="输入变量"
                empty-text="无 params / uses"
                :items="row.inputs"
                :selected-id="selectedVariableId"
                @select="selectVariable"
              />
              <div class="flex items-center justify-center">
                <div class="w-full rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-center">
                  <div class="truncate text-xs font-semibold text-amber-800">{{ row.function || row.stage }}</div>
                  <div class="mt-1 text-[11px] text-amber-600">分支处理</div>
                  <el-icon class="mt-2 text-amber-500"><ArrowRight /></el-icon>
                </div>
              </div>
              <VariableGroup
                title="输出变量"
                empty-text="无 creates / result"
                :items="row.outputs"
                :selected-id="selectedVariableId"
                @select="selectVariable"
              />
            </div>

            <p v-if="row.note" class="mt-3 rounded-md bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-700">
              {{ row.note }}
            </p>
          </article>
        </section>
      </div>

      <aside class="min-w-0 rounded-lg border border-slate-200 bg-white p-3 shadow-sm xl:sticky xl:top-0 xl:max-h-[72vh] xl:overflow-auto">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-sm font-semibold text-slate-800">变量检视器</p>
            <p class="mt-0.5 text-xs text-slate-400">点击左侧变量查看完整值</p>
          </div>
          <el-button
            v-if="selectedVariable"
            size="small"
            text
            :icon="CopyDocument"
            @click="copySelectedVariable"
          >
            复制
          </el-button>
        </div>

        <el-empty v-if="!selectedVariable" class="mt-8" description="暂无可查看变量" />

        <div v-else class="mt-4 space-y-4">
          <div>
            <p class="break-all text-base font-semibold text-slate-900">{{ selectedVariable.name }}</p>
            <div class="mt-2 flex flex-wrap gap-1">
              <span class="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
                {{ selectedVariable.group }}
              </span>
              <span class="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
                stage {{ selectedVariable.eventIndex }}
              </span>
            </div>
          </div>

          <div class="space-y-2 text-xs text-slate-600">
            <div>
              <span class="text-slate-400">来源：</span>
              <span>{{ selectedVariable.stage }} / {{ selectedVariable.function || '--' }}</span>
            </div>
            <div>
              <span class="text-slate-400">上游：</span>
              <span>{{ upstreamText }}</span>
            </div>
            <div>
              <span class="text-slate-400">下游：</span>
              <span>{{ downstreamText }}</span>
            </div>
          </div>

          <div v-if="sameNameVariables.length > 1" class="rounded-lg border border-slate-200 bg-slate-50 p-2">
            <p class="mb-2 text-xs font-medium text-slate-700">同名变量出现位置</p>
            <div class="flex flex-wrap gap-1">
              <button
                v-for="item in sameNameVariables"
                :key="item.id"
                type="button"
                class="rounded-full border px-2 py-1 text-[11px] transition-colors"
                :class="item.id === selectedVariableId
                  ? 'border-brand-300 bg-brand-50 text-brand-700'
                  : 'border-slate-200 bg-white text-slate-500 hover:border-slate-300'"
                @click="selectVariable(item.id)"
              >
                {{ item.eventIndex }} · {{ item.group }}
              </button>
            </div>
          </div>

          <div>
            <div class="mb-2 flex items-center justify-between">
              <p class="text-xs font-medium text-slate-700">完整值</p>
              <span class="text-[11px] text-slate-400">{{ selectedVariable.valueType }}</span>
            </div>
            <pre class="max-h-[44vh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 p-3 text-xs leading-5 text-slate-100">{{ selectedVariable.fullValue }}</pre>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { computed, defineComponent, h, ref, watch } from 'vue'
import { ArrowRight, CopyDocument } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const GROUP_ORDER = ['params', 'uses', 'creates', 'result']
const INPUT_GROUPS = ['params', 'uses']
const OUTPUT_GROUPS = ['creates', 'result']
const VISIBLE_LIMIT = 4

const props = defineProps({
  trace: {
    type: Object,
    default: () => ({}),
  },
})

const onlyMainLine = ref(true)
const showBranches = ref(false)
const selectedVariableId = ref('')

const events = computed(() => (Array.isArray(props.trace?.events) ? props.trace.events : []))
const allRows = computed(() => events.value.map(buildRow))
const mainRows = computed(() => allRows.value.filter((row) => !row.isBranch))
const branchRows = computed(() => allRows.value.filter((row) => row.isBranch))
const displayedMainRows = computed(() => (mainRows.value.length ? mainRows.value : allRows.value))
const allVariables = computed(() => allRows.value.flatMap((row) => [...row.inputAll, ...row.outputAll]))

const selectedVariable = computed(() =>
  allVariables.value.find((item) => item.id === selectedVariableId.value) || allVariables.value[0] || null
)

const sameNameVariables = computed(() => {
  if (!selectedVariable.value) return []
  return allVariables.value.filter((item) => item.name === selectedVariable.value.name)
})

const upstreamVariable = computed(() => {
  if (!selectedVariable.value) return null
  const index = sameNameVariables.value.findIndex((item) => item.id === selectedVariable.value.id)
  return index > 0 ? sameNameVariables.value[index - 1] : null
})

const downstreamVariable = computed(() => {
  if (!selectedVariable.value) return null
  const index = sameNameVariables.value.findIndex((item) => item.id === selectedVariable.value.id)
  return index >= 0 && index < sameNameVariables.value.length - 1 ? sameNameVariables.value[index + 1] : null
})

const upstreamText = computed(() => formatNeighbor(upstreamVariable.value, '无更早同名变量'))
const downstreamText = computed(() => formatNeighbor(downstreamVariable.value, '无后续同名变量'))

const branchSummary = computed(() => {
  const map = new Map()
  for (const row of branchRows.value) {
    map.set(row.branchLabel, (map.get(row.branchLabel) || 0) + 1)
  }
  return [...map.entries()].map(([label, count]) => ({ label, count }))
})

watch(
  allVariables,
  (variables) => {
    if (!variables.some((item) => item.id === selectedVariableId.value)) {
      selectedVariableId.value = variables[0]?.id || ''
    }
  },
  { immediate: true }
)

watch(onlyMainLine, (value) => {
  if (value) showBranches.value = false
})

watch(showBranches, (value) => {
  if (value) onlyMainLine.value = false
})

function buildRow(event) {
  const inputAll = buildVariables(event, INPUT_GROUPS)
  const outputAll = buildVariables(event, OUTPUT_GROUPS)
  const isBranch = isBranchEvent(event)
  return {
    id: `stage-${event.index ?? event.stage}`,
    index: event.index ?? '--',
    time: event.time || '',
    stage: event.stage || '--',
    function: event.function || '',
    note: event.note || '',
    isBranch,
    branchLabel: branchLabel(event),
    inputAll,
    outputAll,
    inputs: splitVisible(inputAll),
    outputs: splitVisible(outputAll),
  }
}

function buildVariables(event, groups) {
  const rows = []
  for (const group of groups) {
    const value = event?.[group]
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    for (const [name, rawValue] of Object.entries(value)) {
      rows.push({
        id: `${event.index ?? 'x'}-${group}-${name}`,
        name,
        group,
        rawValue,
        preview: stringifyBrief(rawValue, 96),
        fullValue: stringifyFull(rawValue),
        valueType: valueType(rawValue),
        eventIndex: event.index ?? '--',
        stage: event.stage || '--',
        function: event.function || '',
        weight: variableWeight(name, rawValue),
      })
    }
  }
  return rows.sort((left, right) => right.weight - left.weight || GROUP_ORDER.indexOf(left.group) - GROUP_ORDER.indexOf(right.group))
}

function splitVisible(items) {
  return {
    visible: items.slice(0, VISIBLE_LIMIT),
    hidden: items.slice(VISIBLE_LIMIT),
  }
}

function isBranchEvent(event) {
  const text = `${event?.stage || ''} ${event?.function || ''}`.toLowerCase()
  if (text.includes('ragas') || text.includes('memory') || text.includes('image') || text.includes('attachment')) return true
  if (text.includes('failed') || text.includes('rejected') || text.includes('error')) return true
  return Boolean(event?.result?.error || event?.result?.message)
}

function branchLabel(event) {
  const text = `${event?.stage || ''} ${event?.function || ''}`.toLowerCase()
  if (text.includes('ragas')) return 'RAGAS'
  if (text.includes('memory')) return 'memory'
  if (text.includes('image') || text.includes('attachment')) return 'attachments / image_analysis'
  if (text.includes('failed') || text.includes('rejected') || text.includes('error') || event?.result?.error) return 'error'
  return 'branch'
}

function variableWeight(name, value) {
  const key = String(name).toLowerCase()
  const important = [
    'question',
    'knowledge_base_id',
    'conversation_id',
    'trace_id',
    'rag_gate',
    'need_rag',
    'route',
    'confidence',
    'agent',
    'agent_plan',
    'agent_reflection',
    'should_retry',
    'selected_round',
    'rounds_used',
    'max_rounds',
    'stop_reason',
    'mode',
    'effective_question',
    'retrieval_question',
    'context',
    'sources',
    'routes',
    'query_plan',
    'rerank',
    'rrf',
    'full_answer',
    'chunk',
    'assistant_message_id',
    'ragas_status',
    'scores',
    'error',
  ]
  const directHit = important.some((word) => key === word || key.includes(word))
  const structuredBonus = Array.isArray(value) || (value && typeof value === 'object') ? 1 : 0
  return (directHit ? 10 : 0) + structuredBonus
}

function selectVariable(id) {
  selectedVariableId.value = id
}

async function copySelectedVariable() {
  if (!selectedVariable.value) return
  try {
    await navigator.clipboard.writeText(selectedVariable.value.fullValue)
    ElMessage.success('变量值已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择文本')
  }
}

function formatNeighbor(item, emptyText) {
  if (!item) return emptyText
  return `${item.eventIndex}. ${item.stage} / ${item.group}`
}

function stringifyFull(value) {
  try {
    return typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '--')
  }
}

function stringifyBrief(value, maxLength = 120) {
  if (value === null || value === undefined || value === '') return '--'
  const text = typeof value === 'string' ? value : stringifyFull(value)
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
}

function valueType(value) {
  if (Array.isArray(value)) return `array(${value.length})`
  if (value === null) return 'null'
  return typeof value
}

const VariableGroup = defineComponent({
  name: 'VariableGroup',
  props: {
    title: { type: String, required: true },
    emptyText: { type: String, required: true },
    items: { type: Object, required: true },
    selectedId: { type: String, default: '' },
  },
  emits: ['select'],
  setup(componentProps, { emit }) {
    const renderButton = (item) =>
      h(
        'button',
        {
          key: item.id,
          type: 'button',
          class: [
            'w-full rounded-md border px-3 py-2 text-left transition-colors',
            item.id === componentProps.selectedId
              ? 'border-brand-300 bg-brand-50 text-brand-700'
              : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50',
          ],
          onClick: () => emit('select', item.id),
        },
        [
          h('div', { class: 'flex items-center justify-between gap-2' }, [
            h('span', { class: 'min-w-0 truncate text-xs font-semibold' }, item.name),
            h('span', { class: 'shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500' }, item.group),
          ]),
          h('p', { class: 'mt-1 line-clamp-2 break-words text-[11px] leading-4 text-slate-500' }, item.preview),
        ]
      )

    return () =>
      h('section', { class: 'min-w-0 rounded-lg border border-slate-200 bg-slate-50 p-2' }, [
        h('div', { class: 'mb-2 flex items-center justify-between gap-2' }, [
          h('p', { class: 'text-xs font-semibold text-slate-700' }, componentProps.title),
          h(
            'span',
            { class: 'text-[11px] text-slate-400' },
            `${componentProps.items.visible.length + componentProps.items.hidden.length} 个`
          ),
        ]),
        componentProps.items.visible.length
          ? h('div', { class: 'space-y-2' }, componentProps.items.visible.map(renderButton))
          : h('p', { class: 'rounded-md border border-dashed border-slate-200 bg-white px-3 py-4 text-center text-xs text-slate-400' }, componentProps.emptyText),
        componentProps.items.hidden.length
          ? h('details', { class: 'mt-2' }, [
              h('summary', { class: 'cursor-pointer rounded-md border border-dashed border-slate-300 bg-white px-3 py-2 text-xs text-slate-500' }, `更多 ${componentProps.items.hidden.length} 个变量`),
              h('div', { class: 'mt-2 space-y-2' }, componentProps.items.hidden.map(renderButton)),
            ])
          : null,
      ])
  },
})
</script>
