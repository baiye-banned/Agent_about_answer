# 开发须知

面向本仓库贡献者的一页说明，重点只有一条：**不要把密钥提交进仓库**。

## 提交前自检

```bash
bash scripts/scan_secrets.sh
```

- 默认扫描「git 已跟踪的文件 + 未跟踪文件（含被 .gitignore 忽略的文件）」，
  命中时以非 0 退出，并且**只输出 `文件:行号 [匹配类型]`，不输出匹配到的内容**，
  避免扫描日志本身造成二次泄漏。
- 只想扫已跟踪文件（跳过本地 `.env` 之类永远不会提交的文件）时：

  ```bash
  bash scripts/scan_secrets.sh --tracked-only
  ```

- 本机装有 gitleaks 时会自动追加一次全历史扫描；要求「没有 gitleaks 就报错」
  的严格模式：

  ```bash
  bash scripts/scan_secrets.sh --require-gitleaks
  ```

## 密钥与配置

- 真实密钥一律放在本地未跟踪文件里（例如 `.env`），仓库里只提交
  `.env.example` 这类占位模板。
- 下列文件类型已被 `.gitignore` 覆盖，不要用 `git add -f` 绕过：
  `.env`、`.env.*`、`*.key`、`*.pem`、`*.pfx`、`*.p12`、`secrets*.json`、
  `credentials*.json`、`deepseek.txt`、`.envrc`。
- 如果不小心提交了密钥：先在服务端**轮换**该密钥，再清理历史，最后才通知协作者；
  仅删除文件或改写提交都不足以挽回已泄漏的凭据。

## 提交信息

使用 Conventional Commits，例如：

```
feat(chat): 支持流式回答
fix(auth): 修正登录失败提示
ci(security): 加固 .gitignore 并引入密钥扫描门禁
```

## CI

`.github/workflows/secret-scan.yml` 在每次 push（所有分支）与 pull request 上运行：
先跑上面的脚本，再用 gitleaks 扫描完整历史。任一环节发现疑似密钥，任务即为红色，
必须处理后才合并。
