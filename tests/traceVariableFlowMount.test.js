// TraceVariableFlow.vue 的挂载用例：真 mount 组件，断言胶水行为。
//
// 这份组件此前零测试引用。docs/MAINTENANCE_GOAL_CLOSURE.md:84 把这一类缺口记为
// 「no test mounts the Vue view, so the untested surface is the call-site glue itself」
// （该处讲的是 Knowledge.vue 的删除接线，此处借指同一类缺口）。
// 已有的 knowledgeViewWiring.test.js 走静态读文件，能证明「代码里有这行」，
// 证明不了「这行真的会跑、跑出什么」——本文件补的是后者：
// 真挂载之后点击 / 改 props，看组件实际做了什么。

import test from 'node:test'
import assert from 'node:assert/strict'
import { mountSfc } from './helpers/vueMount.js'

// 复制走的是 src/utils/clipboard.js 的真实实现，这里只替掉浏览器剪贴板 API。
function installClipboard(writeTextImpl) {
  const written = []
  Object.defineProperty(globalThis.navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (value) => {
        written.push(value)
        return writeTextImpl ? writeTextImpl(value) : undefined
      },
    },
  })
  return written
}

function mainEvent(overrides = {}) {
  return { index: 1, stage: 'RAG 检索', function: 'retrieve', time: '0.1s', ...overrides }
}

// ---------------------------------------------------------------------------
// 渲染：主线 / 分支的归类
// ---------------------------------------------------------------------------

test('按 stage 渲染主线，分支事件默认收起', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [
          mainEvent({ params: { question: 'q1' }, creates: { sources: [1, 2] } }),
          { index: 2, stage: 'RAGAS 评估', function: 'evaluate', time: '0.2s' },
        ],
      },
    },
  })

  assert.match(view.text(), /RAG 检索/)
  assert.match(view.text(), /1 个 stage/)
  // 只看主线开着时整个分支区都不渲染，RAGAS 连汇总胶囊都不出现
  assert.doesNotMatch(view.text(), /RAGAS/)

  view.queryAll('.el-switch__input')[0].click()
  await view.nextTick()
  // 关掉之后才给出分类汇总与展开入口，正文仍然收起
  assert.match(view.text(), /RAGAS · 1/)
  assert.doesNotMatch(view.text(), /RAGAS 评估/)
  assert.ok(view.text().includes('展开 1 个分支 stage'))

  await view.unmount()
})

test('分支标签按 stage / function 归类（memory、attachments、error）', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [
          mainEvent(),
          { index: 2, stage: 'Memory 读写', function: 'recall' },
          { index: 3, stage: '图片理解', function: 'image_analysis' },
          { index: 4, stage: '生成', function: 'generate', result: { error: 'boom' } },
        ],
      },
    },
  })

  // 分支区要求 !onlyMainLine 才渲染
  view.queryAll('.el-switch__input')[0].click()
  await view.nextTick()
  const text = view.text()
  assert.match(text, /memory · 1/)
  assert.match(text, /attachments \/ image_analysis · 1/)
  assert.match(text, /error · 1/)

  await view.unmount()
})

test('全是分支事件时，主线退化成展示全部（displayedMainRows 兜底）', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [{ index: 7, stage: 'RAGAS 评估', function: 'evaluate' }],
      },
    },
  })

  // mainRows 为空 -> displayedMainRows 回落到 allRows，RAGAS 出现在主线区；
  // 计数仍如实显示 0（数的是 mainRows，不是兜底后的条数）
  assert.match(view.text(), /RAGAS 评估/)
  assert.match(view.text(), /0 个 stage/)

  await view.unmount()
})

test('没有事件时给空态，不渲染泳道与检视器', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', { props: { trace: {} } })

  assert.ok(view.text().includes('暂无变量流数据'))
  assert.equal(view.queryAll('aside').length, 0, '无事件时不应渲染变量检视器')
  assert.equal(view.queryAll('article').length, 0, '无事件时不应渲染 stage 卡片')

  await view.unmount()
})

// ---------------------------------------------------------------------------
// 两个开关的联动：onlyMainLine / showBranches 的互相牵制
// ---------------------------------------------------------------------------

test('只看主线默认打开，此时展开分支被禁用', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent(), { index: 2, stage: 'RAGAS 评估', function: 'evaluate' }] } },
  })

  const switches = view.queryAll('.el-switch__input')
  assert.equal(switches.length, 2)
  assert.equal(switches[0].checked, true, '只看主线应当默认打开')
  assert.equal(switches[1].disabled, true, '只看主线开着时，展开分支应当被禁用')

  await view.unmount()
})

test('关掉只看主线才能展开分支；展开分支会反过来关掉只看主线', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent(), { index: 2, stage: 'RAGAS 评估', function: 'evaluate' }] } },
  })

  view.queryAll('.el-switch__input')[0].click()
  await view.nextTick()
  assert.equal(view.queryAll('.el-switch__input')[1].disabled, false, '关掉只看主线后应可展开分支')
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '还没展开，分支正文不应出现')

  view.queryAll('.el-switch__input')[1].click()
  await view.nextTick()
  assert.match(view.text(), /RAGAS 评估/, '展开后应能看到分支 stage')
  // watch(showBranches) 会把 onlyMainLine 拨回 false，两个开关不会同时是「只看主线 + 展开分支」
  assert.equal(view.queryAll('.el-switch__input')[0].checked, false)

  await view.unmount()
})

test('重新打开只看主线会收起分支（watch(onlyMainLine)）', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent(), { index: 2, stage: 'RAGAS 评估', function: 'evaluate' }] } },
  })

  view.queryAll('.el-switch__input')[0].click()
  await view.nextTick()
  view.queryAll('.el-switch__input')[1].click()
  await view.nextTick()
  assert.match(view.text(), /RAGAS 评估/)

  view.queryAll('.el-switch__input')[0].click()
  await view.nextTick()
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '重新只看主线后分支应被收起')

  await view.unmount()
})

// ---------------------------------------------------------------------------
// 变量检视器：选中、同名上下游、props 变化后的复位
// ---------------------------------------------------------------------------

test('默认选中第一个变量，点击其它变量后检视器跟着切换', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [
          mainEvent({ params: { question: 'q1' } }),
          { index: 2, stage: '生成', function: 'generate', creates: { full_answer: 'A1' } },
        ],
      },
    },
  })

  const inspector = () => view.queryAll('aside')[0].textContent
  assert.match(inspector(), /question/)
  assert.match(inspector(), /fullValue|q1/)

  const target = view.queryAll('section button').find((node) => node.textContent.includes('full_answer'))
  assert.ok(target, '应当渲染出 full_answer 变量按钮')
  target.click()
  await view.nextTick()

  assert.match(inspector(), /full_answer/)
  assert.match(inspector(), /A1/)
  assert.doesNotMatch(inspector(), /q1/)

  await view.unmount()
})

test('同名变量给出上游 / 下游位置', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [
          { index: 1, stage: 'RAG 检索', function: 'retrieve', creates: { answer: 'first' } },
          { index: 2, stage: '生成', function: 'generate', creates: { answer: 'second' } },
        ],
      },
    },
  })

  const inspector = () => view.queryAll('aside')[0].textContent
  // 默认选中排序后的第一个同名变量；不论选到哪一个，两端文案都必须成对出现
  assert.match(inspector(), /无更早同名变量|下游：2\.|上游：1\./)

  const buttons = view.queryAll('aside button').filter((node) => /^\d+ · /.test(node.textContent.trim()))
  assert.equal(buttons.length, 2, '同名变量应在检视器里给出两个位置入口')

  buttons[0].click()
  await view.nextTick()
  assert.match(inspector(), /无更早同名变量/, '第一个位置没有上游')

  buttons[1].click()
  await view.nextTick()
  assert.match(inspector(), /上游：1\. RAG 检索 \/ creates/)
  assert.match(inspector(), /无后续同名变量/)

  await view.unmount()
})

test('trace 变化后旧选中消失时，选中复位到新 trace 的第一个变量', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [
          mainEvent({ params: { question: 'q1' } }),
          { index: 2, stage: '生成', function: 'generate', creates: { full_answer: 'A1' } },
        ],
      },
    },
  })

  const inspector = () => view.queryAll('aside')[0].textContent
  const stale = view.queryAll('section button').find((node) => node.textContent.includes('full_answer'))
  stale.click()
  await view.nextTick()
  assert.match(inspector(), /full_answer/)

  // 换一条完全不同的 trace：原选中项不复存在，watch(allVariables) 必须把选中拉回来，
  // 否则检视器会停在一个已经不在数据里的变量上。
  await view.setProps({
    trace: { events: [{ index: 9, stage: '新链路', function: 'fresh', params: { fresh_param: 'z' } }] },
  })

  assert.match(inspector(), /fresh_param/)
  assert.match(inspector(), /z/)
  assert.doesNotMatch(inspector(), /full_answer/)
  assert.doesNotMatch(view.text(), /RAG 检索/)

  await view.unmount()
})

// ---------------------------------------------------------------------------
// 复制：调哪个 helper、复制的是哪个值、toast 级别
// ---------------------------------------------------------------------------

test('复制按钮把「完整值」（不是截断预览）交给 clipboard，并成功提示', async () => {
  const longValue = 'x'.repeat(300)
  const written = installClipboard()
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent({ creates: { full_answer: longValue } })] } },
  })

  const copyButton = view.buttonByText('复制')
  assert.ok(copyButton, '有选中变量时应渲染复制按钮')
  copyButton.click()
  await view.flush()

  assert.deepEqual(written, [longValue], '复制的是完整值，不是 96 字的预览')
  assert.deepEqual(view.messages, [{ level: 'success', message: '变量值已复制' }])

  view.elementPlusUnpatch()
  await view.unmount()
})

test('异步剪贴板被拒时走 textarea 兜底，仍算成功', async () => {
  installClipboard(() => {
    throw new Error('NotAllowedError')
  })
  const originalExecCommand = globalThis.document.execCommand
  globalThis.document.execCommand = () => true

  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent({ creates: { full_answer: 'FALLBACK' } })] } },
  })
  view.buttonByText('复制').click()
  await view.flush()

  assert.deepEqual(view.messages, [{ level: 'success', message: '变量值已复制' }])

  globalThis.document.execCommand = originalExecCommand
  view.elementPlusUnpatch()
  await view.unmount()
})

test('两条复制通道都失败时降级为 warning 提示', async () => {
  installClipboard(() => {
    throw new Error('NotAllowedError')
  })
  const originalExecCommand = globalThis.document.execCommand
  globalThis.document.execCommand = () => false

  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent({ creates: { full_answer: 'NOPE' } })] } },
  })
  view.buttonByText('复制').click()
  await view.flush()

  assert.deepEqual(view.messages, [{ level: 'warning', message: '复制失败，请手动选择文本' }])

  globalThis.document.execCommand = originalExecCommand
  view.elementPlusUnpatch()
  await view.unmount()
})

test('stage 里一个变量都没有时不渲染复制按钮', async () => {
  // 有事件但没有任何 params/uses/creates/result：检视器在，但没有可复制的变量
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [{ index: 1, stage: '空 stage', function: 'noop' }] } },
  })

  assert.equal(view.buttonByText('复制'), undefined)
  assert.ok(view.text().includes('暂无可查看变量'))

  await view.unmount()
})

// ---------------------------------------------------------------------------
// 变量排序与预览
// ---------------------------------------------------------------------------

test('变量按重要性排序，输入按 params/uses 分组、输出按 creates/result', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [
          mainEvent({
            params: { zzz_noise: 1, question: 'q' },
            uses: { context: ['c'] },
            creates: { chunk: 'c1' },
            result: { rerank: 0.5 },
          }),
        ],
      },
    },
  })

  const groups = view.queryAll('section p').map((node) => node.textContent)
  assert.ok(groups.includes('输入变量'))
  assert.ok(groups.includes('输出变量'))
  // 重要变量（question / context / chunk / rerank）权重高，排在噪音变量前面
  const buttons = view.queryAll('section button').map((node) => node.textContent)
  const noiseIndex = buttons.findIndex((text) => text.includes('zzz_noise'))
  const questionIndex = buttons.findIndex((text) => text.includes('question'))
  assert.ok(noiseIndex > -1 && questionIndex > -1)
  assert.ok(questionIndex < noiseIndex, 'question 应排在 zzz_noise 之前')

  await view.unmount()
})

test('超过 4 个变量时折叠进「更多」', async () => {
  const params = Object.fromEntries(Array.from({ length: 6 }, (_, index) => [`p${index}`, index]))
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent({ params })] } },
  })

  assert.match(view.text(), /6 个/)
  assert.match(view.text(), /更多 2 个变量/)

  await view.unmount()
})
