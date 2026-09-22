import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authAPI } from '@/api/auth'
import { userAPI } from '@/api/user'
import { normalizeApiAssetUrl } from '@/utils/url'

// 本地头像的路径前缀。与后端 `service/user_service.py` 的 `AVATAR_URL_PREFIX` 是同一个约定，
// 这份判断只用来决定「要不要带着 token 去取」，取不到就退回首字母占位，不影响任何写入。
const LOCAL_AVATAR_PREFIX = '/uploads/'

export const useUserStore = defineStore('user', () => {
  const token = ref(localStorage.getItem('token') || '')
  const username = ref(localStorage.getItem('username') || '')
  const profile = ref(null)
  // 可以直接交给 `<img>` 的地址。本地头像读取面现在要鉴权（issue #186），而 `<img>` 发不出
  // Authorization 头，所以它是带着 token 取回图片后现做的 object URL，不是库里的那个路径。
  const avatarSrc = ref('')

  const isLoggedIn = computed(() => Boolean(token.value))
  const displayName = computed(() => profile.value?.username || username.value || '用户')
  const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase())
  const avatarUrl = computed(() => profile.value?.avatar || '')

  // object URL 不会自己回收：换头像、退出登录时必须显式 revoke，否则每换一次头像就在内存里
  // 留一份图片字节，直到整页刷新。
  let avatarObjectUrl = ''
  // 取数是异步的，「上传完立刻重新取」与「挂载时那次 profile 取数」可能同时在飞。
  // 用递增的序号只认最后发起的那一次，否则先发后到的结果会把新头像盖回旧头像，
  // 或者把一个没人持有引用的 object URL 漏在内存里。
  let avatarLoadId = 0

  function releaseAvatarObjectUrl() {
    if (avatarObjectUrl) {
      URL.revokeObjectURL(avatarObjectUrl)
      avatarObjectUrl = ''
    }
  }

  async function loadAvatarSrc() {
    const path = avatarUrl.value
    const loadId = ++avatarLoadId
    avatarSrc.value = ''
    if (!path) {
      releaseAvatarObjectUrl()
      return
    }
    // 不是本地头像（例如将来换成 OSS 直链）：按原样交给 `<img>`，给它加 Authorization 头
    // 反而会被对方的 CORS 拒掉。
    if (!path.startsWith(LOCAL_AVATAR_PREFIX)) {
      releaseAvatarObjectUrl()
      avatarSrc.value = normalizeApiAssetUrl(path)
      return
    }
    try {
      const blob = await userAPI.getAvatar(path)
      if (loadId !== avatarLoadId) return
      releaseAvatarObjectUrl()
      avatarObjectUrl = URL.createObjectURL(blob)
      avatarSrc.value = avatarObjectUrl
    } catch {
      // 取不到就停在首字母占位（el-avatar 的默认插槽）：头像取不回来不该拖垮资料页其余内容。
    }
  }

  async function login(credentials) {
    const response = await authAPI.login(credentials)
    token.value = response.token
    username.value = response.username || credentials.username
    localStorage.setItem('token', token.value)
    localStorage.setItem('username', username.value)
    await fetchProfile().catch(() => {})
  }

  async function fetchProfile() {
    if (!token.value) return null
    const response = await userAPI.getProfile()
    profile.value = response
    username.value = response.username || username.value
    localStorage.setItem('username', username.value)
    await loadAvatarSrc()
    return response
  }

  async function uploadAvatar(file) {
    const response = await userAPI.uploadAvatar(file)
    profile.value = {
      ...(profile.value || {}),
      avatar: response.avatar,
    }
    await loadAvatarSrc()
    return response
  }

  async function logout() {
    if (token.value) {
      await authAPI.logout().catch(() => {})
    }
    token.value = ''
    username.value = ''
    profile.value = null
    avatarLoadId += 1
    releaseAvatarObjectUrl()
    avatarSrc.value = ''
    localStorage.removeItem('token')
    localStorage.removeItem('username')
  }

  return {
    token,
    username,
    profile,
    avatarSrc,
    isLoggedIn,
    displayName,
    avatarText,
    avatarUrl,
    login,
    fetchProfile,
    uploadAvatar,
    logout,
  }
})
