#!/usr/bin/env node
// 校验 issue 是否符合 .github/ISSUE_TEMPLATE 的结构要求。
// 用法：node scripts/check_issue.mjs <issue 文件路径>
// 文件第一行非空内容视为标题，其余为正文。
// 通过退出码 0；不合规退出码 1；参数/IO 错误退出码 2。

import { readFileSync } from 'node:fs';

// 小节正文去掉注释与空白后至少要有这么多字符，避免用「无」占位。
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

const RULES = {
  BUG: {
    prefix: '[BUG]',
    keywords: [
      { section: '复现步骤', word: '复现' },
      { section: '日志', word: '日志' },
    ],
  },
  FEATURE: {
    prefix: '[FEATURE]',
    keywords: [{ section: '验收标准', word: '验收' }],
  },
};

function fail(problems) {
  console.error('[FAIL] issue 结构校验未通过');
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error('');
  console.error('  请通过 .github/ISSUE_TEMPLATE 中的模板新建 issue，并补齐全部必填项。');
  process.exit(1);
}

function usage(message) {
  console.error(`[ERROR] ${message}`);
  console.error('用法：node scripts/check_issue.mjs <issue 文件路径>');
  process.exit(2);
}

function stripComments(markdown) {
  return markdown.replace(/<!--[\s\S]*?-->/g, '');
}

const normalizeTitle = (text) =>
  text
    .toLowerCase()
    .replace(/[`*_#]/g, '')
    .replace(/[\s·・/、|｜,，:：;；\-—–]+/g, '');

// 代码围栏内的 # 行是粘贴进来的日志内容，不是小节标题，必须跳过。
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

function cleanContent(content) {
  return content
    .replace(/^\s*#{1,6}\s+.*$/gm, '')
    .replace(/^\s*```.*$/gm, '')
    .replace(/^\s*[-*]\s*\[[ xX]\]\s*$/gm, '')
    .replace(/\s+/g, ' ')
    .trim();
}

// 占位内容即使写成列表项（`- 无`、`1. 无`）也算空；
// 长度按码点算，避免两个字符的 emoji 凑够 UTF-16 长度蒙混过关。
function isFilled(content) {
  const cleaned = cleanContent(content)
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if ([...cleaned].length < MIN_LENGTH) return false;
  return !EMPTY_VALUES.includes(cleaned.toLowerCase());
}

const file = process.argv[2];
if (!file) usage('缺少参数。');

let raw;
try {
  raw = readFileSync(file, 'utf8');
} catch (err) {
  usage(`无法读取文件 ${file}：${err.message}`);
}

const lines = raw.replace(/^﻿/, '').split(/\r?\n/);
const titleIndex = lines.findIndex((line) => line.trim().length > 0);

if (titleIndex === -1) {
  fail(['issue 内容为空。']);
}

const title = lines[titleIndex].trim();
const body = stripComments(lines.slice(titleIndex + 1).join('\n'));
const sections = parseSections(body);

const kind = Object.values(RULES).find((rule) => title.startsWith(rule.prefix));
if (!kind) {
  fail([
    `标题「${title}」未以 ${Object.values(RULES)
      .map((rule) => rule.prefix)
      .join(' 或 ')} 开头。`,
    '请通过仓库的 issue 模板新建，模板会自动带上标题前缀。',
  ]);
}

// 去掉标题行后的正文，供手写正文的关键字兜底使用：
// 只看正文内容，避免「复现频率」这类同级标题把小节校验蒙混过去。
const prose = body
  .split(/\r?\n/)
  .filter((line) => !/^\s*#{1,6}\s/.test(line))
  .join('\n');

const problems = [];
for (const keyword of kind.keywords) {
  const key = normalizeTitle(keyword.section);
  const section =
    sections.find((item) => item.normalized === key) ||
    sections.find((item) => item.normalized.includes(key));
  if (section) {
    if (!isFilled(section.content)) {
      problems.push(
        `「${keyword.section}」小节内容过短或只有占位内容（至少 ${MIN_LENGTH} 个字符，且不能是「无」这类占位文本）。`
      );
    }
    continue;
  }
  // 正文完全没有小节标题时（手写正文），退回关键字检查；
  // 已经用了小节结构，就必须带上模板里的必填小节，否则结构性缺失会被关键字蒙混过关。
  if (sections.length === 0 && prose.includes(keyword.word)) continue;
  problems.push(`正文缺少「${keyword.section}」小节。`);
}

if (problems.length > 0) {
  const found = sections.map((section) => section.title).join(' / ') || '（无小节标题）';
  problems.push(`当前识别到的小节标题：${found}`);
  fail(problems);
}

console.log(`[OK] issue 校验通过：${title}`);
