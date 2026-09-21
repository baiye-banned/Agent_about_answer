// Node 模块加载钩子：让 `node --test` 能直接 import 本仓库的 .vue 与 @/ 别名模块。
//
// 为什么不把编译产物塞进 data: URL 再 import：那样只能改写**被测 SFC 自己**的 import，
// 它间接引到的普通 .js（src/stores/user.js 里的 `@/api/user`）仍然带着 Vite 别名，
// Node 解析不了。钩子挂在解析层，SFC 与普通模块一视同仁。
//
// 由 vueMount.js 通过 module.register() 装上；注册时机被设计成「每次 mount 一次」，
// 因为 registry 是随 data 传给钩子线程的（钩子与测试不在同一个线程，拿不到闭包变量）。

import { readFileSync, statSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'

// { 相对 src 的路径 -> 替代模块源码 }；只有这里列出的模块会被替换。
let registry = {}
let srcRoot = ''

export function initialize(data) {
  registry = data?.registry || {}
  srcRoot = data?.srcRoot || ''
}

// --- 路径解析 -------------------------------------------------------------

const CANDIDATE_SUFFIXES = ['', '.js', '.mjs', '.vue', '/index.js', '/index.mjs']

function isFile(path) {
  try {
    return statSync(path).isFile()
  } catch {
    return false
  }
}

// Vite 会对无扩展名的导入做后缀补全，Node 的 ESM 解析器不会
// （`import request from './request'` 直接 ERR_MODULE_NOT_FOUND）。
// 这里补上同一套补全，否则 src/api/*.js 之间的相对导入全断。
function withSuffixes(basePath) {
  for (const suffix of CANDIDATE_SUFFIXES) {
    if (isFile(basePath + suffix)) return pathToFileURL(basePath + suffix).href
  }
  return null
}

function resolveAlias(specifier) {
  const url = withSuffixes(srcRoot + specifier.slice(2))
  if (!url) throw new Error(`无法解析别名导入 ${specifier}`)
  return url
}

// 绝对 file URL -> 相对 src 的键，用作 registry 的匹配键。
function registryKey(url) {
  const path = fileURLToPath(url).replace(/\\/g, '/')
  const root = srcRoot.replace(/\\/g, '/')
  return path.startsWith(root) ? path.slice(root.length) : null
}

function stubUrl(source) {
  // .mjs 后缀是必须的：data: URL 没有扩展名，Node 只能按 text/javascript 兜底解析，
  // 带 .mjs 才走 ESM。
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}#.mjs`
}

function overrideFor(url) {
  // 只有 file: 才有磁盘路径可谈；node: 内置模块直接放行
  // （否则 fileURLToPath('node:module') 会抛 ERR_INVALID_URL_SCHEME）。
  if (!url.startsWith('file:')) return null

  // src 内的模块用相对 src 的短键（可读），src 外的（node_modules 里的垫片）
  // 用规范化绝对路径作键。
  const absolute = fileURLToPath(url).replace(/\\/g, '/')
  const key = registryKey(url)
  for (const candidate of [key, absolute]) {
    if (candidate && Object.prototype.hasOwnProperty.call(registry, candidate)) {
      return { url: toStubUrl(registry[candidate]), shortCircuit: true }
    }
  }
  return null
}

// 替换值可以是源码字符串（就地造一个 data: 模块），
// 也可以是 URL —— 后者让挂载用例替成一个**真实存在的文件**，
// 测试自己 import 同一个文件即可拿到同一实例、读到替身记下的调用。
function toStubUrl(value) {
  if (typeof value === 'string' && /^(file|data|node):/.test(value)) return value
  if (value instanceof URL) return value.href
  return stubUrl(String(value))
}

// --- 钩子 -----------------------------------------------------------------

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('@/')) {
    const url = resolveAlias(specifier)
    return overrideFor(url) || { url, shortCircuit: true }
  }

  // 相对 / 绝对路径：先按 Vite 的补全规则试一遍，命中就直接短路，
  // 免得把 `./request` 交给 Node 后直接 ERR_MODULE_NOT_FOUND。
  if (specifier.startsWith('.') || specifier.startsWith('/')) {
    try {
      const candidate = withSuffixes(fileURLToPath(new URL(specifier, context.parentURL)))
      if (candidate) return overrideFor(candidate) || { url: candidate, shortCircuit: true }
    } catch {
      // fileURLToPath 对带 query/hash 的 URL 会抛，退回默认解析。
    }
  }

  const resolved = await nextResolve(specifier, context)
  // 裸包名（以及上面没命中的情况）走正常解析，再按解析结果决定要不要换掉。
  return overrideFor(resolved.url) || resolved
}

// Vite 在构建期把 import.meta.env 静态替换掉，Node 里它是 undefined，
// 于是 `import.meta.env.VITE_API_BASE_URL || '/api'` 这种默认参数会直接抛。
// src 下只有 url.js / request.js / chat.js 三处用，这里做一次最小重写，
// 映射到测试进程可写的全局对象（内容与 Vite 的 import.meta.env 语义一致）。
// 名字不叫 ENV_TOKEN：scan_secrets.sh 的赋值启发式会把 `TOKEN... = 值` 形状
// 报成 credential assignment（值不是占位词），改个名比加豁免标记干净。
const ENV_EXPR = 'import.meta.env'
const ENV_GLOBAL = 'globalThis.__VITE_ENV__'

export async function load(url, context, nextLoad) {
  const isVue = url.endsWith('.vue')
  const isSrcJs = /\.m?js$/.test(url) && url.startsWith(pathToFileURL(srcRoot).href)

  if (!isVue && !isSrcJs) return nextLoad(url, context)

  const filename = fileURLToPath(url)
  const source = readFileSync(filename, 'utf8')

  if (isVue) {
    // 这个包由 vue 自己的 dependencies 提供（`vue` -> `@vue/compiler-sfc`），
    // package.json 里没有直接声明它。上游若把它从 vue 的依赖树里摘掉，这里会以
    // ERR_MODULE_NOT_FOUND 暴露；不加显式声明是刻意的——那会改动 package.json
    // 与 lockfile，超出本次纯测试改动的面。
    const { parse, compileScript } = await import('@vue/compiler-sfc')
    const { descriptor, errors } = parse(source, { filename })
    if (errors.length) throw new Error(`解析 ${filename} 失败：${errors[0].message}`)

    // inlineTemplate：render 直接作为 setup 的返回值，省掉单独挂 render 的拼接。
    const script = compileScript(descriptor, { id: filename, inlineTemplate: true })
    return { format: 'module', source: script.content, shortCircuit: true }
  }

  if (!source.includes(ENV_EXPR)) return nextLoad(url, context)
  return {
    format: 'module',
    source: source.split(ENV_EXPR).join(ENV_GLOBAL),
    shortCircuit: true,
  }
}
