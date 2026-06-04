import { displayText } from './displayText.js'

export const RAGAS_STATUS = {
  PENDING: 'pending',
  RUNNING: 'running',
  DONE: 'done',
  FAILED: 'failed',
}

export const RAGAS_METRICS = [
  { key: 'faithfulness', label: 'Faithfulness' },
  { key: 'response_relevancy', label: 'Relevancy' },
  { key: 'context_precision_without_reference', label: 'Context precision' },
]

const PENDING_RAGAS_STATUSES = new Set([RAGAS_STATUS.PENDING, RAGAS_STATUS.RUNNING])

const RAGAS_STATUS_TEXT = {
  [RAGAS_STATUS.PENDING]: '等待中',
  [RAGAS_STATUS.RUNNING]: '评测中',
  [RAGAS_STATUS.DONE]: '已完成',
  [RAGAS_STATUS.FAILED]: '失败',
}

const RAGAS_STATUS_CLASS = {
  [RAGAS_STATUS.PENDING]: 'text-slate-400',
  [RAGAS_STATUS.RUNNING]: 'text-brand-600',
  [RAGAS_STATUS.DONE]: 'text-emerald-600',
  [RAGAS_STATUS.FAILED]: 'text-red-500',
}

export function isPendingRagasStatus(status) {
  return PENDING_RAGAS_STATUSES.has(status)
}

export function ragasStatusText(status) {
  if (RAGAS_STATUS_TEXT[status]) return RAGAS_STATUS_TEXT[status]
  if (status === null || status === undefined || status === '') return ''
  return displayText(status)
}

export function ragasStatusClass(status) {
  return RAGAS_STATUS_CLASS[status] || RAGAS_STATUS_CLASS[RAGAS_STATUS.PENDING]
}
