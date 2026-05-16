<template>
  <div class="flex h-screen flex-col overflow-hidden bg-slate-100 p-4">
    <div class="mb-3 flex shrink-0 items-center justify-between rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div>
        <div class="text-sm font-semibold text-slate-900">学习中心</div>
        <div class="mt-1 text-xs text-slate-500">切换查看两个真实检索流程图</div>
      </div>

      <div class="inline-flex rounded-md border border-slate-200 bg-slate-50 p-1">
        <button
          v-for="option in viewOptions"
          :key="option.value"
          type="button"
          class="rounded px-3 py-1.5 text-sm font-medium transition-colors"
          :class="activeView === option.value ? 'bg-brand-600 text-white shadow-sm' : 'text-slate-600 hover:bg-white hover:text-slate-900'"
          @click="activeView = option.value"
        >
          {{ option.label }}
        </button>
      </div>
    </div>

    <div class="min-h-0 flex-1">
      <AgenticRetrievePaper v-if="activeView === 'agentic'" />
      <RetrieveKnowledgePaper v-else />
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

import AgenticRetrievePaper from './AgenticRetrievePaper.vue'
import RetrieveKnowledgePaper from './RetrieveKnowledgePaper.vue'

const activeView = ref('agentic')

const viewOptions = [
  { label: 'agentic_retrieve_knowledge', value: 'agentic' },
  { label: 'retrieve_knowledge', value: 'retrieve' },
]
</script>
