// 知识库文件详情预览（Knowledge.vue 的 showDetail → knowledgeAPI.getContent）的请求时序保护。
//
// 弹窗标题取自同步切换的 detailFile，正文却来自异步响应；两者之间没有任何时序标识时，
// 「先点慢文件、再点快文件」会让先发出但后返回的旧响应把当前文件的正文改写掉，
// 用户看到的是「B-员工手册.md」这个标题下面挂着 A 的全文（#63）。
//
// 这里用单调递增的请求序号做守卫：只有最后发起的那次请求（或关闭弹窗之后）才能写状态。
// 取数函数由调用方注入，便于在纯 Node 下做行为级断言（tests/detailPreview.test.js）。

import { getApiErrorMessage } from './httpError.js'

// 预览区空态与读取失败两份文案：正文为空且没出错才是「没有内容」，
// 读失败必须说成读失败，不能落到空态上（#63 里记录的语义误导）。
export const DETAIL_PREVIEW_EMPTY_TEXT = '暂无可预览内容'
export const DETAIL_PREVIEW_ERROR_TEXT = '内容读取失败，请稍后重试'

// 预览状态初值。视图侧必须把它包成 reactive() 再交给 createDetailPreview：
// createDetailPreview 是直接写这个对象的属性，只有写进响应式代理才会触发视图更新。
export function createDetailPreviewState() {
  return {
    visible: false, // 弹窗开关（v-model）
    file: null, // 弹窗标题与详情字段的来源，与正文必须始终指向同一份文件
    content: '', // 预览正文
    loading: false, // 骨架屏
    error: '', // 读取失败文案（与空态区分）
  }
}

export function createDetailPreview({ state = createDetailPreviewState(), fetchContent }) {
  // 单调递增的请求序号：发起时自增并持有，响应回来时不再是最新就整条丢弃。
  // close() 也自增，因此关闭弹窗后到达的响应不会再写回任何状态。
  let latestToken = 0

  async function open(file) {
    const token = (latestToken += 1)
    // 标题是同步切换的，正文必须先清空：换文件后不能残留上一份的正文。
    state.file = file
    state.content = ''
    state.error = ''
    state.loading = true
    state.visible = true

    try {
      const response = await fetchContent(file.id)
      // 陈旧响应整条丢弃：既不写正文，也不复位骨架屏（复位交给最新那次请求）。
      if (token !== latestToken) return
      state.content = response?.content || ''
    } catch (error) {
      if (token !== latestToken) return
      // 失败在这里收口，不向调用方抛出：open 的调用方是模板事件处理器，
      // 返回的 Promise 无人接管，抛出去就是一条 unhandledrejection。
      state.error = getApiErrorMessage(error, DETAIL_PREVIEW_ERROR_TEXT)
    } finally {
      if (token === latestToken) state.loading = false
    }
  }

  function close() {
    // 关闭即作废在飞请求：此后到达的响应都不得再写回预览状态。
    latestToken += 1
    state.visible = false
    state.loading = false
    state.error = ''
  }

  return { open, close }
}
