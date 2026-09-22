import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  MAX_LINE,
  VARIANT_RUN_MIN,
  WRAP_WIDTH,
  findParagraphs,
  parseConflicts,
  reflow,
  unwrap,
  wrap,
} from '../scripts/tests_readme_magnet.mjs'

// issue #202：tests/README.md 里两段「These tests cover …」登记清单原本各写成**一整行**
// （改前 8291 / 17210 字符）。每次登记新测试都是在同一行尾部追加子句，于是任意两个同时打开的
// 登记 PR 都改到同一行 —— 行级三方合并没有可用的公共基线，必判冲突，冲突块等于整段全文，
// 用「取一侧」解就会静默丢掉另一侧刚登记的条目（本仓一天实测为此解冲突 ≥7 次）。
// 现在这两段按软换行折成正常宽度的多行（Markdown 段落内的软换行渲染成空格，折行前后渲染一致）。
// 本文件是防止磁铁长回来的棘轮：门禁 + 折行器的自证 + 冲突求解器的红绿对照。
// issue #207 补上冲突求解器的第三种形状：两侧都折行、但各自改写了**同一行的同一处** ——
// 逐行并集会把同一句话写两遍，零丢失校验发现不了（两侧文字确实都在结果里），所以这个形状
// 必须拒绝；签名只能算「疑似」时退一档，照写但向 stderr 告警。

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const README = path.join(REPO_ROOT, 'tests/README.md')
const SCRIPT = path.join(REPO_ROOT, 'scripts/tests_readme_magnet.mjs')

const WORK_DIR = mkdtempSync(path.join(tmpdir(), 'readme-magnet-'))
let caseIndex = 0

function runUnion(body) {
  caseIndex += 1
  const file = path.join(WORK_DIR, `case-${caseIndex}.md`)
  writeFileSync(file, body, 'utf8')
  // 必须用 spawnSync 的 stdio:'pipe'：execFileSync 会把子进程 stderr 直接透传给父进程
  //（拿不到），而 union 的「疑似同点改写」提示只在 stderr 上 —— 用 execFileSync 的话这条
  // 断言会永远看到空字符串，用例静默空转。
  const result = spawnSync(process.execPath, [SCRIPT, 'union', file], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  return {
    code: result.status ?? -1,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    output: `${result.stdout || ''}${result.stderr || ''}`,
    after: readFileSync(file, 'utf8'),
  }
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
  // 正常并集这条路径的行为一个字都不许变：不该多出任何提示（issue #207 加的两档判据都只针对
  // 「两侧的对应行高度重合」，两侧各追加一条不同子句碰不到它们）。这条断言同时是检测器不许
  // 退化成「见谁都拒/见谁都喊」的哨兵。
  assert.equal(result.stderr, '', `正常并集不该有 stderr 输出：${result.stderr}`)
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

test('union refuses when both sides rewrote the same spot of one line (issue #207)', () => {
  // 两侧都已折行、各自改写**同一行的同一处**（例：首行末词改成 conversation-A / conversation-B）。
  // 冲突块里是这同一行的两个变体：互不相等也互不包含，于是逐行并集把两个都追加进同一段，
  // 同一句话被写两遍 —— 而零丢失校验（判据是「冲突块两侧的文本成段连续地出现在结果里」）
  // **必然通过**，因为两侧的文字确实都在结果里，只是一句话写了两遍。下游也拦不住：折行后
  // 每行都在上限内，check 判通过、format 判「已是目标宽度」，坏产物看上去完全合规。
  // 这个形状无法机械求并（git 已把公共行剥掉，谁也没法证明那几个字符是新写的还是改上去的），
  // 拒绝即安全；写出错句子才致命。
  const ours = 'These tests cover frontend stream parsing, the chat store conversation-A'
  const theirs = 'These tests cover frontend stream parsing, the chat store conversation-B'
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
    'lifecycle, and the knowledge base indexing path used by the chat page.',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 1, `同点改写应当拒绝：${result.output}`)
  assert.equal(result.after, file, '拒绝时不得改动文件（写出被写两遍的段落比拒绝更糟）')
  assert.match(result.output, /同一行的同一处/, '拒绝消息要点明判据')
  assert.match(result.output, /写两遍/, '拒绝消息要点明原因：逐行并集会把同一句话写两遍')
  assert.ok(result.output.includes(ours) && result.output.includes(theirs), '拒绝消息要给出两侧原文供人工择一')
})

test('union refuses the same-spot rewrite on a note’s last line, where no sentence head repeats', () => {
  // 上一条的兄弟形状：同点改写在段落的**末行**上，产物里重复的是句子片段（`conversation-A
  // lifecycle used by the chat page. conversation-B lifecycle used by the chat page.`），
  // 而不是句头 —— issue 里那份「数『These tests cover』出现几次」的独立探针在这个形状下**完全
  // 看不见**（句头只出现 1 次）。判据必须落在「两侧对应行逐字对比」上，不能退化成句头计数器。
  const ours = 'conversation-A lifecycle used by the chat page.'
  const theirs = 'conversation-B lifecycle used by the chat page.'
  const file = [
    '# Test Baseline',
    '',
    'These tests cover frontend stream parsing, and the chat store',
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
  assert.equal(result.code, 1, `同点改写（末行）应当拒绝：${result.output}`)
  assert.equal(result.after, file, '拒绝时不得改动文件')
  const flat = result.after.replace(/\s+/g, ' ')
  assert.equal(flat.split('These tests cover ').length - 1, 1, '这条形状里句头本来就只出现一次')
  assert.match(result.output, /同一行的同一处/)
})

test('union warns on stderr when a same-spot rewrite can only be suspected', () => {
  // 现实形状：两侧各自把子句**续写在同一条未满宽的末行**上（段落折在 100 列，末行常常没写满），
  // 冲突块里是「同一段原文 + 各自追加的子句」。对照的是**真实的** tests/README.md（仓库外沙盒：
  // 两条分支各按 README 写的流程追加一行子句再跑 format，真 git merge）：冲突块里那一行是
  // `…from the stale one. It also covers the …`，首尾共享 71 个字符，并集把 `…from the stale one.`
  // 写了两遍 —— 也就是说这条形状比 issue 里那份「只在段尾新起一行」的对照臂更常见，而它产出的
  // 同样是重复句。
  //
  // 它却**不能**升级成拒绝：冲突块里只有分歧区域，git 早把公共行剥掉了，「同一段原文 + 各自追加的
  // 子句」与「两侧各追加了一条措辞相近的子句」在文本上签名重合（实测后者首尾共享 73 个字符，比
  // 本条还长），按相似度拒绝会把正常并集一起拒掉 —— 而正常并集正是这个工具存在的理由。
  // 所以这一档只照写 + 告警，把人工识别的成本从「通读产物」降到「看一眼 stderr」。
  const shared = 'the chat page. It also covers the '
  const ours = `${shared}alpha exporter path (#901).`
  const theirs = `${shared}beta importer path (#902).`
  const file = [
    '# Test Baseline',
    '',
    'These tests cover frontend stream parsing, and the chat store lifecycle used by',
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
  assert.equal(result.code, 0, `只能「疑似」时不该拒绝正常并集：${result.output}`)
  assert.notEqual(result.stderr, '', '这个形状下 stderr 必须非空（issue #207 的核心诉求是不再静默）')
  assert.match(result.stderr, /提醒/)
  assert.match(result.stderr, /两遍/)
  // 告警不是空转：产物里那段共享文本确实连着出现了两遍，正是要人工确认的东西。
  const flat = result.after.replace(/\s+/g, ' ')
  assert.equal(flat.split(shared).length - 1, 2, '告警指向的重复在产物里应当真的存在')
  assert.ok(!/^<{7}|^={7}$|^>{7}/m.test(result.after), '并集后不该还留着冲突标记')
  assert.match(result.after, /\n## Python Tests\n$/, 'union 把冲突块之后的正文截掉了')
})

test('union warns on the shape a real registration actually produces (issue #207, PR #212 condition C1)', () => {
  // 上一条是构造出来的；这一条是**真实的** tests/README.md 走 README 自己写的登记流程
  //（段尾新起一行加子句 -> format -> 真 git merge）后，冲突块里逐字截下来的两行。
  // 两段的末行常常没写满（本文件里 Node 那段当时的末行只有 `the stale one.` 14 个字符），
  // 于是两侧的子句都续写到这同一条短末行上 —— 冲突块只有一行、两边各一个变体。
  //
  // 它落进的是**老的**比值闸门之外：共享串 35 个字符（前缀 `the stale one. It also covers the `
  // 33 个 + 后缀 `).` 2 个），而子句把行撑到 82 个字符，老判据要求 run ≥ 0.5 × 82 = 41 才开口。
  // 于是产物把 `the stale one.` 写了两遍、rc=0、stderr 空 —— issue #207 要治的「静默」原样还在。
  // 下面第一组断言把「共享部分短于较短行的一半」钉住：这正是老判据够不到的那一格，
  // 少了它，这条用例会退化成一个随便什么形状都能过的空断言。
  const shared = 'the stale one. It also covers the '
  const ours = `${shared}alpha exporter path refreshing the sidebar (#901).`
  const theirs = `${shared}beta importer path refreshing the sidebar (#902).`
  assert.ok(ours.startsWith(shared) && theirs.startsWith(shared), '样例必须共享那段前缀')
  assert.ok(shared.length < theirs.length / 2, '共享前缀必须短于较短行的一半，否则老判据本来就拦得住')

  const file = [
    '# Test Baseline',
    '',
    'These tests cover frontend stream parsing, and re-fetching replaces the list with',
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
  assert.equal(result.code, 0, `这个形状只能提醒，不该拒绝：${result.output}`)
  assert.notEqual(result.stderr, '', '真实形状下 stderr 必须非空（C1：这一格原先既不拒也不告警）')
  assert.match(result.stderr, /提醒/)
  // 告警指向的东西在产物里真实存在：前缀那 33 个字符连着出现了两遍。
  const flat = result.after.replace(/\s+/g, ' ')
  assert.equal(flat.split(shared).length - 1, 2, '告警指向的重复在产物里应当真的存在')
})

test('the warn line sits at exactly VARIANT_RUN_MIN shared characters', () => {
  // 告警线的边界用例：共享 24 个字符开口，共享 23 个字符闭嘴。「差一个字符」这件事必须有
  // 用例钉住 —— 它是文档里那句判据（“share at least 24 characters”）唯一的实现依据，
  // 阈值一旦被改动（哪怕只挪 1），这条是唯一会红的。
  // 构造：两行 = 共享前缀 + 各自的 3 个字符 + 共享后缀，run = 前缀 + 后缀。
  // 中间那 3 个字符是故意的：分歧量 3 越过拒绝档的 2 字符门槛（否则这一对会先被拒绝档接走，
  // 告警档根本轮不到），同时把两行撑到 27 个字符、够到拒绝档的长度下限 —— 也就是说
  // 这对样例是**贴着拒绝档边界**走到告警档的，边界用例就该这么挑。
  const head = 'the shared lead-in '
  const tail = ' tail'
  const missHead = head.slice(0, -1)
  assert.equal(head.length + tail.length, VARIANT_RUN_MIN, '样例必须正好压在告警线上')
  assert.equal(missHead.length + tail.length, VARIANT_RUN_MIN - 1, '另一个样例必须正好差一个字符')
  const make = (prefix, midA, midB) => [
    '# Test Baseline',
    '',
    'These tests cover the chat store.',
    '<<<<<<< HEAD',
    `${prefix}${midA}${tail}`,
    '=======',
    `${prefix}${midB}${tail}`,
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')
  const hit = runUnion(make(head, 'abc', 'xyz'))
  const miss = runUnion(make(missHead, 'abc', 'xyz'))
  assert.equal(hit.code, 0, `已到告警线不该拒绝：${hit.output}`)
  assert.notEqual(hit.stderr, '', `共享 ${VARIANT_RUN_MIN} 个字符时必须告警`)
  assert.equal(miss.code, 0, `低于告警线更不该拒绝：${miss.output}`)
  assert.equal(miss.stderr, '', `共享 ${VARIANT_RUN_MIN - 1} 个字符时应当安静`)
  // 告警档是「照写 + 提醒」：产物里那段共享文本**确实**连着出现两遍 —— 这正是告警要让
  // 人工看一眼的东西（把它断言成「只出现一次」就等于要求这一档不要告警）。两侧内容都在，
  // 一个都没丢，所以它不是拒绝档要处理的零丢失问题。
  const flat = hit.after.replace(/\s+/g, ' ')
  assert.equal(flat.split(head.trim()).length - 1, 2, '告警指向的重复在产物里应当真的存在')
  assert.match(flat, /abc tail/, 'branch A 的内容丢了')
  assert.match(flat, /xyz tail/, 'branch B 的内容丢了')
})

test('two short conflicting lines are outside every tier, on purpose (condition C3)', () => {
  // C3：两侧都短于 24 个字符时，共享串不可能够到告警线，同点改写既不拒也不告警 ——
  // 这条用例把这个**已知边界**钉成显式行为，免得日后被当成 bug 顺手「修」成误伤。
  // 为什么选择不覆盖：仓库外实测 5 组同点样本与 7 组「两条各自追加的短语」，两个总体在
  // 共享串比值上**交叠**（同点 0.83-0.95，独立 0.50-0.92，间隔 -0.08）。任何阈值都落在
  // 交叠区里，会连同 `and the export pipeline.` / `and the import pipeline.`（0.92，
  // 比五组同点样本里的四组还高）一起告警。宁可不覆盖并在 tests/README.md 里写清，
  // 也不装一条两边都判不准的判据。
  const ours = 'chat page ok-A.'
  const theirs = 'chat page ok-B.'
  assert.ok(Math.max(ours.length, theirs.length) < VARIANT_RUN_MIN, '样例必须落在短行这一格')
  const file = [
    '# Test Baseline',
    '',
    'These tests cover the chat store.',
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
  assert.equal(result.code, 0, `短行不是拒绝档，不许拒绝：${result.output}`)
  assert.equal(result.stderr, '', '短行这一格按设计安静（tests/README.md 写明了这一点）')
  // 安静 ≠ 并得对：这条形状的产物确实是重复句，正是文档要提醒读者自己看一眼的那种。
  assert.equal(result.after.replace(/\s+/g, ' ').split('chat page ok-').length - 1, 2)
})

test('the warn tier does not fire on two ordinary short appends', () => {
  // 短行判据不许往「凡短行就喊」的方向退：两条**各自独立**追加的短语只共享连接词与词干，
  // 共享串 15 个字符（`and the ` 8 + `porter.` 7），够不到 24 —— 必须安静。
  const file = [
    '# Test Baseline',
    '',
    'These tests cover the chat store.',
    '<<<<<<< HEAD',
    'and the exporter.',
    '=======',
    'and the importer.',
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 0, `两条独立短语不该被拒：${result.output}`)
  assert.equal(result.stderr, '', `两条独立短语不该被告警：${result.stderr}`)
  const flat = result.after.replace(/\s+/g, ' ')
  assert.match(flat, /and the exporter\./, 'branch A 的短语丢了')
  assert.match(flat, /and the importer\./, 'branch B 的短语丢了')
})

test('union still merges two similarly worded appends without refusing (issue #207 false-positive arm)', () => {
  // 上一条的反向对照：两条**各自独立**追加、措辞相近的子句（各自登记一条行为）必须照旧并出来。
  // 它与上一条共享同样的签名（差别只有几个词），拒绝它就是把正常并集误伤 —— 这条用例把
  // 「拒绝线不许往这个方向挪」钉住：可以告警，但两侧的登记一条都不能少、rc 必须是 0。
  const file = [
    '# Test Baseline',
    '',
    'These tests cover frontend stream parsing, and the chat store lifecycle.',
    '<<<<<<< HEAD',
    'and the knowledge base indexing path now refreshes the sidebar after a successful removal (issue #901).',
    '=======',
    'and the knowledge base indexing path now refreshes the sidebar after a failed removal (issue #902).',
    '>>>>>>> b',
    '',
    '## Python Tests',
    '',
  ].join('\n')

  const result = runUnion(file)
  assert.equal(result.code, 0, `两条独立追加不许被拒：${result.output}`)
  const flat = result.after.replace(/\s+/g, ' ')
  assert.match(flat, /after a successful removal \(issue #901\)\./, 'branch A 的登记丢了')
  assert.match(flat, /after a failed removal \(issue #902\)\./, 'branch B 的登记丢了')
  assert.match(result.after, /\n## Python Tests\n$/, 'union 把冲突块之后的正文截掉了')
  assert.ok(!/^<{7}|^={7}$|^>{7}/m.test(result.after), '并集后不该还留着冲突标记')
})

test('tests/README.md states the same warn threshold the tool implements (condition C2)', () => {
  // 评审条件 C2：那句 *"A shape it can only **suspect** is written, but the command says so on
  // stderr"* 原来比实现**宽** —— 「怀疑」并不保证有 stderr（真实形状当时正是静默的）。
  // 现在这句话直接把判据写出来，这条用例把文档里的数字钉在实现常量上：只改一边就会红，
  // 「文档比实现宽」这个失败模式不会再悄悄回来（本仓的文档漂移是复发型的）。
  const readme = readFileSync(README, 'utf8')
  const criterion = `the two sides' conflicting lines share at least ${VARIANT_RUN_MIN} characters`
  assert.ok(readme.includes(criterion), `tests/README.md 里那句判据与实现不一致，应含：${criterion}`)
  // C3 那一格同样要写在文档里：短行不覆盖是**决定**，不是遗漏；读者得知道哪里没有护栏。
  const boundary = `shorter than ${VARIANT_RUN_MIN} characters`
  assert.ok(readme.includes(boundary), `短行的已知边界没写进 tests/README.md，应含：${boundary}`)
})

test('parseConflicts reports unbalanced markers instead of guessing', () => {
  assert.throws(() => parseConflicts(['<<<<<<< HEAD', 'ours', '>>>>>>> b'].join('\n')), /没有配对的标记/)
})
