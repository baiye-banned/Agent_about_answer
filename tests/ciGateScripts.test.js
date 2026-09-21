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
    // 只拔起始符可能让两侧重新拼出新的起始符，所以消毒必须跑到不动点。
    assert.equal(stripComments(stripped), stripped, '消毒没有到不动点')
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

  // 起始符在行首、结束符在很后面时，删掉整段就会连标题一起删，所以这条分支只拔起始符。
  const lineStart = stripComments([START, '', '## 环境', '', '正文', '', END].join('\n'))
  assert.equal(lineStart.includes(START), false)
  assert.equal(lineStart.includes('## 环境'), true, '小节标题被删掉了')
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

test('comment markers inside a code fence are stripped by design, not by oversight', () => {
  // scripts/lib/markdown_sanitize.mjs 顶部写明这是有意为之：不变式要求返回值里不残留起始符，
  // 围着围栏开例外就等于给这条不变式开口子（方向偏严，不会放松判据）。这组用例把行为钉住，
  // 免得以后有人把它当成 bug 顺手改掉——改之前请先读那段注释并同步这里。
  const onlyComment = runGate('check_pr_body.mjs', prBody(['```', START + ' 说明 ' + END, '```'].join('\n')))
  assert.equal(onlyComment.code, 1, onlyComment.output)

  const commentPlusText = runGate(
    'check_pr_body.mjs',
    prBody(['```', START + ' 说明 ' + END, '新增回归测试。', '```'].join('\n'))
  )
  assert.equal(commentPlusText.code, 0, commentPlusText.output)
})
