import { getApiErrorMessage } from './httpError.js'

export async function assertStreamResponse(response) {
  if (response.ok && response.body) return
  if (response.ok && !response.body) throw new Error('流式响应缺少响应体')

  const errorBody = await response.json().catch(() => ({}))
  const message = getApiErrorMessage(
    { response: { data: errorBody } },
    '流式请求失败'
  )
  throw new Error(message)
}
