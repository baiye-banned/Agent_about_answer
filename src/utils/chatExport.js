import { getApiErrorMessage } from './httpError.js'

const EXPORT_ERROR = '导出失败，请稍后重试'

function safeMarkdownFilename(value) {
  const name = String(value || '')
    .replace(/\.md$/i, '')
    .replace(/[<>:"/\\|?*]/g, '')
    .replace(/\p{Cc}/gu, '')
    .trim()
    .replace(/^\.+|[. ]+$/g, '')
  if (!name || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name)) return ''
  return `${name}.md`
}

export function conversationExportFilename(disposition, fallbackTitle = 'conversation') {
  const header = String(disposition || '')
  const extended = header.match(/(?:^|;)\s*filename\*\s*=\s*(?:"([^"]*)"|([^;]*))/i)
  const encoded = (extended?.[1] ?? extended?.[2] ?? '').trim().match(/^UTF-8'[^']*'(.*)$/i)
  if (encoded) {
    try {
      const name = safeMarkdownFilename(decodeURIComponent(encoded[1]))
      if (name) return name
    } catch {
      // 格式不合法的 filename* 可以退回普通 filename 或点击时的会话标题。
    }
  }
  const regular = header.match(/(?:^|;)\s*filename\s*=\s*(?:"((?:\\.|[^"])*)"|([^;]*))/i)
  const filename = (regular?.[1] ?? regular?.[2] ?? '').replace(/\\(.)/g, '$1').trim()
  return safeMarkdownFilename(filename) || safeMarkdownFilename(fallbackTitle) || 'conversation.md'
}

function responseHeader(headers, name) {
  return headers?.get?.(name) ?? Object.entries(headers || {}).find(([key]) => key.toLowerCase() === name)?.[1]
}

export function saveConversationMarkdown(response, fallbackTitle) {
  const contentType = responseHeader(response?.headers, 'content-type') || response?.data?.type || ''
  if (
    (response?.status !== undefined && (response.status < 200 || response.status >= 300)) ||
    !(response?.data instanceof Blob) ||
    !/^text\/markdown(?:\s*;|$)/i.test(contentType)
  ) {
    throw new Error(EXPORT_ERROR)
  }

  const filename = conversationExportFilename(
    responseHeader(response.headers, 'content-disposition'),
    fallbackTitle
  )
  const link = document.createElement('a')
  const url = URL.createObjectURL(response.data)
  try {
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
  } finally {
    link.remove()
    // 等浏览器接管下载后再释放，成功与失败路径都不保留临时 URL。
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
  return filename
}

export async function getConversationExportErrorMessage(error) {
  const status = error?.response?.status
  if (status === 401) return '登录已过期，请重新登录'
  if (status === 404) return '对话不存在或无权导出'
  let data = error?.response?.data
  if (data instanceof Blob) {
    try {
      data = JSON.parse(await data.text())
    } catch {
      return EXPORT_ERROR
    }
  }
  if (data?.detail || data?.message) {
    return getApiErrorMessage({ response: { data } }, EXPORT_ERROR)
  }
  return EXPORT_ERROR
}
