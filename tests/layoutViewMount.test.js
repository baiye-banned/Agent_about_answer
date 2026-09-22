// Layout.vue 的挂载用例：真 mount 外壳视图，覆盖「布局切换」主路径（issue #180 验收 2）。
//
// 由来：`git grep Layout.vue tests/` 此前**零命中** —— 这个视图（侧栏菜单、历史对话列、
// 账户区、`<router-view/>`）一行执行证据都没有。它是所有工作台页面的外壳，
// 却因为「看起来只是排版」而落在覆盖缺口里。
//
// 与既有用例的分工：
//   tests/chatStore.test.js  测 store 的会话状态机（进/出管理模式、选中、全选）
//   本文件                    视图真的跑起来之后：菜单跟着路由走、会话列渲染的是 store 的值、
//                            账户区显示的是 user store 的名字
//
// 替身只扎在 `api/request.js`（HTTP 出口）。chat/user 两个 pinia store、vue-router、
// Element Plus 的 el-menu 全是真货 —— 所以「布局切换」是真点菜单、真导航、真重算
// activeMenu，而不是断言一个被 stub 的开关。
//
// vue-router 必须动态 import（它 import 'vue'，见 chatViewMount.test.js 里的同一条说明）。

import test from 'node:test'
import assert from 'node:assert/strict'
import { mountSfc } from './helpers/vueMount.js'
import { calls, callsOf, resetRequestStub, respond } from './helpers/stubApiRequest.js'

const { createMemoryHistory, createRouter } = await import('vue-router')

const MODULES = {
  'api/request.js': new URL('./helpers/stubApiRequest.js', import.meta.url).href,
}

const CONV1 = { id: 'c1', title: '第一段对话' }
const CONV2 = { id: 'c2', title: '第二段对话' }
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
      { path: '/', redirect: '/chat' },
      { path: '/chat', name: 'Chat', component: blank },
      { path: '/chat/:id', name: 'ChatDetail', component: blank },
      { path: '/knowledge', name: 'Knowledge', component: blank },
      { path: '/profile', name: 'UserProfile', component: blank },
      { path: '/login', name: 'Login', component: blank },
    ],
  })
}

async function mountLayout({ conversations = [CONV1, CONV2], route = '/chat' } = {}) {
  resetRequestStub()
  // 有 token 才会真的去拉 profile（useUserStore.fetchProfile 在无 token 时直接返回）。
  localStorage.setItem('token', 'test-token')
  localStorage.setItem('username', '张三')

  respond('get', (url) => {
    if (url === '/chat/conversations') return conversations
    if (url === '/user/profile') return PROFILE
    return {}
  })

  const router = createTestRouter()
  await router.push(route)
  await router.isReady()

  const view = await mountSfc('views/Layout.vue', { modules: MODULES, plugins: [router] })
  await settle(view)
  return { view, router }
}

// 侧栏菜单项：文案与 :index 一一对应（模板 :15-22）。
function menuItem(view, label) {
  return view
    .queryAll('.el-menu-item')
    .find((node) => node.textContent.trim() === label)
}

function activeMenuItems(view) {
  return view
    .queryAll('.el-menu-item.is-active')
    .map((node) => node.textContent.trim())
}

async function close(view) {
  view.elementPlusUnpatch()
  await view.unmount()
  localStorage.clear()
}

// ---------------------------------------------------------------------------
// a. 主路径：布局切换（点菜单 -> 路由跳转 -> 激活态跟着走）
// ---------------------------------------------------------------------------

test('布局切换：点「知识库管理」菜单项跳转到 /knowledge，激活态随之切换', async () => {
  const { view, router } = await mountLayout({ route: '/chat' })

  try {
    assert.deepEqual(activeMenuItems(view), ['智能问答'], '进入 /chat 时「智能问答」应当处于激活态')

    const item = menuItem(view, '知识库管理')
    assert.ok(item, '侧栏应当渲染出「知识库管理」菜单项')
    item.click()
    await until(view, () => router.currentRoute.value.path === '/knowledge')
    await settle(view)

    // 承重：路由真的被推走了（`el-menu` 的 `:router="true"` 接线），
    // 且 `activeMenu` computed 跟着 route.path 重算 —— 少了它，点了菜单高亮还停在原处。
    assert.equal(router.currentRoute.value.path, '/knowledge', '点菜单项应当把路由推到 /knowledge')
    assert.deepEqual(activeMenuItems(view), ['知识库管理'], '激活态应当跟着路由切换')

    // 切回来同样成立（断言不是「只在新路径上碰巧成立」）。
    menuItem(view, '智能问答').click()
    await until(view, () => router.currentRoute.value.path === '/chat')
    await settle(view)
    assert.deepEqual(activeMenuItems(view), ['智能问答'], '切回 /chat 时激活态应当切回来')

    // 上面两条都经由「点菜单」——而 `el-menu` 在被点击时自己也会更新内部高亮，
    // 所以单靠点击区分不出 `activeMenu` 到底有没有跟着 `route.path` 重算。
    // 这条**不碰菜单**、直接推路由：只有 computed 真的重算，高亮才会跟上。
    await router.push('/knowledge')
    await settle(view)
    assert.deepEqual(
      activeMenuItems(view),
      ['知识库管理'],
      '不经菜单、直接改路由时激活态也应当跟上（这才钉住 activeMenu 跟着 route.path）',
    )
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// b. 外壳数据：历史对话列与账户区渲染的是两个 store 的真实值
// ---------------------------------------------------------------------------

test('挂载：历史对话列与账户区渲染的是 store 的真实值', async () => {
  const { view } = await mountLayout()

  try {
    // 挂载时真的拉了会话列表与用户资料（壳自己 onMounted 里的两件事）。
    assert.ok(
      calls.some((entry) => entry.method === 'get' && entry.args[0] === '/chat/conversations'),
      '挂载时应当拉一次历史对话'
    )
    assert.ok(
      calls.some((entry) => entry.method === 'get' && entry.args[0] === '/user/profile'),
      '挂载时应当拉一次用户资料'
    )

    assert.match(view.text(), /第一段对话/)
    assert.match(view.text(), /第二段对话/)
    // 账户区：displayName / avatarText 都来自 user store（profile 回来之后）。
    assert.match(view.text(), /张三/)
    assert.match(view.text(), /企业知识库/, '侧栏标题应当渲染')
  } finally {
    await close(view)
  }
})

// ---------------------------------------------------------------------------
// c. 交互：管理模式开关与「新建对话」都把壳的状态改对了
// ---------------------------------------------------------------------------

test('管理模式开关与「新建对话」：壳的接线真的改了 store 里的状态', async () => {
  const { view, router } = await mountLayout({ route: '/chat/c1' })

  try {
    // 进入管理模式：模板 :44 的管理区（全选 / 删除选中 / 完成）随之出现。
    // 「历史对话」标题行里的两个图标按钮：第一个是管理模式开关、第二个是新建对话。
    const circleButtons = view
      .queryAll('button')
      .filter((node) => node.classList.contains('is-circle'))
    assert.ok(circleButtons.length >= 2, '标题行应当渲染管理对话与新建对话两个按钮')

    circleButtons[0].click()
    await until(view, () => view.text().includes('全选'))
    assert.match(view.text(), /已选 0 \/ 2/, '进入管理模式后应当出现管理区并显示选中计数')

    // 「新建对话」：清空当前会话并把路由推回 /chat。
    circleButtons[1].click()
    await until(view, () => router.currentRoute.value.path === '/chat')
    assert.equal(router.currentRoute.value.path, '/chat', '点「新建对话」应当回到 /chat')
    const manageCleared = !view.text().includes('全选')
    assert.equal(manageCleared, true, '「新建对话」应当顺带退出管理模式')
  } finally {
    await close(view)
  }
})
