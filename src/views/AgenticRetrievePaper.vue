<template>
  <div class="h-full overflow-hidden rounded-2xl border border-slate-200 bg-slate-100 p-4">
    <div class="flex h-full min-w-[1180px] flex-col rounded-[10px] border border-slate-300 bg-white px-8 py-7 shadow-sm">
      <header class="mb-5 flex shrink-0 items-start justify-between gap-6 border-b border-slate-200 pb-4">
        <div>
          <h2 class="text-2xl font-semibold tracking-normal text-slate-900">agentic_retrieve_knowledge</h2>
          <p class="mt-3 max-w-4xl text-sm leading-6 text-slate-500">
            按你纸上的结构整理：主线从上到下走，遇到失败/重试/接受结果时左右分叉，再回到最终返回。
          </p>
        </div>
        <div class="w-80 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800">
          这张图只改前端展示，不改变后端 agentic_retrieve_knowledge 的真实实现。
        </div>
      </header>

      <div class="grid min-h-0 flex-1 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <main class="diagram-paper h-full overflow-auto rounded-md border-2 border-slate-700 bg-white px-8 py-7">
          <div class="mx-auto min-w-[780px] max-w-[980px]">
            <DiagramNode id="fromStreamChat" title="stream_chat 中 need_rag = true" note="进入 RAG 分支" :selected="selectedId === 'fromStreamChat'" @select="selectNode" />
            <DownArrow label="调用" />
            <DiagramNode id="inputs" title="agentic_retrieve_knowledge(...)" note="question / knowledge_base_id / db / rag_gate / memory_context / trace_recorder" strong :selected="selectedId === 'inputs'" @select="selectNode" />
            <DownArrow label="先建执行记录" />
            <DiagramNode id="traceInit" title="创建 agent_trace" note="planner / steps / reflections / final，最多 2 轮" :selected="selectedId === 'traceInit'" @select="selectNode" />
            <DownArrow label="让 Agent 先想怎么搜" />
            <DiagramNode id="runAgent" title="_run_langchain_agent" note="create_agent + DeepSeek + LANGCHAIN_RETRIEVAL_TOOLS" :selected="selectedId === 'runAgent'" @select="selectNode" />

            <BranchRow
              top-label="规划结果分叉"
              left-id="fallbackPlan"
              left-title="规划失败"
              left-note="fallback：原问题检索 1 轮"
              right-id="agentPlan"
              right-title="规划成功"
              right-note="得到 queries + max_rounds"
              :selected-id="selectedId"
              @select="selectNode"
            />

            <DownArrow label="两条路都收束到可执行计划" />
            <DiagramNode id="normalizePlan" title="规范化 planned_queries / max_rounds" note="queries 不合法就退回原问题；max_rounds 限制在 1~2" :selected="selectedId === 'normalizePlan'" @select="selectNode" />
            <DownArrow label="进入检索循环" />

            <section class="rounded-[28px] border-2 border-slate-800 bg-slate-50 p-5">
              <button
                type="button"
                class="block w-full text-left"
                @click="selectNode('loop')"
              >
                <div class="text-base font-semibold text-slate-900">for round_index in range(1, max_rounds + 1)</div>
                <div class="mt-1 font-mono text-sm text-slate-600">current_query = planned_queries[0] 或重试 query</div>
              </button>

              <div class="mt-5 grid grid-cols-[minmax(0,1fr)_36px_minmax(0,1fr)_36px_minmax(0,1fr)] items-stretch gap-3">
                <MiniFlow id="runtime" title="retrieval_runtime" note="放入 db / knowledge_base_id / trace" :selected="selectedId === 'runtime'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="retrieveCall" title="retrieve_knowledge" note="真正查知识库" :selected="selectedId === 'retrieveCall'" @select="selectNode" />
                <Connector text="->" />
                <MiniFlow id="attempts" title="attempts.append" note="保存本轮 chunks / trace / score" :selected="selectedId === 'attempts'" @select="selectNode" />
              </div>

              <div class="mt-5 border-t border-dashed border-slate-300 pt-5">
                <button
                  type="button"
                  class="mb-3 font-mono text-sm font-semibold text-slate-900 underline decoration-slate-300 underline-offset-4"
                  @click="selectNode('retrieveInside')"
                >
                  retrieve_knowledge 内部像这样展开
                </button>
                <div class="grid grid-cols-[minmax(0,1fr)_28px_minmax(0,1fr)_28px_minmax(0,1fr)] gap-3">
                  <MiniFlow id="queryPlan" title="build_query_plan" note="HyDE / rewrite / keywords" compact :selected="selectedId === 'queryPlan'" @select="selectNode" />
                  <Connector text="->" />
                  <div class="space-y-3">
                    <MiniFlow id="vectorRecall" title="query_vectors" note="original / HyDE / rewrite 查 Chroma" compact :selected="selectedId === 'vectorRecall'" @select="selectNode" />
                    <MiniFlow id="keywordRecall" title="keyword_recall" note="关键词查 MySQL 文件文本" compact :selected="selectedId === 'keywordRecall'" @select="selectNode" />
                  </div>
                  <Connector text="->" />
                  <div class="space-y-3">
                    <MiniFlow id="rrf" title="rrf_fuse" note="多路召回结果融合" compact :selected="selectedId === 'rrf'" @select="selectNode" />
                    <MiniFlow id="rerank" title="rerank_chunks" note="LLM 对候选 chunk 重排" compact :selected="selectedId === 'rerank'" @select="selectNode" />
                  </div>
                </div>
              </div>
            </section>

            <DownArrow label="检索完后做质量判断" />
            <DiagramNode id="reflection" title="_reflect_attempt / _quality_score" note="看 chunk 数量、rerank 分数、路线数，判断是否需要重试" :selected="selectedId === 'reflection'" @select="selectNode" />

            <BranchRow
              top-label="质量判断分叉"
              left-id="retryBranch"
              left-title="质量弱且还有轮次"
              left-note="换 query，再回到 retrieve_knowledge"
              right-id="acceptedBranch"
              right-title="质量够或没轮次了"
              right-note="接受当前检索结果"
              :selected-id="selectedId"
              @select="selectNode"
            />

            <div class="my-2 grid grid-cols-[1fr_160px_1fr] items-center gap-3 text-xs text-slate-500">
              <div class="h-px bg-slate-300"></div>
              <button
                type="button"
                class="rounded-full border border-amber-300 bg-amber-50 px-3 py-1 text-amber-800"
                @click="selectNode('nextQuery')"
              >
                重试时 current_query = planned_retry_query or next_query
              </button>
              <div class="h-px bg-slate-300"></div>
            </div>

            <DownArrow label="所有轮次结束" />
            <DiagramNode id="selectBest" title="best = max(attempts, key=score)" note="不一定选最后一轮，而是选质量分最高的一轮" :selected="selectedId === 'selectBest'" @select="selectNode" />
            <DownArrow label="最终返回给 stream_chat" />

            <div class="grid grid-cols-[minmax(0,1fr)_44px_minmax(0,1fr)] items-stretch gap-3">
              <DiagramNode id="selectedChunks" title="selected_chunks" note="后面拼成 context 给回答模型" :selected="selectedId === 'selectedChunks'" @select="selectNode" />
              <Connector text="+" />
              <DiagramNode id="returnTrace" title="final_trace" note="保存 query_plan / routes / rrf / rerank / agent_trace" :selected="selectedId === 'returnTrace'" @select="selectNode" />
            </div>

            <div class="mt-5 rounded-md border border-brand-200 bg-brand-50 px-4 py-3 text-center text-sm font-semibold text-brand-800">
              最朴素的话：它不写答案，只负责先想搜索词，再去知识库找资料；找得不好就换个词再找一次。
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
  fromStreamChat: {
    title: 'stream_chat -> agentic_retrieve_knowledge',
    kind: '入口',
    file: 'backend/service/chat_service.py',
    detail: 'stream_chat 先完成认证、建对话、构建记忆、判断 need_rag。只有 need_rag 为 true 时才进入这里。',
    code: 'knowledge_chunks, retrieval_trace = await agentic_retrieve_knowledge(...)',
  },
  inputs: {
    title: 'agentic_retrieve_knowledge inputs',
    kind: '入参',
    file: 'backend/agent/agent.py',
    detail: 'question 是 retrieval_question，knowledge_base_id 限定当前知识库，db 用于关键词召回，memory_context 给规划参考。',
    code: 'question, knowledge_base_id, db, rag_gate, memory_context, trace_recorder',
  },
  traceInit: {
    title: 'agent_trace',
    kind: '变量',
    file: 'backend/agent/agent.py',
    detail: '记录 Agent 规划、每轮工具调用、质量反思和最终选择，最后挂到 final_trace["agent"]。',
    code: '{ framework: "langchain", max_rounds: 2, planner: {}, steps: [], reflections: [], final: {} }',
  },
  runAgent: {
    title: '_run_langchain_agent',
    kind: '方法',
    file: 'backend/agent/agent.py',
    detail: '用 LangChain create_agent 调 DeepSeek，让模型先输出一个很小的检索计划。',
    code: 'create_agent(get_deepseek_model(...), tools=LANGCHAIN_RETRIEVAL_TOOLS)',
  },
  agentPlan: {
    title: 'agent_plan',
    kind: '规划成功',
    file: 'backend/agent/agent.py',
    detail: '模型返回 queries、max_rounds 和 reason。后续会规范化，最多只允许 2 轮。',
    code: '{ should_retrieve: true, queries: ["..."], max_rounds: 1~2, reason: "..." }',
  },
  fallbackPlan: {
    title: '_fallback_plan',
    kind: '兜底',
    file: 'backend/agent/agent.py',
    detail: 'Agent 规划失败时，不让主流程中断；直接用原问题检索 1 轮。',
    code: '{ should_retrieve: true, queries: [question], max_rounds: 1, source: "fallback" }',
  },
  normalizePlan: {
    title: 'normalize plan',
    kind: '安全处理',
    file: 'backend/agent/agent.py',
    detail: '清理模型输出，确保 queries 有值，max_rounds 不小于 1、不大于 2。',
    code: 'planned_queries = _normalize_queries(...)\nmax_rounds = max(1, min(MAX_AGENT_ROUNDS, ...))',
  },
  loop: {
    title: 'retrieval loop',
    kind: '循环',
    file: 'backend/agent/agent.py',
    detail: '按 planned query 执行 1~2 轮检索。每轮都会保存 chunks、trace 和质量分。',
    code: 'for round_index in range(1, max_rounds + 1):',
  },
  runtime: {
    title: 'retrieval_runtime',
    kind: '上下文',
    file: 'backend/tool/tools.py',
    detail: '让 LangChain tool 能拿到当前 db、knowledge_base_id 和 trace_recorder。',
    code: 'with retrieval_runtime(db, knowledge_base_id, trace_recorder):',
  },
  retrieveCall: {
    title: 'retrieve_knowledge',
    kind: '工具',
    file: 'backend/tool/tools.py',
    detail: '真正查知识库：先规划查询，再多路召回，最后融合和重排。',
    code: 'chunks, retrieval_trace = await retrieve_knowledge(current_query, ...)',
  },
  attempts: {
    title: 'attempts',
    kind: '变量',
    file: 'backend/agent/agent.py',
    detail: '保存每轮完整检索结果，后面从中挑质量分最高的一轮。',
    code: '{ round, query, chunks, retrieval_trace, score }',
  },
  retrieveInside: {
    title: 'retrieve_knowledge 内部',
    kind: '子流程',
    file: 'backend/tool/tools.py',
    detail: '这一块是 retrieve_knowledge 的压缩版，完整展开仍在学习中心 retrieve_knowledge 图页。',
    code: 'build_query_plan -> query_vectors / keyword_recall -> rrf_fuse -> rerank_chunks -> final_chunks',
  },
  queryPlan: {
    title: 'build_query_plan',
    kind: '查询规划',
    file: 'backend/tool/tools.py',
    detail: '让模型生成 HyDE 假设文档、多个 rewrite query 和 keywords。',
    code: '{ hyde_document, rewrites, keywords, error }',
  },
  vectorRecall: {
    title: 'query_vectors',
    kind: '向量召回',
    file: 'backend/rag/chroma_client.py',
    detail: '用 original、HyDE、rewrite 去 ChromaDB 中按语义相似度找 chunk，并用 knowledge_base_id 隔离知识库。',
    code: 'collection.query(query_texts=[query], where={ knowledge_base_id })',
  },
  keywordRecall: {
    title: 'keyword_recall',
    kind: '关键词召回',
    file: 'backend/tool/tools.py',
    detail: '用关键词从 MySQL 的 KnowledgeFile.content 里补召回，弥补纯向量检索可能漏掉的字面命中。',
    code: 'db.query(KnowledgeFile).filter_by(knowledge_base_id=knowledge_base_id)',
  },
  rrf: {
    title: 'rrf_fuse',
    kind: '融合',
    file: 'backend/tool/tools.py',
    detail: '把 original、HyDE、rewrite、keyword 的结果做多路投票式融合。',
    code: 'entry["rrf_score"] += 1.0 / (k + rank)',
  },
  rerank: {
    title: 'rerank_chunks',
    kind: '重排',
    file: 'backend/tool/tools.py',
    detail: '让 LLM 判断候选 chunk 与问题的相关度，并按 score 排序。',
    code: 'results: [{ id, score, reason }]',
  },
  reflection: {
    title: '_reflect_attempt / _quality_score',
    kind: '反思',
    file: 'backend/agent/agent.py',
    detail: '对本轮结果打分。看 chunk 数量、rerank 分数、route 数量和 rerank 状态。',
    code: 'should_retry = can_retry and score < MIN_QUALITY_SCORE',
  },
  retryBranch: {
    title: 'retry branch',
    kind: '分支',
    file: 'backend/agent/agent.py',
    detail: '如果质量分低于 0.58 且还没到最大轮次，就换一个 query 再检索一次。',
    code: 'current_query = planned_retry_query or reflection["next_query"] or question',
  },
  acceptedBranch: {
    title: 'accepted branch',
    kind: '分支',
    file: 'backend/agent/agent.py',
    detail: '如果质量够，或者已经没有轮次，就不再重试。',
    code: 'if not reflection["should_retry"]: break',
  },
  nextQuery: {
    title: 'next_query',
    kind: '重试 query',
    file: 'backend/agent/agent.py',
    detail: '重试优先用 Agent 一开始规划好的第二个 query；没有的话，才用关键词补强生成的 next_query。',
    code: 'planned_retry_query or reflection["next_query"] or question',
  },
  selectBest: {
    title: 'select best attempt',
    kind: '收束',
    file: 'backend/agent/agent.py',
    detail: '循环结束后，不一定选最后一轮，而是从 attempts 中选 score 最高的一轮。',
    code: 'best = max(attempts, key=lambda item: item["score"])',
  },
  selectedChunks: {
    title: 'selected_chunks',
    kind: '返回值',
    file: 'backend/agent/agent.py',
    detail: '最终给 stream_chat 拼成 context，并进入最终回答模型的知识库上下文。',
    code: 'selected_chunks = best["chunks"]',
  },
  returnTrace: {
    title: 'final_trace',
    kind: '返回值',
    file: 'backend/agent/agent.py',
    detail: '保存 query_plan、routes、rrf、rerank 和 agent_trace，用于学习中心和调试追踪。',
    code: 'final_trace["agent"] = agent_trace',
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
