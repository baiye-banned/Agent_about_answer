#!/usr/bin/env node
// 校验 PR 描述是否按 .github/pull_request_template.md 填写完整。
// 用法：node scripts/check_pr_body.mjs <PR 描述文件路径>
// 通过退出码 0；缺节、占位或截图节不合格退出码 1；参数/IO 错误退出码 2。

import { readFileSync } from 'node:fs';

// 去掉 HTML 注释与空白后，小节正文至少要有这么多字符，避免「无」「-」蒙混过关。
const MIN_LENGTH = 3;
const EMPTY_VALUES = ['无', '暂无', 'n/a', 'na', 'todo', '待补充', '占位', '-', 'ok'];

const REQUIRED_SECTIONS = [
  { key: '类型', label: '类型', kind: 'checkbox' },
  { key: '变更概述', label: '变更概述', kind: 'text' },
  { key: '背景问题', label: '背景·问题', kind: 'text' },
  { key: '关联issue', label: '关联 issue', kind: 'text' },
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

// 去掉 HTML 注释，避免用 <!-- --> 占位当内容。
function stripComments(markdown) {
  return markdown.replace(/<!--[\s\S]*?-->/g, '');
}

const normalizeTitle = (text) =>
  text
    .toLowerCase()
    .replace(/[`*_#]/g, '')
    .replace(/[\s·・/、|｜,，:：;；\-—–]+/g, '');

// 按标题层级切分小节；同名更高层级标题才算下一节，允许小节内部使用 ### 子标题。
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

function findSection(sections, key) {
  return (
    sections.find((section) => section.normalized === key) ||
    sections.find((section) => section.normalized.includes(key)) ||
    null
  );
}

// 清掉空勾选项、代码围栏、残留标题后，判断正文是否算「有实际内容」。
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

const body = stripComments(raw.replace(/^﻿/, ''));

if (body.trim().length === 0) {
  fail(['PR 描述为空，未按模板填写。']);
}

const sections = parseSections(body);
const problems = [];

for (const required of REQUIRED_SECTIONS) {
  const section = findSection(sections, required.key);
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
  if (!isFilled(section.content)) {
    problems.push(`「${required.label}」节为空或只有占位内容。`);
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
