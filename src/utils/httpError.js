import { displayText } from './displayText.js'

export function getApiErrorMessage(error, fallback = '请求失败，请稍后重试') {
  return normalizeErrorMessage(
    firstPresentValue(
      error?.response?.data?.detail,
      error?.response?.data?.message,
      error?.message
    ),
    fallback
  )
}

function firstPresentValue(...values) {
  return values.find((value) => value !== null && value !== undefined && value !== '')
}

function normalizeErrorMessage(value, fallback) {
  if (value === null || value === undefined || value === '') return fallback
  if (typeof value === 'string') return value
  return displayText(value)
}
