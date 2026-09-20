// 知识库视图（Knowledge.vue）的批量操作反馈与顺序上传编排。
// 抽成纯函数/可注入依赖的形式，便于在纯 Node 下做行为级断言（见 tests/knowledgeFeedback.test.js）。

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
export function describeUploadFailure(fileName, remainingCount, reason) {
  const tail = remainingCount > 0
    ? `同批剩余 ${remainingCount} 个文件未上传`
    : '同批其余文件均已上传'
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
