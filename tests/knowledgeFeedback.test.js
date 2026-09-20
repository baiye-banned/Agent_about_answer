import test from 'node:test'
import assert from 'node:assert/strict'

// 覆盖 src/utils/knowledgeFeedback.js：Knowledge.vue 的三个删除入口
// （deleteKnowledgeBase / confirmDelete / confirmBatchDelete，都经 runConfirmedDelete 收口）、
// 批量删除反馈与 handleUpload（顺序上传 + 进度 + 成功文案）都走这几个函数，
// 这里锁的是那几个视图分支的行为。
import {
  DELETE_CANCELLED,
  DELETE_FAILED,
  DELETE_SUCCEEDED,
  computeUploadPercent,
  describeBatchDeleteResult,
  describeUploadSuccess,
  hasDeletedAnyFile,
  isConfirmCancellation,
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
  // 模板上的三处调用点（:32 / :78 / :128）都是 @click 事件处理器直调，
  // Vue 不会接管处理器返回的 Promise：这里按同样的方式调用并把返回的 Promise 丢掉，
  // 模拟"点了取消"以后有没有拒绝逃逸到进程的 unhandledRejection。
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
