import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { MAX_LINE, WRAP_WIDTH, findParagraphs, parseConflicts, reflow, unwrap, wrap } from '../scripts/tests_readme_magnet.mjs'

// issue #202：tests/README.md 里两段「These tests cover …」登记清单原本各写成**一整行**
// （改前 8291 / 17210 字符）。每次登记新测试都是在同一行尾部追加子句，于是任意两个同时打开的
// 登记 PR 都改到同一行 —— 行级三方合并没有可用的公共基线，必判冲突，冲突块等于整段全文，
// 用「取一侧」解就会静默丢掉另一侧刚登记的条目（本仓一天实测为此解冲突 ≥7 次）。
// 现在这两段按软换行折成正常宽度的多行（Markdown 段落内的软换行渲染成空格，折行前后渲染一致）。
// 本文件是防止磁铁长回来的棘轮：门禁 + 折行器的自证 + 冲突求解器的红绿对照。

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const README = path.join(REPO_ROOT, 'tests/README.md')
const SCRIPT = path.join(REPO_ROOT, 'scripts/tests_readme_magnet.mjs')
const MARKER = 'These tests cover '

const WORK_DIR = mkdtempSync(path.join(tmpdir(), 'readme-magnet-'))
let caseIndex = 0

function runUnion(body) {
  caseIndex += 1
  const file = path.join(WORK_DIR, `case-${caseIndex}.md`)
  writeFileSync(file, body, 'utf8')
  let result
  try {
    result = { code: 0, output: execFileSync(process.execPath, [SCRIPT, 'union', file], { encoding: 'utf8' }) }
  } catch (error) {
    result = { code: error.status ?? -1, output: `${error.stdout || ''}${error.stderr || ''}` }
  }
  return { ...result, after: readFileSync(file, 'utf8') }
}

test('tests/README.md has exactly the two registration notes, both within the width cap', () => {
  const lines = readFileSync(README, 'utf8').split('\n')
  const spans = findParagraphs(lines)
  assert.equal(spans.length, 2, '登记段落应恰好两段（Node 一段、Python 一段）')
  for (const span of spans) {
    const over = lines.slice(span.start, span.end).filter((line) => line.length > MAX_LINE)
    assert.deepEqual(over, [], `第 ${span.start + 1} 行起的那段有超过 ${MAX_LINE} 字符的行`)
  }
  // 磁铁的形状判据：一段若又变回单行，行数会塌回 1，这里直接钉住这一点。
  for (const span of spans) {
    assert.ok(span.end - span.start > 1, `第 ${span.start + 1} 行起的那段又变回单行了`)
  }
})

test('both registration notes are in canonical wrapped form', () => {
  const lines = readFileSync(README, 'utf8').split('\n')
  for (const span of findParagraphs(lines)) {
    const physical = lines.slice(span.start, span.end)
    // 折行只在原有空格处断行，所以两侧 unwrap 回来必须逐字节相等 —— 这是内容零丢失的判据。
    assert.equal(unwrap(reflow(physical)), unwrap(physical), '折行改变了段落内容')
    // 再折一次必须一模一样，否则文件没停在规范形态，说明有人手改后没跑 format。
    assert.deepEqual(reflow(physical), physical, `第 ${span.start + 1} 行起的段落不在规范折行形态`)
  }
})

test('wrapping never starts a line with a block-level markdown construct', () => {
  // 软换行把行首交给 Markdown 解析：`# x` 会变标题、`- x` 变列表、`---` 变分割线。
  const BLOCK_START = /^(?:#{1,6}\s|#{1,6}$|>|\||[-+*]\s|[-+*]$|\d{1,9}[.)]\s|={2,}$|-{3,}$)/
  const lines = readFileSync(README, 'utf8').split('\n')
  for (const span of findParagraphs(lines)) {
    for (let i = span.start; i < span.end; i += 1) {
      assert.ok(!BLOCK_START.test(lines[i]), `第 ${i + 1} 行的行首会被当作块级语法：${lines[i].slice(0, 40)}`)
    }
  }
  // 反向对照：把 `#` 放在会超宽的位置，折行器必须不把它甩到行首。
  const glued = wrap(`${'word '.repeat(30)}${'and '.repeat(3)}#59: a note`.trim(), WRAP_WIDTH)
  assert.ok(glued.every((line) => !/^#/.test(line)), '#59 被折到了行首，渲染会变成标题')
})

test('a conflicting merge of two registrations is resolved by union with zero loss', () => {
  // 两条分支各在段尾追加一条子句 —— 正是 issue 复现步骤的形状。
  const file = [
    '# Test Baseline',
    '',
    'Current files:',
    '',
    '- `a.test.js`',
    '',
    'These tests cover the first thing, and the second thing,',
    '<<<<<<< HEAD',
    'and the alpha clause that branch A registered.',
    '=======',
    'and the beta clause that branch B registered.',
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 0, `union 应解出冲突：${result.output}`)
  assert.match(result.after, /alpha clause/)
  assert.match(result.after, /beta clause/)
  assert.ok(!/^<{7}|^={7}$|^>{7}/m.test(result.after), '并集后不该还留着冲突标记')
  // 尾部必须整份保留：求解只替换冲突块，最后一个块之后的正文被漏掉就会**截断整个文件**，
  // 而校验只看登记段落，截断不会被它发现。
  assert.match(result.after, /\n## Python Tests\n$/, 'union 把冲突块之后的正文截掉了')
  // 两侧子句各出现一次：一侧仍是旧格式整段单行时，按行求并集会把这整段文字写两遍。
  const flat = result.after.replace(/\s+/g, ' ')
  assert.equal(flat.split('alpha clause').length - 1, 1)
  assert.equal(flat.split('beta clause').length - 1, 1)
})

test('union resolves a conflict whose other side is still one legacy long line', () => {
  // 本 issue 落地期间的真实形状：登记 PR 从旧格式 base 切出（整段单行），折行侧先合入，
  // 于是冲突块一侧是折行后的多行、另一侧是「同一段文字 + 新子句」的单行。
  // 旧单行完整包含折行侧，逐行求并集会写两遍，必须改判为「取包含另一侧的那一份」。
  const wrapped = [
    'These tests cover the first thing, and the second thing,',
    'and the third thing that the old format used to keep on one very long line.',
  ]
  const legacy = `${wrapped.join(' ')} and the beta clause that branch B registered.`
  assert.ok(legacy.length > MAX_LINE, '样例另一侧必须真的停在旧格式（超长单行）')

  // 冲突块两侧都是**整段**：折行侧是若干行，旧格式侧是同一段文字加新子句的单行
  //（真实 git 在 drift 演练里给出的就是这个形状）。
  const file = [
    '# Test Baseline',
    '',
    'Current files:',
    '',
    '- `a.test.js`',
    '',
    '<<<<<<< HEAD',
    ...wrapped,
    '=======',
    legacy,
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 0, `union 应解出该形状：${result.output}`)
  assert.match(result.output, /完整包含另一侧/)
  const flat = result.after.replace(/\s+/g, ' ')
  assert.equal(flat.split('These tests cover the first thing').length - 1, 1, '整段被写了两遍')
  assert.match(flat, /and the beta clause that branch B registered\./, '新子句丢了')
  assert.match(result.after, /\n## Python Tests\n$/, 'union 把冲突块之后的正文截掉了')
  assert.ok(!/^<{7}|^={7}$|^>{7}/m.test(result.after), '并集后不该还留着冲突标记')
  for (const line of result.after.split('\n')) {
    assert.ok(line.length <= MAX_LINE, `并集后仍有超长行：${line.slice(0, 40)}`)
  }
})

test('union refuses when the legacy and wrapped sides diverge', () => {
  // 两侧格式不同又互不包含：说明内容确有分歧（不是单纯的追加），机械拼接会出错句子。
  const file = [
    'These tests cover the alpha wording that only the legacy side has,',
    '<<<<<<< HEAD',
    'These tests cover a line that the wrapped side rewrote,',
    '=======',
    `These tests cover ${'a completely different clause '.repeat(6)}that the legacy side carries.`,
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 1, '两侧互不包含时应当拒绝')
  assert.equal(result.after, file, '拒绝时不得改动文件')
  assert.match(result.output, /互不包含/)
})

test('union refuses when both sides are still legacy long lines and diverge', () => {
  // PR #205 评审 F1（阻断级）：**两侧都**还是旧格式整段单行时，旧判据（两侧格式是否不同）
  // 为 false，直接落到逐行并集分支 —— 而旧单行本身就含有对面那一份的全部文字，于是整段被写
  // 两遍，`check` 与当时全部 8 条棘轮断言照样全绿。两条在途登记分支互相合并正是这个形状
  //（各自在整段单行尾部追加了不同子句）。这种形状必须拒绝：拒绝即安全，写出错段落才致命。
  const shared = `These tests cover ${'a shared clause that both branches carry verbatim, '.repeat(6)}`
  const ours = `${shared}and the alpha clause that branch A registered (#901).`
  const theirs = `${shared}and the beta clause that branch B registered (#902).`
  assert.ok(ours.length > MAX_LINE && theirs.length > MAX_LINE, '样例两侧都必须真的停在旧格式（超长单行）')
  assert.ok(!ours.includes(theirs) && !theirs.includes(ours), '样例两侧必须互不包含')

  const file = [
    '# Test Baseline',
    '',
    'Current files:',
    '',
    '- `a.test.js`',
    '',
    '<<<<<<< HEAD',
    ours,
    '=======',
    theirs,
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 1, `两侧都旧格式且分歧时应当拒绝：${result.output}`)
  assert.equal(result.after, file, '拒绝时不得改动文件（写出被写两遍的段落比拒绝更糟）')
  assert.match(result.output, /互不包含/, '拒绝消息要点明判据：两侧互不包含')
  assert.match(result.output, /写两遍/, '拒绝消息要点明拒绝的原因：机械拼接会把整段文字写两遍')
})

test('union takes the containing side when both sides are legacy long lines', () => {
  // 上一条的对照臂：两侧都超长并不必然分歧 —— 一侧完整包含另一侧时仍可机械求解（取超集）。
  // 把判据从「两侧格式不同」放宽到「任一侧超长」时，这条包含路径必须一起存活，否则
  // 正常的「旧格式 base + 已折行改动」会从「可解」退化成「拒绝」。
  const head = `These tests cover ${'a shared clause that both branches carry verbatim, '.repeat(5)}a shared tail both branches carry`
  const ours = `${head}, and the alpha clause that branch A registered (#901).`
  const theirs = head
  assert.ok(ours.length > MAX_LINE && theirs.length > MAX_LINE, '样例两侧都必须真的停在旧格式（超长单行）')
  assert.ok(ours.includes(theirs) && !theirs.includes(ours), '样例必须是「一侧完整包含另一侧」')

  const file = [
    '# Test Baseline',
    '',
    'Current files:',
    '',
    '- `a.test.js`',
    '',
    '<<<<<<< HEAD',
    ours,
    '=======',
    theirs,
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 0, `两侧都超长但一侧包含另一侧时应当可解：${result.output}`)
  assert.match(result.output, /完整包含另一侧/)
  const flat = result.after.replace(/\s+/g, ' ')
  assert.equal(flat.split('These tests cover').length - 1, 1, '整段被写了两遍')
  assert.match(flat, /and the alpha clause that branch A registered \(#901\)\./, '包含侧的内容丢了')
  assert.match(result.after, /\n## Python Tests\n$/, 'union 把冲突块之后的正文截掉了')
  assert.ok(!/^<{7}|^={7}$|^>{7}/m.test(result.after), '并集后不该还留着冲突标记')
  for (const line of result.after.split('\n')) {
    assert.ok(line.length <= MAX_LINE, `并集后仍有超长行：${line.slice(0, 40)}`)
  }
})

test('union refuses, and writes nothing, when one paragraph holds more than one conflict block', () => {
  // 两侧共享一段文字时 git 会在中间对齐，把一个段落切成两个冲突块：逐块拼接会把 A 的前半句
  // 接上 B 的前半句、再接共享中段，凑出一段语法通顺但语义错乱的话。求解器必须拒绝。
  const file = [
    '# Test Baseline',
    '',
    'These tests cover the first thing, and',
    '<<<<<<< HEAD',
    'the alpha first half,',
    '=======',
    'the beta first half,',
    '>>>>>>> b',
    'the middle that both branches carry verbatim,',
    '<<<<<<< HEAD',
    'and the alpha second half.',
    '=======',
    'and the beta second half.',
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 1, '无法机械求并集时应当拒绝')
  assert.equal(result.after, file, '拒绝时不得改动文件（半写出的解冲突比拒绝更糟）')
  assert.match(result.output, /无法机械求并集/)
})

test('parseConflicts reports unbalanced markers instead of guessing', () => {
  assert.throws(() => parseConflicts(['<<<<<<< HEAD', 'ours', '>>>>>>> b'].join('\n')), /没有配对的标记/)
})
