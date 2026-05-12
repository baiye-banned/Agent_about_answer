# PROJECT_FLOW_DIAGRAM

这份文档只画当前真实链路。聊天 RAG 编排已经迁移到 LangChain 运行时，不再保留旧 `agentic_rag.py`、`tool.py`、`retrieval.py`、`rag_gate.py` 空壳。

主要模块：

- 前端聊天：`src/views/Chat.vue`、`src/stores/chat.js`、`src/api/chat.js`
- 后端聊天：`backend/service/chat_service.py`
- LangChain RAG：`backend/agent/agent.py`、`tools.py`、`chains.py`、`llm.py`
- 记忆与图片：`backend/rag/memory_service.py`、`vision_service.py`
- 知识库：`backend/service/knowledge_service.py`、`backend/crud/knowledge_file.py`、`backend/rag/chroma_client.py`

## 1. 纯文字问答

```mermaid
flowchart TD
  A["Chat.vue 输入问题"] --> B["chatStore.sendMessage(question, [])"]
  B --> C["streamChat()"]
  C --> D["POST /api/chat/stream"]
  D --> E["chat_service.py::stream_chat()"]
  E --> F["TraceRecorder: request_received / input_normalized"]
  F --> G["resolve_knowledge_base()"]
  G --> H["_build_effective_question()"]
  H --> I["Conversation 创建或读取"]
  I --> J["保存 user Message"]
  J --> K["_build_recent_memory_text()"]
  K --> K1{"recent_text > MEMORY_RECENT_MAX_CHARS?"}
  K1 -- "是" --> K2["_summarize_recent_memory() -> 一条近期记忆"]
  K1 -- "否" --> L["_build_memory_context()"]
  K2 --> L
  L --> M["_build_memory_aware_retrieval_question()"]
  M --> N["tools.py::decide_need_rag()"]
  N --> O{"need_rag?"}
  O -- "否" --> P["chains.py::stream_rag_answer(use_rag=false)"]
  O -- "是" --> Q["agent.py::create_agent() / agentic_retrieve_knowledge()"]
  Q --> R["tools.py::retrieve_knowledge()"]
  R --> S["build_query_plan / query_vectors / keyword_recall / rrf_fuse / rerank_chunks"]
  S --> T["context + sources"]
  T --> U["chains.py::stream_rag_answer(use_rag=true)"]
  P --> V["SSE content / trace / conversation"]
  U --> W["SSE content / trace / sources / conversation"]
  V --> X["保存 assistant Message"]
  W --> X
  X --> Y["RAG 时启动 RAGAS；检查滑窗并把滑出轮次写入长期记忆"]
  Y --> Z{"memory_summary > MEMORY_SUMMARY_MAX_CHARS?"}
  Z -- "是" --> Z1["_compact_summary_if_needed() -> 二次摘要；失败才裁剪"]
```

共用主链路仍是 `Chat.vue -> chatStore.sendMessage() -> streamChat() -> /api/chat/stream -> stream_chat()`。区别只在后端内部：路由、工具、Agent 和最终生成已经由 `agent/tool/rag` 承接。

## 2. 文字 + 图片问答

```mermaid
flowchart TD
  A["Chat.vue 上传图片"] --> B["POST /api/chat/attachments"]
  B --> C["upload_chat_attachment()"]
  C --> D["_put_oss_object() -> Aliyun OSS"]
  D --> E["返回 object_key / url"]
  E --> F["chatStore.sendMessage(question, attachments)"]
  F --> G["POST /api/chat/stream"]
  G --> H["stream_chat()"]
  H --> I["_build_effective_question()"]
  I --> J["_analyze_image_attachments()"]
  J --> K{"image_analysis.status"}
  K -- "success / partial" --> L["question + 图片描述 -> effective_question"]
  K -- "failed 且仍有文字" --> M["保留文字问题继续主链路"]
  L --> N["保存 user Message"]
  M --> N
  N --> O["memory_context + retrieval_question"]
  O --> P["decide_need_rag()"]
  P --> Q{"need_rag?"}
  Q -- "是" --> R["LangChain Agent -> retrieve_knowledge()"]
  Q -- "否" --> S["stream_rag_answer(use_rag=false)"]
  R --> T["context + sources"]
  T --> U["stream_rag_answer(use_rag=true)"]
  S --> V["SSE image_analysis / trace / content"]
  U --> V
  V --> W["保存 assistant Message；RAG 时启动 RAGAS"]
```

图片只是前置分支。识别成功后，它会合并成文本问题进入同一条 LangChain 聊天主链路。

## 3. 纯图片问答

```mermaid
flowchart TD
  A["Chat.vue 只选择图片"] --> B["chatStore.sendMessage('', attachments)"]
  B --> C["display_question = 请分析这张图片"]
  C --> D["stream_chat()"]
  D --> E["_build_effective_question()"]
  E --> F["_analyze_image_attachments()"]
  F --> G{"识别成功?"}
  G -- "否" --> H["SSE: image_analysis + error + [DONE]"]
  G -- "是" --> I["effective_question = 图片描述"]
  I --> J["保存 user Message"]
  J --> K["memory_context + retrieval_question"]
  K --> L["decide_need_rag()"]
  L --> M{"need_rag?"}
  M -- "是" --> N["LangChain Agent -> retrieve_knowledge()"]
  M -- "否" --> O["stream_rag_answer(use_rag=false)"]
  N --> P["stream_rag_answer(use_rag=true)"]
  O --> Q["SSE content / trace"]
  P --> Q
  Q --> R["保存 assistant Message"]
```

纯图片场景只有在图片识别失败且没有文字问题时提前结束；否则会复用完整聊天闭环。

## 4. 创建知识库

```mermaid
flowchart TD
  A["Knowledge.vue 新建知识库"] --> B["knowledgeAPI.createBase(name)"]
  B --> C["POST /api/knowledge-bases"]
  C --> D["knowledge_service.py::create_knowledge_base()"]
  D --> E{"name 为空?"}
  E -- "是" --> F["HTTP 400"]
  E -- "否" --> G{"名称已存在?"}
  G -- "是" --> H["HTTP 400"]
  G -- "否" --> I["crud_knowledge_base.create_knowledge_base()"]
  I --> J["serialize_knowledge_base()"]
  J --> K["knowledgeStore.upsertKnowledgeBase()"]
  K --> L["切换 currentKnowledgeBaseId"]
  L --> M["刷新知识库列表和文件列表"]
```

前端负责刷新和切换视图，后端负责重名校验和默认知识库回退。

## 5. 上传文件入库

```mermaid
flowchart TD
  A["Knowledge.vue 上传文件"] --> B["POST /api/knowledge/upload"]
  B --> C["knowledge_service.py::upload_knowledge()"]
  C --> D["resolve_knowledge_base()"]
  D --> E["await file.read()"]
  E --> F["crud_knowledge_file.extract_file_text()"]
  F --> G{"文件类型"}
  G -- "txt/md/json/csv/yaml/xml/log" --> H["UTF-8 decode"]
  G -- "docx" --> I["python-docx 提取段落"]
  G -- "pdf" --> J["pypdf 提取页面文本"]
  H --> K["create_knowledge_file() 写 MySQL"]
  I --> K
  J --> K
  K --> L["chunk_text()"]
  L --> M["chroma_client.add_chunks()"]
  M --> N["Chroma metadata 带 knowledge_base_id"]
  M -- "异常" --> O["delete_file_chunks() + 删除 MySQL 文件记录"]
  N --> P["返回文件信息并刷新列表"]
  O --> Q["HTTP 500 上传失败"]
```

入库仍是 MySQL 元数据先落库，再写 Chroma；向量写入失败会回滚文件记录和已写入 chunk。
