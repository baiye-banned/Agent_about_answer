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

// 答案开头（标记串的前缀）：采样流式帧时用它确认「已经收到作答内容」。
const ANSWER_PREFIX = 'E2E-STUB'

const QUESTION = '员工出差回来多久内必须提交报销？'

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
    // 而不是收到整包再一次性显示（桩按 700ms/片 下发，这个窗口足够稳定观察到）。
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
    // 也不能只是把检索到的原文回显一遍。
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
    // 有重排分和融合分，说明这条引用确实经过了 向量/关键词召回 + RRF 融合 + 重排，
    // 而不是把库里所有东西一股脑列出来。
    await expect(drawer.getByText(/rerank\s+[\d.]/).first()).toBeVisible()
    await expect(drawer.getByText(/RRF\s+[\d.]/).first()).toBeVisible()
    await shot(page, testInfo, '7-sources-drawer')
  })
})
