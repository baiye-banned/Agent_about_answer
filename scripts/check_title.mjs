#!/usr/bin/env node
// 校验 PR 标题是否符合 Conventional Commits 规范（见 COMMIT_CONVENTION.md）。
// 用法：node scripts/check_title.mjs <标题文件路径>
// 文件的第一行非空内容视为标题；通过退出码 0，不通过退出码 1。

import { readFileSync } from 'node:fs';

const TYPES = [
  'feat',
  'fix',
  'perf',
  'refactor',
  'docs',
  'style',
  'test',
  'build',
  'ci',
  'chore',
  'revert',
];

// type(scope)!: subject —— scope 与 ! 可选，冒号后必须有一个空格和实际描述。
const TITLE_PATTERN = new RegExp(`^(${TYPES.join('|')})(\\((.+)\\))?(!)?: (.+)$`);

function fail(lines) {
  console.error('[FAIL] PR 标题不符合 Conventional Commits 规范');
  for (const line of lines) console.error(`  ${line}`);
  console.error('');
  console.error(`  期望格式：<type>(<scope>)!: <描述>，type 取值：${TYPES.join(' / ')}`);
  console.error('  示例：fix(rag): 修正召回为空时的分支处理');
  process.exit(1);
}

const file = process.argv[2];
if (!file) fail(['缺少参数。用法：node scripts/check_title.mjs <标题文件路径>']);

let raw;
try {
  raw = readFileSync(file, 'utf8');
} catch (err) {
  fail([`无法读取文件 ${file}：${err.message}`]);
}

const title = raw
  .replace(/^﻿/, '')
  .split(/\r?\n/)
  .map((line) => line.trim())
  .find((line) => line.length > 0);

if (!title) fail(['标题为空。']);

const matched = title.match(TITLE_PATTERN);
if (!matched) {
  const reasons = [];
  const head = title.split(':')[0] || title;
  const type = head.replace(/[(!].*$/, '');
  if (!TYPES.includes(type)) {
    reasons.push(
      TYPES.includes(type.toLowerCase())
        ? `type「${type}」必须小写。`
        : `type「${type}」不在允许列表内。`
    );
  }
  if (!/^[^:]+:\s/.test(title)) {
    reasons.push('缺少「: 」（冒号加一个空格）。');
  } else if (title.replace(/^[^:]+:\s*/, '').trim().length === 0) {
    reasons.push('冒号后缺少描述。');
  }
  if (reasons.length === 0) reasons.push('标题格式不匹配。');
  fail([`实际标题：${title}`, ...reasons]);
}

const [, , , scope] = matched;
if (matched[2] && (!scope || scope.trim().length === 0)) {
  fail([`实际标题：${title}`, 'scope 括号内不能为空。']);
}

console.log(`[OK] 标题符合规范：${title}`);
