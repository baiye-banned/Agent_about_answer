import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { countSubstantive, stripComments } from '../scripts/lib/markdown_sanitize.mjs'

// issue #97：两个门禁脚本的注释消毒只做单趟替换，于是
//   后果一（偏严）：正文里一次没配对的注释起始符会一路吃到很后面的结束符，把中间的小节标题删掉，
//                   一份结构完整的正文被判成缺节；
//   后果二（偏松）：删注释时两侧重新拼出的起始符会被当成正文计入长度，1 个可见字符就能过「至少 3 个字符」。
// 这两条在下面各有一组用例，前两组打的是不变式，后两组打的是脚本的实际判定。

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const FIXTURES = path.join(REPO_ROOT, 'tests/fixtures/ci-gate')
const START = '<!--'
const END = '-->'
const HEADINGS = /^## .*/gm

// 门禁脚本只接受文件路径，所以待校验的正文先落盘再跑。文件写在系统临时目录里，测试不做清理动作。
const WORK_DIR = mkdtempSync(path.join(tmpdir(), 'ci-gate-'))
let caseIndex = 0

function runGate(script, body) {
  caseIndex += 1
  const file = path.join(WORK_DIR, `case-${caseIndex}.md`)
  writeFileSync(file, body, 'utf8')
  const args = [path.join(REPO_ROOT, 'scripts', script), file]
  try {
    return { code: 0, output: execFileSync(process.execPath, args, { encoding: 'utf8' }) }
  } catch (error) {
    return { code: error.status ?? -1, output: `${error.stdout || ''}${error.stderr || ''}` }
  }
}

// 夹具与 C:/code/aaa-ops 里的红绿对比脚本用的是同一套替换规则。
const ISSUE_FIXTURE = readFileSync(path.join(FIXTURES, 'issue-unpaired-marker.md'), 'utf8')
const PR_TEMPLATE = readFileSync(path.join(FIXTURES, 'pr-body.md'), 'utf8')
const prBody = (changeContent) => PR_TEMPLATE.replace('CHANGE_CONTENT_PLACEHOLDER', changeContent)

test('stripComments leaves no comment start marker behind', () => {
  const crafted = [
    ['<', '!', START, ' 隐藏内容 ', END, '--'].join(''), // issue #97 后果二的构造：拼接后重新拼出起始符
    ['x<', '!', START, ' 隐藏内容 ', END, '--'].join(''),
    `${START} 没有结束符的注释`,
    `${START}${START}${START}`,
    '正文里提到一次 ' + START + ' 但它在别处才闭合，中间还有一个 ' + END + ' 结束符。',
    [START, '', '## 小节标题', '', '内容', '', END].join('\n'), // 起始符在行首、跨过小节标题
  ]

  for (const input of crafted) {
    const stripped = stripComments(input)
    assert.equal(stripped.includes(START), false, `消毒后仍有起始符：${JSON.stringify(stripped)}`)
    // 结果必须是稳定的：再消毒一次不该再变（判据不能在两次消毒之间漂移）。
    assert.equal(stripComments(stripped), stripped, '再消毒一次结果又变了')
  }
})

test('an unpaired comment start marker never deletes a section heading', () => {
  const before = ISSUE_FIXTURE.match(HEADINGS)
  const stripped = stripComments(ISSUE_FIXTURE)

  assert.equal(before.length, 9, '夹具应当是 9 个小节的完整正文')
  assert.deepEqual(stripped.match(HEADINGS), before, '小节标题被消毒删掉了')
  assert.equal(stripped.includes(START), false, '消毒后仍有起始符')
  // 夹具必须保持复现形态：正文里有一次既不在行首、也不在同一行闭合的起始符。
  // 少了它，这组用例就只是「没触发过 bug」，而不是「bug 已修」。
  const stray = ISSUE_FIXTURE.split('\n').find((line) => line.includes(START) && !line.includes(END))
  assert.ok(stray, '夹具里应当有一行只含未闭合的起始符')

  // 行内起始符只要跨了空行或整行标题，渲染时 `<` 就是普通文本（后面的正文都看得见），
  // 所以标记只被拔掉，标题一律留下。
  for (const input of [
    ['正文提到 ' + START, '', '## 环境', '', '正文', '', END].join('\n'),
    ['正文提到 ' + START, '## 环境', '', '正文', '', END].join('\n'),
  ]) {
    const stripped = stripComments(input)
    assert.equal(stripped.includes(START), false)
    assert.equal(stripped.includes('## 环境'), true, '小节标题被删掉了')
  }
})

test('a heading line inside a line-start comment is not a section heading', () => {
  // 行首起始符是 HTML 块注释，块里的 `##` 行渲染时也不是标题，所以跟着注释一起删掉：
  // 注释掉的标题既不该被当成真标题（否则正文里没写的小节也能通过校验），
  // 也不该把所属小节截断（否则正常正文会被判成缺内容）。
  const hidden = stripComments([START, '', '## 测试情况', '', END, '', '真实内容'].join('\n'))
  assert.equal(hidden.includes('测试情况'), false, '注释掉的标题仍然留在正文里')
  assert.equal(hidden.includes('真实内容'), true)
})

test('a real multi-line comment is still removed when a stray marker precedes it', () => {
  const stripped = stripComments(
    ['正文提到 ' + START + ' 这个起始符。', '', START, '必填。', END].join('\n')
  )

  // 只拔掉没配对的起始符，后面那段形态完整的注释照旧整段删除。
  assert.equal(stripped.includes(START), false)
  assert.equal(stripped.includes(END), false)
  assert.equal(stripped.includes('必填'), false)
  assert.equal(stripped.includes('正文提到'), true, '普通正文不应被牵连删除')
})

test('countSubstantive counts only letters, digits and CJK characters', () => {
  assert.equal(countSubstantive('xyz'), 3)
  assert.equal(countSubstantive('🐛✨📝'), 0, 'emoji 不能凑长度')
  assert.equal(countSubstantive('x' + START), 1, '注释残留不能凑长度')
  assert.equal(countSubstantive('-—·｜'), 0, '纯符号不能凑长度')
})

test('check_issue.mjs accepts a body that mentions a comment start marker', () => {
  const { code, output } = runGate('check_issue.mjs', ISSUE_FIXTURE)
  assert.equal(code, 0, output)
  assert.match(output, /\[OK\] issue 校验通过/)
})

test('check_issue.mjs still accepts the same body with every marker removed (control)', () => {
  const control = ISSUE_FIXTURE.split(START).join('').split(END).join('')
  const { code, output } = runGate('check_issue.mjs', control)
  assert.equal(code, 0, output)
})

test('check_pr_body.mjs rejects a section of one visible character plus a crafted fragment', () => {
  // issue #97 的对照表：裸写 1、2 个字符不通过，3 个字符通过；构造片段不能把 1 个字符抬过去。
  const crafted = ['x<!', START, ' 占位说明 ', END, '--'].join('')
  const cases = [
    ['x', 1],
    ['xy', 1],
    ['xyz', 0],
    [crafted, 1],
  ]

  for (const [changeContent, expected] of cases) {
    const { code, output } = runGate('check_pr_body.mjs', prBody(changeContent))
    assert.equal(code, expected, `「变更内容」写成 ${JSON.stringify(changeContent)} 时退出码应为 ${expected}：${output}`)
    if (code !== 0) assert.match(output, /「变更内容」节内容过短/)
  }
})

test('text hidden inside a comment does not count as section content', () => {
  // 起点只有 1 个可见字符，其余都在注释里——渲染出来根本看不见，不能算进长度判据。
  const hiddenShapes = [
    ['-', START, '', '隐藏内容 abcdefgh', END].join('\n'), // 行首起始符：整段注释删掉
    '-' + START + '\n隐藏内容 abcdefgh\n' + END, // 行内起始符、同一段落里闭合：同样整段删掉
  ]
  for (const hidden of hiddenShapes) {
    const { code, output } = runGate('check_pr_body.mjs', prBody(hidden))
    assert.equal(code, 1, `注释里的内容被算成了正文：${output}`)
    assert.match(output, /「变更内容」节内容过短/)
  }

  // 勾选项、关联 issue 的原因同理：藏在注释里的不算数。
  const forgedCheckbox = runGate(
    'check_pr_body.mjs',
    prBody('把消毒改成单趟扫描。').replace('- [x] 🔧 其他', [START, '- [x] 🔧 其他', END].join('\n'))
  )
  assert.equal(forgedCheckbox.code, 1, forgedCheckbox.output)
  assert.match(forgedCheckbox.output, /「类型」节未勾选任何一项/)
})

test('a commented-out heading neither forges nor splits a section', () => {
  // 伪造：整节只有标题被写在注释里，正文里没有这一节。
  const forged = runGate(
    'check_pr_body.mjs',
    prBody('把消毒改成单趟扫描。').replace(/^## 测试情况$/m, [START, '## 测试情况', END].join('\n'))
  )
  assert.equal(forged.code, 1, forged.output)
  assert.match(forged.output, /缺少「测试情况」节/)

  // 误伤：注释掉的同名标题不该把「变更内容」截断，让真实内容落到别处去。
  const split = runGate(
    'check_pr_body.mjs',
    prBody([START, '## 变更内容', END, '', '真实可见内容 abcdef'].join('\n'))
  )
  assert.equal(split.code, 0, split.output)
})

test('comment markers inside a code fence are stripped by design, not by oversight', () => {
  // scripts/lib/markdown_sanitize.mjs 顶部写明这是有意为之：不变式 1 要求返回值里不残留起始符，
  // 围着围栏开例外就等于给这条不变式开口子（方向偏严，不会放松判据）。围栏里特意只留一段
  // 带实质字符的注释：围栏一旦被当成例外，这里就会多出 3 个实质字符、由不通过变成通过。
  const onlyComment = runGate('check_pr_body.mjs', prBody(['```', START + ' abc ' + END, '```'].join('\n')))
  assert.equal(onlyComment.code, 1, onlyComment.output)

  const commentPlusText = runGate(
    'check_pr_body.mjs',
    prBody(['```', START + ' abc ' + END, '新增回归测试。', '```'].join('\n'))
  )
  assert.equal(commentPlusText.code, 0, commentPlusText.output)
})
