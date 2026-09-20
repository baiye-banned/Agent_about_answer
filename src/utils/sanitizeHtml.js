import { isSafeLinkUrl, isUrlAttribute } from './url.js'

const HTML_NAMESPACE = 'http://www.w3.org/1999/xhtml'

// Element whitelist: everything the renderer can legitimately produce (marked
// with GFM + breaks) plus the inline formatting tags an answer may carry.
// Anything else is not rendered as an element at all, so a new HTML element
// cannot become exploitable just because nobody updated a blacklist.
const ALLOWED_ELEMENTS = new Set([
  'a',
  'abbr',
  'b',
  'blockquote',
  'br',
  'cite',
  'code',
  'dd',
  'del',
  'dl',
  'dt',
  'em',
  'figcaption',
  'figure',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'i',
  'img',
  'ins',
  'kbd',
  'li',
  'mark',
  'ol',
  'p',
  'pre',
  'q',
  's',
  'samp',
  'small',
  'span',
  'strong',
  'sub',
  'sup',
  'table',
  'tbody',
  'td',
  'tfoot',
  'th',
  'thead',
  'tr',
  'u',
  'ul',
  'var',
])

// Attributes kept per allowed element; every other attribute is dropped
// (`id`/`name` would allow DOM clobbering, `style` allows external resource
// loads, `srcset` picks its own URL, `data-*` is of no use to a static answer).
// URL attributes are additionally checked against the shared URL policy.
const ALLOWED_ATTRIBUTES = new Map(
  Object.entries({
    a: ['href', 'title'],
    code: ['class'],
    img: ['alt', 'src', 'title'],
    ol: ['start'],
    span: ['class'],
    td: ['align'],
    th: ['align'],
  }).map(([tag, names]) => [tag, new Set(names)])
)

// Elements dropped together with their content, because what they hold is
// never a rendered text answer: document metadata that acts on the whole page
// (`<base>` rewrites every relative URL, `<meta http-equiv=refresh>` navigates),
// script/style payloads, nested browsing contexts, and form controls used to
// fake a login prompt. Keeping "just the text" of these would leave the
// phishing copy on screen, so the whole subtree goes.
const DROP_WITH_SUBTREE = new Set([
  'applet',
  'audio',
  'base',
  'button',
  'canvas',
  'datalist',
  'embed',
  'fieldset',
  'form',
  'frame',
  'frameset',
  'iframe',
  'input',
  'keygen',
  'label',
  'legend',
  'link',
  'marquee',
  'meta',
  'meter',
  'noscript',
  'object',
  'optgroup',
  'option',
  'output',
  'param',
  'portal',
  'progress',
  'script',
  'select',
  'source',
  'style',
  'template',
  'textarea',
  'title',
  'track',
  'video',
])

function isAllowedInHtmlNamespace(node, tagName) {
  // `<svg>` and `<math>` open a foreign namespace, where the same tag name can
  // mean something else entirely (`<a>` in SVG can carry `xlink:href`,
  // `foreignObject` re-enters HTML). Only HTML-namespace elements are rendered.
  return node.namespaceURI === HTML_NAMESPACE && ALLOWED_ELEMENTS.has(tagName)
}

function stripUnsafeAttributes(node, tagName) {
  const allowed = ALLOWED_ATTRIBUTES.get(tagName)

  for (const attr of [...node.attributes]) {
    const name = attr.name.toLowerCase()
    const isUnsafeUrl = isUrlAttribute(name) && !isSafeLinkUrl(attr.value)

    if (!allowed || !allowed.has(name) || name.startsWith('on') || isUnsafeUrl) {
      node.removeAttribute(attr.name)
    }
  }
}

export function sanitizeHtml(html) {
  if (typeof document === 'undefined') return html

  const template = document.createElement('template')
  template.innerHTML = html

  // Deepest element first: by the time a parent is handled, every child has
  // already been sanitized, so promoting a child out of a removed wrapper can
  // never put unsanitized markup back into the tree.
  const elements = [...template.content.querySelectorAll('*')].reverse()

  for (const node of elements) {
    const tagName = node.tagName.toLowerCase()

    if (isAllowedInHtmlNamespace(node, tagName)) {
      stripUnsafeAttributes(node, tagName)
      continue
    }

    if (DROP_WITH_SUBTREE.has(tagName) || node.namespaceURI !== HTML_NAMESPACE) {
      node.remove()
      continue
    }

    // Unknown but harmless HTML element: drop the tag, keep the children.
    node.replaceWith(...node.childNodes)
  }

  return template.innerHTML
}
