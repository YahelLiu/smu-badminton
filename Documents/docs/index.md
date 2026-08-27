---
layout: home
hero:
  name: SMU Badminton
  text: 羽毛球场预约系统
  tagline: 上海海事大学羽毛球场智能预约，支持即时预约与定时抢场
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/quick-start
    - theme: alt
      text: API 文档
      link: /guide/api
    - theme: alt
      text: GitHub
      link: https://github.com/YahelLiu/smu-badminton
features:
  - title: CAS 统一认证
    details: 集成上海海事大学 CAS 统一认证平台，支持自动/手动验证码输入，Token 缓存减少重复登录
  - title: 即时预约
    details: 一键发起预约请求，自动登录、查询场地可用性、提交预约，全流程自动化
  - title: 定时抢场
    details: 设定目标时间，多线程 Barrier 同步并发抢场，分级休眠策略适配长时间等待
  - title: 验证码识别
    details: 基于 ddddocr 的算术验证码整图识别，纯本地推理，无需额外模型文件或远程 OCR 服务
  - title: 公共缓存加速
    details: 场地时间槽数据 60s 公共缓存跨用户共享，仅 bookedByMe 按用户单独查询
  - title: 任务持久化
    details: SQLite WAL 模式存储预约记录与任务状态，服务重启后自动恢复待执行任务
---
