function isAbsoluteAssetUrl(value) {
  return /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(value)
}

export function normalizeApiAssetUrl(url, apiBaseUrl = import.meta.env.VITE_API_BASE_URL || '/api') {
  if (url === null || url === undefined || url === '') return ''
  const value = String(url).trim()
  if (!value) return ''
  if (isAbsoluteAssetUrl(value)) return value

  const origin = apiBaseUrl.replace(/\/api\/?$/, '')
  const path = value.startsWith('/') ? value : `/${value}`
  return `${origin}${path}`
}
