import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// 覆盖 issue #49：前端能选中的类型必须与后端白名单一致，且越界的文件在选中阶段就被拦下。
// 前端白名单（accept 属性 + 提示文案）单一来源在 src/utils/knowledgeFeedback.js；
// 后端白名单在此静态解析 backend/service/utils_service.py，两边一旦漂移即失败。
import {
  KNOWLEDGE_UPLOAD_ACCEPT,
  KNOWLEDGE_UPLOAD_EXTENSIONS,
  KNOWLEDGE_UPLOAD_HINT,
  describeSkippedUploadFiles,
  describeUploadFailure,
  isSupportedUploadFile,
  partitionUploadFiles,
} from '../src/utils/knowledgeFeedback.js'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// issue #49 里那 6 种「可选中但提交必然 400」的扩展名。
const REJECTED_BY_BACKEND = ['.json', '.csv', '.yaml', '.yml', '.xml', '.log']

function readRepoFile(relativePath) {
  return readFileSync(path.join(repoRoot, relativePath), 'utf8')
}

// 只认 KNOWLEDGE_UPLOAD_TYPES 字典字面量里的扩展名键，不执行 Python、不改后端。
function backendUploadExtensions() {
  const source = readRepoFile('backend/service/utils_service.py')
  const block = /KNOWLEDGE_UPLOAD_TYPES = \{([\s\S]*?)\n\}/.exec(source)
  assert.ok(block, 'backend/service/utils_service.py 中找不到 KNOWLEDGE_UPLOAD_TYPES 字面量')
  return [...block[1].matchAll(/"(\.[^"]+)"\s*:/g)].map((match) => match[1])
}

test('前端白名单与后端 KNOWLEDGE_UPLOAD_TYPES 完全一致', () => {
  assert.deepEqual([...KNOWLEDGE_UPLOAD_EXTENSIONS], backendUploadExtensions())
  // 后端白名单解析结果本身要非空，否则上面的相等断言可能被空集合同构地蒙混过去。
  assert.ok(backendUploadExtensions().includes('.pdf'))
})

test('accept 属性与提示文案都从同一份白名单派生', () => {
  assert.equal(KNOWLEDGE_UPLOAD_ACCEPT, KNOWLEDGE_UPLOAD_EXTENSIONS.join(','))
  assert.equal(KNOWLEDGE_UPLOAD_HINT, `支持 ${KNOWLEDGE_UPLOAD_EXTENSIONS.map((ext) => ext.slice(1)).join('、')}，支持多选批量上传`)
  // 提示文案只能列白名单里的类型，不能出现旧的 json/csv/yaml/yml/xml/log。
  for (const ext of REJECTED_BY_BACKEND) {
    assert.ok(!KNOWLEDGE_UPLOAD_HINT.includes(ext.slice(1)), `${ext} 不应出现在提示文案里`)
  }
})

test('Knowledge.vue 的上传入口直接绑定这份白名单，不再自带第二份清单', () => {
  const source = readRepoFile('src/views/Knowledge.vue')

  assert.match(source, /:accept="KNOWLEDGE_UPLOAD_ACCEPT"/)
  assert.match(source, /\{\{ KNOWLEDGE_UPLOAD_HINT \}\}/)
  // 页面里不得再留一份写死的扩展名列表（旧 acceptTypes 或直接写 .json 这类字面量）。
  assert.ok(!/\bacceptTypes\b/.test(source), 'Knowledge.vue 不应再声明 acceptTypes')
  assert.ok(
    !/\.(json|csv|yaml|yml|xml|log)\b/.test(source),
    'Knowledge.vue 不应再出现白名单外的扩展名'
  )
})

test('partitionUploadFiles 在选中阶段拦下后端不接受的 6 种扩展名', () => {
  const files = [
    { name: '报告.pdf' },
    { name: 'a.json' },
    { name: 'a.csv' },
    { name: 'a.yaml' },
    { name: 'a.yml' },
    { name: 'a.xml' },
    { name: 'a.log' },
    { name: '说明.docx' },
  ]

  const { supported, rejected } = partitionUploadFiles(files)

  assert.deepEqual(
    supported.map((file) => file.name),
    ['报告.pdf', '说明.docx']
  )
  assert.deepEqual(rejected, REJECTED_BY_BACKEND.map((ext) => `a${ext}`))
})

test('partitionUploadFiles 放行白名单、大小写不敏感、无扩展名一律拒绝', () => {
  for (const name of ['a.txt', 'a.md', 'a.docx', 'a.pdf', 'A.PDF', '报告.Txt']) {
    assert.equal(isSupportedUploadFile(name), true, `${name} 应放行`)
  }
  // 与后端 Path(filename).suffix 的语义对齐：无扩展名、点开头的隐藏文件、结尾点都不放行。
  for (const name of ['README', 'archive.tar.gz', '.env', 'a.', '']) {
    assert.equal(isSupportedUploadFile(name), false, `${name} 应拒绝`)
  }

  assert.deepEqual(partitionUploadFiles([]), { supported: [], rejected: [] })
  assert.deepEqual(partitionUploadFiles(undefined), { supported: [], rejected: [] })
})

test('describeSkippedUploadFiles 点名被跳过的文件与支持的格式', () => {
  const message = describeSkippedUploadFiles(['a.json', 'b.csv'])

  assert.match(message, /a\.json、b\.csv/)
  assert.match(message, /仅支持 txt、md、docx、pdf/)
})

test('describeUploadFailure 点名失败文件并说明同批其余文件的去向', () => {
  // 首败即止：失败文件之后还有文件 → 那些文件没有上传。
  assert.equal(
    describeUploadFailure('b.json', 2, '仅支持 txt、md、docx、pdf 格式的文件'),
    '「b.json」上传失败：仅支持 txt、md、docx、pdf 格式的文件；同批剩余 2 个文件未上传'
  )
  // 失败的是本批最后一个 → 其余文件都已上传。
  assert.equal(
    describeUploadFailure('c.pdf', 0, '文件不能超过 20MB'),
    '「c.pdf」上传失败：文件不能超过 20MB；同批其余文件均已上传'
  )
})
