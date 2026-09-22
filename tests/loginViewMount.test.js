// Login.vue 的挂载用例：真 mount 视图，走完一次登录（issue #180 验收 2）。
//
// 由来：`git grep Login.vue tests/` 此前**零命中**。登录是整条链路的入口，
// 它的胶水（表单校验 -> store.login -> 成功提示 -> 按 redirect 跳转 / 失败时把
// 错误留在表单上方）此前只有阅读证据。
//
// 与既有用例的分工：`backend/tests/test_auth_service.py` 等测的是**服务端**校验；
// 本文件测的是**视图**这一侧的接线：请求发没发、发的是什么、跳去哪、错误落在哪。
//
// 替身只扎在 `api/request.js`（HTTP 出口）。user store、el-form 的真校验
//（async-validator 由 vueMount 的垫片让它在 Node 下真的跑起来）、ElMessage 的补丁
// 都是既有实现 —— 所以「校验没过就不发请求」是真校验拦下的，不是假象。
//
// vue-router 必须动态 import（它 import 'vue'，见 chatViewMount.test.js 的同一条说明）。

import test from 'node:test'
import assert from 'node:assert/strict'
import { mountSfc } from './helpers/vueMount.js'
import { calls, callsOf, resetRequestStub, respond } from './helpers/stubApiRequest.js'

const { createMemoryHistory, createRouter } = await import('vue-router')

const MODULES = {
  'api/request.js': new URL('./helpers/stubApiRequest.js', import.meta.url).href,
}

const USERNAME_INPUT = 'input[placeholder="请输入用户名"]'
const PASSWORD_INPUT = 'input[placeholder="请输入密码"]'
const PROFILE = { username: '张三', avatar: '', created_at: '2024-01-01T00:00:00Z' }

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

function createTestRouter() {
  const blank = { render: () => null }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'Login', component: blank },
      { path: '/chat', name: 'Chat', component: blank },
      { path: '/knowledge', name: 'Knowledge', component: blank },
    ],
  })
}

async function mountLogin({ login, route = '/login' } = {}) {
  resetRequestStub()
  localStorage.clear()

  respond('get', (url) => (url === '/user/profile' ? PROFILE : {}))
  respond('post', (url, body) => {
    if (url !== '/auth/login') return {}
    if (login) return login(url, body)
    return { token: 'issued-token', username: body.username }
  })

  const router = createTestRouter()
  await router.push(route)
  await router.isReady()

  const view = await mountSfc('views/Login.vue', { modules: MODULES, plugins: [router] })
  await settle(view)
  return { view, router }
}

// 往表单里打字。走真实 input 事件，el-form 的校验器才会拿到值。
async function typeCredentials(view, username, password) {
  const userInput = view.query(USERNAME_INPUT)
  const passwordInput = view.query(PASSWORD_INPUT)
  assert.ok(userInput, '登录表单应当渲染出用户名输入框')
  assert.ok(passwordInput, '登录表单应当渲染出密码输入框')

  userInput.value = username
  userInput.dispatchEvent(new Event('input', { bubbles: true }))
  passwordInput.value = password
  passwordInput.dispatchEvent(new Event('input', { bubbles: true }))
  await view.flush(3)
}

async function submitLogin(view) {
  const button = view.buttonByText('登录')
  assert.ok(button, '页面上应当有「登录」按钮')
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
// a. 主路径：登录成功 -> 提示 + 按 redirect 跳转 + 本地留存 token
// ---------------------------------------------------------------------------

test('登录成功：请求带上凭据，提示成功，并按 redirect 跳到目标页', async () => {
  const { view, router } = await mountLogin({ route: '/login?redirect=/knowledge' })

  try {
    await typeCredentials(view, '张三', 'secret123')
    const mark = await submitLogin(view)

    // 承重①：请求真的发出去了，且凭据就是表单里那两份。
    // 后面那次 `get /user/profile` 是 user store 的 `login()` 自己接的：拿到 token 之后
    // 顺手拉一次资料（`:740` 的 `await fetchProfile().catch(() => {})`）。一并钉住，
    // 免得将来把这次刷新删掉、而用例因为「只断言第一条」照样绿。
    assert.deepEqual(
      requestsSince(mark),
      ['post /auth/login', 'get /user/profile'],
      '点「登录」应当发一次登录请求，并在拿到 token 后拉一次用户资料'
    )
    // args = [url, body, config]（src/api/auth.js 的 login）。
    const credentials = callsOf('post')[0].args[1]
    assert.deepEqual(
      { username: credentials.username, password: credentials.password },
      { username: '张三', password: 'secret123' },
      '请求体应当带上表单里的用户名与密码'
    )

    // 承重②：成功提示 + 路由跳转（`route.query.redirect` 被尊重）。
    assert.deepEqual(
      view.messages,
      [{ level: 'success', message: '登录成功' }],
      '登录成功后应当弹一条成功提示'
    )
    await until(view, () => router.currentRoute.value.path === '/knowledge')
    assert.equal(
      router.currentRoute.value.path,
      '/knowledge',
      '登录成功后应当跳到 query.redirect 指定的页面'
    )

    // 承重③：token 落到 localStorage（后续请求的 Authorization 头靠它）。
    // 这里没有断言一个被 stub 的开关：user store 是真实实现，它自己写的 localStorage。
    assert.equal(localStorage.getItem('token'), 'issued-token', '登录成功后 token 应当写入本地存储')
    assert.equal(localStorage.getItem('username'), '张三', '登录成功后用户名应当写入本地存储')
  } finally {
    await close(view)
  }
})

test('没有 redirect 参数时：登录成功默认进 /chat', async () => {
  const { view, router } = await mountLogin({ route: '/login' })

  try {
    await typeCredentials(view, '李四', 'secret123')
    await submitLogin(view)

    await until(view, () => router.currentRoute.value.path === '/chat')
    assert.equal(router.currentRoute.value.path, '/chat', '没有 redirect 时应当默认跳 /chat')
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// b. 失败路径：错误留在表单上方，不跳转
// ---------------------------------------------------------------------------

test('登录失败：错误提示留在表单上，且不跳转', async () => {
  const { view, router } = await mountLogin({
    login: () => {
      throw new Error('用户名或密码错误')
    },
  })

  try {
    await typeCredentials(view, '张三', 'wrong-password')
    await submitLogin(view)

    assert.deepEqual(
      view.messages,
      [{ level: 'error', message: '用户名或密码错误' }],
      '登录失败应当弹一条错误提示'
    )
    // `loginError` 走的是模板 :42 的 el-alert —— 错误必须留在表单上，
    // 用户才能对照着改，而不是只有一条会消失的 toast。
    const alert = view.query('.el-alert')
    assert.ok(alert, '登录失败应当在表单上渲染出 el-alert')
    assert.match(alert.textContent, /用户名或密码错误/, 'el-alert 里应当是接口给的那条文案')
    assert.equal(router.currentRoute.value.path, '/login', '登录失败不得跳转')
    assert.equal(localStorage.getItem('token'), null, '登录失败不得写入 token')
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// c. 反向对照：真校验拦下的提交不会发请求
// ---------------------------------------------------------------------------

test('校验未通过：空表单与短密码都发不出请求（对照）', async () => {
  const { view } = await mountLogin()

  try {
    // 空表单。
    const markEmpty = await submitLogin(view)
    assert.deepEqual(requestsSince(markEmpty), [], '空表单不该发出登录请求')
    assert.deepEqual(view.messages, [], '空表单不该弹任何提示')

    // 密码不足 6 位（rules 里的 min: 6）。
    await typeCredentials(view, '张三', '123')
    const markShort = await submitLogin(view)
    assert.deepEqual(requestsSince(markShort), [], '短密码不该发出登录请求')
    assert.deepEqual(view.messages, [], '校验没过不该弹任何提示')

    // 控制项：把密码补足之后同一个按钮就能发出去 —— 证明上面的「没发出去」
    // 是校验拦下的，而不是按钮坏了或选择器没命中。
    await typeCredentials(view, '张三', 'secret123')
    const markOk = await submitLogin(view)
    assert.deepEqual(
      requestsSince(markOk),
      ['post /auth/login', 'get /user/profile'],
      '校验通过后应当正常发出请求（登录 + 紧随其后的资料刷新）'
    )
  } finally {
    await close(view)
  }
})
