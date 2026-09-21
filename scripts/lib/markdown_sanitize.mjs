// check_issue.mjs 与 check_pr_body.mjs 共用的正文消毒与字符计数。
//
// 消毒的不变式（issue #97）：
//   1. 返回值里不再出现注释起始符（`<` `!` `-` `-` 四个连续字符）；
//   2. 小节标题行（`/^(#{2,6})\s+(.*\S)\s*$/`，与两个脚本的 parseSections 同口径）整行不删。
// 两条只在「标题行自己就含一个起始符」时冲突（`## 小节 <!--`）：此时以第 1 条为准，
// 只拔掉标题行里的标记本身，标题行仍然留着。
//
// 单趟 `replace(/<!--[\s\S]*?-->/g, '')` 两条都会破：
//   - 删注释时，被删片段两侧的 `<!` 与 `--` 会重新拼出起始符，构造文本因此能把
//     「至少 3 个字符」的判据从 1 个可见字符骗过去；
//   - 正文里一个没配对的起始符会一路吃到很后面的结束符，把中间的小节标题整段删掉，
//     于是一份结构完整的正文被判成缺节。
//
// 这里改成边走边扫的单趟扫描，遇到起始符就跳过，跳过后立刻修复拼接处：
//   1. 只有形态合法的注释才整段删除：起始符在行首（HTML 块形态，允许跨行到结束符），
//      或起始符与结束符在同一行（行内形态，对应 CommonMark 的行内 HTML）；
//   2. 形态不合法、或删掉它会吃到整行小节标题的，只拔掉起始符本身，不牵连后面的正文
//      （结束符单独出现无害，留着）；
//   3. 删掉一段之后，拼接处可能又拼出一个起始符（`x<!` + `--`、`<` + `!--` 之类），
//      所以每走一步都检查输出尾部，拼出来就把两侧一起拔掉。不变式因此是构造出来的，
//      不需要「反复替换到不动点」——那种写法在 `<!<!--` 反复出现的构造下是平方级。
//
// 有意为之：代码围栏内的注释标记同样会被删除（issue #97 验收标准第 5 条允许二选一）。
// 围栏内粘贴的标记在渲染时其实是可见文本，删掉它会让「整节只有一段围栏注释」被判成
// 空——方向偏严，不会放松判据。之所以不做围栏例外：不变式 1 要求返回值里不能残留起始符，
// 围着围栏开例外就等于给这条不变式开口子。改这里之前先看这条注释与
// tests/ciGateScripts.test.js 里的围栏用例。

const COMMENT_START = '<!--';
const COMMENT_END = '-->';
// 行首 `##`~`######` + 空白 + 非空标题，与 parseSections 的标题口径完全一致。
const HEADING_LINE = /^(#{2,6})\s+(.*\S)\s*$/;
// 实质字符：汉字、字母、数字。emoji 与纯符号不算，避免「🐛✨📝」凑够长度。
const SUBSTANTIVE = /[\p{L}\p{N}]/u;

// 起始符所在行的前面只有空白——CommonMark 的 HTML 块形态，允许跨行到结束符。
// 只往前看本行已经过去的空白，不切整段前缀：每个起始符各看各的空白，合起来仍是线性。
function opensAtLineStart(text, index) {
  for (let at = index - 1; at >= 0; at -= 1) {
    const char = text[at];
    if (char === ' ' || char === '\t') continue;
    return char === '\n';
  }
  return true;
}

// 完整落在 [start, stop) 里的行是否含小节标题。被区间两端截断的行不算：
// 那两行的行首/行尾还在区间外，删除区间不会把整行标题带走。
function containsHeadingLine(text, start, stop, atLineStart) {
  const lines = text.slice(start, stop).split('\n');
  const firstWhole = atLineStart ? 0 : 1;
  const endsAtLineEnd = stop >= text.length || text[stop] === '\n';
  const whole = lines.slice(firstWhole, endsAtLineEnd ? lines.length : lines.length - 1);
  return whole.some((line) => HEADING_LINE.test(line));
}

// 拼接处重新拼出起始符时，输出尾部需要拔掉的长度：尾部是起始符的前缀
// （`<` / `<!` / `<!-`，越长越优先），且后面的文本正好接得上剩下的部分。
function junctionOverlap(out, text, index) {
  for (let length = COMMENT_START.length - 1; length >= 1; length -= 1) {
    if (out.length < length) continue;
    if (out.slice(out.length - length).join('') !== COMMENT_START.slice(0, length)) continue;
    if (text.startsWith(COMMENT_START.slice(length), index)) return length;
  }
  return 0;
}

// 形态合法、且不跨小节标题的注释，返回可以整段删掉的结束下标（end 是结束符的位置）；
// 否则返回 null，调用方只拔掉起始符本身，后面的正文与结束符都保留。
function removableCommentEnd(text, start, end) {
  const stop = end + COMMENT_END.length;
  const atLineStart = opensAtLineStart(text, start);
  const newline = text.indexOf('\n', start);
  const sameLine = newline === -1 || newline >= end;
  if (!atLineStart && !sameLine) return null;
  if (containsHeadingLine(text, start, stop, atLineStart)) return null;
  return stop;
}

// 消毒后的正文：不再含注释起始符，也不再被删掉小节标题行。
// 单趟扫描：每个位置只往前走，拼接处当场修复，不需要回头重扫。
export function stripComments(markdown) {
  const out = [];
  let index = 0;
  // 结束符的位置只会往后走：起始符是从左到右扫的，它需要的「后面第一个结束符」
  // 也只会越来越靠后，所以游标单调前移即可，不必每个起始符都从头 indexOf 一遍。
  let nextEnd = markdown.indexOf(COMMENT_END);
  while (index < markdown.length) {
    const overlap = junctionOverlap(out, markdown, index);
    if (overlap > 0) {
      out.length -= overlap;
      index += COMMENT_START.length - overlap;
      continue;
    }
    if (markdown.startsWith(COMMENT_START, index)) {
      const afterStart = index + COMMENT_START.length;
      while (nextEnd !== -1 && nextEnd < afterStart) {
        nextEnd = markdown.indexOf(COMMENT_END, nextEnd + 1);
      }
      const stop = nextEnd === -1 ? null : removableCommentEnd(markdown, index, nextEnd);
      index = stop ?? afterStart;
      continue;
    }
    out.push(markdown[index]);
    index += 1;
  }
  return out.join('');
}

// 数「实质字符」的个数（按码点，避免 emoji 的代理对凑长度）。
// 字符数判据用它而不是用整串长度：注释残留、纯符号、emoji 都数不出字符，
// 判据因此不会被构造文本糊弄（issue #97 后果二）。
export function countSubstantive(text) {
  let count = 0;
  for (const char of text) {
    if (SUBSTANTIVE.test(char)) count += 1;
  }
  return count;
}
