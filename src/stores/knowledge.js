import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { knowledgeAPI } from '@/api/knowledge'

// 知识库列表分页：与后端 LIST_DEFAULT_LIMIT / LIST_MAX_LIMIT 保持一致（issue #191）。
// 选择器需要完整列表，但**一次请求**只取一页：超出上限的知识库由 loadMoreKnowledgeBases
// 按需追加，界面同时给出「还有更多」的提示，不让它们静默消失。
const KNOWLEDGE_BASE_PAGE_SIZE = 50
const KNOWLEDGE_BASE_FETCH_LIMIT = KNOWLEDGE_BASE_PAGE_SIZE + 1

function splitKnowledgeBasePage(rows) {
  const hasMore = rows.length > KNOWLEDGE_BASE_PAGE_SIZE
  // 后端按创建顺序（旧 -> 新）返回：多出来的那条是最新的一条（在末尾），丢掉它剩下正好一页。
  return { page: hasMore ? rows.slice(0, KNOWLEDGE_BASE_PAGE_SIZE) : rows, hasMore }
}

export const useKnowledgeStore = defineStore('knowledge', () => {
  const knowledgeBases = ref([])
  const loading = ref(false)
  const loadingMore = ref(false)
  const loaded = ref(false)
  const hasMoreKnowledgeBases = ref(false)

  const hasKnowledgeBases = computed(() => knowledgeBases.value.length > 0)

  // 已取回区间的末位 id（不是列表末位）：loadMoreKnowledgeBases 的游标。
  let knowledgeBaseCursor = null

  // 归一化守卫：非对象、以及「是对象但 id 缺失」同样属于脏数据（issue #209）。
  // 后者会穿透成 `{ id: undefined, ... }`：它既没法按 id 合并，也当不了翻页游标，
  // 落进列表就是一条点不开、也没名字的空条目，还会把 hasKnowledgeBases 空态打成 true。
  // 判据只认 `== null`（undefined / null），空串与 0 都是调用方给的明确 id，照常入库。
  function normalizeKnowledgeBase(base) {
    if (!base || typeof base !== 'object' || base.id == null) {
      return null
    }

    return {
      id: base.id,
      name: base.name || '',
      file_count: Number(base.file_count || 0),
      created_at: base.created_at || '',
      updated_at: base.updated_at || '',
    }
  }

  function setKnowledgeBases(list) {
    knowledgeBases.value = Array.isArray(list)
      ? list.map(normalizeKnowledgeBase).filter(Boolean)
      : []
    loaded.value = true
    return knowledgeBases.value
  }

  async function fetchKnowledgeBases(force = false) {
    if (loaded.value && !force) {
      return knowledgeBases.value
    }

    loading.value = true
    try {
      const response = await knowledgeAPI.getBases({ limit: KNOWLEDGE_BASE_FETCH_LIMIT })
      const { page, hasMore } = splitKnowledgeBasePage(Array.isArray(response) ? response : [])
      const bases = setKnowledgeBases(page)
      // 末位 id 是下一页的游标（后端按 id 升序翻页）；末位缺失时不给「加载更多」，
      // 免得给出一个点了没反应的入口。
      knowledgeBaseCursor = bases[bases.length - 1]?.id ?? null
      hasMoreKnowledgeBases.value = hasMore && knowledgeBaseCursor !== null
      return bases
    } finally {
      loading.value = false
    }
  }

  async function refreshKnowledgeBases() {
    return fetchKnowledgeBases(true)
  }

  // 向后追加一页知识库（选择器要的是完整列表，但一次只取一页）。
  // 游标取自**取回的那一页**的末位而不是追加后的列表末位：整页都与已加载内容重复时
  // 追加不会前进，游标必须照常前进，否则这个入口会卡在原地空转。
  async function loadMoreKnowledgeBases() {
    if (!hasMoreKnowledgeBases.value || loadingMore.value || knowledgeBaseCursor === null) return
    loadingMore.value = true
    try {
      const response = await knowledgeAPI.getBases({
        limit: KNOWLEDGE_BASE_FETCH_LIMIT,
        after_id: knowledgeBaseCursor,
      })
      const { page, hasMore } = splitKnowledgeBasePage(Array.isArray(response) ? response : [])
      // 游标与首屏同源：取自**归一化后**那一页的末位，而不是原始末位。取原始末位时，
      // 一个缺 id 的脏行落在页尾就会让游标算不出来（issue #209），这一页之后的内容
      // 从此再也加载不出来 —— 脏行已经被丢掉不入列，却仍旧把翻页入口静默掐断。
      const normalized = page.map(normalizeKnowledgeBase).filter(Boolean)
      const known = new Set(knowledgeBases.value.map((item) => item.id))
      const fresh = normalized.filter((item) => !known.has(item.id))
      if (fresh.length) {
        knowledgeBases.value = [...knowledgeBases.value, ...fresh]
      }
      const nextCursor = normalized[normalized.length - 1]?.id ?? null
      if (nextCursor !== null) knowledgeBaseCursor = nextCursor
      hasMoreKnowledgeBases.value = hasMore && nextCursor !== null
      return fresh
    } finally {
      loadingMore.value = false
    }
  }

  function upsertKnowledgeBase(base) {
    const normalized = normalizeKnowledgeBase(base)
    if (!normalized) {
      return knowledgeBases.value
    }

    const index = knowledgeBases.value.findIndex((item) => item.id === normalized.id)
    if (index >= 0) {
      knowledgeBases.value[index] = {
        ...knowledgeBases.value[index],
        ...normalized,
      }
    } else {
      knowledgeBases.value = [...knowledgeBases.value, normalized]
    }
    loaded.value = true
    return knowledgeBases.value
  }

  return {
    knowledgeBases,
    loading,
    loadingMore,
    loaded,
    hasKnowledgeBases,
    hasMoreKnowledgeBases,
    fetchKnowledgeBases,
    refreshKnowledgeBases,
    loadMoreKnowledgeBases,
    setKnowledgeBases,
    upsertKnowledgeBase,
  }
})
