#!/usr/bin/env node
// tests/README.md 的两段「These tests cover …」是测试登记清单（issue #202）。
// 它们原本各写成**一整行**（改前 8291 / 17210 字符），于是任何两次并发登记都改到同一行：
// 行级三方合并没有可用的公共基线，必判冲突，而冲突块等于整段全文 —— 「取一侧」会静默丢掉
// 另一侧刚登记的条目。根治办法是把这两段按软换行折成正常宽度的多行：
// Markdown 段落内的软换行渲染成空格，所以折行前后**渲染完全一致**，而追加点从「一行」变成
// 「若干行」，冲突块从整段缩到一条子句。本脚本是这套约定的配套工具：
//
//   node scripts/tests_readme_magnet.mjs check  [file]   # 门禁：两段内不得有超长行
//   node scripts/tests_readme_magnet.mjs format [file]   # 按宽度重新折行（幂等）
//   node scripts/tests_readme_magnet.mjs union  <file>   # 冲突后一条命令求并集，自带零丢失校验
//                                                        # 两侧都折行 -> 逐行并集；任一侧还是旧格式
//                                                        # 单行 -> 取包含另一侧全文的那一份再折行，
//                                                        # 互不包含则拒绝（含两侧都还是旧格式）
//
// 退出码：0 通过；1 违规；2 参数/IO 错误。

import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DEFAULT_FILE = path.join(REPO_ROOT, 'tests/README.md');

// 登记段落的起始标记：整段以它开头，段内不再有其它块结构。
const PARAGRAPH_MARKER = 'These tests cover ';
// 折行目标宽度：与本文件其余手写散文一致（p95 约 96 列）。
const WRAP_WIDTH = 100;
// 门禁宽度：留出人工编辑的余量，但仍比磁铁小三个数量级（17210 字符）。
const MAX_LINE = 120;

// 软换行会把行首的记号交给 Markdown 当块级语法解析，渲染结果就变了
// （`- x` 变列表、`# x` 变标题、`---` 变分割线、`> x` 变引用）。
// 折行不得造出这种行首。
const BLOCK_START = /^(?:#{1,6}\s|#{1,6}$|>|\||[-+*]\s|[-+*]$|\d{1,9}[.)]\s|={2,}$|-{3,}$)/;

function readFile(file) {
  return readFileSync(file, 'utf8');
}

// 段落 = 以标记开头的连续非空行。返回 [{ start, end }]（0 基，end 不含）。
function findParagraphs(lines) {
  const spans = [];
  for (let i = 0; i < lines.length; i += 1) {
    if (!lines[i].startsWith(PARAGRAPH_MARKER)) continue;
    let end = i;
    while (end < lines.length && lines[end].trim() !== '') end += 1;
    spans.push({ start: i, end });
    i = end;
  }
  return spans;
}

// 段落 -> 单个逻辑行。软换行渲染成空格，所以这里是折行的逆运算。
const unwrap = (lines) => lines.join(' ');

// 单个逻辑行 -> 按宽度折成多行。只在原有的空格处断行，因此
// 把结果再 unwrap 回来与输入逐字节相等（折行不增删任何字符）。
function wrap(text, width) {
  const words = text.split(' ');
  const out = [];
  let line = '';
  for (const word of words) {
    if (line === '') {
      line = word;
    } else if (line.length + 1 + word.length <= width) {
      line += ' ' + word;
    } else {
      out.push(line);
      line = word;
    }
  }
  if (line !== '') out.push(line);

  // 行首若是块级语法记号，把该词退回上一行。这只会让上一行略微变长，
  // 幅度是一个词的长度，仍远在 MAX_LINE 之内（调用方会复核）。
  for (let i = 1; i < out.length; i += 1) {
    if (!BLOCK_START.test(out[i])) continue;
    const [first, ...rest] = out[i].split(' ');
    out[i - 1] += ' ' + first;
    if (rest.length === 0) out.splice(i, 1);
    else out[i] = rest.join(' ');
  }
  return out;
}

// 把一段重排成宽行文本，其余原样返回。幂等：对已折行的输入再跑一次结果不变。
// 折行只在原有空格处断行，所以 unwrap(结果) 必须与 unwrap(输入) 逐字节相等 ——
// 这是「历史子句零丢失」的自证，不靠人工比对。
function reflow(lines) {
  const wrapped = wrap(unwrap(lines), WRAP_WIDTH);
  if (unwrap(wrapped) !== unwrap(lines)) {
    throw new Error('折行改变了段落内容（unwrap 后与原文不逐字节相等）');
  }
  const tooLong = wrapped.filter((l) => l.length > MAX_LINE);
  if (tooLong.length > 0) {
    throw new Error(
      `折行后仍有 ${tooLong.length} 行超过 ${MAX_LINE} 字符（无法在宽度内断开的词）：` +
        `${tooLong[0].slice(0, 60)}…`,
    );
  }
  return wrapped;
}

function replaceSpans(lines, spans) {
  const out = [];
  let cursor = 0;
  for (const span of spans) {
    out.push(...lines.slice(cursor, span.start));
    out.push(...reflow(lines.slice(span.start, span.end)));
    cursor = span.end;
  }
  out.push(...lines.slice(cursor));
  return out;
}

// 登记段落的宽行检查。只查这两段：文件里另有若干手写长段（代码块、conftest 说明等）
// 不属本 issue 范围，顺手折它们会把改动面扩大到无从评审。
function checkLines(lines) {
  const problems = [];
  for (const span of findParagraphs(lines)) {
    for (let i = span.start; i < span.end; i += 1) {
      const length = lines[i].length;
      if (length > MAX_LINE) {
        problems.push({ line: i + 1, length, head: lines[i].slice(0, 60) });
      }
    }
  }
  return problems;
}

function cmdCheck(file) {
  const lines = readFile(file).split('\n');
  const paragraphs = findParagraphs(lines);
  if (paragraphs.length === 0) {
    process.stderr.write(`check: ${file} 里没有以「${PARAGRAPH_MARKER}」开头的登记段落\n`);
    return 2;
  }
  const problems = checkLines(lines);
  if (problems.length === 0) {
    process.stdout.write(
      `check: ${paragraphs.length} 个登记段落，最长的行 ${Math.max(
        ...paragraphs.flatMap((s) => lines.slice(s.start, s.end).map((l) => l.length)),
      )} 字符（上限 ${MAX_LINE}）\n`,
    );
    return 0;
  }
  process.stderr.write(
    `check: ${problems.length} 行超过 ${MAX_LINE} 字符。登记一条子句请另起一行，` +
      `然后跑 node scripts/tests_readme_magnet.mjs format\n`,
  );
  for (const p of problems) {
    process.stderr.write(`  L${p.line} ${p.length} 字符: ${p.head}…\n`);
  }
  return 1;
}

function cmdFormat(file) {
  const original = readFile(file);
  const lines = original.split('\n');
  if (findParagraphs(lines).length === 0) {
    process.stderr.write(`format: ${file} 里没有以「${PARAGRAPH_MARKER}」开头的登记段落\n`);
    return 2;
  }
  const formatted = replaceSpans(lines, findParagraphs(lines)).join('\n');
  if (formatted === original) {
    process.stdout.write('format: 已是目标宽度，无改动\n');
    return 0;
  }
  writeFileSync(file, formatted, 'utf8');
  const before = findParagraphs(lines).reduce((n, s) => n + (s.end - s.start), 0);
  const after = findParagraphs(formatted.split('\n')).reduce((n, s) => n + (s.end - s.start), 0);
  process.stdout.write(`format: 登记段落 ${before} 行 -> ${after} 行\n`);
  return 0;
}

// 冲突块解析：<<<<<<< / ======= / >>>>>>>。返回 { ours, theirs } 边界。
const CONFLICT = /^<{7}(?: .*)?$|^={7}$|^>{7}(?: .*)?$/;

function parseConflicts(text) {
  const lines = text.split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    if (!/^<{7}(?: .*)?$/.test(lines[i])) {
      i += 1;
      continue;
    }
    const start = i;
    let mid = -1;
    let end = -1;
    for (let j = i + 1; j < lines.length; j += 1) {
      if (mid === -1 && /^={7}$/.test(lines[j])) mid = j;
      else if (/^>{7}(?: .*)?$/.test(lines[j])) {
        end = j;
        break;
      }
    }
    if (mid === -1 || end === -1) throw new Error(`第 ${start + 1} 行的冲突块没有配对的标记`);
    blocks.push({ start, mid, end });
    i = end + 1;
  }
  return blocks;
}

// 并集校验的判据：冲突块两侧各自的文本必须**成段连续地**出现在结果里。
// 只数 `#N` 是不够的 —— 两侧子句共享一大段相同文字时，git 会在中间对齐、把冲突切成
// 两块，逐块拼接就会把 A 的尾句接上 B 的后半句，凑出一句语法上通顺、语义上错乱的话，
// 而两边的 `#N` 一个不少（本单演练实测）。成段包含能同时抓住「丢内容」和「拼接错位」。
const flatten = (text) => text.replace(/\s+/g, ' ').trim();

// 冲突块的一侧是否还停在旧格式（整段单行）。
const sideHasLongLine = (side) => side.some((line) => line.length > MAX_LINE);

// 求解一个冲突块。返回 { lines, kept, note }；无法机械求解时返回 null。
//
// 判据的门槛是「**任一侧**是否还停在旧格式整段单行」，不是「两侧格式是否不同」：
// 旧单行本身就包含了折行侧的全部内容，对它逐行求并集必然把同一段文字写两遍。所以只要有一侧
// 超长，就一律走文本包含判据 —— 一侧完整包含另一侧时取包含它的那一份（严格无丢失），
// 由调用方重新折行；**两侧都超长且互不包含时同样落到这里，返回 null 拒绝**（issue #202 期间
// 两条在途登记分支互相合并正是这个形状：各自在整段单行尾部追加了不同子句）。
// 旧判据只挡住了「一侧超长」，两侧都超长时掉进逐行并集分支，静默把整段写两遍而零丢失校验
// 照样报绿（PR #205 评审 F1）—— 护栏只装了一半，这里补齐。
//
// 两侧都已是折行格式时才逐行求并集：先取 ours，再补上 theirs 里 ours 没有的行。
// 两次登记落在同一个追加点时，冲突块就是「双方各加了几行」，并集即两边都保留；
// 相同行只留一份，避免重复登记。
function resolveSides(ours, theirs) {
  if (sideHasLongLine(ours) || sideHasLongLine(theirs)) {
    const oursText = flatten(ours.join(' '));
    const theirsText = flatten(theirs.join(' '));
    if (oursText === theirsText) {
      return { lines: ours, kept: [], note: '两侧文字相同，取任一侧后重新折行' };
    }
    if (oursText.includes(theirsText)) {
      return { lines: ours, kept: [], note: '一侧仍是旧格式单行且完整包含另一侧，取该侧后重新折行' };
    }
    if (theirsText.includes(oursText)) {
      return { lines: theirs, kept: [], note: '一侧仍是旧格式单行且完整包含另一侧，取该侧后重新折行' };
    }
    return null;
  }
  const union = [...ours];
  const kept = [];
  for (const line of theirs) {
    if (line.trim() !== '' && union.includes(line)) continue;
    union.push(line);
    if (line.trim() !== '') kept.push(line);
  }
  return { lines: union, kept, note: '' };
}

function cmdUnion(file) {
  const original = readFile(file);
  const lines = original.split('\n');
  const blocks = parseConflicts(original);
  if (blocks.length === 0) {
    process.stderr.write(`union: ${file} 里没有冲突块，无需求解\n`);
    return 2;
  }

  const out = [];
  let cursor = 0;
  const merged = [];
  for (const block of blocks) {
    out.push(...lines.slice(cursor, block.start));
    const outStart = out.length;
    const ours = lines.slice(block.start + 1, block.mid);
    const theirs = lines.slice(block.mid + 1, block.end);
    const resolved = resolveSides(ours, theirs);
    if (resolved === null) {
      process.stderr.write(
        `union: 第 ${block.start + 1} 行的冲突块有一侧（或两侧都）仍是旧格式整段单行，且两侧互不包含，\n` +
          `  无法判断该保留哪一份 —— 旧单行里已经含有折行侧的全部文字，逐行并集会把这同一段\n` +
          `  文字写两遍。请人工合并；文件未被改动。\n`,
      );
      return 1;
    }
    out.push(...resolved.lines);
    cursor = block.end + 1;
    merged.push({
      line: block.start + 1,
      kept: resolved.kept,
      note: resolved.note,
      range: [outStart, out.length],
      sides: [flatten(ours.join(' ')), flatten(theirs.join(' '))],
    });
  }
  // 最后一个冲突块之后的正文（README 的其余小节）—— 漏掉这一句会把文件从最后一个冲突块
  // 处截断，而下面的校验只看登记段落，截断不会被发现。
  out.push(...lines.slice(cursor));

  // 机械求并集的前提：**一段登记段里只有一个冲突块**，也就是「两条分支都在段尾追加了子句」。
  // 一旦 git 在两侧共享的文字中间对齐，一个段落里就会出现两个以上的块，此时逐块拼接会把
  // A 的尾句直接接上 B 的后半句 —— 凑出一句语法通顺、语义错乱的话，而两侧的 #N 一个不少。
  // 这种形状无法机械判对，宁可拒绝也不写出错句子。
  const spans = findParagraphs(out);
  const owner = (range) => spans.findIndex((s) => range[0] >= s.start && range[1] <= s.end);
  const perParagraph = new Map();
  for (const m of merged) {
    const index = owner(m.range);
    if (index < 0) {
      process.stderr.write(`union: 第 ${m.line} 行的冲突块不在登记段落里，形状未知，请人工合并；文件未被改动。\n`);
      return 1;
    }
    perParagraph.set(index, (perParagraph.get(index) ?? 0) + 1);
  }
  for (const [index, count] of perParagraph) {
    if (count > 1) {
      process.stderr.write(
        `union: 第 ${spans[index].start + 1} 行起的登记段落里有 ${count} 个冲突块，无法机械求并集——\n` +
          `  一个段落只有一个块时才说明「两侧都在段尾追加」；多个块说明 git 在两侧共享的文字上\n` +
          `  对齐过，逐块拼接会把一方的尾句接到另一方的后半句。请人工合并；文件未被改动。\n`,
      );
      return 1;
    }
  }

  // 兜底断言：两侧文本都要成段留在结果里。上面的前提保证它成立，这里防止将来改坏。
  const flat = flatten(out.join('\n'));
  for (const m of merged) {
    if (m.sides.some((side) => side !== '' && !flat.includes(side))) {
      process.stderr.write(`union: 第 ${m.line} 行冲突块的一侧没能成段保留在结果里；文件未被改动。\n`);
      return 1;
    }
  }

  // 求并集只保证「两侧的行都在」，不保证宽度：冲突一侧可能来自尚未折行的旧版本，
  // 两边拼起来就会出现一条超长行。这里顺手折行，让 union 在两种输入下都一步到位
  //（折行只把空格换成换行，reflow 自带逐字节相等自检，不会再动内容）。
  const resolvedLines = replaceSpans(out, findParagraphs(out));
  const resolved = resolvedLines.join('\n');
  const problems = checkLines(resolvedLines);
  if (problems.length > 0) {
    process.stderr.write(`union: 并集并折行后仍有 ${problems.length} 行超过 ${MAX_LINE} 字符\n`);
    for (const p of problems) process.stderr.write(`  L${p.line} ${p.length} 字符\n`);
    return 1;
  }

  writeFileSync(file, resolved, 'utf8');
  process.stdout.write(`union: 解开 ${blocks.length} 个冲突块，零丢失校验通过\n`);
  for (const m of merged) {
    if (m.note !== '') process.stdout.write(`  L${m.line} ${m.note}\n`);
    if (m.kept.length === 0) continue;
    process.stdout.write(`  L${m.line} 补回 ${m.kept.length} 行：\n`);
    for (const line of m.kept) process.stdout.write(`    ${line.slice(0, 100)}\n`);
  }
  return 0;
}

function main(argv) {
  const [command, target] = argv;
  const file = target ? path.resolve(target) : DEFAULT_FILE;
  try {
    if (command === 'check') return cmdCheck(file);
    if (command === 'format') return cmdFormat(file);
    if (command === 'union') return cmdUnion(file);
  } catch (error) {
    process.stderr.write(`${command ?? 'usage'}: ${error.message}\n`);
    return 2;
  }
  process.stderr.write(
    '用法：node scripts/tests_readme_magnet.mjs <check|format|union> [tests/README.md]\n',
  );
  return 2;
}

// 既当 CLI 又当模块：tests/testsReadmeMagnet.test.js 直接 import 这几个函数来打门禁，
// 只有被当作入口脚本运行时才走 main。
const isEntryPoint =
  process.argv[1] !== undefined && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isEntryPoint) process.exit(main(process.argv.slice(2)));

export { MAX_LINE, WRAP_WIDTH, findParagraphs, parseConflicts, reflow, unwrap, wrap };
