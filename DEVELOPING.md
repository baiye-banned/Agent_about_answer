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
- 只想检查「会提交进仓库的内容」（跳过本地 `.env` 这类永远不会提交的文件）时：

  ```bash
  bash scripts/scan_secrets.sh --tracked-only
  ```

  该模式只查 git 跟踪的内容：内建扫描跳过未跟踪/被忽略文件，gitleaks 的工作区扫描
  也一并跳过（脚本会打印提示），只保留完整历史扫描。**未提交的改动仍在覆盖范围内**
  （内建扫描读的是工作区里的文件内容），**未跟踪文件则两层都不再覆盖**——这正是该开关
  的取舍，默认模式没有这个缺口；CI 必须用默认模式。若因此一次 gitleaks 扫描都跑不成
  （例如在 git worktree 里，历史扫描本就无法进行），脚本以退出码 2 失败，而不是报干净。
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
- 占位词表（内建扫描「像配置不像凭据」的判定依据）含 `none`、`change-me*`、`your-*`、
  `replace-*`、`placeholder*`、`example*`、`sample*`、`dummy*`、`fake*`、`testkey*`、
  `test-only*`、`not-set*`、`xxxx*` 等。扩大词表等于放宽判定，必须说明为什么不会漏掉
  真实密钥（例如 `test-only` 前缀的长随机值只在测试里出现，且真实密钥不会这样命名）；
- 只有**变量名或取值语义**像密钥、实际是测试夹具/标签常量的行，才在行尾加内联标记：

  ```python
  MODE_LABEL = "env"  # scan-secrets:allow source label, not a credential
  ```

  标记必须带非空理由（`# scan-secrets:allow <理由>`），且**只**豁免该行的
  `[credential assignment]`（赋值启发式）命中：`sk-` 长串、`AKIA`+16 位大写、
  `ghp_` 长串、`-----BEGIN ... PRIVATE KEY-----` 头都是按**形态**匹配的，任何标记都
  豁免不了，所以标记无法用来藏起这些形态的凭据。扫描会把被豁免的行清单打印出来
  （`N line(s) exempted ...` 后面跟着 `文件:行号`；仅当至少有一条豁免时才打印，
  一条都没有时不会出现这一行），豁免在 CI 日志里可见而非静默，
  评审时与它豁免的代码在同一份 diff 里一起审。标记的识别只要求行内出现 `#`、标记名与
  非空理由，因此标记文本若被拼进**数据字符串**同样会生效；不要在字符串或文档里随手
  粘贴标记文本，每次打印的豁免清单就是为了让这种情况可见。**禁止**用标记掩盖真密钥或
  提交无关代码；
- 内建扫描把 `KEY == 其它值` 当比较而不是赋值（`==` 的第一个 `=` 属于比较运算符），
  因此这类行不再产生赋值命中（`KEY := 值` 仍是赋值）。注意**比较行上的低熵字面量两层
  都不覆盖**：下面 gitleaks 的熵规则只认高熵取值（`TOKEN == "<32 位十六进制>"` 仍会被
  `generic-api-key` 抓到），而 `PASSWORD == "hunter2"` 这类弱口令两侧都不会报。这是刻意
  取舍——比较不是赋值，把 `==` 读成赋值正是 issue #36 的误报之一；需要在该形态上留痕时
  按普通代码评审处理，不要指望扫描器；
- 形态类命中（`sk-` 之类标记豁免不了的）确需保留样例时，在仓库根目录的
  `.gitleaks.toml` 里做**精确豁免**（只豁免该路径或该条规则，例如把样例文件放进
  `paths` allowlist），并在提交信息里说明原因；
- **禁止**整体关闭某条规则、禁止 `--no-git`/`--patterns-only` 之类跳过扫描的做法
  进入 CI（`--patterns-only` 只用于本地快速自查）。这里拦的是**拿它去扫仓库**：让 CI 对
  仓库本体少扫一层，等于把「什么都没扫」报成干净。门禁自检不在此列——
  `tests/test_scan_secrets_selftest.py` 对临时目录里的夹具用 `--patterns-only`，扫的不是
  仓库，且用例会断言脚本确实打印了跳过说明（跳过始终显式可见，不会变成一次假绿）；

- 当前仓库没有 `.gitleaks.toml`，内联标记的使用情况**以扫描输出为准**：在仓库根目录跑
  `bash scripts/scan_secrets.sh --patterns-only`，上面说的那份逐行豁免清单即当前全量，
  评审时以它为准。本节只记一个**会过期的读数**、方便快速对照，不构成权威：
  **7 行 / 3 个文件**——`backend/config.py` 的 3 个来源标签常量、
  `scripts/smoke_providers.py` 的 1 行（还原被临时置空的 key，非凭据）、
  `tests/test_knowledge_ownership.py` 的 3 行内存测试夹具（见 issue #36）。
  再有新增时按本节约定处理，并顺手更新这个读数——数字一旦过期，后来人就会按错的范围判断
  豁免是否合理。

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
任一环节发现疑似密钥，任务即为红色。`main` 上「Scan for secrets」已在必需检查名单内
（2026-09-21 实读：共 9 条，`strict=false`，不要求分支先跟上 base）；`develop` 上未设必需
检查，红色任务不会从技术上阻止合并，**红了就不要合**——先把命中处理掉（真密钥先轮换，
误报按上节约定做精确豁免）。

「这道门禁真的会红吗」可以随时在 CI 上复现，不必往仓库里提交假密钥：

```bash
gh workflow run secret-scan.yml -f self_test=true
```

该自测会在检出目录之外的临时目录里生成一个**运行时随机**的假密钥，要求扫描返回 1（命中），
删掉文件后再要求返回 0（干净）；只要其中一步不符合预期（例如扫描静默放过、或 gitleaks
缺失导致退出码 2），自测步骤自己失败，job 变红。生成物不进入仓库，也不会被提交。
