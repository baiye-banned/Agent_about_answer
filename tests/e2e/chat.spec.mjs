// 端到端验收：登录 → 建知识库 → 上传资料 → 提问 → 流式作答 → 引用可溯源。
//
// 这条用例覆盖的是「前端产物 + 后端 + 元数据库 + 向量库」串起来的真实链路，
// 唯一的替身是模型上游（tests/e2e/stub_llm_server.py）。替身只负责返回固定文本和
// 确定性向量，不替后端做检索、不替前端做渲染，因此链路里任何一环断掉都会体现为
// 断言失败，而不是被桩悄悄兜住。
//
// 关键的可证伪点：
//   * 标记串 E2E-STUB-ANSWER-OK 只存在于桩里，知识库夹具里没有 —— 答案里出现它，
//     说明本次回答确实经过了「桩 → 后端 → SSE → 前端渲染」全程，而不是回显检索原文。
//   * 引用行「引用来源：<文件名>」只在桩收到带 [来源: …] 的 prompt 时才会输出 ——
//     它出现，说明向量召回 + 上下文拼装确实把上传的资料喂进了模型。
//   * 答案里不得出现 [来源: …] 与夹具独有措辞 —— 用户看到的是答案，不是原始上下文。
import { test, expect } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FIXTURE_PATH = path.join(HERE, 'fixtures', 'travel-expense-policy.md')
const FIXTURE_NAME = 'travel-expense-policy.md'

// 以下常量与 tests/e2e/stub_llm_server.py 一一对应，改一边必须改另一边。
const ANSWER_MARKER = 'E2E-STUB-ANSWER-OK'
const ANSWER_BODY = '员工差旅报销必须在出差结束后的 10 个工作日内提交，逾期需要部门负责人审批。'
const CITATION = `引用来源：${FIXTURE_NAME}`

// 夹具里出现过、桩的回答里没有的措辞：用来证明答案不是检索原文的回显。
const FIXTURE_ONLY_PHRASE = '合规发票'

// 夹具正文第二段（报销时限）里的措辞，桩的固定回答里没有：参考资料抽屉里出现它，
// 说明检回的 chunk 带回了正文，而不只是一个文件名。
const FIXTURE_BODY_PHRASE = '30 个工作日'

// 答案开头（标记串的前缀）：采样流式帧时用它确认「已经收到作答内容」。
const ANSWER_PREFIX = 'E2E-STUB'

const QUESTION = '员工出差回来多久内必须提交报销？'

// ---------------------------------------------------------------------------
// 样式护栏（issue #125）
//
// 这条用例原先只断言 DOM 与文案，对 CSS 完全不设防：tailwind v4 迁移里最危险的
// 失败模式是「构建绿、类名还在，但样式整段消失」—— 例如样式入口漏改时 preflight 与
// 依赖主题变量的工具类会一起丢，而用例照样全绿。下面把两类最易静默丢失的值钉住：
//
//   1. 走 `@config` 从 tailwind.config.js 桥接过来的自定义令牌（brand 色阶、panel 阴影）。
//      桥接一断，类名仍在源码里、DOM 也照常渲染，只是产物里没有对应的规则。
//   2. v4 改过默认值的裸 `border` 边框色。v4 的 preflight 是 `border: 0 solid`，
//      简写不含颜色 ⇒ `border-color` 回落到 `currentColor`；v3 默认是 gray-200。
//      仓库里只有 1 处元素真的落在默认值上（其余都带显式色号），肉眼巡检极难覆盖。
// ---------------------------------------------------------------------------

// brand-600 = #1d4ed8；panel 阴影 = 0 10px 30px rgba(15, 23, 42, 0.08)，两者都来自 tailwind.config.js。
const BRAND_600_RGB = 'rgb(29, 78, 216)'
// v3 preflight 的裸边框默认色 gray-200 = #e5e7eb；迁移后必须仍是这个颜色。
const BORDER_DEFAULT_RGB = 'rgb(229, 231, 235)'
// 文字色：用来证明裸边框没有退化成 currentColor（那正是这次迁移的静默失败形态）。
const BODY_TEXT_RGB = 'rgb(31, 41, 55)'

// 知识库名带上运行标识：重名会被后端拒绝（「知识库名称已存在」），
// 而 CI 复跑、本地连跑都不该互相干扰。
const RUN_ID =
  process.env.E2E_RUN_ID || new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14)
const KB_NAME = `${process.env.E2E_KB_PREFIX || 'e2e 报销制度库'} ${RUN_ID}`

const LOGIN_USER = process.env.E2E_USERNAME || 'demo'
// 口令必须由外部注入，仓库里不留任何「拿来就能登录」的默认值：
// 它要与后端的 SEED_DEMO_PASSWORD 一致，缺了就在用例开头明确失败，
// 免得一个看似可用的默认值把配置缺失掩盖过去。CI 见 .github/workflows/e2e.yml，
// 本地运行方式见 README「端到端验收」。
const LOGIN_PASSCODE = process.env.E2E_PASSWORD || ''

async function shot(page, testInfo, name) {
  await testInfo.attach(`screenshot-${name}`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: 'image/png',
  })
}

test('登录 → 建库 → 上传 → 提问 → 流式作答 → 引用可溯源', async ({ page }, testInfo) => {
  testInfo.annotations.push({ type: 'knowledge-base', description: KB_NAME })

  if (!LOGIN_PASSCODE) {
    throw new Error(
      '缺少环境变量 E2E_PASSWORD：它必须与后端的 SEED_DEMO_PASSWORD 一致。' +
        'CI 由 .github/workflows/e2e.yml 注入，本地运行方式见 README「端到端验收」。',
    )
  }

  await test.step('登录', async () => {
    await page.goto('/login')
    await expect(page.getByRole('heading', { name: '企业知识库智能问答系统' })).toBeVisible()

    // 样式护栏：趁还在登录页，把自定义主题令牌钉死。
    // 这两个值分别覆盖 tailwind.config.js 的 colors.brand 与 boxShadow.panel ——
    // 走 @config 桥接的配置一旦没被读到，构建依旧成功、DOM 依旧渲染，只有这里会红。
    const brandMark = page.locator('div.bg-brand-600').first()
    await expect(brandMark).toBeVisible()
    await expect(brandMark).toHaveCSS('background-color', BRAND_600_RGB)
    await expect(brandMark).toHaveCSS('box-shadow', /rgba\(15, 23, 42, 0\.08\) 0px 10px 30px/)

    // 样式护栏：裸 `border` 的默认色必须仍是 v3 的 gray-200，而不是退化成 currentColor。
    // 页面里没有「无显式色号的边框」元素可断言（仓库仅 1 处，在 Trace 面板内），
    // 因此这里临时挂一个只带 `border` 的节点，测的是产物样式表本身的行为：
    // 它若有问题，同一份 CSS 在真实元素上同样会出问题。断言后立即移除，不干扰后续步骤。
    //
    // 颜色必须归一化后再比：v3 产物写 `border-color:#e5e7eb`，浏览器回 `rgb(229, 231, 235)`；
    // v4 产物写 `oklch(92.8% .006 264.531)`，浏览器照样原样回 `oklch(...)`。
    // 两者是同一个颜色，但字符串不等 —— 直接比字符串会在完全正确的迁移上判红。
    // 这里把颜色真的画进 canvas 再读像素，得到与记法无关的 sRGB 三元组。
    const borderColor = await page.evaluate(() => {
      const probe = document.createElement('div')
      probe.className = 'border'
      document.body.appendChild(probe)
      const computed = getComputedStyle(probe).borderTopColor
      probe.remove()

      const canvas = document.createElement('canvas')
      canvas.width = 1
      canvas.height = 1
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      ctx.fillStyle = '#000'
      ctx.fillStyle = computed
      ctx.fillRect(0, 0, 1, 1)
      const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
      return `rgb(${r}, ${g}, ${b})`
    })
    expect(borderColor).toBe(BORDER_DEFAULT_RGB)
    expect(borderColor).not.toBe(BODY_TEXT_RGB)

    await page.getByPlaceholder('请输入用户名').fill(LOGIN_USER)
    await page.getByPlaceholder('请输入密码').fill(LOGIN_PASSCODE)
    await page.getByRole('button', { name: '登录' }).click()

    // 登录成功后落到 /chat，并且已经渲染出工作台（空会话的引导页）。
    await expect(page).toHaveURL(/\/chat$/)
    await expect(page.getByRole('heading', { name: '今天想了解什么？' })).toBeVisible()
    await shot(page, testInfo, '1-logged-in')
  })

  await test.step('新建知识库', async () => {
    await page.goto('/knowledge')
    await expect(page.getByRole('heading', { name: '知识库管理' })).toBeVisible()

    await page.getByRole('button', { name: '新建知识库' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText('创建后可独立上传资料并用于后续问答检索。')).toBeVisible()
    await dialog.getByPlaceholder('请输入知识库名称').fill(KB_NAME)
    await dialog.getByRole('button', { name: '创建' }).click()

    // 成功提示与「弹窗关闭」都要看：只看提示的话，弹窗没关也会被当成通过。
    await expect(page.getByText('知识库已创建')).toBeVisible()
    await expect(dialog).toBeHidden()
    await shot(page, testInfo, '2-knowledge-base-created')
  })

  await test.step('上传知识库资料', async () => {
    // 走真实用户路径：点「上传文件」触发系统文件选择器，再选夹具文件。
    // 若这条接线断掉（按钮不再触发 input.click），这里会等待超时而失败。
    const [chooser] = await Promise.all([
      page.waitForEvent('filechooser'),
      page.getByRole('button', { name: '上传文件' }).click(),
    ])
    await chooser.setFiles(FIXTURE_PATH)

    await expect(page.getByText('上传成功')).toBeVisible()
    // 上传接口是同步切分 + 向量化的，提示出现即代表已入库；
    // 但仍以「文件出现在列表里」为准，避免提示与实际状态不一致时误判。
    await expect(page.getByRole('cell', { name: FIXTURE_NAME }).first()).toBeVisible()
    await shot(page, testInfo, '3-file-uploaded')
  })

  await test.step('在对话页选中刚才的知识库', async () => {
    await page.goto('/chat')
    await expect(page.getByRole('heading', { name: '今天想了解什么？' })).toBeVisible()

    // 页面首屏默认选中的是列表里的第一个知识库（通常是「默认知识库」），
    // 不显式选择就会问错库 —— 这一步本身就是「知识库可切换」的验收。
    // 只认页头里的这个下拉框：页面上还有 Element Plus 抽屉自带的 <header class="el-drawer__header">，
    // 直接写 locator('header') 会命中多个元素而触发 strict mode 报错。
    const kbSelect = page.locator('header .el-select')
    await kbSelect.click()
    await page.locator('.el-select-dropdown__item:visible').filter({ hasText: KB_NAME }).click()
    await expect(kbSelect).toContainText(KB_NAME)
    await shot(page, testInfo, '4-knowledge-base-selected')
  })

  let streamingPartial = ''

  await test.step('提问并观察到流式生成状态', async () => {
    await page.getByPlaceholder('输入问题，Enter 发送，Shift + Enter 换行').fill(QUESTION)
    await page.keyboard.press('Enter')

    // 流式回答期间前端才会渲染「AI 正在生成」气泡；它出现即代表 SSE 正在推。
    const streamingBubble = page.locator('article').filter({ hasText: 'AI 正在生成' })
    await expect(streamingBubble).toBeVisible()

    // 必须等这一帧真的收到作答内容再断言：若抓到的是空帧（或只有「AI 正在生成」这类
    // 界面文案），下面「不含完整答案」就成了永真的废话断言，什么也证明不了。
    await expect.poll(() => streamingBubble.innerText(), { timeout: 15_000 }).toContain(ANSWER_PREFIX)
    streamingPartial = await streamingBubble.innerText()

    // 已经收到开头、却还没收到完整标记串 —— 证明前端是边收边渲染，
    // 而不是收到整包再一次性显示（桩按 1200ms/片 下发，这个窗口足够稳定观察到；
    // 间隔必须与 CI 一致，见 .github/workflows/e2e.yml 里启动桩的那一步）。
    expect(streamingPartial).not.toContain(ANSWER_MARKER)
    await testInfo.attach('streaming-partial.txt', {
      body: Buffer.from(streamingPartial, 'utf8'),
      contentType: 'text/plain',
    })
    await shot(page, testInfo, '5-streaming')
  })

  await test.step('断言答案内容与引用来源', async () => {
    const answerBubble = page.locator('article').filter({ hasText: ANSWER_MARKER }).last()

    // 流式结束（气泡消失）后再断言，避免把中间态当成最终答案。
    await expect(page.locator('article').filter({ hasText: 'AI 正在生成' })).toBeHidden()
    await expect(answerBubble).toBeVisible()
    await expect(answerBubble).toContainText(ANSWER_MARKER)
    await expect(answerBubble).toContainText(ANSWER_BODY)

    // 引用行由桩按检索上下文生成，检索链路断掉时不会出现。
    await expect(answerBubble).toContainText(CITATION)

    // 用户看到的是答案本身：既不能把带 [来源: …] 的原始上下文吐出来，
    // 也不能只是把检索到的原文回显一遍。这两条在当前实现下近乎恒真（气泡正文只来自
    // SSE 的 delta），留着的意义是钉住「上下文不得混进正文」这个契约；检索是否真的
    // 发生，由下面的参考资料断言与 CI 里的桩日志校验来证明，不由这两条证明。
    await expect(answerBubble).not.toContainText('[来源:')
    await expect(answerBubble).not.toContainText(FIXTURE_ONLY_PHRASE)
    await shot(page, testInfo, '6-answer')
  })

  await test.step('参考资料可溯源', async () => {
    const answerBubble = page.locator('article').filter({ hasText: ANSWER_MARKER }).last()
    const sourcesButton = answerBubble.getByRole('button', { name: /参考 \d+ 篇资料/ })
    await expect(sourcesButton).toBeVisible()
    await sourcesButton.click()

    const drawer = page.locator('.el-drawer').filter({ hasText: '参考资料' })
    await expect(drawer).toBeVisible()
    await expect(drawer).toContainText(FIXTURE_NAME)

    // 只认文件名不够：正文取空时接口照样会返回 file_name，模型拿到的却是空上下文，
    // 用例却仍会绿。夹具正文里独有的措辞出现在抽屉里，才说明检回的 chunk 带回了正文。
    await expect(drawer).toContainText(FIXTURE_BODY_PHRASE)

    // 路名与名次来自后端 RRF 融合的真实来源（planned / hyde / rewrite_* / keyword）。
    // 关键词一路也能召回夹具并渲染出分数，所以必须单独确认向量路真的召回过：
    // 向量路被跳过（retrieval.py 里吞掉 EmbeddingBackendError 的那条分支）时不会有
    // 「planned #…」这一片。
    await expect(drawer.getByText(/planned\s*#\d/).first()).toBeVisible()

    // 有重排分和融合分，说明这条引用确实经过了 向量/关键词召回 + RRF 融合 + 重排，
    // 而不是把库里所有东西一股脑列出来。
    await expect(drawer.getByText(/rerank\s+[\d.]/).first()).toBeVisible()
    await expect(drawer.getByText(/RRF\s+[\d.]/).first()).toBeVisible()
    await shot(page, testInfo, '7-sources-drawer')
  })
})
