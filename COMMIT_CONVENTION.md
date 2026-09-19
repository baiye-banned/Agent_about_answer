# 提交与 PR 规范

本仓库采用 [Conventional Commits](https://www.conventionalcommits.org/)，PR 标题与提交信息使用同一套规则。

## 1. 格式

```text
<type>(<scope>)!: <subject>

<body>

<footer>
```

- `type`：必填，取值见下表。
- `scope`：选填，建议取值见第 3 节。
- `!`：选填，表示破坏性变更（Breaking Change）。
- `subject`：必填，英文，动词开头的祈使句，末尾不加句号。
- 冒号后必须有一个空格；`type` 与 `scope` 均用小写。

## 2. type 取值

| type | 用途 | 示例 |
| --- | --- | --- |
| `feat` | 新增功能 | `feat(rag): add citation snippet to chat response` |
| `fix` | 修复缺陷 | `fix(rag): handle empty recall result branch` |
| `perf` | 性能优化 | `perf(backend): batch milvus insert to cut upload time` |
| `refactor` | 重构（不改变外部行为） | `refactor(backend): extract upload rollback into service` |
| `docs` | 文档变更 | `docs(readme): document local milvus lite setup` |
| `style` | 格式调整（不影响逻辑） | `style(frontend): normalize tailwind class order` |
| `test` | 测试相关 | `test(backend): cover chunking overlap boundary` |
| `build` | 构建与依赖 | `build(deps): pin vite to 5.4` |
| `ci` | CI 与流程配置 | `ci(templates): add pr body and title gates` |
| `chore` | 杂项，不修改 src 与测试 | `chore(release): bump version to 1.2.0` |
| `revert` | 回滚某次提交 | `revert: feat(rag): add citation snippet` |

## 3. scope 建议

`backend` / `rag` / `frontend` / `ci` / `docs`

scope 是**建议**而非强制：跨模块或确实没有合适 scope 时可以省略，但显式写上便于按模块筛选改动。
前端相关的 `frontend` 覆盖 `src/`、构建配置与样式；检索与模型链路统一用 `rag`。

## 4. body 与 footer

- body 用英文分行说明「为什么改」与关键实现取舍，与 subject 之间空一行。
- footer 用于关联 issue：`Closes #12`（合并后自动关闭）、`Refs #34`。
- 破坏性变更必须在 footer 写明，例如：

```text
feat(backend)!: drop legacy /api/chat endpoint

The SSE endpoint /api/chat/stream replaces it.

BREAKING CHANGE: /api/chat is removed, clients must migrate.
Closes #42
```

## 5. PR 标题

PR 标题与提交信息同格式，`pr-title-check` 会强制校验：

```text
feat(sandbox): add auto install support     ✅
fix(rag): 修正召回为空时的分支处理            ✅  subject 允许中文
update stuff                                ❌  缺少 type
fix rag: 修正召回为空时的分支处理             ❌  缺少括号
Fix(rag): 修正召回为空时的分支处理            ❌  type 必须小写
```

若 PR 只有一个提交，标题可直接沿用该提交；多提交 PR 用一句能概括整体改动的标题，并在标题末尾注明关联 issue：`ci(templates): 协作模板与校验门禁 (#2)`。

## 6. 合并方式

| 场景 | 方式 | 原因 |
| --- | --- | --- |
| 日常 PR（`feature/*`、`fix/*`、`ci/*` → `develop`） | **Squash merge** | develop 上每个 PR 只留一条符合规范的提交，历史可读、便于回滚 |
| 发布 PR（`develop` → `main`） | **Merge commit** | 保留 develop 上的提交分组与发布节点，便于回溯某次发布包含哪些改动 |

Squash 时 GitHub 会用 PR 标题作为默认提交标题，因此 PR 标题必须符合第 5 节格式。

## 7. 自动校验

| 校验 | 触发 | 脚本 | 规则 |
| --- | --- | --- | --- |
| PR 标题 | PR 打开 / 编辑 / 重新打开 / 推送新提交 | `scripts/check_title.mjs` | 匹配 `^(feat\|fix\|perf\|refactor\|docs\|style\|test\|build\|ci\|chore\|revert)(\(.+\))?!?: <非空描述>` |
| PR 描述 | 同上 | `scripts/check_pr_body.mjs` | 去掉 HTML 注释后，类型须勾选至少一项 `[x]`；变更概述、背景·问题、关联 issue、变更内容、日志·验证证据、测试情况六节非空；截图节须含图片或写明「无需截图（无 UI 变更）」 |
| Issue 结构 | issue 打开 / 编辑 | `scripts/check_issue.mjs` | 标题以 `[BUG]` 或 `[FEATURE]` 开头；`[BUG]` 需含「复现」「日志」，`[FEATURE]` 需含「验收」，且对应小节非空 |

三个脚本都可以在本地直接跑，参数是待校验内容的文件路径：

```bash
node scripts/check_title.mjs path/to/title.txt
node scripts/check_pr_body.mjs path/to/pr_body.md
node scripts/check_issue.mjs path/to/issue_dump.md
```

校验失败时脚本以非零状态码退出，并打印缺哪一节、为什么不合格。
