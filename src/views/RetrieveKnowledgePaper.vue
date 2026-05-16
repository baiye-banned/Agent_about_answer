<template>
  <div class="h-full overflow-hidden rounded-2xl border border-slate-200 bg-slate-100 p-4">
    <div class="flex h-full min-w-[1180px] flex-col rounded-[10px] border border-slate-300 bg-white px-8 py-7 shadow-sm">
      <header class="mb-5 flex shrink-0 items-start justify-between gap-6 border-b border-slate-200 pb-4">
        <div>
          <h2 class="text-2xl font-semibold tracking-normal text-slate-900">retrieve_knowledge</h2>
          <p class="mt-3 max-w-4xl text-sm leading-6 text-slate-500">
            按当前 Agent 流程图的结构重排：主线从入参往下走，查询规划、多路召回、关键词召回、RRF、rerank 和最终选择都收在一张白纸流程里。
          </p>
        </div>
        <div class="w-80 rounded-md border border-sky-200 bg-sky-50 px-4 py-3 text-xs leading-5 text-sky-800">
          这张图只整理前端展示，不改变后端 retrieve_knowledge 的真实实现。
        </div>
      </header>

      <div class="grid min-h-0 flex-1 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <main class="diagram-paper h-full overflow-auto rounded-md border-2 border-slate-700 bg-white px-8 py-7">
          <div class="mx-auto min-w-[780px] max-w-[980px]">
            <DiagramNode
              id="inputs"
              title="retrieve_knowledge(...)"
              note="question / knowledge_base_id / db / trace_recorder"
              strong
              :selected="selectedId === 'inputs'"
              @select="selectNode"
            />
            <DownArrow label="先记录工具调用" />
            <DiagramNode
              id="traceStart"
              title="_trace_add: langchain_tool_called"
              note="把 question 和 knowledge_base_id 记入 trace"
              :selected="selectedId === 'traceStart'"
              @select="selectNode"
            />
            <DownArrow label="生成检索计划" />

            <section class="rounded-[28px] border-2 border-slate-800 bg-slate-50 p-5">
              <button type="button" class="block w-full text-left" @click="selectNode('buildPlan')">
                <div class="font-mono text-base font-semibold text-slate-900">build_query_plan(question)</div>
                <div class="mt-1 text-sm text-slate-600">让模型生成 HyDE、改写 query 和关键词</div>
              </button>

              <div class="mt-5 grid grid-cols-[minmax(0,1fr)_36px_minmax(0,1fr)_36px_minmax(0,1fr)] items-stretch gap-3">
                <MiniFlow id="planPrompt" title="system/user prompt" note="只允许 JSON 字段输出" :selected="selectedId === 'planPrompt'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="callChatJson" title="call_chat_json" note="调用 LLM 并解析 JSON" :selected="selectedId === 'callChatJson'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="queryPlan" title="query_plan" note="hyde_document / rewrites / keywords / error" :selected="selectedId === 'queryPlan'" @select="selectNode" />
              </div>
            </section>

            <BranchRow
              top-label="查询规划结果分叉"
              left-id="planFallback"
              left-title="规划失败"
              left-note="返回空 HyDE、空 rewrites，并用 fallback keywords"
              right-id="planSuccess"
              right-title="规划成功"
              right-note="清洗 rewrites，合并 keywords，继续向下"
              :selected-id="selectedId"
              @select="selectNode"
            />

            <DownArrow label="初始化本轮 trace 和 route_specs" />
            <DiagramNode
              id="traceInit"
              title="trace + route_results + route_specs"
              note="embedding / query_plan / routes / rrf / rerank"
              :selected="selectedId === 'traceInit'"
              @select="selectNode"
            />
            <DownArrow label="构建向量召回路线" />

            <section class="rounded-[28px] border-2 border-slate-800 bg-white p-5">
              <button type="button" class="block w-full text-left" @click="selectNode('routeSpecs')">
                <div class="font-mono text-base font-semibold text-slate-900">route_specs = original + hyde + rewrite_N</div>
                <div class="mt-1 text-sm text-slate-600">原问题一定存在；HyDE 和 rewrites 有值才加入</div>
              </button>

              <div class="mt-5 grid grid-cols-[minmax(0,1fr)_36px_minmax(0,1fr)_36px_minmax(0,1fr)] items-stretch gap-3">
                <MiniFlow id="originalRoute" title="original" note="使用用户原问题" :selected="selectedId === 'originalRoute'" @select="selectNode" />
                <Connector text="+" />
                <MiniFlow id="hydeRoute" title="hyde" note="使用假设答案文档" :selected="selectedId === 'hydeRoute'" @select="selectNode" />
                <Connector text="+" />
                <MiniFlow id="rewriteRoutes" title="rewrite_1..N" note="使用问题改写结果" :selected="selectedId === 'rewriteRoutes'" @select="selectNode" />
              </div>
            </section>

            <DownArrow label="逐路调用 query_vectors" />
            <section class="rounded-[28px] border-2 border-slate-800 bg-slate-50 p-5">
              <button type="button" class="block w-full text-left" @click="selectNode('routeLoop')">
                <div class="font-mono text-base font-semibold text-slate-900">for route, query in route_specs</div>
                <div class="mt-1 text-sm text-slate-600">每一路都去 ChromaDB 查语义相似片段</div>
              </button>

              <div class="mt-5 grid grid-cols-[minmax(0,1fr)_36px_minmax(0,1fr)_36px_minmax(0,1fr)] items-stretch gap-3">
                <MiniFlow id="queryVectors" title="query_vectors" note="top_k + knowledge_base_id + route" :selected="selectedId === 'queryVectors'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="chromaQuery" title="collection.query" note="where 限定当前知识库" :selected="selectedId === 'chromaQuery'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="routeResults" title="route_results.append" note="保存 route 与 chunks" :selected="selectedId === 'routeResults'" @select="selectNode" />
              </div>

              <div class="mt-5 border-t border-dashed border-slate-300 pt-5">
                <button
                  type="button"
                  class="mb-3 font-mono text-sm font-semibold text-slate-900 underline decoration-slate-300 underline-offset-4"
                  @click="selectNode('queryVectorsInside')"
                >
                  query_vectors 内部像这样展开
                </button>
                <div class="grid grid-cols-[minmax(0,1fr)_28px_minmax(0,1fr)_28px_minmax(0,1fr)] gap-3">
                  <MiniFlow id="getCollection" title="get_collection" note="打开语义向量集合" compact :selected="selectedId === 'getCollection'" @select="selectNode" />
                  <Connector text="->" />
                  <div class="space-y-3">
                    <MiniFlow id="whereFilter" title="where filter" note="knowledge_base_id 隔离" compact :selected="selectedId === 'whereFilter'" @select="selectNode" />
                    <MiniFlow id="distanceItems" title="distances + metadata" note="取距离、文件名、chunk_id" compact :selected="selectedId === 'distanceItems'" @select="selectNode" />
                  </div>
                  <Connector text="->" />
                  <MiniFlow id="vectorChunks" title="vector chunks" note="组装为统一 chunk 结构" compact :selected="selectedId === 'vectorChunks'" @select="selectNode" />
                </div>
              </div>
            </section>

            <DownArrow label="再做关键词补召回" />
            <DiagramNode
              id="keywordRecall"
              title="keyword_recall(db, knowledge_base_id, keyword_terms, top_k)"
              note="读 MySQL 的 KnowledgeFile.content，临时切块并计算 keyword_score"
              :selected="selectedId === 'keywordRecall'"
              @select="selectNode"
            />
            <DownArrow label="向量路线 + 关键词路线合并" />

            <section class="rounded-[28px] border-2 border-slate-800 bg-white p-5">
              <button type="button" class="block w-full text-left" @click="selectNode('rrfFuse')">
                <div class="font-mono text-base font-semibold text-slate-900">rrf_fuse(route_results)</div>
                <div class="mt-1 text-sm text-slate-600">把 original / hyde / rewrite / keyword 多路结果做融合排序</div>
              </button>
              <div class="mt-5 grid grid-cols-[minmax(0,1fr)_36px_minmax(0,1fr)_36px_minmax(0,1fr)] items-stretch gap-3">
                <MiniFlow id="chunkKey" title="_chunk_key" note="file_id:chunk_id 去重" :selected="selectedId === 'chunkKey'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="rrfScore" title="rrf_score += 1/(60+rank)" note="多路排名投票" :selected="selectedId === 'rrfScore'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="fused" title="fused sorted" note="按 rrf_score 降序" :selected="selectedId === 'fused'" @select="selectNode" />
              </div>
            </section>

            <DownArrow label="只取前 12 个候选交给 rerank" />
            <DiagramNode
              id="rerank"
              title="rerank_chunks(question, fused[:12])"
              note="LLM 给候选 chunk 打相关性分数，并返回 reason"
              :selected="selectedId === 'rerank'"
              @select="selectNode"
            />

            <BranchRow
              top-label="rerank 结果分叉"
              left-id="rerankFallback"
              left-title="rerank 失败或跳过"
              left-note="返回空列表，最终回退使用 fused"
              right-id="rerankSuccess"
              right-title="rerank 成功"
              right-note="按 rerank_score 降序得到 reranked"
              :selected-id="selectedId"
              @select="selectNode"
            />

            <DownArrow label="选择最终上下文片段" />
            <section class="rounded-[28px] border-2 border-slate-800 bg-slate-50 p-5">
              <button type="button" class="block w-full text-left" @click="selectNode('selectFinal')">
                <div class="font-mono text-base font-semibold text-slate-900">_select_final_chunks(reranked or fused, keyword_chunks)</div>
                <div class="mt-1 text-sm text-slate-600">先取排名靠前的片段，再用关键词强命中做保护</div>
              </button>
              <div class="mt-5 grid grid-cols-[minmax(0,1fr)_36px_minmax(0,1fr)_36px_minmax(0,1fr)] items-stretch gap-3">
                <MiniFlow id="topN" title="ranked_chunks[:N]" note="默认取前 RETRIEVAL_RERANK_TOP_N" :selected="selectedId === 'topN'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="keywordGuard" title="keyword_score >= 10" note="高分关键词 chunk 可前置保护" :selected="selectedId === 'keywordGuard'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="dedupe" title="dedupe + trim" note="去重后截断到 N 个" :selected="selectedId === 'dedupe'" @select="selectNode" />
              </div>
            </section>

            <DownArrow label="记录完成事件并返回" />
            <div class="grid grid-cols-[minmax(0,1fr)_44px_minmax(0,1fr)] items-stretch gap-3">
              <DiagramNode id="finalChunks" title="final_chunks" note="给回答模型拼接知识库上下文" :selected="selectedId === 'finalChunks'" @select="selectNode" />
              <Connector text="+" />
              <DiagramNode id="finalTrace" title="trace" note="query_plan / routes / rrf / rerank" :selected="selectedId === 'finalTrace'" @select="selectNode" />
            </div>

            <div class="mt-5 rounded-md border border-brand-200 bg-brand-50 px-4 py-3 text-center text-sm font-semibold text-brand-800">
              最朴素的话：它把一个问题改成多种搜索方式，到向量库和文本里一起找，再把结果投票、重排，最后挑几段最适合拿去回答的资料。
            </div>
          </div>
        </main>

        <aside class="h-full overflow-y-auto rounded-md border-2 border-slate-700 bg-white p-4">
          <div class="mb-4 flex items-center justify-between">
            <h3 class="text-base font-semibold text-slate-900">节点详情</h3>
            <span class="rounded border border-slate-300 bg-slate-50 px-2 py-1 text-xs text-slate-500">{{ currentNode.kind }}</span>
          </div>

          <DetailBlock label="符号名" :value="currentNode.title" />
          <DetailBlock label="它做什么" :value="currentNode.detail" />
          <DetailBlock label="来源文件" :value="currentNode.file" />
          <DetailBlock label="关键值 / 伪代码" :value="currentNode.code" code />
        </aside>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, defineComponent, h, ref } from 'vue'

const selectedId = ref('inputs')

const nodeDetails = {
  inputs: {
    title: 'retrieve_knowledge inputs',
    kind: '入参',
    file: 'backend/tool/tools.py',
    detail: '接收本轮检索问题、当前知识库 ID、数据库会话和可选 trace_recorder。knowledge_base_id 会同时约束向量库和 SQL 文件。',
    code: 'question, knowledge_base_id, db, trace_recorder',
  },
  traceStart: {
    title: '_trace_add: langchain_tool_called',
    kind: '追踪',
    file: 'backend/tool/tools.py',
    detail: '进入 retrieve_knowledge 时先写一条工具调用事件，方便学习中心和调试页知道检索工具已经开始工作。',
    code: '_trace_add(trace_recorder, "langchain_tool_called", "retrieve_knowledge", params={...})',
  },
  buildPlan: {
    title: 'build_query_plan',
    kind: '方法',
    file: 'backend/tool/tools.py',
    detail: '让模型把用户问题扩展成更适合检索的计划，包括 HyDE 假设文档、多个改写 query 和关键词。',
    code: 'query_plan = await build_query_plan(question)',
  },
  planPrompt: {
    title: 'system_prompt / user_prompt',
    kind: '提示词',
    file: 'backend/tool/tools.py',
    detail: 'system_prompt 约束模型只输出 JSON；user_prompt 告诉模型要生成 hyde_document、rewrites 和 keywords。',
    code: 'fields: hyde_document, rewrites, keywords',
  },
  callChatJson: {
    title: 'call_chat_json',
    kind: '模型调用',
    file: 'backend/rag/llm.py',
    detail: '调用聊天模型并解析 JSON。如果调用失败，build_query_plan 会捕获异常，不让检索主流程中断。',
    code: 'data = await call_chat_json(system_prompt, user_prompt)',
  },
  queryPlan: {
    title: 'query_plan',
    kind: '变量',
    file: 'backend/tool/tools.py',
    detail: '检索计划对象。后面的向量召回路线和关键词召回都会从这里取数据。',
    code: '{ hyde_document, rewrites, keywords, error }',
  },
  planFallback: {
    title: 'build_query_plan fallback',
    kind: '兜底',
    file: 'backend/tool/tools.py',
    detail: '模型规划失败时返回空 HyDE、空 rewrites，并用 _fallback_keywords(question) 生成关键词，保证仍然能搜索。',
    code: '{ hyde_document: "", rewrites: [], keywords: _fallback_keywords(question), error: str(exc) }',
  },
  planSuccess: {
    title: 'build_query_plan success',
    kind: '成功分支',
    file: 'backend/tool/tools.py',
    detail: '清洗模型输出：rewrites 最多 3 个，keywords 会和问题里的 fallback keywords 合并去重。',
    code: '_clean_list(data.get("rewrites"))[:3]\n_merge_keywords(_clean_list(data.get("keywords")), question)[:24]',
  },
  traceInit: {
    title: 'trace / route_results / route_specs',
    kind: '变量',
    file: 'backend/tool/tools.py',
    detail: 'trace 保存 embedding 状态、规划、各路召回、RRF 和 rerank；route_results 用来给 RRF 融合。',
    code: 'trace = { embedding, query_plan, routes: [], rrf: [], rerank: { status: "skipped" } }',
  },
  routeSpecs: {
    title: 'route_specs',
    kind: '路线表',
    file: 'backend/tool/tools.py',
    detail: '向量召回路线表。original 一定存在；HyDE 和 rewrites 只有在 query_plan 有值时才加入。',
    code: 'route_specs = [("original", question)]',
  },
  originalRoute: {
    title: 'original route',
    kind: '召回路线',
    file: 'backend/tool/tools.py',
    detail: '直接使用用户原问题做向量检索，是最基础也最稳的一路。',
    code: '("original", question)',
  },
  hydeRoute: {
    title: 'hyde route',
    kind: '召回路线',
    file: 'backend/tool/tools.py',
    detail: '如果模型生成了 HyDE 假设答案文档，就把这段文档当作 query 去向量库里找相似片段。',
    code: 'if query_plan.get("hyde_document"): route_specs.append(("hyde", query_plan["hyde_document"]))',
  },
  rewriteRoutes: {
    title: 'rewrite routes',
    kind: '召回路线',
    file: 'backend/tool/tools.py',
    detail: '遍历 rewrites，形成 rewrite_1、rewrite_2、rewrite_3 等路线，扩大语义召回面。',
    code: 'for index, rewrite in enumerate(query_plan.get("rewrites") or [], start=1)',
  },
  routeLoop: {
    title: 'route loop',
    kind: '循环',
    file: 'backend/tool/tools.py',
    detail: '每一路 route/query 都调用 query_vectors，返回的 chunks 会进入 route_results，并写入 trace["routes"]。',
    code: 'for route, query in route_specs:',
  },
  queryVectors: {
    title: 'query_vectors',
    kind: '向量召回',
    file: 'backend/rag/chroma_client.py',
    detail: '用当前 query 到 ChromaDB 里做语义相似度搜索，top_k 受 RETRIEVAL_ROUTE_TOP_K 控制。',
    code: 'query_vectors(query, top_k=RETRIEVAL_ROUTE_TOP_K, knowledge_base_id=knowledge_base_id, route=route)',
  },
  chromaQuery: {
    title: 'collection.query',
    kind: 'Chroma 查询',
    file: 'backend/rag/chroma_client.py',
    detail: '真正访问向量集合。where={knowledge_base_id} 用来确保只查当前知识库的数据。',
    code: 'collection.query(query_texts=[query], n_results=min(max(top_k, 1), 20), where=where)',
  },
  routeResults: {
    title: 'route_results',
    kind: '变量',
    file: 'backend/tool/tools.py',
    detail: '把每一路的 chunks 按 route 名保存起来，后续 RRF 需要知道某个 chunk 分别来自哪些路线、排第几名。',
    code: 'route_results.append((route, chunks))\ntrace["routes"].append({ route, query, count, items })',
  },
  queryVectorsInside: {
    title: 'query_vectors internals',
    kind: '子流程',
    file: 'backend/rag/chroma_client.py',
    detail: 'query_vectors 会打开集合、设置知识库过滤条件、调用 Chroma 查询，再把原始结果整理成统一 chunk 字典。',
    code: 'get_collection -> where -> collection.query -> normalize chunks',
  },
  getCollection: {
    title: 'get_collection',
    kind: '集合',
    file: 'backend/rag/chroma_client.py',
    detail: '懒加载或创建语义向量集合，集合名包含 embedding 模型和维度。',
    code: 'client.get_or_create_collection(COLLECTION_NAME, embedding_function=_embedding_fn)',
  },
  whereFilter: {
    title: 'where filter',
    kind: '隔离条件',
    file: 'backend/rag/chroma_client.py',
    detail: '有 knowledge_base_id 时设置 Chroma where 条件，避免跨知识库召回。',
    code: 'where = {"knowledge_base_id": knowledge_base_id} if knowledge_base_id else None',
  },
  distanceItems: {
    title: 'distances + metadata',
    kind: '原始结果',
    file: 'backend/rag/chroma_client.py',
    detail: '从 Chroma 返回值中取 documents、metadatas、distances、ids，用来组装统一 chunk。',
    code: 'distance = distances[0][index]\nmeta = results["metadatas"][0][index]',
  },
  vectorChunks: {
    title: 'vector chunks',
    kind: '返回值',
    file: 'backend/rag/chroma_client.py',
    detail: '向量召回输出的标准片段，包含 content、file_name、file_id、chunk_id、route、distance。',
    code: '{ id, chunk_id, content, file_name, file_id, route, distance }',
  },
  keywordRecall: {
    title: 'keyword_recall',
    kind: '关键词召回',
    file: 'backend/tool/tools.py',
    detail: '用 query_plan.keywords 和问题本身合并出的关键词，到 MySQL 文件全文中做字面匹配补召回。',
    code: 'keyword_terms = _merge_keywords(query_plan.get("keywords") or [], question)\nkeyword_chunks = keyword_recall(db, knowledge_base_id, keyword_terms, RETRIEVAL_ROUTE_TOP_K)',
  },
  rrfFuse: {
    title: 'rrf_fuse',
    kind: '融合',
    file: 'backend/tool/tools.py',
    detail: '把向量多路线和关键词路线统一融合。一个 chunk 在多条路线靠前出现，rrf_score 就会更高。',
    code: 'fused = rrf_fuse(route_results)',
  },
  chunkKey: {
    title: '_chunk_key',
    kind: '去重键',
    file: 'backend/tool/tools.py',
    detail: '同一个文件同一个 chunk 可能被多条路线召回，用 file_id:chunk_id 合并为同一项。',
    code: 'return f"{chunk.get(\'file_id\', 0)}:{chunk.get(\'chunk_id\') or chunk.get(\'id\')}"',
  },
  rrfScore: {
    title: 'rrf_score',
    kind: '融合分',
    file: 'backend/tool/tools.py',
    detail: 'RRF 不直接看原始相似度，而是看各路线排名。排名越靠前，加分越多。',
    code: 'entry["rrf_score"] += 1.0 / (k + rank)',
  },
  fused: {
    title: 'fused',
    kind: '变量',
    file: 'backend/tool/tools.py',
    detail: '融合后的候选片段列表，按 rrf_score 从高到低排序，并写入 trace["rrf"] 的前 10 项。',
    code: 'return sorted(fused.values(), key=lambda item: item["rrf_score"], reverse=True)',
  },
  rerank: {
    title: 'rerank_chunks',
    kind: '重排',
    file: 'backend/tool/tools.py',
    detail: '把 fused 前 12 个候选压缩成 compact_candidates，交给 LLM 判断与问题的相关度。',
    code: 'reranked, rerank_trace = await rerank_chunks(question, fused[:12])',
  },
  rerankFallback: {
    title: 'rerank fallback',
    kind: '兜底',
    file: 'backend/tool/tools.py',
    detail: '如果没有 chunks，rerank 是 skipped；如果模型失败，状态是 failed。最终会用 reranked or fused 回退到 fused。',
    code: 'return [], {"status": "failed", "error": str(exc), "items": []}\n_select_final_chunks(reranked or fused, keyword_chunks)',
  },
  rerankSuccess: {
    title: 'rerank success',
    kind: '成功分支',
    file: 'backend/tool/tools.py',
    detail: '模型返回 results，每项包含 id、score、reason。代码把 score 写回 chunk 并按 rerank_score 排序。',
    code: 'next_chunk = {**chunk, "rerank_score": score, "rerank_reason": reason}',
  },
  selectFinal: {
    title: '_select_final_chunks',
    kind: '最终选择',
    file: 'backend/tool/tools.py',
    detail: '从 rerank 或 fused 的结果里选最终上下文片段，同时保护关键词强命中的最高分 chunk。',
    code: 'final_chunks = _select_final_chunks(reranked or fused, keyword_chunks)',
  },
  topN: {
    title: 'ranked_chunks[:N]',
    kind: '截断',
    file: 'backend/tool/tools.py',
    detail: '先取排名靠前的 RETRIEVAL_RERANK_TOP_N 个片段，避免把太多上下文塞给回答模型。',
    code: 'selected = list(ranked_chunks[:RETRIEVAL_RERANK_TOP_N])',
  },
  keywordGuard: {
    title: 'keyword guard',
    kind: '保护分支',
    file: 'backend/tool/tools.py',
    detail: '如果关键词召回最高分大于等于 10，且还没被选中，就把它前置，防止数字、制度名等字面命中被语义排序漏掉。',
    code: 'if best_score >= 10 and not already_selected:\n    selected = [best_keyword, *selected]',
  },
  dedupe: {
    title: 'dedupe + trim',
    kind: '收尾',
    file: 'backend/tool/tools.py',
    detail: '按 _chunk_key 去重，最后再次截断到 RETRIEVAL_RERANK_TOP_N。',
    code: 'return deduped[:RETRIEVAL_RERANK_TOP_N]',
  },
  finalChunks: {
    title: 'final_chunks',
    kind: '返回值',
    file: 'backend/tool/tools.py',
    detail: '最终返回给上层的知识片段，后续会被 stream_chat 拼成回答模型可读的上下文。',
    code: 'return final_chunks, trace',
  },
  finalTrace: {
    title: 'trace',
    kind: '返回值',
    file: 'backend/tool/tools.py',
    detail: '保存本轮检索全过程：query_plan、routes、rrf、rerank。学习中心和调试回放可以用它解释结果从哪里来。',
    code: '{ query_plan, routes, rrf, rerank }',
  },
}

const currentNode = computed(() => nodeDetails[selectedId.value] || nodeDetails.inputs)

function selectNode(id) {
  selectedId.value = id
}

const DiagramNode = defineComponent({
  props: {
    id: { type: String, required: true },
    title: { type: String, required: true },
    note: { type: String, default: '' },
    selected: { type: Boolean, default: false },
    strong: { type: Boolean, default: false },
  },
  emits: ['select'],
  setup(props, { emit }) {
    return () =>
      h(
        'button',
        {
          type: 'button',
          class: [
            'mx-auto block w-[560px] border-2 px-5 py-4 text-left transition-colors',
            props.strong ? 'rounded-[28px]' : 'rounded-md',
            props.selected
              ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-100'
              : 'border-slate-900 bg-white hover:bg-slate-50',
          ],
          onClick: () => emit('select', props.id),
        },
        [
          h('div', { class: 'font-mono text-base font-semibold text-slate-900' }, props.title),
          props.note ? h('div', { class: 'mt-2 text-sm leading-5 text-slate-500' }, props.note) : null,
        ]
      )
  },
})

const BranchRow = defineComponent({
  props: {
    topLabel: { type: String, required: true },
    leftId: { type: String, required: true },
    leftTitle: { type: String, required: true },
    leftNote: { type: String, required: true },
    rightId: { type: String, required: true },
    rightTitle: { type: String, required: true },
    rightNote: { type: String, required: true },
    selectedId: { type: String, required: true },
  },
  emits: ['select'],
  setup(props, { emit }) {
    const branchButton = (id, title, note) =>
      h(
        'button',
        {
          type: 'button',
          class: [
            'min-h-[104px] rounded-md border-2 px-4 py-3 text-left transition-colors',
            props.selectedId === id
              ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-100'
              : 'border-slate-900 bg-white hover:bg-slate-50',
          ],
          onClick: () => emit('select', id),
        },
        [
          h('div', { class: 'font-semibold text-slate-900' }, title),
          h('div', { class: 'mt-2 text-sm leading-5 text-slate-500' }, note),
        ]
      )

    return () =>
      h('div', { class: 'mx-auto my-4 w-[760px]' }, [
        h('div', { class: 'mx-auto h-8 w-px bg-slate-900' }),
        h('div', { class: 'text-center text-xs text-slate-500' }, props.topLabel),
        h('div', { class: 'grid grid-cols-[1fr_120px_1fr] items-start' }, [
          h('div', { class: 'border-t-2 border-slate-900 pt-4' }, branchButton(props.leftId, props.leftTitle, props.leftNote)),
          h('div', { class: 'relative h-full border-t-2 border-slate-900' }, [
            h('div', { class: 'mx-auto h-16 w-px bg-slate-900' }),
          ]),
          h('div', { class: 'border-t-2 border-slate-900 pt-4' }, branchButton(props.rightId, props.rightTitle, props.rightNote)),
        ]),
        h('div', { class: 'grid grid-cols-[1fr_120px_1fr] items-end' }, [
          h('div', { class: 'h-10 border-b-2 border-slate-900' }),
          h('div', { class: 'mx-auto h-10 w-px bg-slate-900' }),
          h('div', { class: 'h-10 border-b-2 border-slate-900' }),
        ]),
      ])
  },
})

const MiniFlow = defineComponent({
  props: {
    id: { type: String, required: true },
    title: { type: String, required: true },
    note: { type: String, default: '' },
    selected: { type: Boolean, default: false },
    compact: { type: Boolean, default: false },
  },
  emits: ['select'],
  setup(props, { emit }) {
    return () =>
      h(
        'button',
        {
          type: 'button',
          class: [
            props.compact ? 'min-h-[88px] px-3 py-2' : 'min-h-[116px] px-4 py-3',
            'w-full rounded-md border text-left transition-colors',
            props.selected ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-100' : 'border-slate-300 bg-white hover:border-slate-700',
          ],
          onClick: () => emit('select', props.id),
        },
        [
          h('div', { class: 'font-mono text-sm font-semibold text-slate-900' }, props.title),
          props.note ? h('div', { class: 'mt-2 text-xs leading-5 text-slate-500' }, props.note) : null,
        ]
      )
  },
})

const DownArrow = defineComponent({
  props: {
    label: { type: String, default: '' },
  },
  setup(props) {
    return () =>
      h('div', { class: 'flex flex-col items-center py-2 text-xs text-slate-500' }, [
        props.label ? h('div', { class: 'mb-1 rounded-full bg-white px-2' }, props.label) : null,
        h('div', { class: 'h-8 w-px bg-slate-900' }),
        h('div', { class: 'font-mono text-lg leading-none text-slate-900' }, '↓'),
      ])
  },
})

const Connector = defineComponent({
  props: {
    text: { type: String, default: '->' },
  },
  setup(props) {
    return () => h('div', { class: 'flex items-center justify-center font-mono text-lg text-slate-500' }, props.text)
  },
})

const DetailBlock = defineComponent({
  props: {
    label: { type: String, required: true },
    value: { type: [String, Number], default: '' },
    code: { type: Boolean, default: false },
  },
  setup(props) {
    return () =>
      h('div', { class: 'mb-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2' }, [
        h('div', { class: 'text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400' }, props.label),
        h(
          'div',
          {
            class: [
              'mt-1 whitespace-pre-wrap break-words text-sm leading-6',
              props.code ? 'rounded bg-slate-950 p-2 font-mono text-xs text-slate-100' : 'text-slate-700',
            ],
          },
          String(props.value || '--')
        ),
      ])
  },
})
</script>

<style scoped>
.diagram-paper {
  background-image:
    linear-gradient(rgba(148, 163, 184, 0.12) 1px, transparent 1px),
    linear-gradient(90deg, rgba(148, 163, 184, 0.12) 1px, transparent 1px);
  background-size: 28px 28px;
}
</style>
