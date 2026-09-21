// 知识库文件列表（Knowledge.vue 的 fetchFiles → knowledgeAPI.getList）的请求时序保护。
//
// 切换知识库时只同步改 currentKnowledgeBaseId，文件列表却来自异步响应，
// 两者之间没有时序标识：快速切换（或新库更快返回）时，先发出、后返回的**旧库**
// 响应会把当前库的列表覆盖掉，用户看到的是上一个知识库的文件
// （issue #83 第 8 项，与 #13 的会话切换覆盖问题同族，这里是不同视图、不同请求）。
//
// 用单调递增的请求序号做守卫，与 detailPreview.js 的 #63 方案同款：
// 只有最后发起的那次请求才能写状态。取数函数与状态写入都由调用方注入，
// 便于在纯 Node 下做行为级断言（tests/fileListRequest.test.js）。

export function createFileListRequest({ getKnowledgeBaseId, fetchList, applyFiles, applyLoading }) {
  // open() 之外还提供 invalidate()：组件卸载时主动作废在飞请求，
  // 此后到达的响应不得再写进已经卸载组件的状态对象（issue #83 第 9 项同款口径）。
  let latestToken = 0

  async function load() {
    const token = (latestToken += 1)
    const knowledgeBaseId = getKnowledgeBaseId()

    // 没有选中知识库：同步清空，不发请求。这条路径也要占一个序号，
    // 否则在飞的旧库请求回来时仍会把它填回去。
    if (!knowledgeBaseId) {
      applyFiles([])
      applyLoading(false)
      return []
    }

    applyLoading(true)
    try {
      const response = await fetchList({ knowledge_base_id: knowledgeBaseId })
      // 过期响应整条丢弃：既不写文件列表，也不复位 loading（复位交给最新那次请求）。
      if (token !== latestToken) return null
      const files = Array.isArray(response) ? response : []
      applyFiles(files)
      return files
    } finally {
      if (token === latestToken) applyLoading(false)
    }
  }

  function invalidate() {
    // 作废所有在飞请求，并顺带收起加载态：作废之后那笔请求的 finally 会因序号过期
    // 而不再复位 loading，不在这里复位就会一直卡在加载态（detailPreview 的 close 同理）。
    latestToken += 1
    applyLoading(false)
  }

  return { load, invalidate }
}
