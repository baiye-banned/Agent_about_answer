import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { getApiErrorMessage } from '@/utils/httpError'

const request = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 30000,
})

request.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

request.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const status = error.response?.status
    const message = getApiErrorMessage(error)
    const silent = Boolean(error.config?.silent)

    if (status === 401) {
      if (router.currentRoute.value.path !== '/login') {
        localStorage.removeItem('token')
        localStorage.removeItem('username')
        router.replace('/login')
        if (!silent) {
          ElMessage.warning('登录已过期，请重新登录')
        }
      } else if (!silent) {
        ElMessage.error(message || '用户名或密码错误')
      }
    } else if (!silent) {
      ElMessage.error(message)
    }

    return Promise.reject(error)
  }
)

export default request
