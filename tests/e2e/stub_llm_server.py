"""e2e 专用的本地 OpenAI 兼容桩服务，仅用标准库实现。

存在意义：e2e 要在公开仓库的 CI 里跑通「登录 → 建库 → 上传 → 检索 → 流式作答」整条链路，
但不能、也不应该把任何真实模型密钥放进 CI。因此本进程冒充三个上游：

  * POST /v1/chat/completions   —— 同时承担路由器、检索规划器、最终作答三种角色
  * POST /v1/embeddings         —— 确定性向量，用于 Milvus 向量召回
  * POST /v1/reranks            —— 确定性重排分数

桩不是「万能后端」：它对不同角色返回的 JSON 是按真实后端解析逻辑构造的，
作答文本是固定常量，只有拿到真实检索上下文时才会多输出一行引用来源。
也就是说，检索链路一旦断掉，e2e 的断言就会失败，而不是被桩悄悄兜住。

只读本文件即可了解全部外部依赖；不读写任何密钥，不使用网络出口。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --- 固定作答文本：e2e 断言依赖此常量 ---------------------------------------
# 标记串只出现在桩里，不会出现在知识库夹具里，因此「回答包含标记」可以证明
# 答案是本次经由桩生成的，而不是把检索到的原文回显出来。
ANSWER_MARKER = "E2E-STUB-ANSWER-OK"
ANSWER_BODY = "员工差旅报销必须在出差结束后的 10 个工作日内提交，逾期需要部门负责人审批。"
ANSWER_TEXT = f"{ANSWER_MARKER}：{ANSWER_BODY}"

CITATION_PREFIX = "引用来源："
# chat_service 拼上下文时使用的格式，桩据此回引来源，证明检索结果确实进入了 prompt。
SOURCE_PATTERN = re.compile(r"\[来源:\s*([^\]]+)\]")

DEFAULT_DIM = 1024


def _log(message: str) -> None:
    print(f"[stub-llm] {message}", flush=True)


def _digest(*parts: str) -> bytes:
    return hashlib.blake2b("\x00".join(parts).encode("utf-8"), digest_size=8).digest()


def embed_text(text: str, dim: int) -> list[float]:
    """把文本映射成确定性向量：字符 n-gram 哈希到固定桶，带符号后做 L2 归一化。

    同样的文本必然得到同样的向量（cosine=1），共享 n-gram 的文本 cosine 更高，
    足以让「上传的夹具」在向量召回里被查出来。
    """
    normalized = re.sub(r"\s+", "", text or "").lower()
    vector = [0.0] * dim
    grams: list[str] = []
    for size in (1, 2, 3):
        grams.extend(normalized[i : i + size] for i in range(len(normalized) - size + 1))
    if not grams:
        grams = ["<empty>"]

    for gram in grams:
        digest = _digest("gram", gram)
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign / len(gram)

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        vector[0] = 1.0
        norm = 1.0
    return [value / norm for value in vector]


def rerank_score(query: str, document: str) -> float:
    """确定性重排分数，落在 [0, 1]，用于让前端渲染 rerank 分值。"""
    digest = _digest("rerank", query or "", document or "")
    return round(int.from_bytes(digest[:2], "big") / 65535.0, 4)


def keyword_tokens(text: str) -> list[str]:
    """检索规划器的极简替代：抽 token 与 CJK 二元组，供关键词召回使用。"""
    tokens = [token for token in re.split(r"[^0-9A-Za-z一-鿿]+", text or "") if len(token) >= 2]
    grams = [token[i : i + 2] for token in tokens for i in range(len(token) - 1)]
    return tokens + grams


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") if item.get("text") is not None else item.get("content", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return "" if content is None else str(content)


def classify(system_prompt: str) -> str:
    """按真实系统的系统提示词分派角色，不猜模型名。"""
    if "路由器" in system_prompt:
        return "router"
    if "检索规划器" in system_prompt:
        return "query_plan"
    if "重排器" in system_prompt:
        return "rerank_llm"
    if "智能问答助手" in system_prompt or "企业对话助手" in system_prompt:
        return "answer"
    return "generic"


def _extract_question(user_prompt: str) -> str:
    match = re.search(r"用户问题：(.+?)(?:\n\n|$)", user_prompt or "", flags=re.S)
    return match.group(1).strip() if match else (user_prompt or "").strip()


def build_completion_text(kind: str, system_prompt: str, user_prompt: str) -> str:
    """按角色产出响应正文；JSON 角色返回可直接被 parse_json_object 解析的对象。"""
    if kind == "router":
        return json.dumps(
            {
                "need_rag": True,
                "route": "rag",
                "confidence": 0.99,
                "reason": "e2e 桩：问题询问企业制度，需要检索知识库",
                "source": "e2e-stub",
            },
            ensure_ascii=False,
        )
    if kind == "query_plan":
        question = _extract_question(user_prompt)
        return json.dumps(
            {
                "hyde_document": f"公司制度文件中关于「{question}」的规定与流程。",
                "rewrites": [question],
                "keywords": keyword_tokens(question),
            },
            ensure_ascii=False,
        )
    if kind == "rerank_llm":
        # 仅在 qwen3-rerank 端点不可用时才会走到这里；按真实 prompt 里的候选 id 返回分数。
        ids = re.findall(r'"id"\s*:\s*"([^"]+)"', user_prompt or "")
        return json.dumps(
            {"results": [{"id": item, "score": rerank_score("e2e", item), "reason": "e2e 桩重排"} for item in ids]},
            ensure_ascii=False,
        )

    if kind == "answer":
        # TEMP 可证伪性探针：刻意无视检索上下文、不回引来源，等价于「检索结果没有进入
        # prompt」。用来确认 e2e 的「引用可溯源」断言真的会因此变红，而不是一条永真的
        # 废话断言。仅用于本次验证，下一条提交立即回退。
        return ANSWER_TEXT

    return f"{ANSWER_MARKER}：e2e 桩通用回复。"


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "e2e-stub-llm/1.0"

    # --- 基础设施 ---------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖父类签名
        _log(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}".strip())

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            # 非 UTF-8 与非法 JSON 都要在这里收住：UnicodeDecodeError 不是
            # JSONDecodeError 的子类，漏掉它会让连接直接断掉、客户端只看到空响应，
            # 现场没有任何线索可查。
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _log(f"请求体不是合法的 UTF-8 JSON（长度 {length}）：{exc}")
            return {}

    def _record(self, kind: str, payload: dict) -> None:
        entry = {"kind": kind, "path": self.path, "payload": payload}
        self.server.requests.append(entry)  # type: ignore[attr-defined]
        log_file = self.server.log_file  # type: ignore[attr-defined]
        if not log_file:
            return
        try:
            with self.server.log_lock, open(log_file, "a", encoding="utf-8") as handle:  # type: ignore[attr-defined]
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            # 日志落盘失败绝不能连带把请求打死：异常一旦冒泡出 handler，客户端只会看到
            # 「连接被断开」，HTTP 层没有任何线索，排查成本极高。这里降级为 stderr 告警，
            # 并停掉后续的文件写入（避免每个请求都重复失败一次）。
            self.server.log_file = ""  # type: ignore[attr-defined]
            _log(f"请求日志写入失败（{log_file}）：{exc}；已停用文件日志，后续只打印到 stderr")

    # --- 路由 -------------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.startswith("/health"):
            self._send_json({"status": "ok", "service": "e2e-stub-llm"})
            return
        if self.path.startswith("/__stub__/log"):
            self._send_json({"requests": self.server.requests})  # type: ignore[attr-defined]
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        payload = self._read_json()
        if self.path.endswith("/chat/completions"):
            self._handle_chat(payload)
            return
        if self.path.endswith("/embeddings"):
            self._handle_embeddings(payload)
            return
        if self.path.endswith("/reranks") or self.path.endswith("/rerank"):
            self._handle_rerank(payload)
            return
        self._send_json({"error": f"unknown path {self.path}"}, status=404)

    # --- 各端点 -----------------------------------------------------------
    def _handle_chat(self, payload: dict) -> None:
        messages = payload.get("messages") or []
        system_prompt = _flatten_content(messages[0].get("content")) if messages else ""
        user_prompt = _flatten_content(messages[-1].get("content")) if messages else ""
        kind = classify(system_prompt)
        text = build_completion_text(kind, system_prompt, user_prompt)
        self._record(kind, {"stream": bool(payload.get("stream")), "question": _extract_question(user_prompt)})

        if payload.get("stream"):
            self._send_chat_stream(text)
        else:
            self._send_json(
                {
                    "id": "chatcmpl-e2e-stub",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": payload.get("model") or "e2e-stub",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            )

    def _send_chat_stream(self, text: str) -> None:
        """按 chunk 下发 SSE，让前端真的经历一段流式渲染状态。"""
        delay = self.server.chunk_delay_ms / 1000.0  # type: ignore[attr-defined]
        chunk_size = self.server.chunk_size  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def frame(delta: dict, finish_reason=None) -> bytes:
            chunk = {
                "id": "chatcmpl-e2e-stub",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "e2e-stub",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

        pieces = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]
        try:
            self.wfile.write(frame({"role": "assistant", "content": ""}))
            for piece in pieces:
                self.wfile.write(frame({"content": piece}))
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
            self.wfile.write(frame({}, finish_reason="stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _log("客户端提前断开流式连接（通常是页面关闭）")

    def _handle_embeddings(self, payload: dict) -> None:
        inputs = payload.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        inputs = inputs or []
        dim = int(payload.get("dimensions") or self.server.dim)  # type: ignore[attr-defined]
        self._record("embeddings", {"count": len(inputs), "dimensions": dim})
        self._send_json(
            {
                "object": "list",
                "model": payload.get("model") or "e2e-stub-embedding",
                "data": [
                    {"object": "embedding", "index": index, "embedding": embed_text(text, dim)}
                    for index, text in enumerate(inputs)
                ],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }
        )

    def _handle_rerank(self, payload: dict) -> None:
        documents = payload.get("documents") or []
        query = payload.get("query") or ""
        top_n = payload.get("top_n") or len(documents)
        self._record("rerank", {"query": query, "documents": len(documents), "top_n": top_n})
        results = [
            {"index": index, "relevance_score": rerank_score(query, document), "score": rerank_score(query, document)}
            for index, document in enumerate(documents)
        ]
        results.sort(key=lambda item: item["relevance_score"], reverse=True)
        self._send_json({"results": results[: int(top_n)]})


class StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="e2e 本地 OpenAI 兼容桩服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM, help="embeddings 默认维度，与后端 EMBEDDING_DIM 保持一致")
    parser.add_argument("--chunk-delay-ms", type=int, default=120, help="流式分片之间的间隔，便于前端渲染出流式状态")
    parser.add_argument("--chunk-size", type=int, default=12)
    parser.add_argument("--log-file", default="", help="把收到的请求以 JSONL 追加写入该文件")
    args = parser.parse_args(argv)

    # 提前验证 --log-file 可写：路径不对时应当在这里就失败退出（启动步骤直接红），
    # 而不是等到第一个请求才炸在 handler 里，让客户端只看到一个莫名的断连。
    if args.log_file:
        try:
            with open(args.log_file, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            print(f"[stub-llm] --log-file 不可写（{args.log_file}）：{exc}", file=sys.stderr)
            return 2

    server = StubServer((args.host, args.port), StubHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.log_file = args.log_file  # type: ignore[attr-defined]
    server.log_lock = threading.Lock()  # type: ignore[attr-defined]
    server.dim = args.dim  # type: ignore[attr-defined]
    server.chunk_delay_ms = args.chunk_delay_ms  # type: ignore[attr-defined]
    server.chunk_size = args.chunk_size  # type: ignore[attr-defined]

    _log(f"listening on http://{args.host}:{args.port} (dim={args.dim})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
