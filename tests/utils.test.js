import test from 'node:test'
import assert from 'node:assert/strict'

import { formatDate, formatDateTime, formatFileSize, formatJson, formatScore, storage } from '../src/utils/index.js'
import { validateImageFile } from '../src/utils/fileValidation.js'
import { getApiErrorMessage } from '../src/utils/httpError.js'
import { imageAnalysisWarningText } from '../src/utils/imageAnalysisStatus.js'
import {
  buildMemoryVariables,
  filterTraceEventsByStageText,
  findLatestMemoryValue,
  findRetrievalRoutes,
  traceEventPayload,
  normalizeTrace,
} from '../src/utils/memoryTrace.js'
import { ragasStatusText } from '../src/utils/ragasStatus.js'
import { normalizeApiAssetUrl } from '../src/utils/url.js'

test('formatDate only treats nullish or empty values as blank', () => {
  assert.equal(formatDate(null), '')
  assert.equal(formatDate(undefined), '')
  assert.equal(formatDate(''), '')
  assert.notEqual(formatDate(0), '')
  assert.equal(formatDate('not-a-date'), '')
})

test('formatDate returns readable Chinese relative labels', () => {
  const now = Date.now()

  assert.equal(formatDate(new Date(now - 30_000).toISOString()), '刚刚')
  assert.equal(formatDate(new Date(now - 5 * 60_000).toISOString()), '5分钟前')
  assert.equal(formatDate(new Date(now - 2 * 60 * 60_000).toISOString()), '2小时前')
})

test('formatDateTime only falls back for nullish or empty values', () => {
  assert.equal(formatDateTime(null), '-')
  assert.equal(formatDateTime(undefined), '-')
  assert.equal(formatDateTime(''), '-')
  assert.notEqual(formatDateTime(0), '-')
  assert.equal(formatDateTime('not-a-date'), '-')
})

test('formatFileSize keeps zero and rejects empty values', () => {
  assert.equal(formatFileSize(0), '0 B')
  assert.equal(formatFileSize(''), '-')
  assert.equal(formatFileSize(null), '-')
})

test('formatJson preserves falsy values instead of coercing them to empty objects', () => {
  assert.equal(formatJson(false), 'false')
  assert.equal(formatJson(0), '0')
})

test('formatScore preserves zero and formats numeric input', () => {
  assert.equal(formatScore(0), '0.00')
  assert.equal(formatScore('3.14159'), '3.14')
  assert.equal(formatScore(''), '--')
  assert.equal(formatScore(Infinity), '--')
})

test('storage helpers tolerate unavailable localStorage', () => {
  const originalLocalStorage = globalThis.localStorage

  try {
    delete globalThis.localStorage

    assert.equal(storage.get('missing'), null)
    assert.doesNotThrow(() => storage.set('missing', { ok: true }))
    assert.doesNotThrow(() => storage.remove('missing'))
  } finally {
    if (originalLocalStorage === undefined) {
      delete globalThis.localStorage
    } else {
      globalThis.localStorage = originalLocalStorage
    }
  }
})

test('storage helpers round-trip JSON values through localStorage', () => {
  const originalLocalStorage = globalThis.localStorage
  const items = new Map()

  globalThis.localStorage = {
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, String(value)),
    removeItem: (key) => items.delete(key),
  }

  try {
    storage.set('profile', { name: 'Alice', count: 0 })
    assert.deepEqual(storage.get('profile'), { name: 'Alice', count: 0 })

    storage.remove('profile')
    assert.equal(storage.get('profile'), null)
  } finally {
    if (originalLocalStorage === undefined) {
      delete globalThis.localStorage
    } else {
      globalThis.localStorage = originalLocalStorage
    }
  }
})

test('buildMemoryVariables preserves primitive trace groups', () => {
  const variables = buildMemoryVariables({
    index: 1,
    uses: false,
    creates: 0,
    result: [],
    params: { ok: false },
  })

  assert.deepEqual(
    variables.map((item) => [item.name, item.value]),
    [
      ['value', false],
      ['value', 0],
      ['value', []],
      ['ok', false],
    ]
  )
})

test('buildMemoryVariables tolerates missing trace events', () => {
  assert.deepEqual(buildMemoryVariables(null), [])
  assert.deepEqual(buildMemoryVariables(undefined), [])
})

test('findLatestMemoryValue tolerates missing variable lists', () => {
  assert.equal(findLatestMemoryValue(null, 'memory_context'), '')
  assert.equal(findLatestMemoryValue(undefined, 'memory_context'), '')
  assert.equal(findLatestMemoryValue({}, 'memory_context'), '')
})

test('trace event helpers tolerate missing event lists', () => {
  assert.deepEqual(normalizeTrace(null), { trace_id: '', status: '', events: [] })
  assert.deepEqual(findRetrievalRoutes(null), [])
  assert.deepEqual(findRetrievalRoutes({}), [])
  assert.deepEqual(filterTraceEventsByStageText(null, 'memory'), [])
  assert.deepEqual(filterTraceEventsByStageText({}, 'memory'), [])
})

test('trace event helpers ignore malformed events inside event lists', () => {
  const events = [
    null,
    'bad-event',
    { stage: 'memory_context_built' },
    { stage: 'retrieval_completed', creates: { routes: ['vector'] } },
  ]

  assert.deepEqual(findRetrievalRoutes(events), ['vector'])
  assert.deepEqual(filterTraceEventsByStageText(events, 'memory'), [{ stage: 'memory_context_built' }])
})

test('traceEventPayload returns the first displayable event payload', () => {
  assert.deepEqual(traceEventPayload({ result: { answer: 'ok' }, params: { q: 'x' } }), { answer: 'ok' })
  assert.deepEqual(traceEventPayload({ result: '', params: { q: 'x' } }), { q: 'x' })
  assert.deepEqual(traceEventPayload({ result: null, params: '', creates: { routes: ['vector'] } }), { routes: ['vector'] })
  assert.deepEqual(traceEventPayload({ result: 0, params: { ignored: true } }), 0)
  assert.deepEqual(traceEventPayload(null), {})
})

test('normalizeApiAssetUrl keeps absolute urls and prefixes api origin', () => {
  assert.equal(normalizeApiAssetUrl(null, '/api'), '')
  assert.equal(normalizeApiAssetUrl('', '/api'), '')
  assert.equal(normalizeApiAssetUrl('  ', '/api'), '')
  assert.equal(normalizeApiAssetUrl('https://cdn.example.com/a.png', '/api'), 'https://cdn.example.com/a.png')
  assert.equal(normalizeApiAssetUrl('//cdn.example.com/a.png', '/api'), '//cdn.example.com/a.png')
  assert.equal(normalizeApiAssetUrl('data:image/png;base64,abc', '/api'), 'data:image/png;base64,abc')
  assert.equal(normalizeApiAssetUrl('blob:http://localhost/image-id', '/api'), 'blob:http://localhost/image-id')
  assert.equal(normalizeApiAssetUrl(' /uploads/a.png ', '/api'), '/uploads/a.png')
  assert.equal(normalizeApiAssetUrl('/uploads/a.png', 'http://127.0.0.1:8020/api'), 'http://127.0.0.1:8020/uploads/a.png')
  assert.equal(normalizeApiAssetUrl('uploads/a.png', 'http://127.0.0.1:8020/api'), 'http://127.0.0.1:8020/uploads/a.png')
  assert.equal(normalizeApiAssetUrl('/uploads/a.png', '/api'), '/uploads/a.png')
})

test('validateImageFile validates type and optional size limit', () => {
  assert.equal(validateImageFile({ type: 'image/png', size: 10 }), '')
  assert.equal(
    validateImageFile({ type: 'text/plain', size: 10 }, { typeMessage: 'bad type' }),
    'bad type'
  )
  assert.equal(
    validateImageFile({ type: 'image/jpeg', size: 2 * 1024 * 1024 }, { maxSizeMb: 1, sizeMessage: 'too big' }),
    'too big'
  )
  assert.equal(
    validateImageFile({ type: 'image/png' }, { maxSizeMb: 1, sizeMessage: 'missing size' }),
    'missing size'
  )
  assert.equal(validateImageFile({ type: 'image/webp', size: 2 * 1024 * 1024 }), '')
})

test('getApiErrorMessage returns displayable strings', () => {
  assert.equal(
    getApiErrorMessage({ response: { data: { detail: 'bad request' } } }, 'fallback'),
    'bad request'
  )
  assert.equal(getApiErrorMessage({ response: { data: { detail: 0 } } }, 'fallback'), '0')
  assert.equal(getApiErrorMessage({ response: { data: { message: false } } }, 'fallback'), 'false')
  assert.equal(
    getApiErrorMessage({ response: { data: { detail: { field: 'name' } } } }, 'fallback'),
    '{"field":"name"}'
  )
  assert.equal(getApiErrorMessage({ message: '' }, 'fallback'), 'fallback')
  assert.equal(getApiErrorMessage(new Error('boom'), 'fallback'), 'boom')
})

test('status text helpers return strings for unknown values', () => {
  const circular = {}
  circular.self = circular

  assert.equal(ragasStatusText(null), '')
  assert.equal(ragasStatusText(0), '0')
  assert.equal(ragasStatusText({ status: 'custom' }), '{"status":"custom"}')
  assert.equal(imageAnalysisWarningText('failed', { reason: 'ocr' }), '{"reason":"ocr"}')
  assert.equal(ragasStatusText(circular), '[object Object]')
  assert.equal(imageAnalysisWarningText('failed', circular), '[object Object]')
})
