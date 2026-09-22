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
- `subject`：必填，非空描述。校验只要求非空，中文、英文都可以；仓库习惯写英文、动词开头的祈使句，末尾不加句号。
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

PR 标题与提交信息同格式，`pr-title-check` 会在标题不合规时把检查标红（是否阻断合并取决于仓库的分支保护设置）：

```text
feat(sandbox): add auto install support     ✅
fix(rag): 修正召回为空时的分支处理            ✅  subject 允许中文
update stuff                                ❌  缺少 type
fix rag: 修正召回为空时的分支处理             ❌  type 被解析成「fix rag」，scope 必须写在括号里
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
| PR 描述 | 同上 | `scripts/check_pr_body.mjs` | 去掉 HTML 注释后，类型须勾选至少一项 `[x]`；变更概述、背景·问题、变更内容、日志·验证证据、测试情况五节非空；关联 issue 节须含 `#编号` 或写明「无关联 issue」及原因；截图节须含图片或写明「无需截图（无 UI 变更）」 |
| Issue 结构 | issue 打开 / 编辑 | `scripts/check_issue.mjs` | 标题以 `[BUG]` 或 `[FEATURE]` 开头；`[BUG]` 须有非空的「复现步骤」「日志」小节，`[FEATURE]` 须有非空的「验收标准」小节；正文完全没有小节标题时（手写正文）退回为关键字检查 |

判定细节：

- 去掉 HTML 注释后不足 3 个实质字符（汉字、字母、数字），或内容正好是 `无` / `暂无` / `N/A` / `TODO` / `待补充` / `-` / `_No response_` 等占位文本，都算「空」；长度按码点数，纯 emoji、纯符号凑不出内容。
- 注释消毒按「渲染后看不见的才算注释」判断（`scripts/lib/markdown_sanitize.mjs`，两个脚本共用）：行首的起始符是 HTML 块注释，整段删到结束符——块里的 `##` 行渲染时也不是标题，跟着一起删；行内的起始符只有在同一段落里闭合（中间没有空行、没有整行小节标题）时才当注释删，否则只拔掉标记本身，正文照常参与长度判据。因此正文里写到一次未配对的行内起始符，不会再吃掉中间的小节标题，注释掉的内容也不会被算成正文。返回值里不会残留注释起始符（`<` `!` `-` `-` 四个连续字符）；代码围栏内的注释标记同样会被删除——这是有意为之：围着围栏开例外就等于给这条不变式开口子（方向偏严，不会放松判据）。
- 占位文本会被「规范化」后再判定：先去掉标点与列表符号（`-`、`1.`、`*`），再看剩下的是不是全由占位词拼成。因此 `暂无。`、`- 无`、`1. 无`、`_No response_`，以及 `- 无` + `- 待补充` 这类多行占位，都算「空」；而 `无 UI 变更` 这类含真实内容的写法不会被误判。
- 关联 issue 节写「无关联 issue」时，**短语之外还要写原因**（至少 3 个非占位实质字符），例如「无关联 issue，原因：补齐仓库协作模板」；只写短语本身判红。「无关联**的** issue」这类自然写法同样接受。「原因」「理由」「因为」「是」这类标签与连接词不算实质字符，所以「无关联 issue，原因：无」同样判红。
- 小节标题按 `##`~`######` 层级解析，代码围栏（``` / ~~~）内的 `#` 行是粘贴进来的日志，不会被当成小节标题；`###` 子标题也不会截断所属的 `##` 小节。
- 三个脚本都在本地直接跑，参数是待校验内容的文件路径：

```bash
node scripts/check_title.mjs path/to/title.txt
node scripts/check_pr_body.mjs path/to/pr_body.md
node scripts/check_issue.mjs path/to/issue_dump.md
```

校验失败时脚本以非零状态码退出，并打印缺哪一节、为什么不合格。

三点边界：

- 三个 workflow 只负责在对应事件上运行校验并把检查标红；**是否阻断合并由仓库的分支保护 / required status check 决定**。现状（2026-09-22 实读 `gh api repos/baiye-banned/Agent_about_answer/branches/<分支>/protection`）：`main` 上「PR 标题规范校验」「PR 描述必填节校验」都在必需检查名单内（共 9 条，`strict=false`，不要求分支先跟上 base）；`develop` 上已设 8 条必需检查（同样是 `strict=false`），即 `main` 那 9 条**去掉「PR 描述必填节校验」**——该 job 在 Dependabot PR 上会被判 `skipped`，设成必需会把依赖 PR 永久卡死，理由与名单见 [BRANCHING.md](BRANCHING.md) §2。
- `issue-validator` 由 `issues` 事件触发，运行的是**默认分支**上的 workflow 与脚本，因此规则改动要等合入默认分支后才对所有 issue 生效；`pr-*` 两个校验由 `pull_request` 事件触发，运行的是 PR 合并结果里的 workflow，脚本则优先取 base 分支上的版本。
- 因为优先用 base 分支上的脚本，修改规则本身的 PR 会先被**旧脚本**校验一遍：放宽规则要等脚本合入 base 后才生效，加严规则则可能先把自己判红。base 分支上还没有脚本时（引入门禁的第一个 PR）回退用当前分支的脚本。
