// issue #186 前端侧：头像读取面改成鉴权之后，`<img>` 发不出 Authorization 头，头像是怎么到
// 用户眼前的。
//
// 后端把 `/uploads` 的匿名静态挂载换成了要求「身份 + 归属」的路由（见
// tests/test_uploads_anonymous_read_186.py），于是 `<el-avatar :src="库里那个路径">` 这种用法
// 在浏览器里必然 401：`<img src>` 的请求不带 Authorization 头，和匿名请求没有区别——**修复本身
// 会把「用户看得到自己的头像」这个功能一起挡掉**，而这一点在后端用例里是看不见的（后端用例
// 恰好是「本人带 token 取自己的头像必须成功」，它证明的是接口还在，不是用户还看得见）。
//
// 本文件钉的就是中间那一段：store 要带着 token 把图片取回来（走 api/user.js 的 getAvatar），
// 并把它变成 `<img>` 能用的 object URL；以及这件事不会漏内存、不会被乱序的响应盖回旧头像。
//
// 替身扎在模块边界（`@/api/user`、`@/api/auth`）：store 是真货，所以「什么时候发请求、拿回来的
// 东西怎么落地」都是真的在执行，只有网络是假的。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { register } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

const OLD_AVATAR = '/uploads/avatars/old-avatar.png'
const NEW_AVATAR = '/uploads/avatars/new-avatar.png'

// 网络替身：getAvatar 一律先挂起，由用例自己决定谁先落地——顺序就是本文件要测的东西之一。
const stubSource = [
  'const pending = []',
  'export const calls = { profile: [], upload: [], avatar: [] }',
  'let profileResult = null',
  'let profileError = null',
  'let avatarError = null',
  'let uploadResult = null',
  'export function resetStub() {',
  '  pending.length = 0',
  '  calls.profile.length = 0',
  '  calls.upload.length = 0',
  '  calls.avatar.length = 0',
  '  profileResult = null',
  '  profileError = null',
  '  avatarError = null',
  '  uploadResult = null',
  '}',
  'export function setProfile(result, error = null) { profileResult = result; profileError = error }',
  'export function setUploadResult(result) { uploadResult = result }',
  'export function setAvatarError(error) { avatarError = error }',
  'export function settleAvatar(path, bytes) {',
  '  const index = pending.findIndex((entry) => entry.path === path)',
  '  if (index < 0) throw new Error(`no in-flight avatar request for ${path}`)',
  '  const [entry] = pending.splice(index, 1)',
  '  const blob = new Blob([bytes], { type: "image/png" })',
  '  entry.resolve(blob)',
  '  return blob',
  '}',
  'export function pendingPaths() { return pending.map((entry) => entry.path) }',
  'export const userAPI = {',
  '  getProfile: async () => {',
  '    calls.profile.push(true)',
  '    if (profileError) throw profileError',
  '    return profileResult',
  '  },',
  '  uploadAvatar: async (file) => { calls.upload.push(file); return uploadResult },',
  '  getAvatar: (path) => new Promise((resolve, reject) => {',
  '    calls.avatar.push(path)',
  '    if (avatarError) { reject(avatarError); return }',
  '    pending.push({ path, resolve, reject })',
  '  }),',
  '}',
  '',
].join('\n')

const authSource = [
  'export const authAPI = {',
  '  login: async () => ({ token: "token-1", username: "alice" }),',
  '  logout: async () => {},',
  '}',
  '',
].join('\n')

// `#.mjs` 后缀与 helpers/vueSfcLoader.js 的 stubUrl 一致：data: URL 没有扩展名时
// Node 只能按兜底类型解析，带上才确定走 ESM。
const userStub = `data:text/javascript;base64,${Buffer.from(stubSource).toString('base64')}#.mjs`
const authStub = `data:text/javascript;base64,${Buffer.from(authSource).toString('base64')}#.mjs`

// 复用仓库既有的加载钩子（挂载用例那套）：它除了做 `@/` 别名解析与模块替换，还会把
// `import.meta.env` 重写成 `globalThis.__VITE_ENV__`——`src/utils/url.js` 的默认参数
// 在 Node 下会因此抛 TypeError，不是被测代码的问题。这里只借用同一套环境，不挂载组件。
globalThis.__VITE_ENV__ = Object.fromEntries(
  Object.entries(process.env).filter(([key]) => key.startsWith('VITE_'))
)
register(pathToFileURL(fileURLToPath(new URL('./helpers/vueSfcLoader.js', import.meta.url))).href, {
  data: {
    registry: { 'api/user.js': userStub, 'api/auth.js': authStub },
    srcRoot: fileURLToPath(new URL('../src/', import.meta.url)),
  },
})

// store 在 setup 里读 localStorage（Node 没有这个全局量）。
const storage = new Map()
globalThis.localStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
}

// object URL 的两个静态方法记账：createObjectURL 是唯一能把 Blob 变成 `<img>` 可用地址的地方，
// 它被调了几次、给出的是哪一份字节，就是「头像真的被取回来了」与「有没有漏一份 URL」的证据。
const created = []
const revoked = []
const realCreate = URL.createObjectURL
const realRevoke = URL.revokeObjectURL
URL.createObjectURL = (blob) => {
  const url = realCreate.call(URL, blob)
  created.push({ url, blob })
  return url
}
URL.revokeObjectURL = (url) => {
  revoked.push(url)
  realRevoke.call(URL, url)
}

const { createPinia, setActivePinia } = await import('pinia')
const { useUserStore } = await import('../src/stores/user.js')
const { calls, resetStub, setProfile, setUploadResult, setAvatarError, settleAvatar, pendingPaths } =
  await import(userStub)

function newStore({ token = 'token-1' } = {}) {
  storage.clear()
  if (token) storage.set('token', token)
  resetStub()
  created.length = 0
  revoked.length = 0
  setActivePinia(createPinia())
  return useUserStore()
}

const urlFor = (blob) => created.find((entry) => entry.blob === blob)?.url

// store 的方法要等 profile / 上传的响应落地之后才会去取头像，中间隔着若干次 microtask；
// 让它们跑完再断言，才不会把「还没发出去」看成「没发」。
const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

test('本人取自己的头像：带着 token 走接口，交给 <img> 的是 object URL', async () => {
  const store = newStore()
  setProfile({ username: 'alice', avatar: OLD_AVATAR })

  const profilePromise = store.fetchProfile()
  await flush()
  assert.deepEqual(pendingPaths(), [OLD_AVATAR], '头像没有走 api 取数，说明它还是被直接塞给 <img> 的')
  // 取数还没落地时不能先挂一个空的 src，也不能拿着库里的路径去顶（那正是 401 的那个路径）。
  assert.equal(store.avatarSrc, '')
  assert.notEqual(store.avatarSrc, OLD_AVATAR)

  const blob = settleAvatar(OLD_AVATAR, 'alice-avatar-bytes')
  await profilePromise

  assert.deepEqual(calls.avatar, [OLD_AVATAR])
  assert.equal(store.avatarSrc, urlFor(blob))
  assert.match(store.avatarSrc, /^blob:/)
  assert.equal(store.avatarUrl, OLD_AVATAR, '库里那个路径仍然保留在 profile 里，前端不改写它')
})

test('没有头像时不发取数请求，src 保持为空（el-avatar 回落到首字母）', async () => {
  const store = newStore()
  setProfile({ username: 'alice', avatar: '' })

  await store.fetchProfile()

  assert.deepEqual(calls.avatar, [])
  assert.equal(store.avatarSrc, '')
  assert.equal(created.length, 0)
})

test('非本地头像（如 OSS 直链）按原样交给 <img>，不往别人的域上带 Authorization 头', async () => {
  const store = newStore()
  setProfile({ username: 'alice', avatar: 'https://cdn.example.com/avatars/a.png' })

  await store.fetchProfile()

  assert.deepEqual(calls.avatar, [])
  assert.equal(store.avatarSrc, 'https://cdn.example.com/avatars/a.png')
  assert.equal(created.length, 0)
})

test('换头像：旧的 object URL 被回收，新的立刻可用', async () => {
  const store = newStore()
  setProfile({ username: 'alice', avatar: OLD_AVATAR })
  const firstProfile = store.fetchProfile()
  await flush()
  const oldBlob = settleAvatar(OLD_AVATAR, 'old')
  await firstProfile
  const oldUrl = urlFor(oldBlob)

  setUploadResult({ avatar: NEW_AVATAR })
  const uploadPromise = store.uploadAvatar('file')
  await flush()
  const newBlob = settleAvatar(NEW_AVATAR, 'new')
  await uploadPromise

  assert.equal(store.avatarUrl, NEW_AVATAR)
  assert.equal(store.avatarSrc, urlFor(newBlob))
  assert.notEqual(store.avatarSrc, oldUrl)
  assert.deepEqual(revoked, [oldUrl], '换头像后旧的那份图片字节还挂在内存里')
})

test('取不到头像（401/网络错误）时不冒泡：资料照常拿到，src 停在首字母', async () => {
  const store = newStore()
  setProfile({ username: 'alice', avatar: OLD_AVATAR })
  setAvatarError(new Error('401'))

  await store.fetchProfile()

  assert.equal(store.profile.username, 'alice')
  assert.equal(store.avatarSrc, '')
  assert.equal(created.length, 0)
})

test('挂载时那次取数与上传后那次取数乱序返回：只认最后发起的那一次，且不留下没人回收的 URL', async () => {
  const store = newStore()
  setProfile({ username: 'alice', avatar: OLD_AVATAR })
  const staleProfile = store.fetchProfile()
  await flush()

  // 上传完成后 store 会再发起一次取数：后发起的这一次必须先落地。
  setUploadResult({ avatar: NEW_AVATAR })
  const uploadPromise = store.uploadAvatar('file')
  await flush()
  assert.deepEqual(pendingPaths(), [OLD_AVATAR, NEW_AVATAR], '两次取数没有同时在飞，乱序这条用例就不是在测它')

  const freshBlob = settleAvatar(NEW_AVATAR, 'new')
  await uploadPromise
  const staleBlob = settleAvatar(OLD_AVATAR, 'old')
  await staleProfile

  assert.equal(store.avatarSrc, urlFor(freshBlob))
  assert.equal(urlFor(staleBlob), undefined, '落后的那一次也造了 object URL，这份 URL 没人持有、不会被回收')
  assert.deepEqual(revoked, [], '没有任何 URL 需要被回收：陈旧的那一次根本没产出 URL')
})

// ---------------------------------------------------------------------------
// 接线：两个渲染头像的视图必须消费 store 里取回来的那一个 src
// ---------------------------------------------------------------------------

// 行为本体在上面（store 那一层），这里按仓库既有做法（knowledgeViewWiring.test.js）静态读文件
// 钉住视图有没有接上：store 改对了、视图还直接拿库里的路径拼 `<img>` 的话，上面全部用例照样绿，
// 而线上头像依旧是 401。两个渲染头像的地方都要覆盖，漏一个就有一只头像不显示。
const AVATAR_VIEWS = ['src/views/Layout.vue', 'src/views/UserProfile.vue']

test('渲染头像的视图用 store 取回来的 src，不再自己拼库里那个路径', () => {
  for (const relative of AVATAR_VIEWS) {
    const source = readFileSync(fileURLToPath(new URL(`../${relative}`, import.meta.url)), 'utf8')
    assert.ok(source.includes(':src="avatarSrc"'), `${relative} 没有把 avatarSrc 绑在 <el-avatar> 上`)
    assert.ok(source.includes('userStore.avatarSrc'), `${relative} 的 avatarSrc 不是 store 里取回来的那一个`)
    assert.ok(
      !source.includes('normalizeApiAssetUrl'),
      `${relative} 还在把库里那个路径直接拼给 <img>：读取面已要鉴权，浏览器这条请求必 401`
    )
  }
})

test('退出登录：object URL 被回收，src 清空', async () => {
  const store = newStore()
  setProfile({ username: 'alice', avatar: OLD_AVATAR })
  const profilePromise = store.fetchProfile()
  await flush()
  const blob = settleAvatar(OLD_AVATAR, 'alice')
  await profilePromise
  const url = urlFor(blob)

  await store.logout()

  assert.deepEqual(revoked, [url])
  assert.equal(store.avatarSrc, '')
  assert.equal(store.profile, null)
})
