import test from 'node:test'
import assert from 'node:assert/strict'
import {
  conversationExportFilename,
  getConversationExportErrorMessage,
  saveConversationMarkdown,
} from '../src/utils/chatExport.js'

test('导出文件名：UTF-8 filename* 优先于 ASCII filename，并清理路径和控制字符', () => {
  const header = `attachment; filename="conversation.md"; filename*=UTF-8''${encodeURIComponent('采购/报销：问题？\n.md')}`
  assert.equal(conversationExportFilename(header, '旧标题'), '采购报销：问题？.md')
  assert.equal(conversationExportFilename('attachment; filename="../../answer.md"'), 'answer.md')
})

test('导出文件名：编码损坏时使用普通文件名，缺失时使用点击时标题或安全默认名', () => {
  assert.equal(conversationExportFilename(`attachment; filename*=UTF-8''%E4%ZZ; filename="answer.md"`), 'answer.md')
  assert.equal(conversationExportFilename('', '标题/问题？'), '标题问题？.md')
  assert.equal(conversationExportFilename('', '/<>:\\?*\n'), 'conversation.md')
  assert.equal(conversationExportFilename('attachment; filename="CON.md"', '正常标题'), '正常标题.md')
  assert.equal(conversationExportFilename('attachment; filename="answer; one.md"'), 'answer; one.md')
})

function mockDownload(t, { clickError } = {}) {
  const events = []
  const link = {
    click() {
      events.push(['click', this.href, this.download])
      if (clickError) throw clickError
    },
    remove() { events.push(['remove']) },
  }
  const originalDocument = globalThis.document
  globalThis.document = {
    createElement(tag) {
      assert.equal(tag, 'a')
      return link
    },
    body: { appendChild() { events.push(['append']) } },
  }
  t.after(() => {
    if (originalDocument === undefined) delete globalThis.document
    else globalThis.document = originalDocument
  })
  t.mock.method(URL, 'createObjectURL', (blob) => {
    events.push(['create', blob])
    return 'blob:export-download'
  })
  t.mock.method(URL, 'revokeObjectURL', (url) => { events.push(['revoke', url]) })
  return events
}

const afterDownload = () => new Promise((resolve) => setTimeout(resolve, 5))

test('下载：保存原始 Markdown Blob，采用响应头文件名，移除链接并延后释放 URL', async (t) => {
  const events = mockDownload(t)
  const blob = new Blob(['# 采购\n\n全文'], { type: 'text/markdown;charset=utf-8' })
  const response = {
    status: 200,
    data: blob,
    headers: { 'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent('采购.md')}` },
  }
  assert.equal(saveConversationMarkdown(response, '点击时标题'), '采购.md')
  assert.deepEqual(events, [
    ['create', blob], ['append'], ['click', 'blob:export-download', '采购.md'], ['remove'],
  ])
  await afterDownload()
  assert.deepEqual(events.at(-1), ['revoke', 'blob:export-download'])
})

test('下载：点击抛错仍移除链接并释放 URL', async (t) => {
  const events = mockDownload(t, { clickError: new Error('download unavailable') })
  assert.throws(() => saveConversationMarkdown({
    status: 200,
    data: new Blob(['# chat'], { type: 'text/markdown' }),
    headers: {},
  }), /download unavailable/)
  assert.deepEqual(events.at(-1), ['remove'])
  await afterDownload()
  assert.deepEqual(events.at(-1), ['revoke', 'blob:export-download'])
})

test('下载：HTTP 失败或成功返回 JSON/HTML 都不能创建下载 URL', (t) => {
  const events = mockDownload(t)
  for (const response of [
    { status: 500, data: new Blob(['partial'], { type: 'text/markdown' }), headers: {} },
    { status: 200, data: new Blob(['{"detail":"error"}'], { type: 'application/json' }), headers: {} },
    { status: 200, data: new Blob(['{"detail":"error"}'], { type: 'text/markdown' }), headers: { 'content-type': 'application/json' } },
    { status: 200, data: new Blob(['<html>login</html>'], { type: 'text/html' }), headers: {} },
    { status: 200, data: { detail: 'error' }, headers: { 'content-type': 'text/markdown' } },
  ]) {
    assert.throws(() => saveConversationMarkdown(response), /导出失败/)
  }
  assert.equal(events.length, 0)
})

test('导出错误：Blob JSON 可读，401/404 及无法读取的正文都有友好提示', async () => {
  assert.equal(await getConversationExportErrorMessage({ response: { status: 401 } }), '登录已过期，请重新登录')
  assert.equal(await getConversationExportErrorMessage({ response: { status: 404 } }), '对话不存在或无权导出')
  assert.equal(await getConversationExportErrorMessage({ response: {
    status: 500,
    data: new Blob([JSON.stringify({ detail: '暂时无法生成导出文件' })], { type: 'application/json' }),
  } }), '暂时无法生成导出文件')
  assert.equal(await getConversationExportErrorMessage({ response: { status: 502, data: new Blob(['bad gateway']) } }), '导出失败，请稍后重试')
  assert.equal(await getConversationExportErrorMessage(new Error('Network Error')), '导出失败，请稍后重试')
})
