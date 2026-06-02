import { defineConfig } from 'vitepress'

function resolveBase() {
  const repo = process.env.GITHUB_REPOSITORY?.split('/')[1]
  if (!process.env.GITHUB_ACTIONS || !repo) {
    return '/'
  }
  return repo.endsWith('.github.io') ? '/' : `/${repo}/`
}

export default defineConfig({
  base: resolveBase(),
  lang: 'zh-CN',
  title: 'SMU Badminton 文档',
  description: '上海海事大学羽毛球场预约系统文档',
  cleanUrls: true,
  lastUpdated: true,
  themeConfig: {
    nav: [
      { text: '快速开始', link: '/guide/quick-start' },
      { text: 'API文档', link: '/guide/api' },
      { text: 'FAQ', link: '/guide/faq' },
    ],
    sidebar: [
      {
        text: '使用指南',
        items: [
          { text: '文档首页', link: '/' },
          { text: '快速开始', link: '/guide/quick-start' },
          { text: '安装部署', link: '/guide/install' },
          { text: 'CAS认证', link: '/guide/cas-auth' },
          { text: '预约功能', link: '/guide/booking' },
          { text: 'API文档', link: '/guide/api' },
          { text: '配置参数', link: '/guide/config' },
          { text: 'OCR验证码', link: '/guide/ocr-captcha' },
          { text: 'FAQ', link: '/guide/faq' },
        ],
      },
    ],
    outline: [2, 3],
    search: {
      provider: 'local',
    },
    footer: {
      message: 'SMU Badminton Docs',
      copyright: 'Copyright © SMU Badminton',
    },
  },
})
