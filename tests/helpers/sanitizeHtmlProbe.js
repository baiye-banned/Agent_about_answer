// `src/utils/sanitizeHtml.js` 的观察层：把渲染器交给消毒器的 HTML、以及消毒器交回的 HTML
// 各留一份档，**本身不改行为** —— 每次调用都原样转给真实实现。
//
// 为什么需要它：`tests/markdownRendererMount.test.js` 要证的是「渲染器实际产出的 HTML
// 经过消毒」。只断言「挂载后的 DOM 里没有 <base>」是不够的 —— 那份 payload 可能压根没被
// 渲染成 HTML，负向断言于是空转通过。有了入参留档就不必再猜：渲染器真的把含 `<base>` 的
// HTML 交给了消毒器，这一点由观测而非推理给出。
//
// 为什么不是「换成恒等函数」：那样断言的是替身的行为，而且一旦首次挂载把真实模块冻进
// 模块缓存，同文件里再换替身就不再生效。观察层与真实实现是同一个模块实例，可以在同一次
// 运行里既观测又在需要时旁路（probeDisableSanitizer），行为与生产完全一致。
//
// 与 stubApiRequest.js / stubConfirm.js 同一套共享实例机制：挂载用例通过**文件 URL**
// 把它替换进模块图，测试自己 import 同一文件即可读到这里的记录。

import { sanitizeHtml as realSanitizeHtml } from '../../src/utils/sanitizeHtml.js'

// 记录到的调用：{ input: 渲染器交来的 HTML, output: 消毒后的 HTML }
export const sanitizeCalls = []

// 旁路开关：置真时不再消毒（只用于反向对照，证明断言可以被打红）。默认关。
let bypassed = false

export function probeDisableSanitizer() {
  bypassed = true
}

export function probeEnableSanitizer() {
  bypassed = false
}

export function resetSanitizeProbe() {
  sanitizeCalls.length = 0
  bypassed = false
}

export function sanitizeHtml(html) {
  const output = bypassed ? html : realSanitizeHtml(html)
  sanitizeCalls.push({ input: html, output })
  return output
}
