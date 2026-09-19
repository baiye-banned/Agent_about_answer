import test from 'node:test'
import assert from 'node:assert/strict'

// 覆盖 src/utils/knowledgeFeedback.js：Knowledge.vue 的 confirmBatchDelete（批量删除反馈）
// 与 handleUpload（顺序上传 + 进度 + 成功文案）都走这几个函数，这里锁的是那两个视图分支的行为。
import {
  computeUploadPercent,
  describeBatchDeleteResult,
  describeUploadSuccess,
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
