<template>
  <div class="h-full overflow-y-auto bg-slate-50 p-6">
    <div class="mx-auto max-w-4xl">
      <div class="mb-6">
        <h1 class="text-2xl font-semibold text-slate-900">个人设置</h1>
        <p class="mt-1 text-sm text-slate-500">查看账号信息并维护登录密码。</p>
      </div>

      <div class="grid grid-cols-1 gap-5 lg:grid-cols-[320px_1fr]">
        <el-card shadow="never" class="border border-slate-200">
          <template #header>
            <span class="font-medium text-slate-800">账号信息</span>
          </template>

          <div class="flex items-center gap-4">
            <el-upload
              :show-file-list="false"
              :before-upload="handleAvatarUpload"
              accept="image/png,image/jpeg,image/webp"
            >
              <button
                type="button"
                class="relative rounded-full outline-none ring-brand-500 hover:ring-2 focus:ring-2"
                title="点击上传头像"
              >
                <el-avatar :size="60" :src="avatarSrc">{{ userStore.avatarText }}</el-avatar>
                <span class="absolute inset-x-0 bottom-0 rounded-b-full bg-slate-900/60 py-0.5 text-[10px] text-white">
                  更换
                </span>
              </button>
            </el-upload>
            <div class="min-w-0">
              <div class="truncate text-lg font-semibold text-slate-900">{{ userStore.displayName }}</div>
              <div class="mt-1 text-sm text-slate-500">企业知识库用户</div>
            </div>
          </div>

          <dl class="mt-6 space-y-3 text-sm">
            <div class="flex justify-between gap-4">
              <dt class="text-slate-500">用户名</dt>
              <dd class="font-medium text-slate-800">{{ userStore.username || '-' }}</dd>
            </div>
            <div class="flex justify-between gap-4">
              <dt class="text-slate-500">创建时间</dt>
              <dd class="text-slate-800">{{ formatTime(userStore.profile?.created_at) }}</dd>
            </div>
          </dl>
        </el-card>

        <el-card shadow="never" class="border border-slate-200">
          <template #header>
            <span class="font-medium text-slate-800">修改密码</span>
          </template>

          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            label-position="top"
            class="max-w-lg"
            @keyup.enter="handleSubmit"
          >
            <el-form-item label="当前密码" prop="oldPassword">
              <el-input
                v-model="form.oldPassword"
                type="password"
                show-password
                placeholder="请输入当前密码"
                autocomplete="current-password"
              />
            </el-form-item>

            <el-form-item label="新密码" prop="newPassword">
              <el-input
                v-model="form.newPassword"
                type="password"
                show-password
                placeholder="请输入新密码"
                autocomplete="new-password"
              />
            </el-form-item>

            <el-form-item label="确认新密码" prop="confirmPassword">
              <el-input
                v-model="form.confirmPassword"
                type="password"
                show-password
                placeholder="请再次输入新密码"
                autocomplete="new-password"
              />
            </el-form-item>

            <el-button type="primary" :loading="submitting" @click="handleSubmit">
              保存修改
            </el-button>
          </el-form>
        </el-card>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { userAPI } from '@/api/user'

const userStore = useUserStore()
const formRef = ref(null)
const submitting = ref(false)
const avatarSrc = computed(() => normalizeAvatarUrl(userStore.avatarUrl))

const form = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})

const rules = {
  oldPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请确认新密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== form.newPassword) {
          callback(new Error('两次输入的密码不一致'))
          return
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
}

onMounted(() => {
  userStore.fetchProfile().catch(() => {})
})

async function handleSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid || submitting.value) return

  submitting.value = true
  try {
    await userAPI.updatePassword({
      old_password: form.oldPassword,
      new_password: form.newPassword,
    })
    ElMessage.success('密码修改成功')
    form.oldPassword = ''
    form.newPassword = ''
    form.confirmPassword = ''
    formRef.value?.clearValidate()
  } catch (error) {
    const message =
      error.response?.data?.detail ||
      error.response?.data?.message ||
      '密码修改失败，请检查当前密码后重试'
    ElMessage.error(message)
  } finally {
    submitting.value = false
  }
}

async function handleAvatarUpload(file) {
  const allowedTypes = ['image/png', 'image/jpeg', 'image/webp']
  if (!allowedTypes.includes(file.type)) {
    ElMessage.error('仅支持 png、jpg、jpeg、webp 格式头像')
    return false
  }

  if (file.size > 2 * 1024 * 1024) {
    ElMessage.error('头像文件不能超过 2MB')
    return false
  }

  try {
    await userStore.uploadAvatar(file)
    ElMessage.success('头像已更新')
  } catch {
    // request interceptor already shows the backend message
  }

  return false
}

function normalizeAvatarUrl(url) {
  if (!url) return ''
  if (/^https?:\/\//.test(url)) return url
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || '/api'
  const origin = apiBaseUrl.replace(/\/api\/?$/, '')
  return `${origin}${url}`
}

function formatTime(value) {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
</script>
