// Tailwind v4 起 PostCSS 插件从 `tailwindcss` 包迁到 `@tailwindcss/postcss`：
// v4 的 `tailwindcss` 包不再导出 postcss 入口，继续写 `tailwindcss: {}` 会让
// 构建直接抛错（错误串由 tailwindcss@4.x 的 dist/lib.mjs 给出）。
//
// autoprefixer 一并移除：v4 内置 Lightning CSS 负责加前缀（@tailwindcss/postcss
// 在生产构建下自动启用），重复配置只会让同一件事有两个来源。
// 移除前已全仓 grep：除 package.json 与本文件外无任何引用。
export default {
  plugins: {
    '@tailwindcss/postcss': {},
  },
}
