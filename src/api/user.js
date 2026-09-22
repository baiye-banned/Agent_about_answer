import request from './request'
import { normalizeApiAssetUrl } from '@/utils/url'

export const userAPI = {
  getProfile() {
    return request.get('/user/profile')
  },
  updatePassword(data) {
    return request.put('/user/password', data, { silent: true })
  },
  uploadAvatar(file) {
    const formData = new FormData()
    formData.append('file', file)
    return request.post('/user/avatar', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  // 头像读取面带鉴权（issue #186），而浏览器给 `<img src>` 发请求时不带 Authorization 头，
  // 所以头像路径不能直接交给 el-avatar：这里用与其它接口同一条 axios 实例把图片取成 Blob，
  // 由 store 转成 object URL 再交给 `<img>`。token 因此走请求头，既不进 URL，也不落进
  // 访问日志与 Referer。
  // baseURL 置空：`/uploads/...` 不在 `/api` 前缀下，要与页面同源（部署时由 nginx 把
  // `/uploads/` 代理到后端，开发时 vite.config.js 的 proxy 做同一件事）。
  getAvatar(path) {
    return request.get(normalizeApiAssetUrl(path), {
      baseURL: '',
      responseType: 'blob',
      silent: true,
    })
  },
}
