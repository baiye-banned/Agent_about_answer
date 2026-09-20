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
