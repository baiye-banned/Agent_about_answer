import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { knowledgeAPI } from '@/api/knowledge'

export const useKnowledgeStore = defineStore('knowledge', () => {
  const knowledgeBases = ref([])
  const loading = ref(false)
  const loaded = ref(false)

  const hasKnowledgeBases = computed(() => knowledgeBases.value.length > 0)

  function normalizeKnowledgeBase(base) {
    if (!base || typeof base !== 'object') {
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
      const response = await knowledgeAPI.getBases()
      return setKnowledgeBases(response)
    } finally {
      loading.value = false
    }
  }

  async function refreshKnowledgeBases() {
    return fetchKnowledgeBases(true)
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
    loaded,
    hasKnowledgeBases,
    fetchKnowledgeBases,
    refreshKnowledgeBases,
    setKnowledgeBases,
    upsertKnowledgeBase,
  }
})
