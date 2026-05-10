<template>
  <div class="space-y-4">
    <div class="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
      <div class="font-medium text-slate-800">真实 Trace 回放</div>
      <div class="mt-2 grid gap-2 text-xs sm:grid-cols-2">
        <span>trace_id：{{ trace?.trace_id || '--' }}</span>
        <span>status：{{ trace?.status || '--' }}</span>
        <span>conversation_id：{{ trace?.conversation_id || '--' }}</span>
        <span>message_id：{{ trace?.message_id || '--' }}</span>
      </div>
    </div>

    <el-empty v-if="!events.length" description="输入 trace_id 后加载真实流程数据" />

    <el-tabs v-else v-model="activeTab">
      <el-tab-pane label="时间线" name="timeline">
        <div class="max-h-[46vh] space-y-3 overflow-y-auto pr-1">
          <button
            v-for="event in events"
            :key="event.index"
            type="button"
            class="w-full rounded-xl border p-3 text-left transition-colors"
            :class="Number(event.index) === Number(selectedIndex)
              ? 'border-brand-300 bg-brand-50'
              : 'border-slate-200 bg-white hover:border-slate-300'"
            @click="$emit('select-event', event.index)"
          >
            <div class="flex items-center justify-between gap-3">
              <span class="text-sm font-semibold text-slate-800">
                {{ event.index }}. {{ event.stage || '--' }}
              </span>
              <span class="text-xs text-slate-400">{{ event.time || '--' }}</span>
            </div>
            <div class="mt-1 text-xs text-slate-500">{{ event.function || '--' }}</div>
            <p class="mt-2 text-xs leading-5 text-slate-500">
              {{ event.note || '--' }}
            </p>
          </button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="变量表" name="variables">
        <el-table :data="variableRows" size="small" border max-height="420">
          <el-table-column prop="name" label="字段名" width="160" />
          <el-table-column prop="group" label="来源组" width="96" />
          <el-table-column prop="stage" label="stage / function" width="190" />
          <el-table-column prop="value" label="当前值" min-width="260" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="当前事件" name="event">
        <div class="rounded-xl border border-slate-200 bg-white p-3">
          <div class="mb-3 text-sm font-semibold text-slate-800">
            {{ selectedEvent?.stage || '--' }} / {{ selectedEvent?.function || '--' }}
          </div>
          <pre class="max-h-[46vh] overflow-auto rounded-lg bg-slate-950 p-3 text-xs leading-5 text-slate-100">{{ selectedEventJson }}</pre>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  trace: {
    type: Object,
    default: () => ({}),
  },
  selectedIndex: {
    type: [Number, String],
    default: 0,
  },
})

defineEmits(['select-event'])

const activeTab = ref('timeline')

const events = computed(() => (Array.isArray(props.trace?.events) ? props.trace.events : []))

const selectedEvent = computed(() => {
  const found = events.value.find((event) => Number(event.index) === Number(props.selectedIndex))
  return found || events.value[0] || null
})

const variableRows = computed(() => {
  const rows = []
  for (const event of events.value) {
    for (const groupName of ['params', 'uses', 'creates', 'result']) {
      const group = event?.[groupName]
      if (!group || typeof group !== 'object') continue
      for (const [name, value] of Object.entries(group)) {
        rows.push({
          name,
          group: groupName,
          stage: `${event.stage || '--'} / ${event.function || '--'}`,
          value: stringifyBrief(value),
        })
      }
    }
  }
  return rows
})

const selectedEventJson = computed(() => stringifyJson(selectedEvent.value || {}))

function stringifyJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '--')
  }
}

function stringifyBrief(value) {
  if (value === null || value === undefined || value === '') return '--'
  const text = typeof value === 'string' ? value : stringifyJson(value)
  return text.length > 260 ? `${text.slice(0, 260)}...` : text
}
</script>
