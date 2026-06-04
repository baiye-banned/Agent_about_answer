import { ElMessage } from 'element-plus'


function writeWithTextarea(text) {
  if (!globalThis.document?.body) return false
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()

  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
}

export async function writeClipboardText(text) {
  const value = String(text ?? '')
  try {
    if (globalThis.navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {}

  return writeWithTextarea(value)
}

export async function copyText(text, options = {}) {
  const {
    successMessage = '已复制',
    failureMessage = '复制失败，请手动选择文本',
  } = options

  if (await writeClipboardText(text)) {
    ElMessage.success(successMessage)
    return true
  }

  ElMessage.warning(failureMessage)
  return false
}
