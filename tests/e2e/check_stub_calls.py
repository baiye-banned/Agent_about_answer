"""校验 e2e 期间桩被调用到的角色，证明检索链路真的被完整走过。

用例全绿只说明「浏览器里看到的行为」是对的。后端在路由器、检索规划器、重排器上
各自都有兜底分支（backend/rag/retrieval.py 的 except 与 _fallback_*、rag/rerank.py
的后备重排），任何一环退化到兜底，用例仍然会绿 —— 例如检索规划器整个坏掉时，
关键词路照样能召回夹具。桩是唯一知道「后端究竟调了什么」的旁观者：它把每个请求
的角色写进 --log-file 指定的 JSONL，这里据此判定

  * 必需角色一个都不能少：路由 / 检索规划 / 向量化 / 重排 / 作答；
  * 不允许出现通用兜底角色（generic、rerank_llm），它们意味着后端的系统提示词已经
    变成桩不认识的形状，真实环境里对应的能力已经失效，只是被兜底掩盖住了。

本地跑完 e2e 后同样可以用它自查，路径见 README「端到端验收」。用法：
    python tests/e2e/check_stub_calls.py <stub-requests.jsonl>
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

# 桩按后端真实的系统提示词分派角色（见 stub_llm_server.py 的 classify）：
#   router      路由：判断该不该检索
#   query_plan  检索规划：hyde 文档、改写、关键词
#   embeddings  向量化：入库切分与查询向量
#   rerank      专用重排端点
#   answer      最终作答
REQUIRED_KINDS = ("router", "query_plan", "embeddings", "rerank", "answer")
# generic：后端换了系统提示词，桩只能给通用回复（后端随后会走自己的兜底分支）；
# rerank_llm：专用重排端点没被用上，退到了 LLM 后备重排。
FORBIDDEN_KINDS = ("generic", "rerank_llm")

DEFAULT_LOG_NAME = "stub-requests.jsonl"


def load_kinds(path: pathlib.Path) -> collections.Counter:
    """按 JSONL 逐行统计角色；坏行直接暴露（不做静默跳过，否则日志本身坏了也算通过）。"""
    kinds: collections.Counter = collections.Counter()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            kinds[json.loads(line)["kind"]] += 1
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SystemExit(f"{path} 第 {number} 行不是合法的桩请求记录：{exc}")
    return kinds


def main(argv: list[str]) -> int:
    path = pathlib.Path(argv[1] if len(argv) > 1 else DEFAULT_LOG_NAME)
    if not path.is_file():
        print(f"找不到桩的请求日志：{path}（桩必须用 --log-file 指向该路径）", file=sys.stderr)
        return 2

    kinds = load_kinds(path)
    print(f"桩收到的调用角色：{dict(kinds)}")

    missing = [kind for kind in REQUIRED_KINDS if not kinds[kind]]
    degraded = {kind: kinds[kind] for kind in FORBIDDEN_KINDS if kinds[kind]}
    if missing or degraded:
        print(
            f"检索链路没有被完整走到：缺少 {missing or '无'}；退化到兜底 {degraded or '无'}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
