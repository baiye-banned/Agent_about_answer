// UserProfile.vue 的挂载用例：真 mount 资料页，走完一次「修改密码」（issue #180 验收 2）。
//
// 由来：`git grep UserProfile.vue tests/` 此前**零命中**。这一页的胶水
//（挂载拉资料、三份密码字段的联动校验、成功后清空表单与清校验态、头像上传前的本地校验）
// 此前只有阅读证据。
//
// 与既有用例的分工：`tests/knowledgeUploadTypes.test.js` 之类测的是纯校验函数的本体；
// 本文件测的是**视图接线**：点「保存修改」到底发了什么、成功后表单有没有被清干净。
//
// 替身只扎在 `api/request.js`（HTTP 出口）。user store、`utils/fileValidation` 的真实校验、
// el-form 的真校验（async-validator 垫片让它在 Node 下真的跑）全是真货。
//
// 本视图不依赖路由（没有 useRoute/useRouter），所以不需要装 router 插件。

import test from 'node:test'
import assert from 'node:assert/strict'
import { mountSfc } from './helpers/vueMount.js'
import { calls, callsOf, resetRequestStub, respond } from './helpers/stubApiRequest.js'

const MODULES = {
  'api/request.js': new URL('./helpers/stubApiRequest.js', import.meta.url).href,
}

const PROFILE = { username: '张三', avatar: '/uploads/avatars/a.png', created_at: '2024-01-01T00:00:00Z' }

const OLD_PASSWORD_INPUT = 'input[placeholder="请输入当前密码"]'
const NEW_PASSWORD_INPUT = 'input[placeholder="请再次输入新密码"]'
// 三个密码框的 placeholder 里，只有「新密码」是「请输入新密码」（另两个一个是「当前」、
// 一个是「再次」），所以按 placeholder 精确匹配即可，不必依赖顺序。
const NEW_PASSWORD_ONLY = 'input[placeholder="请输入新密码"]'

const settle = async (view) => {
  await view.flush(8)
  await new Promise((resolve) => setTimeout(resolve, 40))
}

async function until(view, predicate, budgetMs = 2000) {
  const deadline = Date.now() + budgetMs
  for (;;) {
    if (predicate()) return true
    if (Date.now() > deadline) return false
    await view.flush(1)
    await new Promise((resolve) => setTimeout(resolve, 2))
  }
}

async function mountProfile({ updatePassword } = {}) {
  resetRequestStub()
  localStorage.setItem('token', 'test-token')
  localStorage.setItem('username', '张三')

  respond('get', (url) => (url === '/user/profile' ? PROFILE : {}))
  respond('put', (url, body) => {
    if (url !== '/user/password') return {}
    if (updatePassword) return updatePassword(url, body)
    return {}
  })

  const view = await mountSfc('views/UserProfile.vue', { modules: MODULES })
  await settle(view)
  return view
}

function typeInto(view, selector, value) {
  const input = view.query(selector)
  assert.ok(input, `应当渲染出 ${selector} 输入框`)
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  return input
}

async function submitPassword(view) {
  const button = view.buttonByText('保存修改')
  assert.ok(button, '页面上应当有「保存修改」按钮')
  const mark = calls.length
  button.click()
  await settle(view)
  return mark
}

function requestsSince(mark) {
  return calls.slice(mark).map((entry) => `${entry.method} ${entry.args[0]}`)
}

async function close(view) {
  view.elementPlusUnpatch()
  await view.unmount()
  localStorage.clear()
}

// ---------------------------------------------------------------------------
// a. 主路径：修改密码成功 -> 请求内容正确 + 提示 + 表单清空
// ---------------------------------------------------------------------------

test('修改密码成功：请求只带两份密码，成功后提示并清空三个输入框', async () => {
  const view = await mountProfile()

  try {
    // 挂载期真的拉了资料，且渲染出来的是接口给的值（username / 创建时间）。
    assert.ok(
      calls.some((entry) => entry.method === 'get' && entry.args[0] === '/user/profile'),
      '挂载时应当拉一次用户资料'
    )
    assert.match(view.text(), /张三/, '账号信息里应当渲染用户名')

    typeInto(view, OLD_PASSWORD_INPUT, 'old-secret')
    typeInto(view, NEW_PASSWORD_ONLY, 'new-secret')
    typeInto(view, NEW_PASSWORD_INPUT, 'new-secret')
    await view.flush(3)

    const mark = await submitPassword(view)

    // 承重①：请求发出去且字段名正确。`confirmPassword` 只是本地确认，**不得**上行
    //（后端只接受 old_password / new_password）。
    assert.deepEqual(requestsSince(mark), ['put /user/password'], '点「保存修改」应当恰好发一次请求')
    // args = [url, body, config]（src/api/user.js 的 updatePassword）。
    assert.deepEqual(
      callsOf('put')[0].args[1],
      { old_password: 'old-secret', new_password: 'new-secret' },
      '上行字段应当只有当前密码与新密码'
    )
    assert.deepEqual(
      view.messages,
      [{ level: 'success', message: '密码修改成功' }],
      '成功后应当弹一条成功提示'
    )

    // 承重②：三个输入框都被清空（`:160-163`）。少了它，密码明文留在页面上，
    // 用户下次回来还看得见自己上一次输的密码。
    assert.deepEqual(
      {
        old: view.query(OLD_PASSWORD_INPUT).value,
        next: view.query(NEW_PASSWORD_ONLY).value,
        confirm: view.query(NEW_PASSWORD_INPUT).value,
      },
      { old: '', next: '', confirm: '' },
      '修改成功后三个输入框都应当清空'
    )
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// b. 失败路径：错误提示 + 不清空用户输入（否则要重打三遍）
// ---------------------------------------------------------------------------

test('修改密码失败：提示错误，且用户输入不被清掉', async () => {
  const view = await mountProfile({
    updatePassword: () => {
      throw new Error('当前密码不正确')
    },
  })

  try {
    typeInto(view, OLD_PASSWORD_INPUT, 'wrong-old')
    typeInto(view, NEW_PASSWORD_ONLY, 'new-secret')
    typeInto(view, NEW_PASSWORD_INPUT, 'new-secret')
    await view.flush(3)

    await submitPassword(view)

    assert.deepEqual(
      view.messages,
      [{ level: 'error', message: '当前密码不正确' }],
      '失败时应当把接口给的文案弹出来'
    )
    assert.equal(
      view.query(OLD_PASSWORD_INPUT).value,
      'wrong-old',
      '失败时不该清空输入，用户要能改一个字重试'
    )
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// c. 反向对照：两次新密码不一致时是真校验拦下的，不发请求
// ---------------------------------------------------------------------------

test('两次新密码不一致：校验拦下请求（对照）', async () => {
  const view = await mountProfile()

  try {
    typeInto(view, OLD_PASSWORD_INPUT, 'old-secret')
    typeInto(view, NEW_PASSWORD_ONLY, 'new-secret')
    typeInto(view, NEW_PASSWORD_INPUT, 'different')
    await view.flush(3)

    const mark = await submitPassword(view)
    assert.deepEqual(requestsSince(mark), [], '两次新密码不一致时不该发出请求')
    assert.deepEqual(view.messages, [], '校验没过不该弹任何提示')

    // 控制项：改成一致之后，同一个按钮就能发出去 —— 证明上面的「没发出去」
    // 是 confirmPassword 的校验器拦下的，不是按钮坏了或选择器没命中。
    typeInto(view, NEW_PASSWORD_INPUT, 'new-secret')
    await view.flush(3)
    const markOk = await submitPassword(view)
    assert.deepEqual(requestsSince(markOk), ['put /user/password'], '两次一致后应当正常发出请求')
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// d. 头像：本地校验先拦（不合格的文件连上传请求都不发）
// ---------------------------------------------------------------------------

test('头像上传：非图片文件在本地就被拦下，不发上传请求', async () => {
  const view = await mountProfile()

  try {
    // el-upload 的 before-upload 钩子：直接调组件暴露的那个函数拿不到，
    // 这里用真实路径 —— 构造一个 File 交给 el-upload 内部的 input。
    const fileInput = view.query('.el-upload input[type="file"]')
    assert.ok(fileInput, '账号信息卡片里应当有头像上传用的 file input')

    const file = new File(['not an image'], 'notes.txt', { type: 'text/plain' })
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true })
    fileInput.dispatchEvent(new Event('change', { bubbles: true }))
    await until(view, () => view.messages.length > 0)
    await settle(view)

    assert.deepEqual(
      view.messages,
      [{ level: 'error', message: '仅支持 png、jpg、jpeg、webp 格式头像' }],
      '非图片文件应当被本地校验拦下并给出文案'
    )
    assert.deepEqual(
      calls.filter((entry) => entry.method === 'post'),
      [],
      '被本地校验拦下的文件不该发出上传请求'
    )
  } finally {
    await close(view)
  }
})
