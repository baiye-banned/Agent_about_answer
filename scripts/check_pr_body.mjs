#!/usr/bin/env node
// 校验 PR 描述是否按 .github/pull_request_template.md 填写完整。
// 用法：node scripts/check_pr_body.mjs <PR 描述文件路径>
// 通过退出码 0；缺节、占位或截图节不合格退出码 1；参数/IO 错误退出码 2。

import { readFileSync } from 'node:fs';

import { countSubstantive, stripComments } from './lib/markdown_sanitize.mjs';

// 去掉 HTML 注释与空白后，小节正文至少要有这么多个实质字符（汉字/字母/数字），避免「无」「-」蒙混过关。
const MIN_LENGTH = 3;
// _no response_ 是 GitHub issue 表单对空字段自动写入的占位文本，必须当作空。
const EMPTY_VALUES = [
  '无',
  '暂无',
  'n/a',
  'na',
  'todo',
  '待补充',
  '占位',
  '-',
  'ok',
  '_no response_',
];
// 占位文本可以带标点、写成多行或列表项（`暂无。`、`- 无`、`TODO（待补充）`），
// 比对前统一去掉标点与空白，否则多加一个句号就能绕过去。长词优先，避免被短词先切走。
const PUNCTUATION = /[\s　。．.!！?？~～、,，;；:：\-—_*`#（）()【】\[\]「」『』“”‘’"'…·|｜/\\]+/g;
const stripPunctuation = (text) => text.replace(PUNCTUATION, '').toLowerCase();
const PLACEHOLDER_WORDS = EMPTY_VALUES.map(stripPunctuation)
  .filter((word) => word.length > 0)
  .sort((a, b) => b.length - a.length);

// 反复抠掉给定的词，直到没有可抠的为止；长词优先，避免被短词先切走。
function removeWords(text, words) {
  let rest = text;
  let changed = true;
  while (changed) {
    changed = false;
    for (const word of words) {
      if (rest.includes(word)) {
        rest = rest.split(word).join('');
        changed = true;
      }
    }
  }
  return rest;
}

// 整段内容由占位词拼成（`- 无`、`无 待补充`、`TODO（待补充）`）就算空：
// 逐个抠掉占位词后什么都不剩才算占位，因此「无 UI 变更」这类真实内容不会被误杀。
function isOnlyPlaceholders(normalized) {
  return removeWords(normalized, PLACEHOLDER_WORDS).length === 0;
}

const REQUIRED_SECTIONS = [
  { key: '类型', label: '类型', kind: 'checkbox' },
  { key: '变更概述', label: '变更概述', kind: 'text' },
  { key: '背景问题', label: '背景·问题', kind: 'text', aliases: ['背景与问题'] },
  { key: '关联issue', label: '关联 issue', kind: 'issueLink' },
  { key: '变更内容', label: '变更内容', kind: 'text' },
  { key: '日志验证证据', label: '日志·验证证据', kind: 'text' },
  { key: '测试情况', label: '测试情况', kind: 'text' },
];

const SCREENSHOT_KEY = '截图';
const SCREENSHOT_LABEL = '截图';

const IMAGE_PATTERNS = [
  /!\[[^\]]*\]\([^)]+\)/, // markdown 图片
  /<img\s[^>]*src=/i, // 内联 HTML 图片
  /https?:\/\/\S+\.(png|jpe?g|gif|webp|svg|bmp)\b/i, // 图片直链
  /https?:\/\/github\.com\/user-attachments\/assets\/\S+/i, // GitHub 附件
  /https?:\/\/\S*(user-images|private-user-images)\S*/i, // GitHub 图床
];

// COMMIT_CONVENTION.md「关联 issue 节须含 #编号 或写明「无关联 issue」及原因」：
// 只写短语本身不算，短语之外还要剩下至少 REASON_MIN_LENGTH 个非占位实质字符（即原因）。
// 容忍「无关联的 issue」这类自然写法。
const NO_ISSUE_PHRASE = /(?:无|没有|不涉及|无需|未有|不存在)\s*关联\s*(?:的)?\s*issue/gi;
const REASON_MIN_LENGTH = 3;
// 「原因」「理由」这类标签与「因为」「是」这类连接词本身不是原因，占位词更不是。
// 长度与实质判定都在抠掉这两类词之后的剩余串上做，否则「原因：无」＝标签 2 字 + 占位词就能凑够 3 字阈值。
const REASON_FILLER_WORDS = ['原因', '理由', '因为', '由于', '是'];

function declaresNoIssueWithReason(content) {
  const stripped = content.replace(NO_ISSUE_PHRASE, ' ');
  if (stripped === content) return false;
  const reason = removeWords(
    removeWords(stripPunctuation(stripped), PLACEHOLDER_WORDS),
    REASON_FILLER_WORDS
  );
  if (countSubstantive(reason) === 0) return false;
  return [...reason].length >= REASON_MIN_LENGTH;
}

function fail(problems) {
  console.error('[FAIL] PR 描述校验未通过');
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error('');
  console.error('  请按 .github/pull_request_template.md 补齐以上小节后重新提交。');
  process.exit(1);
}

function usage(message) {
  console.error(`[ERROR] ${message}`);
  console.error('用法：node scripts/check_pr_body.mjs <PR 描述文件路径>');
  process.exit(2);
}

// 注释消毒与字符计数都在 scripts/lib/markdown_sanitize.mjs：消毒只删「渲染时真的看不见」
// 的注释，其余起始符只拔掉标记本身，长度也只数实质字符——两个方向都不会被构造文本
// 糊弄（issue #97）。
const normalizeTitle = (text) =>
  text
    .toLowerCase()
    .replace(/[`*_#]/g, '')
    .replace(/[\s·・/、|｜,，:：;；\-—–]+/g, '');

// 按标题层级切分小节；同名更高层级标题才算下一节，允许小节内部使用 ### 子标题。
// 代码围栏内的 # 行是粘贴进来的内容（日志、片段），不是小节标题，必须跳过。
function parseSections(markdown) {
  const lines = markdown.split(/\r?\n/);
  const headings = [];
  let fence = null;
  lines.forEach((line, index) => {
    const fenceMark = line.match(/^\s*(```|~~~)/);
    if (fenceMark) {
      if (fence === null) fence = fenceMark[1];
      else if (fence === fenceMark[1]) fence = null;
      return;
    }
    if (fence !== null) return;
    const matched = line.match(/^(#{2,6})\s+(.*\S)\s*$/);
    if (matched) {
      headings.push({ level: matched[1].length, title: matched[2], line: index });
    }
  });

  return headings.map((heading, index) => {
    let end = lines.length;
    for (let next = index + 1; next < headings.length; next += 1) {
      if (headings[next].level <= heading.level) {
        end = headings[next].line;
        break;
      }
    }
    return {
      title: heading.title,
      normalized: normalizeTitle(heading.title),
      content: lines.slice(heading.line + 1, end).join('\n').trim(),
    };
  });
}

// 小节标题匹配：先按 key 与别名精确匹配，再退化为包含匹配（容忍「背景与问题」这类写法）。
function findSection(sections, required) {
  const keys =
    typeof required === 'string' ? [required] : [required.key, ...(required.aliases ?? [])];
  for (const key of keys) {
    const exact = sections.find((section) => section.normalized === key);
    if (exact) return exact;
  }
  for (const key of keys) {
    const partial = sections.find((section) => section.normalized.includes(key));
    if (partial) return partial;
  }
  return null;
}

// 清掉空勾选项、残留标题与围栏标记后，判断正文是否算「有实际内容」。
// 围栏内的行原样保留：日志块即使整段以 # 开头也是真实内容。
function cleanContent(content) {
  const kept = [];
  let fence = null;
  for (const line of content.split(/\r?\n/)) {
    const fenceMark = line.match(/^\s*(```|~~~)/);
    if (fenceMark) {
      if (fence === null) fence = fenceMark[1];
      else if (fence === fenceMark[1]) fence = null;
      continue;
    }
    if (fence === null) {
      if (/^\s*#{1,6}\s+.*$/.test(line)) continue;
      if (/^\s*[-*]\s*\[[ xX]\]\s*$/.test(line)) continue;
    }
    kept.push(line);
  }
  return kept.join(' ').replace(/\s+/g, ' ').trim();
}

// 占位内容即使写成列表项（`- 无`、`1. 无`）、多行（`- 无` + `- 待补充`）或带标点（`暂无。`）也算空；
// 长度只数实质字符（汉字/字母/数字），纯符号、emoji、注释残留都凑不出长度。
function isFilled(content) {
  const cleaned = cleanContent(content)
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (countSubstantive(cleaned) < MIN_LENGTH) return false;
  const normalized = stripPunctuation(cleaned);
  if (normalized.length === 0) return false;
  return !isOnlyPlaceholders(normalized);
}

const file = process.argv[2];
if (!file) usage('缺少参数。');

let raw;
try {
  raw = readFileSync(file, 'utf8');
} catch (err) {
  usage(`无法读取文件 ${file}：${err.message}`);
}

const body = stripComments(raw.replace(/^﻿/, ''));

if (body.trim().length === 0) {
  fail(['PR 描述为空，未按模板填写。']);
}

const sections = parseSections(body);
const problems = [];

for (const required of REQUIRED_SECTIONS) {
  const section = findSection(sections, required);
  if (!section) {
    problems.push(`缺少「${required.label}」节。`);
    continue;
  }
  if (required.kind === 'checkbox') {
    if (!/^\s*[-*]\s*\[[xX]\]/m.test(section.content)) {
      problems.push(`「${required.label}」节未勾选任何一项（需要至少一个 [x]）。`);
    }
    continue;
  }
  if (required.kind === 'issueLink') {
    // 模板预填的裸 "Closes #" 不算关联：必须有 issue 编号，或写明无关联及原因。
    const hasIssueRef = /#\d+/.test(section.content);
    if (!hasIssueRef && !declaresNoIssueWithReason(section.content)) {
      problems.push(
        `「${required.label}」节既没有「#编号」形式的 issue 引用，也没有写明「无关联 issue」及原因（原因至少 ${REASON_MIN_LENGTH} 个字符）。`
      );
    }
    continue;
  }
  if (!isFilled(section.content)) {
    problems.push(
      `「${required.label}」节内容过短或只有占位内容（至少 ${MIN_LENGTH} 个汉字/字母/数字，且不能是「无」这类占位文本）。`
    );
  }
}

const screenshot = findSection(sections, SCREENSHOT_KEY);
if (!screenshot) {
  problems.push(`缺少「${SCREENSHOT_LABEL}」节。`);
} else {
  const hasImage = IMAGE_PATTERNS.some((pattern) => pattern.test(screenshot.content));
  const declaresNoUi = /无需截图/.test(screenshot.content) && /(ui|界面)/i.test(screenshot.content);
  if (!hasImage && !declaresNoUi) {
    problems.push(
      `「${SCREENSHOT_LABEL}」节既没有图片，也没有写明「无需截图（无 UI 变更）」。`
    );
  }
}

if (problems.length > 0) {
  const found = sections.map((section) => section.title).join(' / ') || '（无小节标题）';
  problems.push(`当前识别到的小节标题：${found}`);
  fail(problems);
}

console.log(`[OK] PR 描述校验通过，${REQUIRED_SECTIONS.length + 1} 个必填节均已填写。`);
