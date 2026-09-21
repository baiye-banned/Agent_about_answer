export function formatDate(dateStr) {
  if (dateStr === null || dateStr === undefined || dateStr === '') return ''
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const diff = now - d
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`
  return d.toLocaleDateString('zh-CN')
}

export function formatDateTime(value, fallback = '-') {
  if (value === null || value === undefined || value === '') return fallback
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return fallback

  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatFileSize(bytes, fallback = '-') {
  if (bytes === null || bytes === undefined || bytes === '') return fallback
  const units = ['B', 'KB', 'MB', 'GB']
  let size = Number(bytes)
  if (!Number.isFinite(size) || size < 0) return fallback
  let index = 0

  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }

  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

export function formatJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

export function formatScore(score, fallback = '--') {
  if (score === null || score === undefined || score === '') return fallback
  const numeric = Number(score)
  if (!Number.isFinite(numeric)) return fallback
  return numeric.toFixed(2)
}

function getStorage() {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

export const storage = {
  get(key) {
    try {
      const target = getStorage()
      if (!target) return null
      return JSON.parse(target.getItem(key))
    } catch {
      return null
    }
  },
  set(key, value) {
    try {
      const target = getStorage()
      if (target) target.setItem(key, JSON.stringify(value))
    } catch {
      // 隐私模式或配额写满时 setItem 会抛错；本地存储属尽力而为，失败即忽略。
    }
  },
  remove(key) {
    try {
      const target = getStorage()
      if (target) target.removeItem(key)
    } catch {
      // 同上：删除失败无需打扰调用方。
    }
  },
}
