// async-validator 的 CJS/ESM 互操作垫片。
//
// element-plus 的 ElForm 用 `import Schema from 'async-validator'` 拿到校验器。
// 在浏览器/打包器里它解析到 ESM 产物，default 就是 Schema 构造函数；
// 但在 Node 里 import.meta.resolve 给到的是 dist-node/index.js（CJS），
// 经 Node 的 CJS 互操作后 default 变成了整个 module.exports 包装对象
// （{ __esModule: true, default: Schema }），于是 `new Schema(...)` 构造出一个普通对象，
// 校验静默失效 —— 表现为 ElForm.validate() 对空表单也 resolve(true)。
//
// 这不影响线上（Vite 走 ESM 产物），但会让挂载用例里的表单校验变成空转。
// 垫片只把少包的一层拆掉，校验逻辑仍是 async-validator 自己的。
import { createRequire } from 'node:module'

// 走 require 而不是 import：加载钩子把 async-validator 的入口换成了本文件，
// 用 import 的话垫片会解析到自己身上（自引用死循环）。
// require 不经 ESM 钩子，正好直达原始 CJS 产物。
const cjs = createRequire(import.meta.url)('async-validator')

const Schema = cjs?.default ?? cjs

export default Schema
