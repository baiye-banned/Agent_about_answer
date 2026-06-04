import { displayText } from './displayText.js'

export const IMAGE_ANALYSIS_STATUS = {
  PARTIAL: 'partial',
  FAILED: 'failed',
}

const WARNING_STATUSES = new Set([
  IMAGE_ANALYSIS_STATUS.PARTIAL,
  IMAGE_ANALYSIS_STATUS.FAILED,
])

export function shouldShowImageAnalysisWarning(status) {
  return WARNING_STATUSES.has(status)
}

export function imageAnalysisWarningText(status, error = '') {
  if (error !== null && error !== undefined && error !== '') return displayText(error)
  if (status === IMAGE_ANALYSIS_STATUS.FAILED) {
    return '图片部分未能识别，当前回答可能仅基于文字问题生成。'
  }
  return '图片内容仅部分识别，已结合可识别信息继续回答。'
}
