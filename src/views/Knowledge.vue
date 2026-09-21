<template>
  <div class="h-full overflow-y-auto bg-slate-50 p-6">
    <div class="mx-auto max-w-7xl">
      <div class="mb-5 flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 class="text-2xl font-semibold text-slate-900">知识库管理</h1>
          <p class="mt-1 text-sm text-slate-500">上传企业资料，为智能问答提供检索依据。</p>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <el-select
            v-model="currentKnowledgeBaseId"
            class="w-48"
            placeholder="选择知识库"
            @change="handleKnowledgeBaseChange"
          >
            <el-option
              v-for="base in knowledgeBases"
              :key="base.id"
              :label="`${base.name}（${base.file_count || 0}）`"
              :value="base.id"
            />
          </el-select>
          <el-button :icon="Plus" @click="openKnowledgeBaseDialog('create')">新建知识库</el-button>
          <el-button :icon="Edit" :disabled="!currentKnowledgeBaseId" @click="openKnowledgeBaseDialog('rename')">
            重命名
          </el-button>
          <el-button
            type="danger"
            :icon="Delete"
            :disabled="knowledgeBases.length <= 1 || !currentKnowledgeBaseId"
            @click="deleteKnowledgeBase"
          >
            删除知识库
          </el-button>
          <el-button :icon="Refresh" @click="fetchFiles">刷新</el-button>
          <input
            ref="uploadInputRef"
            type="file"
            class="hidden"
            :accept="KNOWLEDGE_UPLOAD_ACCEPT"
            multiple
            @change="handleUploadInputChange"
          />
          <el-button
            type="primary"
            :icon="Upload"
            :loading="uploading"
            :disabled="!currentKnowledgeBaseId"
            @click="openUploadDialog"
          >
            {{ uploading ? `上传中 ${uploadPercent}%` : '上传文件' }}
          </el-button>
        </div>
      </div>

      <section class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <el-input
            v-model.trim="keyword"
            placeholder="搜索文件名"
            clearable
            class="md:max-w-sm"
            :prefix-icon="Search"
            @input="handleSearch"
          />

          <div class="flex flex-wrap items-center justify-end gap-2">
            <el-select v-model="sortField" class="w-36" @change="applyFilters">
              <el-option label="上传时间" value="created_at" />
              <el-option label="文件名称" value="name" />
              <el-option label="文件大小" value="size" />
            </el-select>
            <el-button
              type="danger"
              :icon="Delete"
              :disabled="!selectedFiles.length"
              @click="confirmBatchDelete"
            >
              删除选中
            </el-button>
          </div>
        </div>

        <div
          v-if="!allFiles.length && !loading"
          class="rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 py-16 text-center text-slate-500 cursor-pointer hover:border-brand-500 hover:bg-brand-50 transition-colors"
          @click="openUploadDialog"
        >
          <el-icon :size="44" class="mb-3 text-brand-600"><Upload /></el-icon>
          <div class="text-base font-medium text-slate-700">点击上传知识文件</div>
          <div class="mt-1 text-sm">{{ KNOWLEDGE_UPLOAD_HINT }}</div>
        </div>

        <el-table
          v-else
          :data="pagedFiles"
          v-loading="loading"
          stripe
          class="w-full"
          @sort-change="handleSortChange"
          @selection-change="handleSelectionChange"
        >
          <el-table-column type="selection" width="48" />
          <el-table-column label="" width="54">
            <template #default="{ row }">
              <el-icon :size="22" class="text-slate-500">
                <component :is="getFileIcon(row.name)" />
              </el-icon>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="文件名" min-width="240" sortable="custom">
            <template #default="{ row }">
              <el-button link type="primary" class="font-medium" @click="showDetail(row)">
                {{ row.name }}
              </el-button>
            </template>
          </el-table-column>
          <el-table-column prop="size" label="大小" width="130" sortable="custom">
            <template #default="{ row }">{{ formatSize(row.size) }}</template>
          </el-table-column>
          <el-table-column prop="created_at" label="上传时间" width="190" sortable="custom">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :icon="View" @click="showDetail(row)">详情</el-button>
              <el-button link type="danger" :icon="Delete" @click="confirmDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="filteredFiles.length" class="mt-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <span class="text-sm text-slate-500">共 {{ filteredFiles.length }} 个文件</span>
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :page-sizes="[10, 20, 50]"
            :total="filteredFiles.length"
            layout="sizes, prev, pager, next"
          />
        </div>
      </section>
    </div>

    <el-dialog
      v-model="knowledgeBaseDialogVisible"
      :title="knowledgeBaseDialogTitle"
      width="480px"
      top="12vh"
      class="knowledge-base-form-dialog"
      destroy-on-close
      @closed="resetKnowledgeBaseDialog"
    >
      <div class="space-y-5">
        <div class="rounded-lg bg-slate-50 px-4 py-3 text-sm leading-6 text-slate-500">
          {{ knowledgeBaseDialogDescription }}
        </div>

        <el-form
          ref="knowledgeBaseFormRef"
          :model="knowledgeBaseForm"
          :rules="knowledgeBaseRules"
          label-position="top"
          require-asterisk-position="right"
          status-icon
          @submit.prevent="submitKnowledgeBaseDialog"
        >
          <el-form-item label="知识库名称" prop="name">
            <el-input
              ref="knowledgeBaseInputRef"
              v-model="knowledgeBaseForm.name"
              maxlength="100"
              clearable
              placeholder="请输入知识库名称"
            />
          </el-form-item>
        </el-form>
      </div>

      <template #footer>
        <div class="flex justify-end gap-3">
          <el-button @click="knowledgeBaseDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="knowledgeBaseSubmitting" @click="submitKnowledgeBaseDialog">
            {{ knowledgeBaseDialogSubmitText }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="detail.visible" :title="detail.file?.name || '文件详情'" width="760px" top="6vh">
      <div v-if="detail.file" class="space-y-4">
        <div class="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
          <div><span class="text-slate-500">文件名：</span>{{ detail.file.name }}</div>
          <div><span class="text-slate-500">大小：</span>{{ formatSize(detail.file.size) }}</div>
          <div><span class="text-slate-500">上传时间：</span>{{ formatTime(detail.file.created_at) }}</div>
          <div><span class="text-slate-500">类型：</span>{{ getFileExt(detail.file.name) }}</div>
        </div>

        <el-divider />

        <div class="flex items-center justify-between">
          <span class="text-sm font-medium text-slate-700">内容预览</span>
          <el-button size="small" :icon="CopyDocument" :disabled="!detail.content" @click="copyContent">
            复制内容
          </el-button>
        </div>

        <div class="max-h-[420px] overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-4">
          <el-skeleton v-if="detail.loading" :rows="6" animated />
          <!-- 读取失败与「真的没有内容」分开呈现：空态文案不能拿来解释一次失败的读取。 -->
          <pre v-else-if="detail.error" class="m-0 whitespace-pre-wrap font-sans text-sm leading-6 text-red-600">{{ detail.error }}</pre>
          <pre v-else class="m-0 whitespace-pre-wrap font-sans text-sm leading-6 text-slate-700">{{ detail.content || DETAIL_PREVIEW_EMPTY_TEXT }}</pre>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  CopyDocument,
  Delete,
  Document,
  Edit,
  Files,
  Picture,
  Plus,
  Refresh,
  Search,
  Upload,
  View,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { knowledgeAPI } from '@/api/knowledge'
import { useKnowledgeStore } from '@/stores/knowledge'
import { confirmCenteredDelete } from '@/utils/confirm'
import { copyText } from '@/utils/clipboard'
import {
  DETAIL_PREVIEW_EMPTY_TEXT,
  createDetailPreview,
  createDetailPreviewState,
} from '@/utils/detailPreview'
import { createFileListRequest } from '@/utils/fileListRequest'
import { getApiErrorMessage } from '@/utils/httpError'
import {
  DELETE_SUCCEEDED,
  KNOWLEDGE_UPLOAD_ACCEPT,
  KNOWLEDGE_UPLOAD_HINT,
  describeBatchDeleteResult,
  describeSkippedUploadFiles,
  describeUploadFailure,
  describeUploadSuccess,
  hasDeletedAnyFile,
  partitionUploadFiles,
  refreshAfterDelete,
  refreshAfterUpload,
  runConfirmedDelete,
  uploadFilesInOrder,
} from '@/utils/knowledgeFeedback'
import { formatDateTime, formatFileSize } from '@/utils'

const allFiles = ref([])
const currentKnowledgeBaseId = ref(null)
const loading = ref(false)
const keyword = ref('')
const sortField = ref('created_at')
const sortOrder = ref('descending')
const page = ref(1)
const pageSize = ref(10)
const uploading = ref(false)
const uploadPercent = ref(0)
const uploadInputRef = ref(null)
const selectedFiles = ref([])

const knowledgeBaseDialogVisible = ref(false)
const knowledgeBaseSubmitting = ref(false)
const knowledgeBaseDialogMode = ref('create')
const knowledgeBaseFormRef = ref(null)
const knowledgeBaseInputRef = ref(null)
const knowledgeBaseForm = reactive({
  name: '',
})

// 详情预览的状态放响应式容器，请求时序保护在 createDetailPreview 里：
// 标题是同步切换的、正文来自异步响应，迟到的旧响应必须被丢弃（#63）。
const detail = reactive(createDetailPreviewState())
const { open: showDetail, close: closeDetail } = createDetailPreview({
  state: detail,
  // silent：详情读取失败只在预览区给出红字（模板里的 detail.error），
  // 不再叠加 request.js 拦截器的顶部 toast。错误只提示一次（issue #83 第 9 项）。
  fetchContent: (id) => knowledgeAPI.getContent(id, { silent: true }),
})

// 关闭弹窗（点 X / 按 ESC / 点遮罩，或任何把 visible 置回 false 的路径）都要作废在飞请求：
// 关闭后到达的响应不得再写回正文。
watch(
  () => detail.visible,
  (visible) => {
    if (!visible) closeDetail()
  }
)

// 卸载时同样要作废：离开页面后在飞的响应不得再写进已经卸载组件的状态对象（issue #83 第 9 项）。
onBeforeUnmount(() => closeDetail())

const knowledgeStore = useKnowledgeStore()
const knowledgeBases = computed(() => knowledgeStore.knowledgeBases)

const PICTURE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'])
const ARCHIVE_EXTENSIONS = new Set(['zip', 'rar', '7z'])
const knowledgeBaseRules = {
  name: [
    {
      validator: (_rule, value, callback) => {
        if (!String(value || '').trim()) {
          callback(new Error('知识库名称不能为空'))
          return
        }
        callback()
      },
      trigger: ['blur', 'change'],
    },
  ],
}

const filteredFiles = computed(() => {
  const query = keyword.value.toLowerCase()
  const list = allFiles.value.filter((file) => !query || file.name?.toLowerCase().includes(query))

  return list.sort((a, b) => {
    const order = sortOrder.value === 'ascending' ? 1 : -1

    if (sortField.value === 'name') {
      return String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN') * order
    }

    if (sortField.value === 'size') {
      return ((a.size || 0) - (b.size || 0)) * order
    }

    return (new Date(a.created_at || 0) - new Date(b.created_at || 0)) * order
  })
})

const pagedFiles = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredFiles.value.slice(start, start + pageSize.value)
})

const knowledgeBaseDialogTitle = computed(() =>
  knowledgeBaseDialogMode.value === 'create' ? '新建知识库' : '重命名知识库'
)

const knowledgeBaseDialogDescription = computed(() =>
  knowledgeBaseDialogMode.value === 'create'
    ? '创建后可独立上传资料并用于后续问答检索。'
    : '更新当前知识库名称，不会影响其中已上传的资料和已有对话绑定。'
)

const knowledgeBaseDialogSubmitText = computed(() =>
  knowledgeBaseDialogMode.value === 'create' ? '创建' : '保存'
)

onMounted(() => {
  initializeKnowledgeBases()
})

watch([filteredFiles, pageSize], () => {
  const maxPage = Math.max(1, Math.ceil(filteredFiles.value.length / pageSize.value))
  if (page.value > maxPage) {
    page.value = maxPage
  }
})

// 列表取数带请求时序守卫：切库时先发出、后返回的旧库响应不得覆盖当前库的列表
// （issue #83 第 8 项）。守卫本体在 utils/fileListRequest.js，这里只负责接线。
const fileList = createFileListRequest({
  getKnowledgeBaseId: () => currentKnowledgeBaseId.value,
  fetchList: (params) => knowledgeAPI.getList(params),
  applyFiles: (files) => {
    allFiles.value = files
  },
  applyLoading: (value) => {
    loading.value = value
  },
})

// 卸载时作废在飞的列表请求：响应不得再写进已卸载组件的状态对象（issue #83 第 9 项同款口径）。
onBeforeUnmount(() => fileList.invalidate())

async function fetchFiles() {
  return fileList.load()
}

async function initializeKnowledgeBases() {
  const list = await fetchKnowledgeBases()
  currentKnowledgeBaseId.value = resolveKnowledgeBaseId(currentKnowledgeBaseId.value, list)
  await fetchFiles()
}

async function fetchKnowledgeBases() {
  return knowledgeStore.refreshKnowledgeBases()
}

async function handleKnowledgeBaseChange() {
  selectedFiles.value = []
  page.value = 1
  await fetchFiles()
}

function openKnowledgeBaseDialog(mode) {
  const current = knowledgeBases.value.find((item) => item.id === currentKnowledgeBaseId.value)
  if (mode === 'rename' && !current) return

  knowledgeBaseDialogMode.value = mode
  knowledgeBaseForm.name = mode === 'rename' ? current.name : ''
  knowledgeBaseDialogVisible.value = true

  nextTick(() => {
    knowledgeBaseFormRef.value?.clearValidate?.()
    knowledgeBaseInputRef.value?.focus?.()
  })
}

async function submitKnowledgeBaseDialog() {
  if (knowledgeBaseSubmitting.value) return

  const form = knowledgeBaseFormRef.value
  if (!form) return

  const valid = await form.validate().catch(() => false)
  if (!valid) return

  const current = knowledgeBases.value.find((item) => item.id === currentKnowledgeBaseId.value)
  const name = knowledgeBaseForm.name.trim()

  if (knowledgeBaseDialogMode.value === 'rename') {
    if (!current) return
    if (name === current.name) {
      knowledgeBaseDialogVisible.value = false
      return
    }
  }

  knowledgeBaseSubmitting.value = true
  try {
    if (knowledgeBaseDialogMode.value === 'create') {
      const created = await knowledgeAPI.createBase(name, { silent: true })
      knowledgeStore.upsertKnowledgeBase(created)
      currentKnowledgeBaseId.value = created.id
      selectedFiles.value = []
      page.value = 1
      allFiles.value = []
      ElMessage.success('知识库已创建')
      await refreshKnowledgeBasesPreserving(created)
      await fetchFiles()
    } else if (current) {
      const renamed = await knowledgeAPI.renameBase(current.id, name, { silent: true })
      knowledgeStore.upsertKnowledgeBase(renamed)
      ElMessage.success('知识库已重命名')
      await refreshKnowledgeBasesPreserving(renamed)
    }

    knowledgeBaseDialogVisible.value = false
  } catch (error) {
    const message = getApiErrorMessage(error, '操作失败，请稍后重试')
    if (isDuplicateKnowledgeBaseError(error, message)) {
      try {
        await ElMessageBox.alert('知识库已存在', '提示', {
          confirmButtonText: '知道了',
          type: 'warning',
          showClose: false,
          appendTo: 'body',
          customClass: 'knowledge-base-alert-dialog',
        })
      } finally {
        knowledgeBaseDialogVisible.value = false
      }
      return
    }
    ElMessage.error(message)
  } finally {
    knowledgeBaseSubmitting.value = false
  }
}

function isDuplicateKnowledgeBaseError(error, message) {
  return error.response?.status === 400 && String(message || '').includes('知识库名称已存在')
}

function resolveKnowledgeBaseId(preferredId, list = knowledgeBases.value) {
  const bases = Array.isArray(list) ? list : []
  if (preferredId && bases.some((item) => item.id === preferredId)) {
    return preferredId
  }
  return bases[0]?.id || null
}

async function refreshKnowledgeBasesPreserving(preferredBase) {
  try {
    const list = await fetchKnowledgeBases()
    if (preferredBase?.id && !list.some((item) => item.id === preferredBase.id)) {
      knowledgeStore.upsertKnowledgeBase(preferredBase)
    }
  } catch {
    if (preferredBase?.id) {
      knowledgeStore.upsertKnowledgeBase(preferredBase)
    }
  }

  currentKnowledgeBaseId.value = resolveKnowledgeBaseId(preferredBase?.id || currentKnowledgeBaseId.value)
}

function resetKnowledgeBaseDialog() {
  knowledgeBaseSubmitting.value = false
  knowledgeBaseForm.name = ''
  knowledgeBaseFormRef.value?.resetFields?.()
}

// 三个删除入口的错误提示统一走这里，与上传路径的 notifyUploadError 保持同一形态。
function notifyDeleteError(message) {
  ElMessage.error(message)
}

// 上传路径的错误提示出口。刷新失败也走这里，但文案由 describeUploadRefreshFailure 给出，
// 说的是「上传成功，但列表刷新失败」——上传的 catch 只负责上传本身的失败。
function notifyUploadError(message) {
  ElMessage.error(message)
}

async function deleteKnowledgeBase() {
  const current = knowledgeBases.value.find((item) => item.id === currentKnowledgeBaseId.value)
  if (!current) return

  const { status, result } = await runConfirmedDelete({
    confirm: () =>
      confirmCenteredDelete(
        `确定删除知识库「${current.name}」吗？该知识库下的资料会一并删除，已有对话将切换到其他知识库。`,
        '删除知识库'
      ),
    // 与上传路径一致地用 silent 抑制拦截器提示，错误文案由本视图统一给出，避免弹两次。
    remove: () => knowledgeAPI.deleteBase(current.id, { silent: true }),
    notifyError: notifyDeleteError,
  })
  // 取消或失败都不参与成功分支：不提示成功、不切换当前知识库、不刷新。
  // 失败时服务端状态未变，列表仍是真实的，用户可直接重试。
  if (status !== DELETE_SUCCEEDED) return

  ElMessage.success('知识库已删除')
  // 刷新失败不能逃逸成未捕获拒绝，也不能让用户以为删除没成功。
  await refreshAfterDelete({
    refresh: async () => {
      // 选中先落到删除响应给的 fallback 知识库，再刷侧栏列表（返工轮 minor-2）。
      // 这个 id 来自**已成功的删除响应**（后端 delete_knowledge_base 必带 target.id），
      // 不依赖这次刷新；放在 fetchKnowledgeBases() 之后就是整段等它——侧栏一失败，
      // 选中仍停在刚被删掉的知识库上，后续上传/删除都会 404，提示却只说「请手动刷新页面」。
      // 候选列表先剔掉刚删的那个：刷新没成功时手里的还是旧列表，不去掉就可能又选回它。
      currentKnowledgeBaseId.value = resolveKnowledgeBaseId(
        result.fallback_knowledge_base_id,
        knowledgeBases.value.filter((item) => item.id !== current.id)
      )
      await fetchKnowledgeBases()
      await fetchFiles()
    },
    notifyError: notifyDeleteError,
  })
}

function handleSearch() {
  page.value = 1
}

function applyFilters() {
  page.value = 1
}

function handleSortChange({ prop, order }) {
  if (prop) {
    sortField.value = prop
  }
  sortOrder.value = order || 'descending'
  page.value = 1
}

function handleSelectionChange(selection) {
  selectedFiles.value = selection
}

async function handleUploadInputChange(event) {
  const files = Array.from(event.target.files || [])
  event.target.value = ''
  await handleUpload(files)
}

async function handleUpload(files) {
  const uploadFiles = Array.isArray(files) ? files : [files]
  const validFiles = uploadFiles.filter(Boolean)
  if (!validFiles.length || !currentKnowledgeBaseId.value || uploading.value) return

  // 白名单外的文件在选中阶段就跳过并点名，避免提交后被后端 400 拒绝、连带跳过同批合法文件。
  const { supported, rejected } = partitionUploadFiles(validFiles)
  if (rejected.length) {
    ElMessage.warning(describeSkippedUploadFiles(rejected))
  }
  if (!supported.length) return

  let failedIndex = -1
  let attempted = 0
  let uploadedAll = false
  uploading.value = true
  uploadPercent.value = 0

  try {
    await uploadFilesInOrder(
      supported,
      async (file, onProgress) => {
        const index = attempted
        attempted += 1
        try {
          return await knowledgeAPI.upload(file, currentKnowledgeBaseId.value, onProgress, {
            silent: true,
          })
        } catch (error) {
          failedIndex = index
          throw error
        }
      },
      (percent) => {
        uploadPercent.value = percent
      }
    )
    uploadedAll = true
  } catch (error) {
    const reason = getApiErrorMessage(error, '上传失败，请稍后重试')
    if (failedIndex < 0) {
      ElMessage.error(reason)
    } else {
      // 首败即止：失败文件之后的同批文件都没有上传，提示里要说清楚；
      // 选中阶段被跳过的文件同样没上传，一并如实交代。
      ElMessage.error(
        describeUploadFailure(
          supported[failedIndex].name,
          supported.length - failedIndex - 1,
          reason,
          rejected.length
        )
      )
    }
  } finally {
    uploading.value = false
    uploadPercent.value = 0
  }

  // 文件一个都没传成时上面已经提示过了，不再进成功分支。
  if (!uploadedAll) return

  ElMessage.success(describeUploadSuccess(supported.length))
  // 刷新必须留在上面那个 try 之外（issue #154）：留在里面时，刷新自己的拒绝会被上传的
  // catch 接走，而 failedIndex < 0 在上传成功时恒成立，于是逐字弹「上传失败，请稍后重试」——
  // 文件其实已经入库，只是列表没跟上；用户据此重传会再入一份（后端对文件名没有唯一约束）。
  // 走 refreshAfterUpload 后失败只报「上传成功，但列表刷新失败」，上传成功的事实不被改写。
  await refreshAfterUpload({
    refresh: refreshKnowledgeBaseAndFiles,
    notifyError: notifyUploadError,
  })
}

function openUploadDialog() {
  if (!currentKnowledgeBaseId.value || uploading.value) return
  uploadInputRef.value?.click()
}

async function confirmDelete(file) {
  const { status } = await runConfirmedDelete({
    confirm: () => confirmCenteredDelete(`确定删除「${file.name}」吗？删除后不可恢复。`, '删除文件'),
    remove: () => knowledgeAPI.delete(file.id, { silent: true }),
    notifyError: notifyDeleteError,
  })
  if (status !== DELETE_SUCCEEDED) return

  ElMessage.success('删除成功')
  await refreshAfterDelete({
    refresh: refreshKnowledgeBaseAndFiles,
    notifyError: notifyDeleteError,
  })
}

async function confirmBatchDelete() {
  if (!selectedFiles.value.length) return

  const { status, result } = await runConfirmedDelete({
    confirm: () =>
      confirmCenteredDelete(
        `确定删除选中的 ${selectedFiles.value.length} 个资料吗？删除后不可恢复。`,
        '批量删除资料'
      ),
    remove: () => knowledgeAPI.batchDelete(selectedFiles.value.map((file) => file.id), { silent: true }),
    notifyError: notifyDeleteError,
  })
  if (status !== DELETE_SUCCEEDED) return

  const feedback = describeBatchDeleteResult(result)
  ElMessage[feedback.type](feedback.message)

  // 整批失败时也要保留选中状态：此时删掉 0 个，服务端没有变化，用户可直接重试。
  if (!hasDeletedAnyFile(result)) return

  selectedFiles.value = []
  await refreshAfterDelete({
    refresh: refreshKnowledgeBaseAndFiles,
    notifyError: notifyDeleteError,
  })
}

async function refreshKnowledgeBaseAndFiles() {
  await fetchKnowledgeBases()
  await fetchFiles()
}

async function copyContent() {
  await copyText(detail.content, {
    successMessage: '已复制到剪贴板',
    failureMessage: '复制失败，请手动选择内容',
  })
}

function getFileIcon(name) {
  const ext = getFileExt(name).toLowerCase()
  if (PICTURE_EXTENSIONS.has(ext)) return Picture
  if (ARCHIVE_EXTENSIONS.has(ext)) return Files
  return Document
}

function getFileExt(name) {
  return String(name || '').split('.').pop() || 'unknown'
}

const formatSize = formatFileSize
const formatTime = formatDateTime
</script>

<style scoped>
.compact-upload :deep(.el-upload-dragger) {
  padding: 0;
  border: 0;
  background: transparent;
}
</style>

<style>
.knowledge-base-alert-dialog {
  position: fixed;
  top: 50%;
  left: 50%;
  margin: 0;
  padding: 0;
  transform: translate(-50%, -50%);
  width: min(380px, calc(100vw - 32px));
  overflow: hidden;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #ffffff;
  box-shadow: 0 20px 45px rgb(15 23 42 / 16%);
}

.knowledge-base-alert-dialog .el-message-box__header {
  padding: 22px 24px 0;
}

.knowledge-base-alert-dialog .el-message-box__title {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #0f172a;
  font-size: 18px;
  font-weight: 650;
  line-height: 1.35;
}

.knowledge-base-alert-dialog .el-message-box__title::before {
  display: inline-flex;
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: #eff6ff;
  color: #2563eb;
  content: "!";
  font-size: 18px;
  font-weight: 700;
}

.knowledge-base-alert-dialog .el-message-box__content {
  padding: 14px 24px 0;
  color: #475569;
  font-size: 14px;
  line-height: 1.75;
}

.knowledge-base-alert-dialog .el-message-box__container {
  display: block;
}

.knowledge-base-alert-dialog .el-message-box__status {
  display: none;
}

.knowledge-base-alert-dialog .el-message-box__message {
  margin: 0;
}

.knowledge-base-alert-dialog .el-message-box__message p {
  margin: 0;
  word-break: break-word;
}

.knowledge-base-alert-dialog .el-message-box__btns {
  display: flex;
  justify-content: flex-end;
  padding: 22px 24px 24px;
}

.knowledge-base-alert-dialog .el-button {
  min-width: 84px;
  height: 36px;
  border-radius: 6px;
  font-weight: 500;
}

.knowledge-base-alert-dialog .el-button--primary {
  border-color: #2563eb;
  background: #2563eb;
}

.knowledge-base-alert-dialog .el-button--primary:hover,
.knowledge-base-alert-dialog .el-button--primary:focus {
  border-color: #1d4ed8;
  background: #1d4ed8;
}

.knowledge-base-form-dialog {
  width: min(480px, calc(100vw - 32px));
}

.knowledge-base-form-dialog .el-dialog {
  border-radius: 14px;
}

.knowledge-base-form-dialog .el-dialog__header {
  padding: 20px 24px 12px;
}

.knowledge-base-form-dialog .el-dialog__body {
  padding: 0 24px 8px;
}

.knowledge-base-form-dialog .el-dialog__footer {
  padding: 12px 24px 20px;
}

.knowledge-base-form-dialog .el-input {
  width: 100%;
}
</style>
