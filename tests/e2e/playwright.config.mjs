// e2e 浏览器验收的 Playwright 配置。
//
// 只跑 chromium：CI 里 `npx playwright install --with-deps chromium` 只装一个浏览器，
// 本地和 CI 的结论才对得上；多装只会拖长流水线而不会多覆盖到产品代码。
//
// 关于 baseURL：e2e 打的是「构建产物 + vite preview」，不是 dev server。dev server 的
// /api 代理写在 vite.config.js 里，preview 不复用它，因此前端在构建时把
// VITE_API_BASE_URL 定成后端的绝对地址（见 .github/workflows/e2e.yml），后端开了 CORS。
import { defineConfig, devices } from '@playwright/test'

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:4173'

export default defineConfig({
  // 配置与用例放在同一目录，产物也落在这一层，彼此不越界。
  testDir: '.',
  testMatch: '**/*.spec.mjs',

  // 串行执行：用例之间共享同一个后端与数据库，"并行"在这里只会制造假失败。
  fullyParallel: false,
  workers: 1,
  retries: 0,

  // 本地调试时误留 test.only 会让其余用例静默不跑，CI 下直接判错。
  forbidOnly: Boolean(process.env.CI),

  // 单条用例要等后端建库、上传切分向量化、再走完整条 RAG 流式链路，
  // 默认 30s 不够；这里给足，但每一步的等待仍用断言/等待事件，不写死 sleep。
  timeout: 120_000,
  expect: { timeout: 20_000 },

  outputDir: './artifacts/test-results',
  reporter: [
    ['list'],
    ['html', { outputFolder: './artifacts/report', open: 'never' }],
  ],

  use: {
    baseURL: BASE_URL,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    viewport: { width: 1440, height: 900 },
    // 失败时留痕：trace 能回放每一步，截图给出肉眼可看的现场。
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
