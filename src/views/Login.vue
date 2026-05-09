<template>
  <div class="min-h-screen bg-slate-100 flex items-center justify-center p-6">
    <div class="w-full max-w-[420px]">
      <div class="mb-8 text-center">
        <div class="inline-flex h-12 w-12 items-center justify-center rounded-lg bg-brand-600 text-white shadow-panel">
          <el-icon :size="24"><Connection /></el-icon>
        </div>
        <h1 class="mt-4 text-2xl font-semibold text-slate-900">企业知识库智能问答系统</h1>
        <p class="mt-2 text-sm text-slate-500">登录后进入企业知识问答工作台</p>
      </div>

      <el-card shadow="never" class="border border-slate-200">
        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          @keyup.enter="handleLogin"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model.trim="form.username"
              size="large"
              placeholder="请输入用户名"
              :prefix-icon="User"
              autocomplete="username"
            />
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              size="large"
              placeholder="请输入密码"
              show-password
              :prefix-icon="Lock"
              autocomplete="current-password"
            />
          </el-form-item>

          <el-button
            type="primary"
            size="large"
            class="w-full mt-2"
            :loading="loading"
            @click="handleLogin"
          >
            登录
          </el-button>
        </el-form>
      </el-card>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Connection, Lock, User } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const formRef = ref(null)
const loading = ref(false)
const form = reactive({
  username: '',
  password: '',
})

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
}

async function handleLogin() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid || loading.value) return

  loading.value = true
  try {
    await userStore.login(form)
    ElMessage.success('登录成功')
    router.replace(route.query.redirect || '/chat')
  } catch (error) {
    const message =
      error.response?.data?.detail ||
      error.response?.data?.message ||
      '用户名或密码错误'
    ElMessage.error(message)
  } finally {
    loading.value = false
  }
}
</script>
