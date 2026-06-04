import { formatJson } from './index.js'


export const MEMORY_VARIABLE_DESCRIPTIONS = {
  recent_text: '短期记忆变量。本轮从 messages 表按最近窗口实时读取并格式化生成，不单独持久化。',
  'conv.memory_summary': '长期记忆摘要，持久化在 conversations.memory_summary 字段中。',
  'conv.memory_summary_upto_message_id': '长期记忆已摘要到的消息 ID，避免近期窗口重复包含已摘要消息。',
  memory_context: '本轮实际注入回答链路的会话记忆，由长期摘要和最近对话窗口拼接生成。',
  retrieval_question: '结合 memory_context 改写后的检索问题，用于 RAG 检索时消解指代和省略。',
  memory_used: '本轮是否存在可用的会话记忆上下文。',
  used_for_retrieval: '本轮检索问题是否因为记忆上下文而不同于原始问题。',
  window_turns: '近期记忆滑动窗口保留的对话轮数。',
  summary_length: '当前长期记忆摘要的字符长度。',
  summary_limit: '长期记忆摘要允许保留的最大字符数。',
  reason: '本次长期记忆更新或压缩被跳过、失败的原因。',
  memory_summary: '长期记忆摘要变量，持久化在 conversations.memory_summary 字段中。',
}

export function buildMemoryVariables(event) {
  const source = event ?? {}
  const variables = []
  const addGroup = (group, prefix = '') => {
    if (group === null || group === undefined) return
    const entries = typeof group === 'object' && !Array.isArray(group)
      ? Object.entries(group)
      : [['value', group]]
    entries.forEach(([name, value]) => {
      const variableName = prefix ? `${prefix}.${name}` : name
      variables.push({
        key: `${source.index ?? 'unknown'}-${prefix || 'value'}-${name}`,
        name: memoryVariableName(variableName),
        description: memoryVariableDescription(variableName),
        value,
      })
    })
  }

  addGroup(source.uses, 'uses')
  addGroup(source.creates, 'creates')
  addGroup(source.result, 'result')
  addGroup(source.params, 'params')
  return variables
}

export function normalizeTrace(trace = {}) {
  const value = trace ?? {}
  return {
    trace_id: value.trace_id ?? '',
    status: value.status ?? '',
    events: asEventList(value.events),
  }
}

export function findRetrievalRoutes(events = []) {
  const event = [...asEventList(events)].reverse().find((item) => item.stage === 'retrieval_completed')
  return Array.isArray(event?.creates?.routes) ? event.creates.routes : []
}

export function filterTraceEventsByStageText(events = [], text = '') {
  return asEventList(events).filter((event) => event.stage?.includes(text))
}

export function traceEventPayload(event) {
  if (hasDisplayValue(event?.result)) return event.result
  if (hasDisplayValue(event?.params)) return event.params
  if (hasDisplayValue(event?.creates)) return event.creates
  return {}
}

export function formatMemoryValue(value) {
  if (value === null || value === undefined || value === '') return '（空）'
  if (typeof value === 'string') return value
  return formatJson(value)
}

export function memoryValueSize(value) {
  if (value === null || value === undefined || value === '') return '0 字'
  if (typeof value === 'string') return `${value.length} 字`
  return `${formatJson(value).length} 字`
}

export function findLatestMemoryValue(variables, name) {
  if (!Array.isArray(variables)) return ''
  const variable = [...variables].reverse().find((item) => item.name === name)
  return variable?.value ?? ''
}

function memoryVariableName(name) {
  const cleanName = name.replace(/^(uses|creates|result|params)\./, '')
  const nameMap = {
    memory_summary: 'conv.memory_summary',
    summary_upto_message_id: 'conv.memory_summary_upto_message_id',
  }
  return nameMap[cleanName] || cleanName
}

function memoryVariableDescription(name) {
  return MEMORY_VARIABLE_DESCRIPTIONS[memoryVariableName(name)] || '记忆管理链路中的真实运行时变量。'
}

function hasDisplayValue(value) {
  return value !== null && value !== undefined && value !== ''
}

function asEventList(events) {
  return Array.isArray(events) ? events.filter((event) => event && typeof event === 'object') : []
}
