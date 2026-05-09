<template>
  <el-container class="h-screen bg-slate-100">
    <el-aside width="280px" class="flex flex-col border-r border-slate-200 bg-white">
      <div class="flex h-16 items-center gap-3 border-b border-slate-200 px-5">
        <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-600 text-white">
          <el-icon><Connection /></el-icon>
        </div>
        <div>
          <div class="text-sm font-semibold leading-5 text-slate-900">企业知识库</div>
          <div class="text-xs text-slate-500">智能问答 Agent</div>
        </div>
      </div>

      <el-menu :default-active="activeMenu" :router="true" class="border-none py-3">
        <el-menu-item index="/chat">
          <el-icon><ChatDotSquare /></el-icon>
          <span>智能问答</span>
        </el-menu-item>
        <el-menu-item index="/knowledge">
          <el-icon><FolderOpened /></el-icon>
          <span>知识库管理</span>
        </el-menu-item>
      </el-menu>

      <div class="flex min-h-0 flex-1 flex-col border-t border-slate-200">
        <div class="flex items-center justify-between px-4 py-3">
          <span class="text-xs font-medium text-slate-500">历史对话</span>
          <div class="flex items-center gap-2">
            <el-tooltip :content="chatStore.historyManageMode ? '完成管理' : '管理对话'" placement="right">
              <el-button
                size="small"
                circle
                :icon="Operation"
                :type="chatStore.historyManageMode ? 'primary' : 'default'"
                @click="toggleHistoryManageMode"
              />
            </el-tooltip>
            <el-tooltip content="新建对话" placement="right">
              <el-button size="small" circle :icon="Plus" @click="newConversation" />
            </el-tooltip>
          </div>
        </div>

        <div
          v-if="chatStore.historyManageMode"
          class="mx-3 mb-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-3"
        >
          <div class="flex items-center justify-between gap-3">
            <button
              type="button"
              class="text-xs font-medium text-brand-600 transition-colors hover:text-brand-700"
              @click="chatStore.toggleSelectAllConversations"
            >
              {{ allConversationsSelected ? '取消全选' : '全选' }}
            </button>
            <span class="text-xs text-slate-400">
              已选 {{ chatStore.selectedConversationIds.length }} / {{ chatStore.conversations.length }}
            </span>
          </div>
          <div class="mt-3 flex items-center gap-2">
            <el-button
              size="small"
              type="danger"
              :disabled="!chatStore.selectedConversationIds.length"
              @click="removeSelectedConversations"
            >
              删除选中
            </el-button>
            <el-button size="small" @click="chatStore.exitHistoryManageMode()">完成</el-button>
          </div>
        </div>

        <div v-loading="chatStore.loading" class="flex-1 min-h-0 overflow-y-auto px-2 pb-3">
          <button
            v-for="conversation in chatStore.conversations"
            :key="conversation.id"
            type="button"
            :class="[
              'group flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors',
              chatStore.isConversationSelected(conversation.id)
                ? 'bg-brand-50 text-brand-700 ring-1 ring-brand-200'
                : conversation.id === chatStore.currentId
                ? 'bg-brand-50 text-brand-700'
                : 'text-slate-600 hover:bg-slate-100',
            ]"
            @click="handleConversationClick(conversation.id)"
          >
            <el-checkbox
              v-if="chatStore.historyManageMode"
              :model-value="chatStore.isConversationSelected(conversation.id)"
              @click.stop
              @change="chatStore.toggleConversationSelection(conversation.id)"
            />
            <el-icon :size="15"><ChatLineRound /></el-icon>
            <span class="flex-1 truncate">{{ conversation.title || '未命名对话' }}</span>
            <el-popconfirm
              v-if="!chatStore.historyManageMode"
              title="确定删除该对话吗？"
              confirm-button-text="删除"
              cancel-button-text="取消"
              confirm-button-type="danger"
              placement="right"
              width="190"
              @confirm="removeConversation(conversation.id)"
            >
              <template #reference>
                <el-button
                  link
                  type="danger"
                  size="small"
                  :icon="Delete"
                  class="opacity-0 group-hover:opacity-100"
                  @click.stop
                />
              </template>
            </el-popconfirm>
          </button>

          <div
            v-if="!chatStore.conversations.length && !chatStore.loading"
            class="px-4 py-8 text-center text-xs text-slate-400"
          >
            暂无历史对话
          </div>
        </div>
      </div>

      <div class="border-t border-slate-200 p-4">
        <el-dropdown trigger="click" @command="handleUserCommand">
          <button type="button" class="flex w-full items-center gap-3 rounded-md p-2 text-left hover:bg-slate-100">
            <el-avatar :size="32" :src="avatarSrc">{{ userStore.avatarText }}</el-avatar>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm font-medium text-slate-800">{{ userStore.displayName }}</span>
              <span class="block text-xs text-slate-500">工作台账户</span>
            </span>
            <el-icon class="text-slate-400"><ArrowDown /></el-icon>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="profile">个人设置</el-dropdown-item>
              <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-aside>

    <el-container class="min-w-0">
      <el-main class="min-w-0 p-0">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowDown,
  ChatDotSquare,
  ChatLineRound,
  Connection,
  Delete,
  FolderOpened,
  Operation,
  Plus,
} from '@element-plus/icons-vue'
import { ElMessageBox } from 'element-plus'
import { useChatStore } from '@/stores/chat'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const chatStore = useChatStore()
const userStore = useUserStore()

const activeMenu = computed(() => (route.path.startsWith('/knowledge') ? '/knowledge' : '/chat'))
const avatarSrc = computed(() => normalizeAvatarUrl(userStore.avatarUrl))
const allConversationsSelected = computed(() =>
  Boolean(chatStore.conversations.length) &&
  chatStore.selectedConversationIds.length === chatStore.conversations.length
)

onMounted(() => {
  userStore.fetchProfile().catch(() => {})
  chatStore.fetchConversations()
})

function newConversation() {
  chatStore.exitHistoryManageMode()
  chatStore.clearMessages()
  router.push('/chat')
}

function selectConversation(id) {
  chatStore.exitHistoryManageMode()
  router.push(`/chat/${id}`)
  chatStore.selectConversation(id)
}

function handleConversationClick(id) {
  if (chatStore.historyManageMode) {
    chatStore.toggleConversationSelection(id)
    return
  }
  selectConversation(id)
}

async function removeConversation(id) {
  await chatStore.deleteConversation(id)
  if (route.params.id === id) {
    router.push('/chat')
  }
}

function toggleHistoryManageMode() {
  if (chatStore.historyManageMode) {
    chatStore.exitHistoryManageMode()
    return
  }
  chatStore.enterHistoryManageMode()
}

async function removeSelectedConversations() {
  if (!chatStore.selectedConversationIds.length) return

  await ElMessageBox.confirm(
    `确定删除选中的 ${chatStore.selectedConversationIds.length} 条历史对话吗？删除后不可恢复。`,
    '批量删除对话',
    {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )

  const deletingIds = [...chatStore.selectedConversationIds]
  for (const id of deletingIds) {
    await chatStore.deleteConversation(id)
    if (route.params.id === id) {
      router.push('/chat')
    }
  }
  chatStore.exitHistoryManageMode()
}

function normalizeAvatarUrl(url) {
  if (!url) return ''
  if (/^https?:\/\//.test(url)) return url
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || '/api'
  const origin = apiBaseUrl.replace(/\/api\/?$/, '')
  return `${origin}${url}`
}

async function handleUserCommand(command) {
  if (command === 'profile') {
    router.push('/profile')
    return
  }

  if (command === 'logout') {
    await userStore.logout()
    chatStore.clearMessages()
    chatStore.exitHistoryManageMode()
    router.replace('/login')
  }
}
</script>
