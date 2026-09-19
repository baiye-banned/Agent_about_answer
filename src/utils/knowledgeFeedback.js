// 知识库视图（Knowledge.vue）的批量操作反馈与顺序上传编排。
// 抽成纯函数/可注入依赖的形式，便于在纯 Node 下做行为级断言（见 tests/knowledgeFeedback.test.js）。

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
