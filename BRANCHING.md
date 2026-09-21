# 分支模型与发布流程

本文是仓库的分支与发布约定。**日常改动一律先进 `develop`，`main` 只承担"对外可见的发布点"这一个职责**。所有落地方式都通过 PR，不直接往 `main` / `develop` 推提交。

## 1. 分支模型

| 分支 | 角色 | 谁能合入 | 合并方式 |
| --- | --- | --- | --- |
| `main` | 发布分支。每个提交都是一个已对外发布的版本，随时可以被 tag / 部署。 | 只接受来自 `develop` 的发布 PR，以及紧急热修复 PR | **merge commit**（保留发布分支上的提交分组） |
| `develop` | 集成分支，同时也是仓库的**默认分支**（新 PR 默认指向它）。日常 PR 都提到这里。 | 所有短分支的 PR | **squash**（每个 PR 压成一条提交） |
| `<type>/<topic>-<issue号>` | 短分支，从 `develop` 切出，用完即删。 | — | — |

短分支的类型前缀与 Conventional Commits 的类型保持一致：

- `feat/`：新功能
- `fix/`：缺陷修复
- `docs/`：文档
- `ci/`：CI / 工作流 / 门禁
- `test/`：测试
- `chore/`：构建、依赖、杂项（`chore/release-v<version>`、`chore/sync-*` 属于这一类）
- `refactor/`：不改变外部行为的重构
- `hotfix/`：**唯一的例外**，从 `main` 切出，用于线上紧急修复，修完直接提 PR 回 `main`，随后由回同步流程带回 `develop`。

命名要求：小写、短横线分词、结尾带 issue 号（例如 `fix/upload-rollback-17`），便于从分支名反查上下文。`hotfix/*` 之外唯一的另一条例外是 `sync/main-into-develop`：它不是给人开发用的分支，而是 `Sync main into develop` 工作流维护的一次性 bot 分支（永远指向 `main` 的 tip），既不带 issue 号也不需要人推送，详见 §3。

## 2. 合并方式（不要选错）

- **日常 PR（`<type>/* → develop`）用 squash。** 历史整洁，一个 PR 一条提交，提交标题沿用 PR 标题。
- **发布 PR（`chore/release-v* → main`）和回同步 PR（`sync/main-into-develop → develop`，内容等于 `main`）用 merge commit，禁止 squash。** 原因：squash 会生成一条内容相同、但没有血缘关系的新提交，被合并分支上的原始提交永远不是目标分支的祖先。对回同步来说，这会让"`main` 领先 `develop`"永远成立，回同步 PR 会一次次重复出现；对发布 PR 来说，则会把 `develop` 的提交分组压平，发布点失去可追溯性。

### 分支保护（仓库设置，不由代码强制）

约定是 `main` 与 `develop` 都不能直接 push、必须通过 PR 合入，`main` 还要满足「合并前 CI 通过」。

**现状（2026-09-21 核实）：`main` 与 `develop` 都已开启保护**，且都是 `allow_deletions=false`、`allow_force_pushes=false`（`gh api repos/baiye-banned/Agent_about_answer/branches/main/protection`，`develop` 同理）。两边都已勾选「Require a pull request before merging」（`required_approving_review_count=0`，即只要求走 PR、不要求 approve）；差别在必需检查：`main` 已设 9 条（`strict=false`，不要求分支先跟上 base），`develop` 未设必需检查。其余相关设置：`allow_merge_commit=true`、`allow_squash_merge=true`、`allow_rebase_merge=true`、`allow_auto_merge=false`、`delete_branch_on_merge=true`。

**这些保护只是兜底，不能当作流程正确性的前提。** 保护规则和仓库设置是两处独立配置，`delete_branch_on_merge` 由平台在合并 PR 时执行，不区分 head 是短分支还是长期分支（详见 §3「回同步工作流」）。2026-09-20 01:36Z 事故发生时两个分支都还没有任何保护（事故当天的清理线现场确认仓库无 ruleset、无 branch protection），`main` 正是这样被自动删掉的；本文这两条保护是事故之后才补上的。把安全寄托在"保护没被改动"上，等于把一个删除 `main` 的开关留在别人手里；正确的做法是让长期分支永远不出现在 PR 的 head 位置。

管理员开启保护时至少需要：

- Require a pull request before merging：`main` 与 `develop` 都勾。**现状：两者都已勾选**，合并审查人数要求为 0（只要求走 PR、不要求 approve）。
- Require status checks to pass：至少加在 `main` 上。**现状：`main` 已设 9 条必需检查**——`node --test (Node 22)`、`Playwright e2e (chromium)`、`PR 标题规范校验`、`PR 描述必填节校验`、`pytest (Python 3.10)`、`Scan for secrets`、`vite build (Node 22)`、`后端静态检查 (compileall + ruff, Python 3.10)`、`前端静态检查 (node --check + eslint, Node 22)`；`develop` 尚未设置，是本条保留的建议项。
- **保留 Allow merge commits**：发布 PR 和回同步 PR 都依赖 merge commit，关掉它这两条流程就跑不通。squash 可以同时开着，日常 PR 靠约定选 squash。
- 不要开 Allow force pushes 和 Allow deletions：本流程明确不改写历史、不删 `main` / `develop`。

## 3. 发布流程

一次发布 = 一个发布 PR + 一个 tag，**没有自动合并、没有自动打 tag**。自动化只做到"把发布 PR 开出来"为止，剩下的都要人确认。

1. **发起发布**：在 GitHub Actions 里手动运行 `Release` 工作流（`workflow_dispatch`）。
   - `version`：要发布的版本号，不带 `v` 前缀，如 `1.1.0`（必须形如 `X.Y.Z`）。
   - `ref`：切发布分支的来源，默认 `develop`；只有热修复场景才填别的。
2. **工作流产出**：从 `ref` 切出 `chore/release-v<version>` → 用 `npm version <version> --no-git-tag-version` 改 `package.json` / `package-lock.json` → 提交 `chore(release): v<version>` → 推分支 → 开一个指向 `main` 的 PR：
   - 标题：`chore(release): v<version>`
   - 正文：「变更内容」里按 Conventional Commits 分类列出 `git log --no-merges origin/main..HEAD` 的全部提交，另有合并后待办清单。
3. **评审**：按普通 PR 评审发布 PR，确认版本号合适、变更范围符合预期。
4. **合并**：用 **merge commit** 合并到 `main`。不要 squash。
5. **打 tag**：合并后在 `main` 上手动创建（工作流不碰 tag）：

   ```bash
   git switch main && git pull
   git tag -a v1.1.0 -m "Release v1.1.0"
   git push origin v1.1.0
   gh release create v1.1.0 --title v1.1.0
   ```

   GitHub Release 的正文可以直接复用发布 PR 「变更内容」里的分类结果。
6. **回同步**：合并到 `main` 会触发 `Sync main into develop` 工作流，它把一次性 bot 分支 `sync/main-into-develop` 重置到 `main` 的当前 tip，再自动开一个 `sync/main-into-develop → develop` 的 PR（标题 `chore(sync): merge main into develop`），同样用 **merge commit** 合并，让 `main` 上的提交重新成为 `develop` 的祖先。

### 回同步工作流（`Sync main into develop`）

- 触发：`push` 到 `main`，或手动 `workflow_dispatch`。
- 行为：先把 bot 分支 `sync/main-into-develop` 重置到 `main` 的当前 tip 并推上去（分支不存在时创建，已存在时更新），再从 compare API 判断 `main` 是否领先 `develop`；不领先就直接结束；领先则先查有没有已开的同款 PR，没有才创建一个。
- **PR 的 head 是 bot 分支 `sync/main-into-develop`，不是 `main`。** 这是硬性设计，原因见下。
- 只创建 PR 和推送这个 bot 分支，**不自动合并、不推送 `main` / `develop`、不删除任何分支**；`main` 与 `develop` 不在工作流的任何写操作里。
- **为什么 head 不能是 `main`**：仓库开着 `delete_branch_on_merge=true`，合并 PR 后平台会删除该 PR 的 head 分支，且这条设置不区分 head 是短生命周期分支还是长期分支。2026-09-20 01:36Z 的实际事故：回同步 PR #46（head=`main`）在 01:36:08Z 合并，01:36:10Z `main` 被平台自动删除，仓库的发布分支凭空消失（事后用 `5e596ca` 重建）。只要 head 还是 `main`，每一次回同步合并都是一次新的删 `main` 尝试。
- 改用 bot 分支后，合并时被自动删除的就是 `sync/main-into-develop`——**这是预期行为**：它是一次性分支，内容永远等于当时的 `main` tip，下一次运行由工作流重新创建。工作流内对该分支的 `--force` 推送同样只作用于它自己。
- 文件名为 `sync-main-into-develop.yml`。issue #3 里提到的 `sync-main-to-develop.yml` 是同一个东西，落地时统一用了 `into` 这个写法。
- `push` 事件有个前提：**被推的那个分支上得先有这个工作流文件**。本文件随第一次发布合并进入 `main`，所以在那之前（以及第一次合并之前）`main` 上的推送不会触发它；从那以后的推送才会自动开 PR。
- **本次修复的生效时机**：`push` 事件执行的是被推分支上的那版工作流文件，所以在本修复合入 `develop`、并随下一次发布合并进入 `main` 之前，`main` 上的推送跑的仍是旧版本（head=`main`），回同步仍可能删掉 `main`。这段窗口期只能靠人工复核兜着：回同步 PR 合并后立刻读一次分支列表确认 `main` 还在（`gh api repos/baiye-banned/Agent_about_answer/branches --jq '.[].name'`），不在就用 `git push origin <旧的 main tip>:refs/heads/main` 恢复。开启的删除保护**是否**拦得住平台这次自动删除，本文没有实测结论，不作为前提。
- 手动触发（`workflow_dispatch`，包括 `gh workflow run`）另有一个前提：**工作流文件必须在仓库默认分支上**。本仓库默认分支是 `develop`，所以 `Sync main into develop` 与 `Release` 两个手动入口，都要等本文件合入 `develop` 之后才会出现。

## 4. 门禁与提交约定

- 提交信息遵循 [COMMIT_CONVENTION.md](COMMIT_CONVENTION.md)：`<type>(<scope>): <subject>`，标题小写、冒号后带空格。
- PR 描述按 [.github/pull_request_template.md](.github/pull_request_template.md) 逐节写全；标题与描述由 CI 检查，缺节或格式不符会判红。
- 自动创建的回同步 / 发布 PR 也按同一模板生成正文，否则它们会被自己的门禁卡住。
- 提交里不得出现真实密钥；仓库有密钥扫描工作流。工作流里的凭据一律使用自带的 `GITHUB_TOKEN`（写 `${{ secrets.GITHUB_TOKEN }}`），不引入额外 secret。

## 5. 已知限制与边界

- **`GITHUB_TOKEN` 不会触发下游工作流。** 这是 GitHub 的既定行为：用 `GITHUB_TOKEN` 推送的分支、用 `GITHUB_TOKEN` 创建的 PR，都不会自动拉起其他 workflow。所以自动开的回同步 / 发布 PR 上，CI 一开始是空的，需要维护者点一次 **Close → Reopen**（`reopened` 属于用户触发的事件），或往分支上补一个提交来触发检查。
- **不自动合并、不自动打 tag，工作流自身不执行删除分支。** 两条工作流都只有 `contents: write` + `pull-requests: write`，写操作限于"推一个 `chore/release-v*` 分支 / 把 `sync/main-into-develop` 重置到 `main` 的 tip"和"开一个 PR"。要和平台行为区分开：`delete_branch_on_merge` 是 **GitHub 在合并 PR 时**删掉该 PR 的 head 分支，不是工作流发出的删除操作——回同步 PR 合并后 `sync/main-into-develop` 会被平台删掉，这是预期的，也是「长期分支不能当 PR head」这条设计的由来。
- **手动触发依赖默认分支。** `workflow_dispatch` 要求工作流文件存在于默认分支（本仓库是 `develop`）；在文件合入 `develop` 之前，`gh workflow run release.yml` / `sync-main-into-develop.yml` 都会返回 `404 workflow ... not found on the default branch`。这是 GitHub 的既定行为，不是配置错误。
- **不重写历史。** 本流程不涉及 `rebase` 已推送的公共分支或改写已发布提交；`develop` 与 `main` 的历史只增不改，两者都不会被强推。唯一的 `push --force` 出现在回同步工作流里，且只作用于一次性 bot 分支 `sync/main-into-develop`——它的语义就是"当前 `main` 的镜像"，没有需要保留的历史。发布 PR 与回同步 PR 一律用 merge commit 合并，正是为了让发布点保持可追溯。
- **热修复**走 `hotfix/* → main`，合并后依赖回同步工作流带回 `develop`；如果同时 `develop` 上也有待发布内容，注意冲突需人工解决。
