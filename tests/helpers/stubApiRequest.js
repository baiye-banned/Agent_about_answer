// src/api/request.js 的替身：挂载用例里唯一需要挡掉的东西就是网络。
//
// 只换 HTTP 边界，不换被测代码：pinia store、视图胶水、组件照常真跑，
// 所以 store 有没有把 avatar 写回、视图有没有清表单，都是真的在执行。
//
// 关键点：挂载用例通过**文件 URL**把它替换进模块图，测试自己再 import 同一个文件，
// 拿到的是同一个模块实例，于是这里记下来的 calls 就是被测代码真实发出的调用。

export const calls = []

let handlers = {}

// 每个用例开头调一次，避免上一个用例的记录串味。
export function resetRequestStub() {
  calls.length = 0
  handlers = {}
}

// 指定某个方法的返回 / 抛错：respond('post', () => { throw new Error('boom') })
export function respond(method, impl) {
  handlers[method] = impl
}

function record(method) {
  return (...args) => {
    calls.push({ method, args })
    const impl = handlers[method]
    if (!impl) return Promise.resolve({})
    try {
      return Promise.resolve(impl(...args))
    } catch (error) {
      return Promise.reject(error)
    }
  }
}

export function callsOf(method) {
  return calls.filter((entry) => entry.method === method)
}

export default {
  get: record('get'),
  put: record('put'),
  post: record('post'),
  delete: record('delete'),
}
