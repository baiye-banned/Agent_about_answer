function isAbsoluteAssetUrl(value) {
  return /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(value)
}

// Attributes whose value is a URL and can therefore carry a script protocol.
const URL_ATTRIBUTE_NAMES = new Set([
  'href',
  'src',
  'xlink:href',
  'action',
  'formaction',
  'poster',
  'background',
  'cite',
])

const ALLOWED_LINK_PROTOCOLS = new Set(['http:', 'https:', 'mailto:'])

// Characters a browser never counts as part of a URL scheme: C0 controls and
// space (tabs and line breaks anywhere in the value), DEL and C1 controls.
// HTML entities such as `&#9;` are already decoded by the HTML parser before an
// attribute value reaches this module, so removing the same characters here
// matches the scheme the browser will actually execute.
const IGNORED_URL_CHARS = /[\x00-\x20\x7f-\x9f]/g

const URL_SCHEME = /^([a-z][a-z0-9+.-]*):/

export function isUrlAttribute(name) {
  if (typeof name !== 'string') return false
  return URL_ATTRIBUTE_NAMES.has(name.toLowerCase())
}

export function isSafeLinkUrl(value) {
  if (value === null || value === undefined) return true

  // Inspection only: the original value is preserved when it is considered safe.
  const normalized = String(value).replace(IGNORED_URL_CHARS, '').toLowerCase()
  const scheme = URL_SCHEME.exec(normalized)

  // No scheme at all means a relative URL, an `#anchor` or a `//host` path,
  // which inherits the page protocol and cannot execute script.
  return scheme === null || ALLOWED_LINK_PROTOCOLS.has(`${scheme[1]}:`)
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
