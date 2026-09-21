import test from 'node:test'
import assert from 'node:assert/strict'

// 覆盖 src/utils/knowledgeFeedback.js：Knowledge.vue 的三个删除入口
// （deleteKnowledgeBase / confirmDelete / confirmBatchDelete，都经 runConfirmedDelete 收口）、
// 批量删除反馈与 handleUpload（顺序上传 + 进度 + 成功文案）都走这几个函数，
// 这里锁的是那几个视图分支的行为。
import {
  CREATE_REFRESH_FAILED_HINT,
  DELETE_CANCELLED,
  DELETE_FAILED,
  DELETE_REFRESH_FAILED_HINT,
  DELETE_SUCCEEDED,
  UPLOAD_REFRESH_FAILED_HINT,
  computeUploadPercent,
  describeBatchDeleteResult,
  describeCreateRefreshFailure,
  describeDeleteRefreshFailure,
  describeUploadRefreshFailure,
  describeUploadSuccess,
  hasDeletedAnyFile,
  isConfirmCancellation,
  refreshAfterCreate,
  refreshAfterDelete,
  refreshAfterUpload,
  runConfirmedDelete,
  uploadFilesInOrder,
} from '../src/utils/knowledgeFeedback.js'

test('describeBatchDeleteResult reports success only when nothing failed', () => {
  assert.deepEqual(describeBatchDeleteResult({ total: 3, succeeded: 3, failed: 0 }), {
    type: 'success',
    message: '已删除选中资料',
  })
  // 空批次/缺字段不得被当成失败。
  assert.deepEqual(describeBatchDeleteResult({ total: 0, succeeded: 0, failed: 0 }), {
    type: 'success',
    message: '已删除选中资料',
  })
  assert.deepEqual(describeBatchDeleteResult({}), {
    type: 'success',
    message: '已删除选中资料',
  })
})

test('describeBatchDeleteResult keeps both the succeeded and the failed count', () => {
  assert.deepEqual(describeBatchDeleteResult({ total: 3, succeeded: 1, failed: 2 }), {
    type: 'warning',
    message: '已删除 1 个资料，2 个删除失败',
  })
  assert.deepEqual(describeBatchDeleteResult({ total: 5, succeeded: 4, failed: 1 }), {
    type: 'warning',
    message: '已删除 4 个资料，1 个删除失败',
  })
})

test('describeBatchDeleteResult never claims success when the whole batch failed', () => {
  const feedback = describeBatchDeleteResult({ total: 2, succeeded: 0, failed: 2 })

  assert.equal(feedback.type, 'error')
  assert.equal(feedback.message, '已删除 0 个资料，2 个删除失败')
  assert.ok(!feedback.message.includes('已删除选中资料'))
})

test('describeUploadSuccess names the batch size only for more than one file', () => {
  assert.equal(describeUploadSuccess(1), '上传成功')
  assert.equal(describeUploadSuccess(2), '已上传 2 个文件')
  assert.equal(describeUploadSuccess(4), '已上传 4 个文件')
})

test('computeUploadPercent spreads per-file progress over the whole batch', () => {
  // 第 0 个文件传到一半：整批 25%。
  assert.equal(computeUploadPercent(0, 50, 100, 2), 25)
  // 第 1 个文件传完：整批 100%。
  assert.equal(computeUploadPercent(1, 100, 100, 2), 100)
  // 单个文件（uploadOne 完成后的整批进度）走 0/1。
  assert.equal(computeUploadPercent(2, 0, 1, 4), 50)
  assert.equal(computeUploadPercent(0, 0, 100, 4), 0)
})

test('computeUploadPercent skips updates it cannot compute', () => {
  // 浏览器可能给出 total 为 0 的进度事件：此时不更新进度条，而不是显示 NaN。
  assert.equal(computeUploadPercent(0, 10, 0, 4), null)
  assert.equal(computeUploadPercent(0, 10, undefined, 4), null)
  assert.equal(computeUploadPercent(0, 10, 100, 0), null)
})

test('uploadFilesInOrder uploads files one by one and reports batch progress', async () => {
  const files = [{ name: 'a.txt' }, { name: 'b.txt' }]
  const attempted = []
  const percents = []

  const uploaded = await uploadFilesInOrder(
    files,
    async (file, onProgress) => {
      attempted.push(file.name)
      onProgress({ loaded: 50, total: 100 })
      onProgress({ loaded: 100, total: 100 })
    },
    (percent) => percents.push(percent)
  )

  assert.equal(uploaded, 2)
  assert.deepEqual(attempted, ['a.txt', 'b.txt'])
  // 进度单调递增到 100%，中途不出现 NaN/回退：
  // a 半程 25 → a 满程 50 → a 结束 50 → b 半程 75 → b 满程 100 → b 结束 100。
  assert.deepEqual(percents, [25, 50, 50, 75, 100, 100])
})

test('uploadFilesInOrder stops the batch on the first failure and rethrows it', async () => {
  const files = [{ name: 'a.txt' }, { name: 'b.txt' }, { name: 'c.txt' }]
  const attempted = []
  const percents = []
  const failure = new Error('上传失败')

  await assert.rejects(
    uploadFilesInOrder(
      files,
      async (file) => {
        attempted.push(file.name)
        if (file.name === 'b.txt') throw failure
      },
      (percent) => percents.push(percent)
    ),
    (error) => error === failure
  )

  // 当前语义：首个失败即中止整批，剩余文件不再上传；错误原样抛给视图去做提示与复位。
  assert.deepEqual(attempted, ['a.txt', 'b.txt'])
  assert.ok(!percents.includes(100))
})

// --- 删除入口的「确认 → 执行 → 反馈」编排（origin/develop 的三个删除入口都没有错误分支）---

test('isConfirmCancellation only recognises the dismissal values element-plus rejects with', () => {
  // element-plus 2.13.7 的 messageBox 以字符串拒绝（不是 Error）：未开
  // distinguishCancelAndClose 时，取消按钮、ESC、点遮罩都归一成 'cancel'。
  assert.equal(isConfirmCancellation('cancel'), true)
  assert.equal(isConfirmCancellation('close'), true)

  // 接口失败绝不能被当成"用户取消"，否则会静默吞掉真实错误。
  assert.equal(isConfirmCancellation(new Error('cancel')), false)
  assert.equal(isConfirmCancellation(undefined), false)
  assert.equal(isConfirmCancellation(null), false)
  assert.equal(isConfirmCancellation({ response: { status: 500 } }), false)
})

test('runConfirmedDelete stays silent and skips the delete when the user cancels', async () => {
  const calls = []

  const outcome = await runConfirmedDelete({
    confirm: () => Promise.reject('cancel'),
    remove: async () => calls.push('remove'),
    notifyError: (message) => calls.push(`error:${message}`),
  })

  assert.equal(outcome.status, DELETE_CANCELLED)
  // 取消既不是成功也不是失败：不删、不提示（成功提示由视图只在 succeeded 时给出）。
  assert.deepEqual(calls, [])
})

test('runConfirmedDelete treats a close (ESC / overlay) as a cancellation too', async () => {
  const calls = []

  const outcome = await runConfirmedDelete({
    confirm: () => Promise.reject('close'),
    remove: async () => calls.push('remove'),
    notifyError: (message) => calls.push(`error:${message}`),
  })

  assert.equal(outcome.status, DELETE_CANCELLED)
  assert.deepEqual(calls, [])
})

test('runConfirmedDelete leaves no unhandled rejection at an event-handler call site', async () => {
  // 模板上的三处调用点（:32 / :78 / :128）都是 @click 直调。Vue 的事件层
  // （callWithAsyncErrorHandling）会接住处理器返回的 Promise：dev 构建在 warn 后重新
  // 抛出成未捕获拒绝，prod 构建只记一条 console.error。这里绕过该层、直接丢掉返回的
  // Promise，断言修复后任意分支（成功 / 失败 / 取消）都不再产生控制台输出。
  const unhandled = []
  const onUnhandled = (reason) => unhandled.push(reason)
  process.on('unhandledRejection', onUnhandled)

  try {
    runConfirmedDelete({
      confirm: () => Promise.reject('cancel'),
      remove: async () => {},
      notifyError: () => {},
    })
    runConfirmedDelete({
      confirm: () => Promise.reject('close'),
      remove: async () => {},
      notifyError: () => {},
    })
    // 让微任务队列排空，给未捕获拒绝一个冒泡的机会。
    await new Promise((resolve) => setTimeout(resolve, 0))
  } finally {
    process.off('unhandledRejection', onUnhandled)
  }

  assert.deepEqual(unhandled, [])
})

test('runConfirmedDelete surfaces the API message and never reaches the success path on failure', async () => {
  const notifications = []
  const failure = {
    response: { data: { detail: '知识库不存在' } },
    message: 'Request failed with status code 404',
  }

  const outcome = await runConfirmedDelete({
    confirm: async () => {},
    remove: async () => {
      throw failure
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(outcome.status, DELETE_FAILED)
  assert.equal(outcome.error, failure)
  // 文案优先取接口 detail，而不是 axios 的英文 message。
  assert.deepEqual(notifications, ['知识库不存在'])
})

test('runConfirmedDelete falls back to the delete wording when the failure has no usable message', async () => {
  const notifications = []

  const outcome = await runConfirmedDelete({
    confirm: async () => {},
    remove: async () => {
      throw new Error('')
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(outcome.status, DELETE_FAILED)
  assert.deepEqual(notifications, ['删除失败，请稍后重试'])
})

test('runConfirmedDelete lets the caller override the failure wording', async () => {
  const notifications = []

  await runConfirmedDelete({
    confirm: async () => {},
    remove: async () => {
      throw new Error('')
    },
    failureMessage: '删除资料失败，请稍后重试',
    notifyError: (message) => notifications.push(message),
  })

  assert.deepEqual(notifications, ['删除资料失败，请稍后重试'])
})

test('runConfirmedDelete reports an unexpected confirm failure instead of swallowing it', async () => {
  const notifications = []
  let removed = false

  const outcome = await runConfirmedDelete({
    confirm: async () => {
      throw new TypeError('messageBox is not mounted')
    },
    remove: async () => {
      removed = true
    },
    notifyError: (message) => notifications.push(message),
  })

  // 非取消的确认异常按失败处理：既不静默，也不逃逸成未捕获拒绝，更不会去调接口。
  assert.equal(outcome.status, DELETE_FAILED)
  assert.equal(removed, false)
  // 文案沿用仓库统一的 getApiErrorMessage：有 message 就用它，没有才回落到兜底文案。
  assert.deepEqual(notifications, ['messageBox is not mounted'])
})

test('runConfirmedDelete hands the API result back on success and notifies nothing', async () => {
  const notifications = []

  const outcome = await runConfirmedDelete({
    confirm: async () => {},
    remove: async () => ({ fallback_knowledge_base_id: 'kb-2' }),
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(outcome.status, DELETE_SUCCEEDED)
  assert.deepEqual(outcome.result, { fallback_knowledge_base_id: 'kb-2' })
  assert.deepEqual(notifications, [])
})

test('hasDeletedAnyFile keeps the selection when the batch deleted nothing', () => {
  // batchDelete 用 allSettled 收敛，整批失败时回的是数据而不是拒绝。
  assert.equal(hasDeletedAnyFile({ total: 2, succeeded: 0, failed: 2 }), false)
  assert.equal(hasDeletedAnyFile({ total: 0, succeeded: 0, failed: 0 }), false)
  assert.equal(hasDeletedAnyFile({}), false)
  assert.equal(hasDeletedAnyFile(undefined), false)

  assert.equal(hasDeletedAnyFile({ total: 2, succeeded: 2, failed: 0 }), true)
  // 部分成功仍然清空选中并刷新（失败明细不在此函数语义内）。
  assert.equal(hasDeletedAnyFile({ total: 3, succeeded: 1, failed: 2 }), true)
})

// issue #83 第 7 项：删除成功之后的刷新调用不能没有错误分支。
// 三个删除入口（deleteKnowledgeBase / confirmDelete / confirmBatchDelete）的刷新
// 都改走 refreshAfterDelete，刷新失败时被捕获并复用 notifyDeleteError 出口。
test('refreshAfterDelete returns true and stays silent when the refresh succeeds', async () => {
  const notifications = []
  let refreshed = 0

  const result = await refreshAfterDelete({
    refresh: async () => {
      refreshed += 1
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, true)
  assert.equal(refreshed, 1)
  assert.deepEqual(notifications, [])
})

test('refreshAfterDelete captures a failed refresh instead of returning a rejected promise', async () => {
  // 先证红：修复前三个入口是裸 await，刷新一失败事件处理器就返回被拒 Promise，
  // 浏览器记一条 unhandledrejection，用户什么都看不到。
  const notifications = []
  const error = new Error('Request failed with status code 500')
  error.response = { status: 500, data: { detail: '服务暂时不可用' } }

  const result = await refreshAfterDelete({
    refresh: () => Promise.reject(error),
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, false)
  assert.deepEqual(notifications, ['删除成功，但列表刷新失败：服务暂时不可用'])
})

test('refreshAfterDelete also captures synchronous throws from the refresh closure', async () => {
  const notifications = []

  const result = await refreshAfterDelete({
    refresh: () => {
      throw new Error('同步炸了')
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, false)
  assert.equal(notifications.length, 1)
})

test('describeDeleteRefreshFailure says the delete succeeded and falls back to the shared hint', () => {
  // 刷新失败不等于删除失败：说成失败会让用户再点一次删除，第二次以 404 收场。
  assert.match(describeDeleteRefreshFailure(new Error('boom')), /^删除成功，但列表刷新失败：/)
  assert.ok(describeDeleteRefreshFailure({}).endsWith(DELETE_REFRESH_FAILED_HINT))
  assert.equal(
    describeDeleteRefreshFailure({}),
    `删除成功，但列表刷新失败：${DELETE_REFRESH_FAILED_HINT}`
  )
})

// 上传面（issue #154）。与删除面同一条规则，但失败不得被读成「上传失败」：
// 文件已经入库，用户据此重传会真的再入一份（后端对文件名没有唯一约束）。

test('describeUploadRefreshFailure says the upload succeeded and falls back to the shared hint', () => {
  assert.match(describeUploadRefreshFailure(new Error('boom')), /^上传成功，但列表刷新失败：/)
  assert.ok(describeUploadRefreshFailure({}).endsWith(UPLOAD_REFRESH_FAILED_HINT))
  assert.equal(
    describeUploadRefreshFailure({}),
    `上传成功，但列表刷新失败：${UPLOAD_REFRESH_FAILED_HINT}`
  )
  // 承重：这条文案里不能出现「上传失败」。逐字误报正是本项的缺陷形态。
  assert.ok(
    !describeUploadRefreshFailure({}).includes('上传失败'),
    '刷新失败的文案不得含「上传失败」：文件已经入库了'
  )
  // 接口给了可显示的原因时用它，而不是笼统的兜底。
  const error = new Error('Request failed with status code 500')
  error.response = { status: 500, data: { detail: '服务暂时不可用' } }
  assert.equal(
    describeUploadRefreshFailure(error),
    '上传成功，但列表刷新失败：服务暂时不可用'
  )
})

test('refreshAfterUpload returns true and stays silent when the refresh succeeds', async () => {
  const notifications = []
  let refreshed = 0

  const result = await refreshAfterUpload({
    refresh: async () => {
      refreshed += 1
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, true)
  assert.equal(refreshed, 1)
  assert.deepEqual(notifications, [])
})

test('refreshAfterUpload captures a failed refresh instead of returning a rejected promise', async () => {
  // 参数是模板事件处理器：返回被拒 Promise 就是一条 unhandledrejection，
  // 用户什么都看不到。刷新失败必须被捕获并转成一条带归属的提示。
  const notifications = []
  const error = new Error('网络连接已断开')

  const result = await refreshAfterUpload({
    refresh: () => Promise.reject(error),
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, false)
  assert.deepEqual(notifications, ['上传成功，但列表刷新失败：网络连接已断开'])
})

test('refreshAfterUpload also captures synchronous throws from the refresh closure', async () => {
  const notifications = []

  const result = await refreshAfterUpload({
    refresh: () => {
      throw new Error('同步炸了')
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, false)
  assert.equal(notifications.length, 1)
})

// issue #157：创建成功之后的刷新与删除侧同款收口。
// 修复前这次刷新是裸 await，留在 submitKnowledgeBaseDialog 自己的 try 里，
// 刷新一失败就被创建自己的 catch 接走，逐字弹刷新的原始错误 —— 文案里看不出
// 「创建其实成功了」，而对话框还停在打开态，用户照着重试撞上 400「知识库已存在」。
test('refreshAfterCreate returns true and stays silent when the refresh succeeds', async () => {
  const notifications = []
  let refreshed = 0

  const result = await refreshAfterCreate({
    refresh: async () => {
      refreshed += 1
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, true)
  assert.equal(refreshed, 1)
  assert.deepEqual(notifications, [])
})

test('refreshAfterCreate captures a failed refresh instead of returning a rejected promise', async () => {
  const notifications = []
  const error = new Error('Request failed with status code 500')
  error.response = { status: 500, data: { detail: '服务暂时不可用' } }

  const result = await refreshAfterCreate({
    refresh: () => Promise.reject(error),
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, false)
  // 归属必须是「创建成功、刷新没跟上」，不是「操作失败」：后者会让用户重复创建。
  assert.deepEqual(notifications, ['知识库已创建，但列表刷新失败：服务暂时不可用'])
})

test('refreshAfterCreate also captures synchronous throws from the refresh closure', async () => {
  const notifications = []

  const result = await refreshAfterCreate({
    refresh: () => {
      throw new Error('同步炸了')
    },
    notifyError: (message) => notifications.push(message),
  })

  assert.equal(result, false)
  assert.equal(notifications.length, 1)
})

// 没有出口时不得抛出：模板事件处理器返回被拒 Promise 就是一条 unhandledrejection。
test('refreshAfterCreate stays non-throwing even without a notifyError exit', async () => {
  const result = await refreshAfterCreate({ refresh: () => Promise.reject(new Error('boom')) })

  assert.equal(result, false)
})

test('describeCreateRefreshFailure says the create succeeded and falls back to the shared hint', () => {
  // 刷新失败不等于创建失败：说成失败会让用户再点一次「创建」，第二次以 400「知识库已存在」收场。
  assert.match(describeCreateRefreshFailure(new Error('boom')), /^知识库已创建，但列表刷新失败：/)
  assert.ok(describeCreateRefreshFailure({}).endsWith(CREATE_REFRESH_FAILED_HINT))
  assert.equal(
    describeCreateRefreshFailure({}),
    `知识库已创建，但列表刷新失败：${CREATE_REFRESH_FAILED_HINT}`
  )
})
