# 开发须知

面向本仓库贡献者的一页说明，重点只有一条：**不要把密钥提交进仓库**。

## 提交前自检

```bash
bash scripts/scan_secrets.sh
```

- 默认扫描「git 已跟踪的文件 + 未跟踪文件（含被 .gitignore 忽略的文件）」，
  命中时以非 0 退出，并且**只输出 `文件:行号 [匹配类型]`，不输出匹配到的内容**，
  避免扫描日志本身造成二次泄漏。
  因此本地 `.env` 里放了真实密钥时，默认扫描会把它判红——**这是预期**，
  并不是让你去删掉那条规则：它确实以明文躺在你的工作区里。
  只想检查「会提交进仓库的内容」时，见下面的 `--tracked-only`。
- 只想扫已跟踪文件（跳过本地 `.env` 之类永远不会提交的文件）时：

  ```bash
  bash scripts/scan_secrets.sh --tracked-only
  ```

- gitleaks 是**必需**的：装了就扫「完整历史 + 工作区（含未跟踪文件）」，
  没装则以退出码 2 失败，绝不会静默跳过——「什么都没扫」被当成「干净」是最危险的假绿。
  仅想跑零依赖的模式扫描（例如没装 gitleaks 时快速看一眼）才用逃生口：

  ```bash
  bash scripts/scan_secrets.sh --patterns-only
  ```

  安装 gitleaks：<https://github.com/gitleaks/gitleaks#installing>。
  在 git worktree 里（`.git` 是文件）gitleaks 无法打开历史，脚本会打印提示并只扫工作区，
  历史扫描请在普通克隆或 CI 里做。

## 密钥与配置

- 真实密钥一律放在本地未跟踪文件里（例如 `.env`），仓库里只提交
  `.env.example` 这类占位模板。
- 下列文件类型已被 `.gitignore` 覆盖，不要用 `git add -f` 绕过：
  `.env`、`.env.*`、`*.key`、`*.pem`、`*.pfx`、`*.p12`、`secrets*.json`、
  `credentials*.json`、`deepseek.txt`、`.envrc`。
- 如果不小心提交了密钥：先在服务端**轮换**该密钥，再清理历史，最后才通知协作者；
  仅删除文件或改写提交都不足以挽回已泄漏的凭据。

## 误报（false positive）处理约定

扫描命中不等于一定是真密钥，但**不允许**用「关规则 / 跳过扫描」的方式让它变绿：

- 优先改成不触发规则的形式，例如把示例值写成明显的占位串（`your-api-key-here`、
  `example-token`）或从代码里挪进 `.env.example`；
- 确需保留某个看起来像密钥的样例值时，在仓库根目录的 `.gitleaks.toml` 里做
  **精确豁免**（只豁免该路径或该条规则，例如把样例文件放进 `paths` allowlist），
  并在提交信息里说明原因；
- **禁止**整体关闭某条规则、禁止 `--no-git`/`--patterns-only` 之类跳过扫描的做法
  进入 CI（`--patterns-only` 只用于本地快速自查）；
- 当前仓库无误报，因此暂未提供 `.gitleaks.toml`；一旦需要豁免，按上面三条添加。

## 提交信息

使用 Conventional Commits，例如：

```
feat(chat): 支持流式回答
fix(auth): 修正登录失败提示
ci(security): 加固 .gitignore 并引入密钥扫描门禁
```

## CI

`.github/workflows/secret-scan.yml` 在每次 push（所有分支）与 pull request 上运行：
先跑上面的脚本（内含 gitleaks 完整历史扫描），再用官方 gitleaks action 扫本次推送范围。
任一环节发现疑似密钥，任务即为红色。仓库目前未配置分支保护，红色任务不会从技术上
阻止合并，**红了就不要合**——先把命中处理掉（真密钥先轮换，误报按上节约定做精确豁免）。
