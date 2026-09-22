// 知识库文件列表（Knowledge.vue 的 fetchFiles → knowledgeAPI.getList）的请求时序保护与翻页。
//
// 切换知识库时只同步改 currentKnowledgeBaseId，文件列表却来自异步响应，
// 两者之间没有时序标识：快速切换（或新库更快返回）时，先发出、后返回的**旧库**
// 响应会把当前库的列表覆盖掉，用户看到的是上一个知识库的文件
// （issue #83 第 8 项，与 #13 的会话切换覆盖问题同族，这里是不同视图、不同请求）。
//
// 用单调递增的请求序号做守卫，与 detailPreview.js 的 #63 方案同款：
// 只有最后发起的那次请求才能写状态。取数函数与状态写入都由调用方注入，
// 便于在纯 Node 下做行为级断言（tests/fileListRequest.test.js）。
//
// issue #191：文件列表接口本身有页大小上限（后端 LIST_DEFAULT_LIMIT / LIST_MAX_LIMIT），
// 所以这里除了取第一页还提供 loadMore() 向后翻页，并把「还有没有更早的」交给调用方显示——
// 上限落地时如果不同批给出按需加载的入口，老数据在界面上就是静默消失。

// 与后端 LIST_DEFAULT_LIMIT 保持一致：一页多少条由前端决定，后端只负责夹取上限。
export const FILE_PAGE_SIZE = 50

export function createFileListRequest({
  getKnowledgeBaseId,
  fetchList,
  applyFiles,
  applyLoading,
  applyHasMore,
  pageSize = FILE_PAGE_SIZE,
}) {
  // open() 之外还提供 invalidate()：组件卸载时主动作废在飞请求，
  // 此后到达的响应不得再写进已经卸载组件的状态对象（issue #83 第 9 项同款口径）。
  let latestToken = 0
  // 已加载区间的末位 id：loadMore 的游标，翻页期间不受新上传影响（键集游标）。
  let cursor = null
  // 还有没有更早的一页。取不满一页即说明已经到最早一个，此后 loadMore 不该再发请求：
  // 调用方虽然按 applyHasMore 收起了入口，但守卫必须留在这里——直接调用 loadMore 的
  // 人（用例、后续的其它入口）不会自己挡住自己，空转的请求会拿着同一个游标反复问。
  // 与 chat / knowledge 两个 store 里的同名守卫同款。
  let moreAvailable = false

  // 取一页的请求要多要一条（pageSize + 1）：用「是否多出来」判断还有没有更早的文件。
  // 只取一页时无法区分「正好取满」和「已经取完」，会给出一个点了没反应的按钮。
  function splitPage(rows) {
    const hasMore = rows.length > pageSize
    // 后端按「新 -> 旧」返回：多出来的那条是最旧的一条，在末尾，丢掉它剩下正好一页。
    return { page: hasMore ? rows.slice(0, pageSize) : rows, hasMore }
  }

  async function load() {
    const token = (latestToken += 1)
    const knowledgeBaseId = getKnowledgeBaseId()

    // 没有选中知识库：同步清空，不发请求。这条路径也要占一个序号，
    // 否则在飞的旧库请求回来时仍会把它填回去。
    if (!knowledgeBaseId) {
      applyFiles([])
      applyHasMore?.(false)
      applyLoading(false)
      cursor = null
      moreAvailable = false
      return []
    }

    applyLoading(true)
    try {
      const response = await fetchList({ knowledge_base_id: knowledgeBaseId, limit: pageSize + 1 })
      // 过期响应整条丢弃：既不写文件列表，也不复位 loading（复位交给最新那次请求）。
      if (token !== latestToken) return null
      const { page, hasMore } = splitPage(Array.isArray(response) ? response : [])
      applyFiles(page)
      cursor = page.length ? page[page.length - 1].id ?? null : null
      moreAvailable = hasMore && cursor !== null
      applyHasMore?.(moreAvailable)
      return page
    } finally {
      if (token === latestToken) applyLoading(false)
    }
  }

  // 向后翻页：以已加载区间的末位 id 作游标取更早的一页。取不满一页即说明已经到最早一个。
  // 不新增世代（这只是在同一世代里追加），但要检查世代有没有变：切库或卸载之后
  // 迟到的这一页不得再拼进新库的列表。
  async function loadMore() {
    const token = latestToken
    const knowledgeBaseId = getKnowledgeBaseId()
    if (!knowledgeBaseId || cursor === null || !moreAvailable) return null

    const response = await fetchList({
      knowledge_base_id: knowledgeBaseId,
      limit: pageSize + 1,
      before_id: cursor,
    })
    if (token !== latestToken) return null
    const { page, hasMore } = splitPage(Array.isArray(response) ? response : [])
    // 追加语义由调用方落实（列表在它手里）：这里只说明这一页是接在后面的。
    applyFiles(page, { append: true })
    if (page.length) cursor = page[page.length - 1].id ?? cursor
    moreAvailable = hasMore && page.length > 0
    applyHasMore?.(moreAvailable)
    return page
  }

  function invalidate() {
    // 作废所有在飞请求，并顺带收起加载态：作废之后那笔请求的 finally 会因序号过期
    // 而不再复位 loading，不在这里复位就会一直卡在加载态（detailPreview 的 close 同理）。
    latestToken += 1
    applyLoading(false)
    cursor = null
    moreAvailable = false
    applyHasMore?.(false)
  }

  return { load, loadMore, invalidate }
}
