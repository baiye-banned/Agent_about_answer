#!/usr/bin/env node
// 校验 issue 是否符合 .github/ISSUE_TEMPLATE 的结构要求。
// 用法：node scripts/check_issue.mjs <issue 文件路径>
// 文件第一行非空内容视为标题，其余为正文。
// 通过退出码 0；不合规退出码 1；参数/IO 错误退出码 2。

import { readFileSync } from 'node:fs';

// 小节正文去掉注释与空白后至少要有这么多字符，避免用「无」占位。
const MIN_LENGTH = 3;
const EMPTY_VALUES = ['无', '暂无', 'n/a', 'na', 'todo', '待补充', '占位', '-', 'ok'];

const RULES = {
  BUG: {
    prefix: '[BUG]',
    keywords: [
      { key: '复现', label: '复现步骤' },
      { key: '日志', label: '日志' },
    ],
  },
  FEATURE: {
    prefix: '[FEATURE]',
    keywords: [{ key: '验收', label: '验收标准' }],
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

function parseSections(markdown) {
  const lines = markdown.split(/\r?\n/);
  const headings = [];
  lines.forEach((line, index) => {
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

function isFilled(content) {
  const cleaned = cleanContent(content);
  if (cleaned.length < MIN_LENGTH) return false;
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

const problems = [];
for (const keyword of kind.keywords) {
  const section = sections.find((item) => item.normalized.includes(keyword.key));
  if (section) {
    if (!isFilled(section.content)) {
      problems.push(`「${keyword.label}」小节为空或只有占位内容。`);
    }
    continue;
  }
  // 没有对应小节标题时，退回为全文关键字检查（兼容手写正文）。
  if (!body.includes(keyword.key)) {
    problems.push(`正文缺少「${keyword.label}」相关内容。`);
  }
}

if (problems.length > 0) {
  const found = sections.map((section) => section.title).join(' / ') || '（无小节标题）';
  problems.push(`当前识别到的小节标题：${found}`);
  fail(problems);
}

console.log(`[OK] issue 校验通过：${title}`);
