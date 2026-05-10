<template>
  <div class="overflow-x-auto overflow-y-hidden rounded-2xl border border-slate-200 bg-white">
    <div class="relative" :style="canvasStyle">
      <svg
        class="absolute inset-0"
        :viewBox="`0 0 ${width} ${height}`"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <defs>
          <marker
            id="learn-arrow"
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="4"
            orient="auto"
            markerUnits="strokeWidth"
          >
            <path d="M 0 0 L 8 4 L 0 8 z" fill="currentColor" />
          </marker>
        </defs>

        <g v-for="edge in renderedEdges" :key="edge.key">
          <path
            :d="edge.path"
            fill="none"
            :class="edge.active ? 'text-brand-500' : 'text-slate-300'"
            stroke="currentColor"
            stroke-width="2.2"
            :stroke-dasharray="edge.active ? '0' : '7 7'"
            marker-end="url(#learn-arrow)"
          />
        </g>
      </svg>

      <div
        v-for="edge in renderedEdges"
        :key="`${edge.key}-label`"
        class="pointer-events-none absolute -translate-x-1/2 -translate-y-1/2 rounded-full border px-3 py-1 text-[11px] leading-4 shadow-sm"
        :class="edge.active ? 'border-brand-200 bg-brand-50 text-brand-700' : 'border-slate-200 bg-white text-slate-500'"
        :style="{ left: `${edge.labelX}px`, top: `${edge.labelY}px` }"
      >
        {{ edge.label }}
      </div>

      <button
        v-for="node in renderedNodes"
        :key="node.id"
        type="button"
        class="absolute text-left outline-none transition-transform hover:-translate-y-0.5"
        :style="node.wrapperStyle"
        @click="$emit('select', node)"
      >
        <template v-if="node.kind === 'variable'">
          <div class="relative">
            <div class="absolute -top-6 left-1/2 -translate-x-1/2 whitespace-nowrap text-center text-[11px] font-medium text-slate-500">
              {{ node.name }}
            </div>
            <div
              class="flex h-36 w-36 flex-col items-center justify-center rounded-full border-2 px-4 text-center shadow-sm"
              :class="node.active ? 'border-brand-500 bg-brand-50 text-brand-700' : 'border-slate-200 bg-white text-slate-700'"
            >
              <div class="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-400">
                变量
              </div>
              <div class="mt-2 line-clamp-4 text-sm font-medium leading-6">
                {{ node.value }}
              </div>
            </div>
            <div class="mt-2 max-w-36 text-center text-[11px] leading-4 text-slate-400">
              {{ node.file }}
            </div>
          </div>
        </template>

        <template v-else>
          <div class="relative">
            <div
              class="flex h-28 w-56 flex-col justify-between rounded-2xl border px-4 py-3 shadow-sm"
              :class="node.active ? 'border-brand-500 bg-brand-50 text-brand-700' : 'border-slate-200 bg-white text-slate-700'"
            >
              <div class="flex items-center justify-between gap-3">
                <div class="min-w-0">
                  <div class="truncate text-sm font-semibold">
                    {{ node.name }}
                  </div>
                  <div class="mt-1 text-[11px] leading-4 text-slate-400">
                    {{ node.file }}
                  </div>
                </div>
                <span class="shrink-0 rounded-full bg-white/80 px-2 py-1 text-[11px] font-medium text-slate-500">
                  方法
                </span>
              </div>
              <div class="line-clamp-2 text-sm leading-6 text-slate-600">
                {{ node.value }}
              </div>
            </div>
          </div>
        </template>
      </button>

      <div class="pointer-events-none absolute right-4 top-4 flex gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-[11px] text-slate-500 shadow-sm">
        <span>气泡 = 变量</span>
        <span>方框 = 方法</span>
        <span>线条 = 数据流</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  nodes: {
    type: Array,
    default: () => [],
  },
  edges: {
    type: Array,
    default: () => [],
  },
  selectedNodeId: {
    type: String,
    default: '',
  },
  activeNodeIds: {
    type: Array,
    default: () => [],
  },
  width: {
    type: Number,
    default: 3720,
  },
  height: {
    type: Number,
    default: 860,
  },
})

defineEmits(['select'])

const activeSet = computed(() => new Set(props.activeNodeIds || []))

function nodeSize(node) {
  return node.kind === 'variable'
    ? { width: 144, height: 144 }
    : { width: 224, height: 112 }
}

const renderedNodes = computed(() =>
  props.nodes.map((node) => {
    const size = nodeSize(node)
    const active = activeSet.value.has(node.id) || props.selectedNodeId === node.id
    return {
      ...node,
      active,
      width: size.width,
      height: size.height,
      wrapperStyle: {
        left: `${node.x}px`,
        top: `${node.y}px`,
        width: `${size.width}px`,
        minHeight: `${size.height}px`,
      },
    }
  })
)

function resolveNode(nodeId) {
  return renderedNodes.value.find((node) => node.id === nodeId) || null
}

function makePath(source, target) {
  const x1 = source.x + source.width
  const y1 = source.y + source.height / 2
  const x2 = target.x
  const y2 = target.y + target.height / 2
  const delta = x2 - x1
  const bend = Math.max(70, Math.min(180, Math.abs(delta) * 0.35))
  const c1x = x1 + bend
  const c2x = x2 - bend
  return {
    path: `M ${x1} ${y1} C ${c1x} ${y1}, ${c2x} ${y2}, ${x2} ${y2}`,
    labelX: (x1 + x2) / 2,
    labelY: (y1 + y2) / 2 - 14,
  }
}

const renderedEdges = computed(() =>
  props.edges
    .map(([sourceId, targetId, label], index) => {
      const source = resolveNode(sourceId)
      const target = resolveNode(targetId)
      if (!source || !target) return null
      const { path, labelX, labelY } = makePath(source, target)
      return {
        key: `${sourceId}-${targetId}-${index}`,
        path,
        label,
        active: source.active && target.active,
        labelX,
        labelY,
      }
    })
    .filter(Boolean)
)

const canvasStyle = computed(() => ({
  width: `${props.width}px`,
  height: `${props.height}px`,
}))
</script>
