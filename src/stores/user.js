import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authAPI } from '@/api/auth'
import { userAPI } from '@/api/user'

export const useUserStore = defineStore('user', () => {
  const token = ref(localStorage.getItem('token') || '')
  const username = ref(localStorage.getItem('username') || '')
  const profile = ref(null)

  const isLoggedIn = computed(() => Boolean(token.value))
  const displayName = computed(() => profile.value?.username || username.value || '用户')
  const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase())
  const avatarUrl = computed(() => profile.value?.avatar || '')

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
    return response
  }

  async function uploadAvatar(file) {
    const response = await userAPI.uploadAvatar(file)
    profile.value = {
      ...(profile.value || {}),
      avatar: response.avatar,
    }
    return response
  }

  async function logout() {
    if (token.value) {
      await authAPI.logout().catch(() => {})
    }
    token.value = ''
    username.value = ''
    profile.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('username')
  }

  return {
    token,
    username,
    profile,
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
