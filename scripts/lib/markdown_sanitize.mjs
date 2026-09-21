// check_issue.mjs 与 check_pr_body.mjs 共用的正文消毒与字符计数。
//
// 消毒的不变式（issue #97）：
//   1. 返回值里不再出现注释起始符（`<` `!` `-` `-` 四个连续字符）；
//   2. 不删除「渲染后看得见」的内容，尤其是小节标题行。
//
// 单趟 `replace(/<!--[\s\S]*?-->/g, '')` 两条都会破：
//   - 删注释时，被删片段两侧的 `<!` 与 `--` 会重新拼出起始符，构造文本因此能把
//     「至少 3 个字符」的判据从 1 个可见字符骗过去；
//   - 正文里一个没配对的起始符会一路吃到很后面的结束符，把中间的小节标题整段删掉，
//     于是一份结构完整的正文被判成缺节。
//
// 判据是「渲染出来看不见的内容不算数」，所以先判断哪一段真的是注释：
//   1. 起始符在行首（本行前面只有空白）——CommonMark 的 HTML 块注释，可以跨空行，
//      整段删到结束符；块里的 `##` 行本来也不渲染成标题，跟着一起删不留痕迹；
//   2. 起始符在行内——只有它在同一个段落里闭合才算注释：中间出现空行或整行小节标题，
//      渲染器就会把这个 `<` 当普通文本显示、后面的内容也都看得见，此时只拔掉起始符
//      本身，正文与结束符都保留，长度判据也照常把它们算进去；
//   3. 找不到结束符，或删掉一段之后拼接处又拼出一个起始符（`x<!` + `--`、`<` + `!--`
//      之类），每走一步都检查输出尾部，拼出来就把两侧一起拔掉——不变式 1 是当场构造
//      出来的，不需要「反复替换到不动点」（那种写法在 `<!<!--` 反复出现的构造下是平方级）。
//
// 有意为之：代码围栏不做例外，围栏内的注释整体一样会被删除（issue #97 验收标准第 5 条
// 允许二选一，这里选「维持现状 + 写明理由」）。理由是渲染差异与不变式 1 冲突：围栏内
// 的标记其实是可见文本，但不变式 1 要求返回值里不能残留起始符，围着围栏开例外就等于给
// 这条不变式开口子。方向偏严——整节只有一段围栏注释时会被判空，不会反过来放松判据。
// 改这里之前先看这段注释与 tests/ciGateScripts.test.js 里的围栏用例。

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

// 行内起始符到结束符之间是否跨了段落：逐行走到结束符所在的那行，遇到空行或整行小节标题
// 就说明这段不在同一个段落里，渲染器不会把它当注释（`<` 会原样显示，后面的内容都看得见）。
function crossesParagraphBreak(text, start, end) {
  let lineEnd = text.indexOf('\n', start);
  while (lineEnd !== -1 && lineEnd < end) {
    const next = text.indexOf('\n', lineEnd + 1);
    const line = text.slice(lineEnd + 1, next === -1 ? text.length : next);
    if (line.trim().length === 0 || HEADING_LINE.test(line)) return true;
    lineEnd = next;
  }
  return false;
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

// 渲染时真的会当注释的那一段，返回可以整段删掉的结束下标（end 是结束符的位置）；
// 否则返回 null，调用方只拔掉起始符本身，后面的正文与结束符都保留。
function removableCommentEnd(text, start, end) {
  if (!opensAtLineStart(text, start) && crossesParagraphBreak(text, start, end)) return null;
  return end + COMMENT_END.length;
}

// 消毒后的正文：不再含注释起始符，也不再删掉渲染后看得见的正文。
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
