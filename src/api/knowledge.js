import request from './request'

export const knowledgeAPI = {
  getList(params) {
    return request.get('/knowledge', { params })
  },
  getBases() {
    return request.get('/knowledge-bases')
  },
  createBase(name) {
    return request.post('/knowledge-bases', { name })
  },
  renameBase(id, name) {
    return request.put(`/knowledge-bases/${id}`, { name })
  },
  deleteBase(id) {
    return request.delete(`/knowledge-bases/${id}`)
  },
  getDetail(id) {
    return request.get(`/knowledge/${id}`)
  },
  getContent(id) {
    return request.get(`/knowledge/${id}/content`)
  },
  upload(file, knowledgeBaseId, onProgress) {
    const formData = new FormData()
    formData.append('file', file)
    if (knowledgeBaseId) {
      formData.append('knowledge_base_id', knowledgeBaseId)
    }

    return request.post('/knowledge/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: onProgress,
    })
  },
  delete(id) {
    return request.delete(`/knowledge/${id}`)
  },
  batchDelete(ids) {
    return Promise.all(ids.map((id) => request.delete(`/knowledge/${id}`)))
  },
}
