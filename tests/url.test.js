import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { isSafeLinkUrl, isUrlAttribute, normalizeApiAssetUrl } from '../src/utils/url.js'

const char = String.fromCharCode

// Control characters browsers strip before reading a URL scheme.
const NUL = char(0)
const LF = char(10)
const CR = char(13)
const C1 = char(0x85)

function splitScheme(scheme) {
  // `java<TAB>script:` — the exact bypass shape from issue #12.
  return `java${scheme}script:alert(document.domain)`
}

test('isSafeLinkUrl rejects javascript: URL hidden behind control characters', () => {
  const controlChars = {
    'tab (0x09)': char(9),
    'line feed (0x0a)': LF,
    'carriage return (0x0d)': CR,
    'vertical tab (0x0b)': char(11),
    'form feed (0x0c)': char(12),
    'NUL (0x00)': NUL,
    'C1 control (0x85)': C1,
  }

  for (const [label, control] of Object.entries(controlChars)) {
    assert.equal(isSafeLinkUrl(splitScheme(control)), false, `scheme split by ${label} must be rejected`)
  }

  // Several controls at once, plus controls around the whole value.
  assert.equal(isSafeLinkUrl(`${LF}java${char(9)}${CR}script:alert(1)${char(9)}`), false)
})

test('isSafeLinkUrl rejects javascript: regardless of case and surrounding space', () => {
  assert.equal(isSafeLinkUrl('javascript:alert(1)'), false)
  assert.equal(isSafeLinkUrl('JaVaScRiPt:alert(1)'), false)
  assert.equal(isSafeLinkUrl('  javascript:alert(1)  '), false)
  assert.equal(isSafeLinkUrl(`${char(9)}javascript:alert(1)`), false)
})

test('isSafeLinkUrl rejects other script-capable protocols', () => {
  for (const url of [
    'vbscript:msgbox(1)',
    'data:text/html,<script>alert(1)</script>',
    'blob:https://example.com/id',
    'file:///etc/passwd',
    'jAvAsCrIpT:void(0)',
  ]) {
    assert.equal(isSafeLinkUrl(url), false, `${url} must be rejected`)
  }
})

test('isSafeLinkUrl keeps normal links untouched', () => {
  for (const url of [
    'https://example.com/docs',
    'http://127.0.0.1:8020/uploads/a.png',
    'mailto:someone@example.com',
    '/uploads/a.png',
    'uploads/a.png',
    './relative/path',
    '../up/one/level',
    '#anchor',
    '?query=1',
    '//cdn.example.com/a.png',
    '',
    '   ',
    'a/b:c', // no scheme: the colon is not part of a scheme
  ]) {
    assert.equal(isSafeLinkUrl(url), true, `${url} must be kept`)
  }

  assert.equal(isSafeLinkUrl(null), true)
  assert.equal(isSafeLinkUrl(undefined), true)
})

test('isUrlAttribute covers href, src and xlink:href synonyms', () => {
  for (const name of ['href', 'HREF', 'src', 'SRC', 'xlink:href', 'XLINK:HREF', 'action', 'formaction']) {
    assert.equal(isUrlAttribute(name), true, `${name} must be treated as a URL attribute`)
  }

  for (const name of ['onclick', 'onerror', 'style', 'class', 'data-url', '']) {
    assert.equal(isUrlAttribute(name), false, `${name} is not a URL attribute`)
  }

  assert.equal(isUrlAttribute(undefined), false)
})

test('MarkdownRenderer sanitizes URL attributes through the shared policy', () => {
  // The sanitizer lives in its own module so the renderer and the tests run the
  // exact same code; keep asserting on the source that is actually executed.
  const source = readFileSync(new URL('../src/utils/sanitizeHtml.js', import.meta.url), 'utf8')

  assert.match(source, /import \{ isSafeLinkUrl, isUrlAttribute \} from '\.\/url\.js'/)
  assert.match(source, /isUrlAttribute\(name\) && !isSafeLinkUrl\(attr\.value\)/)
  // The prefix check that issue #12 bypassed must not come back.
  assert.equal(source.includes("startsWith('javascript:')"), false)

  // Same loop as the component: every attribute whose name is a URL attribute
  // and whose value fails the policy is removed.
  const keptAfterSanitize = (attributes) =>
    attributes.filter(([name, value]) => !(name.toLowerCase().startsWith('on') || (isUrlAttribute(name) && !isSafeLinkUrl(value))))

  assert.deepEqual(
    keptAfterSanitize([
      ['href', splitScheme(char(9))],
      ['href', splitScheme('\n')],
      ['xlink:href', 'javascript:alert(1)'],
      ['src', 'data:text/html,<script>alert(1)</script>'],
      ['onclick', 'alert(1)'],
      ['href', 'https://example.com/docs'],
      ['href', '#anchor'],
      ['target', '_blank'],
    ]),
    [
      ['href', 'https://example.com/docs'],
      ['href', '#anchor'],
      ['target', '_blank'],
    ]
  )
})

test('normalizeApiAssetUrl still resolves asset paths', () => {
  assert.equal(normalizeApiAssetUrl('/uploads/a.png', '/api'), '/uploads/a.png')
  assert.equal(normalizeApiAssetUrl('uploads/a.png', 'http://127.0.0.1:8020/api'), 'http://127.0.0.1:8020/uploads/a.png')
})
