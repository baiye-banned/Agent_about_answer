// 知识库视图（Knowledge.vue）的批量操作反馈、删除确认编排与顺序上传编排。
// 抽成纯函数/可注入依赖的形式，便于在纯 Node 下做行为级断言（见 tests/knowledgeFeedback.test.js）。

import { getApiErrorMessage } from './httpError.js'

// 删除入口共用的三态结果。调用方只在 'succeeded' 时进入成功分支（刷新、清空选中、成功提示）。
export const DELETE_CANCELLED = 'cancelled'
export const DELETE_FAILED = 'failed'
export const DELETE_SUCCEEDED = 'succeeded'

// ElMessageBox 的确认 Promise 在用户放弃时以字符串 'cancel' 拒绝；未开
// distinguishCancelAndClose 时点遮罩/按 ESC 的 'close' 也归一成 'cancel'
// （element-plus 2.13.7 messageBox.mjs）。两者都属于"用户主动放弃"，静默返回。
export function isConfirmCancellation(error) {
  return error === 'cancel' || error === 'close'
}

// 删除入口共用的「确认 → 执行 → 反馈」三段式编排：
//   { status: 'cancelled' }          用户取消/关闭确认框：静默返回，不提示、不执行删除
//   { status: 'failed', error }      拒绝：调用 notifyError 给出错误提示，不进入成功分支
//   { status: 'succeeded', result }  成功：result 为 remove() 的返回值，由调用方决定刷新与文案
// 取消与失败都不抛错，因此不会沿事件处理器冒泡成未捕获的 Promise 拒绝；
// 两者也都不改动调用方状态，列表与选中状态原样保留，用户可以直接重试。
export async function runConfirmedDelete({
  confirm,
  remove,
  notifyError,
  failureMessage = '删除失败，请稍后重试',
}) {
  // 确认与执行两段共用同一套失败反馈：错误文案优先取接口 detail/message，缺失时回落到 failureMessage。
  const fail = (error) => {
    notifyError?.(getApiErrorMessage(error, failureMessage))
    return { status: DELETE_FAILED, error }
  }

  try {
    await confirm()
  } catch (error) {
    // 取消是正常路径，不是失败：既不提示也不执行 remove。
    if (isConfirmCancellation(error)) return { status: DELETE_CANCELLED }
    // 其余拒绝（例如确认框自身异常）按失败提示，同样不得逃逸成未捕获拒绝。
    return fail(error)
  }

  try {
    return { status: DELETE_SUCCEEDED, result: await remove() }
  } catch (error) {
    return fail(error)
  }
}

// 批量删除后是否还要清空选中并刷新：只有真的删掉了资料才清。
// batchDelete 用 allSettled 收敛，整批失败时不会拒绝而是回一个 succeeded=0 的结果，
// 此时服务端没有任何变化，必须保留选中状态让用户直接重试（刷新会连带清掉勾选）。
export function hasDeletedAnyFile(result) {
  return (Number(result?.succeeded) || 0) > 0
}

// 删除成功后的刷新有三条入口（删知识库、删单个文件、批量删），刷新都在成功分支里。
// runConfirmedDelete 只覆盖到「确认 → 执行」，刷新这一段当时不在它的范围内，
// 三处都是裸 await：刷新一失败就产生未捕获的 Promise 拒绝，用户还什么都看不到
// （issue #83 第 7 项）。这里把刷新也收进同一个出口，并复用删除入口的 notifyError。
export const DELETE_REFRESH_FAILED_HINT = '请手动刷新页面'

// 文案要说清「删除已经成功」：刷新失败不等于删除失败，不能让用户以为没删掉、
// 又去点一次删除，那第二次会以 404 收场。
export function describeDeleteRefreshFailure(error) {
  return `删除成功，但列表刷新失败：${getApiErrorMessage(error, DELETE_REFRESH_FAILED_HINT)}`
}

// 刷新失败的处置在删除与上传两条链上完全一致（都不抛出、都交给调用方的 notifyError
// 出口），只有文案里的操作名不同。收口的形态由这一份实现给出，两个具名出口各自绑定文案，
// 免得两处各写一份 try/catch 后各自漂移 —— 上传面正是漂移出来的（issue #154：
// 刷新的裸 await 留在了上传自己的 try 里，被上传的 catch 接走）。
export async function refreshAfterMutation({ refresh, notifyError, describeFailure }) {
  try {
    await refresh()
    return true
  } catch (error) {
    notifyError?.(describeFailure(error))
    return false
  }
}

// 返回刷新是否成功，调用方一般不关心——存在的意义是保证「不抛出」：
// 参数里传进来的是模板事件处理器，返回被拒 Promise 就是一条 unhandledrejection。
export async function refreshAfterDelete({ refresh, notifyError }) {
  return refreshAfterMutation({
    refresh,
    notifyError,
    describeFailure: describeDeleteRefreshFailure,
  })
}

export const UPLOAD_REFRESH_FAILED_HINT = '请手动刷新页面'

// 与 describeDeleteRefreshFailure 同一条规则：刷新失败不等于操作失败。
// 上传成功后的刷新此前留在 handleUpload 的 try 内裸 await，刷新一失败就被上传自己的
// catch 接走，逐字弹「上传失败，请稍后重试」——而文件其实已经入库，列表只是没跟上。
// 用户据此重传会真的再入一份（后端对文件名没有唯一约束），所以文案必须先认下「上传成功」。
export function describeUploadRefreshFailure(error) {
  return `上传成功，但列表刷新失败：${getApiErrorMessage(error, UPLOAD_REFRESH_FAILED_HINT)}`
}

// 上传成功后的刷新出口，与删除侧同形：失败只报「刷新失败」，不回写上传失败态。
export async function refreshAfterUpload({ refresh, notifyError }) {
  return refreshAfterMutation({
    refresh,
    notifyError,
    describeFailure: describeUploadRefreshFailure,
  })
}

// 上传白名单：必须与 backend/service/utils_service.py 的 KNOWLEDGE_UPLOAD_TYPES 完全一致
// （后端那份的注释写明扩展名还要与 crud.knowledge_file.extract_file_text 的抽取链一致）。
// 顺序沿用后端，便于与后端的「仅支持 … 格式的文件」文案逐字对照。
// tests/knowledgeUploadTypes.test.js 会静态解析后端白名单，锁死两边一致。
export const KNOWLEDGE_UPLOAD_EXTENSIONS = ['.txt', '.md', '.docx', '.pdf']
// 取文件选择器 accept 属性用的字符串形式，视图与提示文案都从这里派生，避免两处各写一份清单。
export const KNOWLEDGE_UPLOAD_ACCEPT = KNOWLEDGE_UPLOAD_EXTENSIONS.join(',')
export const KNOWLEDGE_UPLOAD_NAMES = KNOWLEDGE_UPLOAD_EXTENSIONS.map((ext) => ext.slice(1))
export const KNOWLEDGE_UPLOAD_HINT = `支持 ${KNOWLEDGE_UPLOAD_NAMES.join('、')}，支持多选批量上传`

// 与后端 Path(filename).suffix 的语义对齐：取最后一段扩展名并小写，".env"、"a." 视为无扩展名。
function fileExtension(name) {
  const value = String(name || '')
  const dot = value.lastIndexOf('.')
  if (dot <= 0 || dot === value.length - 1) return ''
  return value.slice(dot).toLowerCase()
}

export function isSupportedUploadFile(name) {
  return KNOWLEDGE_UPLOAD_EXTENSIONS.includes(fileExtension(name))
}

// 批量删除的提示文案与级别：成功数、失败数都必须如实呈现，不允许只报成功。
export function describeBatchDeleteResult(result = {}) {
  const succeeded = Number(result.succeeded) || 0
  const failed = Number(result.failed) || 0
  if (failed > 0) {
    return {
      type: succeeded > 0 ? 'warning' : 'error',
      message: `已删除 ${succeeded} 个资料，${failed} 个删除失败`,
    }
  }
  return { type: 'success', message: '已删除选中资料' }
}

export function describeUploadSuccess(uploadedCount) {
  return uploadedCount > 1 ? `已上传 ${uploadedCount} 个文件` : '上传成功'
}

// 进度按整批文件折算：第 index 个文件（从 0 起）占 1/fileCount 的权重。
// total 缺失时不更新（保持上一次进度），由调用方决定跳过。
export function computeUploadPercent(index, loaded, totalBytes, fileCount) {
  if (!totalBytes || !fileCount) return null
  return Math.round(((index + loaded / totalBytes) / fileCount) * 100)
}

// 批量上传前按白名单分流：白名单外的文件在「选中阶段」就被拦下并点名，
// 而不是提交后由后端 400 拒绝 —— 后者会连带中止同批合法文件（见 uploadFilesInOrder 的首败即止语义）。
export function partitionUploadFiles(files) {
  const supported = []
  const rejected = []
  for (const file of files || []) {
    if (isSupportedUploadFile(file && file.name)) supported.push(file)
    else rejected.push((file && file.name) || '')
  }
  return { supported, rejected }
}

export function describeSkippedUploadFiles(rejectedNames) {
  const names = (rejectedNames || []).join('、')
  return `已跳过不支持的文件：${names}（仅支持 ${KNOWLEDGE_UPLOAD_NAMES.join('、')}）`
}

// 上传失败提示：点名失败文件，并如实说明同批其余文件的去向。
// uploadFilesInOrder 首败即止，所以只有排在该文件之前的文件上传成功，其余都不上传。
// skippedCount 是本次选择里在选中阶段就被跳过的文件数：它们同样没有上传，
// 如果不提，末句「均已上传」会和事实打架（失败文件恰为本批最后一个受支持文件时）。
export function describeUploadFailure(fileName, remainingCount, reason, skippedCount = 0) {
  let tail = remainingCount > 0
    ? `同批剩余 ${remainingCount} 个文件未上传`
    : '同批其余文件均已上传'
  if (skippedCount > 0) {
    tail += `（另有 ${skippedCount} 个不支持的文件在选中阶段已跳过）`
  }
  return `「${fileName}」上传失败：${reason}；${tail}`
}

// 顺序上传整批文件：任意一个失败都会中止整批并向调用方抛出（当前视图语义），
// 由调用方负责错误提示与状态复位。返回成功上传的文件数。
export async function uploadFilesInOrder(files, uploadOne, onPercentChange) {
  const fileCount = files.length
  let uploaded = 0
  for (const [index, file] of files.entries()) {
    await uploadOne(file, (progress = {}) => {
      const percent = computeUploadPercent(index, progress.loaded, progress.total, fileCount)
      if (percent !== null) onPercentChange(percent)
    })
    uploaded += 1
    onPercentChange(computeUploadPercent(uploaded, 0, 1, fileCount))
  }
  return uploaded
}
