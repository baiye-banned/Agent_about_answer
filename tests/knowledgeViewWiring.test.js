import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

// Knowledge.vue 的接线断言（issue #83 第 7、8、9 项）。
//
// 这三项的行为本体已经抽到 src/utils 下并在各自的用例里做行为级断言
// （knowledgeFeedback.test.js 的 refreshAfterDelete、fileListRequest.test.js 的时序守卫、
// detailPreview.test.js 的作废语义）。视图层剩下的是「有没有接上」——
// 没有接上时行为用例全绿而线上依旧出问题，所以这里按仓库既有做法
// （knowledgeUploadTypes.test.js 静态读后端文件）把接线钉死。
const VIEW = readFileSync(
  fileURLToPath(new URL('../src/views/Knowledge.vue', import.meta.url)),
  'utf8'
)

// 取一个顶层函数声明的函数体，供「这个入口有没有接上」的断言使用。
function functionBody(name) {
  const start = VIEW.indexOf(`async function ${name}(`)
  assert.ok(start > -1, `没有找到函数 ${name}`)
  const boundaries = ['\nasync function ', '\nfunction ']
    .map((marker) => VIEW.indexOf(marker, start + 1))
    .filter((index) => index > -1)
  return VIEW.slice(start, Math.min(...boundaries))
}

test('三个删除入口的刷新都经 refreshAfterDelete 收口（第 7 项）', () => {
  for (const name of ['deleteKnowledgeBase', 'confirmDelete', 'confirmBatchDelete']) {
    const body = functionBody(name)
    assert.ok(body.includes('refreshAfterDelete({'), `${name} 的刷新没有接错误分支`)
    assert.ok(body.includes('notifyError: notifyDeleteError'), `${name} 没有复用 notifyDeleteError 出口`)

    // 刷新调用必须落在 refreshAfterDelete 的参数里（即它的 refresh 闭包内），
    // 才吃得到错误分支。修复前这里是无保护的裸 await，刷新一失败就逃逸成未捕获拒绝。
    const guardIndex = body.indexOf('refreshAfterDelete({')
    for (const call of ['await refreshKnowledgeBaseAndFiles()', 'await fetchFiles()', 'await fetchKnowledgeBases()']) {
      const index = body.indexOf(call)
      assert.ok(
        index === -1 || index > guardIndex,
        `${name} 的 ${call} 落在 refreshAfterDelete 之外，没有被错误分支覆盖`
      )
    }
  }

  // 上传路径的刷新有自己的 try/catch（既有行为，不属本项），不计入这三条入口。
  assert.equal((VIEW.match(/refreshAfterDelete\(\{/g) || []).length, 3)
})

test('文件列表取数接在带时序守卫的 loader 上（第 8 项）', () => {
  assert.ok(VIEW.includes('createFileListRequest({'))
  assert.ok(VIEW.includes('return fileList.load()'))
  // 修复前 fetchFiles 直接写 allFiles，没有任何时序标识。
  assert.ok(!VIEW.includes('allFiles.value = Array.isArray(response) ? response : []'))
  // 守卫里的 invalidate() 必须真的有调用方：卸载时作废在飞的列表请求，
  // 否则它只是「写了没人用」的装饰（对抗评审指出的静态漏接）。
  assert.equal((VIEW.match(/fileList\.invalidate\(\)/g) || []).length, 1)
  assert.ok(VIEW.includes('onBeforeUnmount(() => fileList.invalidate())'))
})

test('详情预览：卸载作废在飞请求，失败只提示一次（第 9 项）', () => {
  // 卸载钩子必须真的把 closeDetail（= detailPreview 的 close，序号自增）接上。
  assert.ok(VIEW.includes('onBeforeUnmount(() => closeDetail())'))
  // 失败文案只由预览区红字给出，抑制拦截器的顶部 toast。
  assert.ok(VIEW.includes('knowledgeAPI.getContent(id, { silent: true })'))
})
