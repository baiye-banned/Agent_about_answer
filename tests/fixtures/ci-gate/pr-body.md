## 类型

- [x] 🔧 其他

## 变更概述

把 CI 门禁的注释消毒改成单趟扫描，并收紧字符数判据。

## 背景·问题

门禁脚本在正文提到注释起始符时会吃掉后面的小节标题，字符数判据也能被构造文本绕过。

## 关联 issue

Closes #97

## 变更内容

CHANGE_CONTENT_PLACEHOLDER

## 日志·验证证据

在夹具上跑一遍门禁脚本：

```
$ node scripts/check_pr_body.mjs tests/fixtures/ci-gate/pr-body.md
[OK] PR 描述校验通过，8 个必填节均已填写。
```

## 测试情况

用 tests/fixtures/ci-gate 下的夹具覆盖绕过与误伤两种构造，逐条跑 node --test。

## 截图

无需截图（无 UI 变更）
