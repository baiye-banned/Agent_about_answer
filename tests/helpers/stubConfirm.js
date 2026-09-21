// src/utils/confirm.js 的替身：把「删除确认框」换成可控的结果。
//
// 为什么替掉这一层：confirmCenteredDelete 只是 ElMessageBox.confirm 的一行包装
// （src/utils/confirm.js），被测的是**调用点胶水**——runConfirmedDelete 返回后
// 那句 `if (status !== DELETE_SUCCEEDED) return` 与紧随其后的刷新。
// 真开 ElMessageBox 会把用例拖进 Element Plus 的 focus-trap，它在 jsdom 下要
// 挂载工具没装的全局（实测 HTMLInputElement is not defined，抛在 watcher 回调里），
// 断言也就变成在测弹窗实现，与本线无关。
//
// 替身必须忠实复制真实契约（element-plus 2.13.7 的 messageBox）：
//   点「删除」-> resolve('confirm')
//   点「取消」/ ESC / 点遮罩 -> reject('cancel')
// 注意确认框是 reject 而不是 resolve(false)：runConfirmedDelete 的取消分支
// 正是靠 isConfirmCancellation 识别这个字符串，替身若改成 resolve(false)，
// 取消路径就会走进失败分支——那是替身在骗测试。
//
// 与 stubApiRequest.js 同一套共享实例机制：挂载用例通过**文件 URL**把它替换进
// 模块图，测试自己 import 同一个文件，于是这里记下的 confirmations 就是视图
// 真实发出过的确认请求。

export const confirmations = []

let outcome = 'confirm'

// 每个用例开头调一次，避免上一个用例的结果串味。
export function resetConfirmStub() {
  confirmations.length = 0
  outcome = 'confirm'
}

// 指定确认框的结果：'confirm' 放行删除，'cancel' 表示用户放弃。
export function respondWith(result) {
  outcome = result
}

export function confirmCenteredDelete(message, title) {
  confirmations.push({ message, title })
  if (outcome === 'cancel') return Promise.reject('cancel')
  return Promise.resolve('confirm')
}
