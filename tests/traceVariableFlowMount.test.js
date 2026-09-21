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

// 变量按钮的选中态类名（VariableGroup 的 :selected-id 命中时加的那一串）。
const SELECTED_CLASS = 'border-brand-300'

function selectedButtons(view) {
  return view
    .queryAll('section button')
    .filter((node) => node.className.includes(SELECTED_CLASS))
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
//
// 组件里有两处开关联动 watch（src/components/TraceVariableFlow.vue）：
//   watch(onlyMainLine)：只看主线打开时把 showBranches 拨回 false
//   watch(showBranches)：showBranches 打开时把 onlyMainLine 拨回 false
// 本组用例覆盖的是前者。后者在当前模板下**没有任何可达路径**：能把 showBranches
// 置真的入口只有两个，一个是 el-switch，它在 onlyMainLine 为真时是 disabled
// （下面实测「点它不动」），另一个是分支区里的「展开 N 个分支 stage」按钮，
// 而那个 section 带 `v-if="branchRows.length && !onlyMainLine"`，只有只看主线
// 为假时才存在。也就是说 UI 上「互斥」实际是单向的，第二处 watch 是防御性冗余；
// 测试不为它编造断言，只在未覆盖面清单里如实记账，不制造「双向互斥已覆盖」的假象。
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

  // :disabled 的表达式是 `onlyMainLine || !branchRows.length`，两个子句各管一个来源。
  // 上面只证到前半句（全部由 onlyMainLine 决定，看不出后半句在不在）；
  // 这里换成一条没有分支事件的 trace 再关掉只看主线，开关必须仍然禁用——
  // 少了后半句就会出现一个「没有任何分支可展开」却可用的开关。
  const noBranch = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent()] } },
  })
  noBranch.queryAll('.el-switch__input')[0].click()
  await noBranch.nextTick()
  assert.equal(noBranch.queryAll('.el-switch__input')[0].checked, false, '前置条件：只看主线已关掉')
  assert.equal(
    noBranch.queryAll('.el-switch__input')[1].disabled,
    true,
    '没有分支事件时，展开分支开关应当仍然禁用'
  )

  await noBranch.unmount()
})

test('只看主线打开时展开分支开关被禁用且点击无效；关掉后展开，再打开只看主开会把展开态收回', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent(), { index: 2, stage: 'RAGAS 评估', function: 'evaluate' }] } },
  })

  const switches = () => view.queryAll('.el-switch__input')
  assert.equal(switches()[1].disabled, true, '只看主线开着时，展开分支应当被禁用')

  // 禁用态下「点开关把只看主线反过来关掉」这条路径不存在：input 与真正承载
  // click handler 的外层 .el-switch 各点一次，两个开关都必须纹丝不动。
  switches()[1].click()
  view.queryAll('.el-switch')[1].click()
  await view.nextTick()
  assert.equal(switches()[0].checked, true, '禁用态点击不应把只看主线拨到关')
  assert.equal(switches()[1].checked, false, '禁用态点击不应把展开分支拨到开')
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '分支区仍未渲染')

  switches()[0].click()
  await view.nextTick()
  assert.equal(switches()[1].disabled, false, '关掉只看主线后应可展开分支')
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '还没展开，分支正文不应出现')
  assert.ok(view.buttonByText('展开 1 个分支 stage'), '未展开时给的是展开入口')

  switches()[1].click()
  await view.nextTick()
  assert.match(view.text(), /RAGAS 评估/, '展开后应能看到分支 stage')

  // 开关入口的往返：只看主线开 -> 关。
  switches()[0].click()
  await view.nextTick()
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '只看主线打开时整个分支区不渲染')

  switches()[0].click()
  await view.nextTick()
  // 承重断言：展开态被 watch(onlyMainLine) 一并收回。少了它 showBranches 会停在
  // true —— 分支正文直接铺开、展开入口消失，两个开关同时处于「开」。
  assert.equal(switches()[1].checked, false, '重新关掉只看主线后，展开分支不应还停在开')
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '分支正文应仍是收起的')
  assert.ok(view.buttonByText('展开 1 个分支 stage'), '应回到未展开状态')

  await view.unmount()
})

test('分支区按钮展开出来的分支，同样会被重新打开的只看主线收回', async () => {
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: { trace: { events: [mainEvent(), { index: 2, stage: 'RAGAS 评估', function: 'evaluate' }] } },
  })

  const switches = () => view.queryAll('.el-switch__input')
  switches()[0].click()
  await view.nextTick()

  // showBranches 的第二个入口：分支区里的按钮（模板直接写 showBranches = !showBranches），
  // 与 el-switch 的 v-model 是两条独立路径。
  view.buttonByText('展开 1 个分支 stage').click()
  await view.nextTick()
  assert.match(view.text(), /RAGAS 评估/, '按钮展开后应能看到分支 stage')
  assert.ok(view.buttonByText('收起分支'), '展开后按钮文案换成收起')

  view.buttonByText('收起分支').click()
  await view.nextTick()
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '收起后分支正文应消失')
  assert.equal(switches()[1].checked, false, '两条入口共用 showBranches，收起后开关也应回到关')
  assert.ok(view.buttonByText('展开 1 个分支 stage'), '按钮文案换回展开')

  // 再经按钮展开，然后走一趟「只看主线开 -> 关」：按钮展开出来的分支同样要被
  // watch(onlyMainLine) 收回，只看主线与展开态不会同时为真。
  view.buttonByText('展开 1 个分支 stage').click()
  await view.nextTick()
  assert.match(view.text(), /RAGAS 评估/)

  switches()[0].click()
  await view.nextTick()
  switches()[0].click()
  await view.nextTick()
  assert.equal(switches()[1].checked, false, '重新关掉只看主线后，展开分支不应还停在开')
  assert.doesNotMatch(view.text(), /RAGAS 评估/, '分支正文应仍是收起的')
  assert.ok(view.buttonByText('展开 1 个分支 stage'), '应回到未展开状态')

  await view.unmount()
})

// ---------------------------------------------------------------------------
// 变量检视器：选中、同名上下游、props 变化后的复位
// ---------------------------------------------------------------------------

test('默认选中第一个变量，点击其它变量后检视器跟着切换', async () => {
  // 值长度刻意超过列表预览的 96 字上限：这样「检视器渲染的是完整值」与
  // 「渲染的是截断预览」才区分得开（'q1' 这种短值两种写法都一样，断言是空转的）。
  const longValue = `q1-${'x'.repeat(200)}`
  const view = await mountSfc('components/TraceVariableFlow.vue', {
    props: {
      trace: {
        events: [
          mainEvent({ params: { question: longValue } }),
          { index: 2, stage: '生成', function: 'generate', creates: { full_answer: 'A1' } },
        ],
      },
    },
  })

  const inspector = () => view.queryAll('aside')[0].textContent
  const shownValue = () => view.query('aside pre').textContent
  assert.match(inspector(), /question/)
  assert.equal(shownValue(), longValue, '检视器给的是完整值，不是截断预览')
  assert.ok(!shownValue().endsWith('...'), '完整值不应带预览的省略号')

  const target = view.queryAll('section button').find((node) => node.textContent.includes('full_answer'))
  assert.ok(target, '应当渲染出 full_answer 变量按钮')
  target.click()
  await view.nextTick()

  assert.match(inspector(), /full_answer/)
  assert.equal(shownValue(), 'A1')
  assert.doesNotMatch(inspector(), /q1-/, '切换后不应再残留上一个变量的值')

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
  // 默认选中按 allVariables 顺序的第一个同名变量（event index 1），
  // 所以上游为空、下游指向第二个位置——两端文案成对出现，不是「三者命中其一」。
  assert.match(inspector(), /上游：无更早同名变量/)
  assert.match(inspector(), /下游：2\. 生成 \/ creates/)

  const buttons = view.queryAll('aside button').filter((node) => /^\d+ · /.test(node.textContent.trim()))
  assert.equal(buttons.length, 2, '同名变量应在检视器里给出两个位置入口')
  assert.match(buttons[0].textContent, /^1 · creates$/)
  assert.match(buttons[1].textContent, /^2 · creates$/)

  buttons[0].click()
  await view.nextTick()
  assert.match(inspector(), /无更早同名变量/, '第一个位置没有上游')
  assert.match(inspector(), /下游：2\. 生成 \/ creates/, '第一个位置的下游是第二个位置')

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
  assert.equal(selectedButtons(view).length, 1, '同一时刻只有一个变量按钮处于选中态')
  assert.match(selectedButtons(view)[0].textContent, /full_answer/)

  // 换一条完全不同的 trace：原选中项不复存在，watch(allVariables) 必须把选中 id 拉回来。
  // 只看检视器正文是证不到的——selectedVariable 计算属性自带 `|| allVariables[0]` 兜底，
  // id 停在旧值上时检视器照样显示新 trace 的第一个变量；真正的差异在**选中高亮**：
  // VariableGroup 的 :selected-id 拿的是原始 id，不复位就没有任何按钮是高亮的。
  await view.setProps({
    trace: { events: [{ index: 9, stage: '新链路', function: 'fresh', params: { fresh_param: 'z' } }] },
  })

  assert.match(inspector(), /fresh_param/)
  assert.match(inspector(), /z/)
  assert.doesNotMatch(view.text(), /RAG 检索/)

  const selected = selectedButtons(view)
  assert.equal(selected.length, 1, '选中 id 必须复位到新 trace 的变量上，否则高亮会整个消失')
  assert.match(selected[0].textContent, /fresh_param/)

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

  // ElMessage 的补丁挂在 element-plus 的模块对象上，跨用例共享：用例中途断言失败
  // 也必须还原，否则补丁会残留下来污染后续用例记录到的 toast。
  try {
    const copyButton = view.buttonByText('复制')
    assert.ok(copyButton, '有选中变量时应渲染复制按钮')
    copyButton.click()
    await view.flush()

    assert.deepEqual(written, [longValue], '复制的是完整值，不是 96 字的预览')
    assert.deepEqual(view.messages, [{ level: 'success', message: '变量值已复制' }])
  } finally {
    view.elementPlusUnpatch()
    await view.unmount()
  }
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

  try {
    view.buttonByText('复制').click()
    await view.flush()

    assert.deepEqual(view.messages, [{ level: 'success', message: '变量值已复制' }])
  } finally {
    globalThis.document.execCommand = originalExecCommand
    view.elementPlusUnpatch()
    await view.unmount()
  }
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

  try {
    view.buttonByText('复制').click()
    await view.flush()

    assert.deepEqual(view.messages, [{ level: 'warning', message: '复制失败，请手动选择文本' }])
  } finally {
    globalThis.document.execCommand = originalExecCommand
    view.elementPlusUnpatch()
    await view.unmount()
  }
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
            params: { zzz_noise: 1, question: 'q', sources: ['s1'] },
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
  const sourcesIndex = buttons.findIndex((text) => text.includes('sources'))
  assert.ok(noiseIndex > -1 && questionIndex > -1 && sourcesIndex > -1)
  assert.ok(questionIndex < noiseIndex, 'question 应排在 zzz_noise 之前')
  // sources 与 question 的 important 命中相同，差别只在 structuredBonus：
  // 结构化值（数组 / 对象）+1，标量 +0，所以 sources 必须排在 question 之前。
  assert.ok(sourcesIndex < questionIndex, '结构化值应排在同等重要的标量之前')

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
