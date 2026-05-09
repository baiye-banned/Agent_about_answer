import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { knowledgeAPI } from '@/api/knowledge'

export const useKnowledgeStore = defineStore('knowledge', () => {
  const knowledgeBases = ref([])
  const loading = ref(false)
  const loaded = ref(false)

  const hasKnowledgeBases = computed(() => knowledgeBases.value.length > 0)

  async function fetchKnowledgeBases(force = false) {
    if (loaded.value && !force) {
      return knowledgeBases.value
    }

    loading.value = true
    try {
      const response = await knowledgeAPI.getBases()
      knowledgeBases.value = Array.isArray(response) ? response : []
      loaded.value = true
      return knowledgeBases.value
    } finally {
      loading.value = false
    }
  }

  async function refreshKnowledgeBases() {
    return fetchKnowledgeBases(true)
  }

  return {
    knowledgeBases,
    loading,
    loaded,
    hasKnowledgeBases,
    fetchKnowledgeBases,
    refreshKnowledgeBases,
  }
})
