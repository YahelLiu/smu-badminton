# CAS 认证

## 概述

SMU Badminton 集成了上海海事大学 CAS（Central Authentication Service）统一认证平台，实现用户身份验证。整个认证流程涉及 CAS 登录和 OAuth2 授权两个阶段。

## 认证流程

```
用户请求登录
    |
    v
访问 CAS 登录页面 --> 获取验证码图片
    |
    v
识别验证码（自动 OCR / 手动输入）
    |
    v
提交用户名 + 密码 + 验证码
    |
    v
CAS 验证成功 --> 重定向到 WF OAuth2 授权
    |
    v
OAuth2 授权成功 --> 回调返回 access_token + id_token
    |
    v
Token 缓存（默认 TTL 900 秒）
```

## 验证码处理

系统支持两种验证码处理方式：

### 自动识别（默认）

调用 OCR 模块自动识别验证码，无需人工干预：

1. 获取 CAS 登录页面，提取验证码图片和 `execution` 令牌
2. 将验证码图片送入 OCR 引擎识别
3. 自动提交登录请求

### 手动输入

当 OCR 识别失败时，可切换为手动模式：

1. 先调用 `/api/captcha` 获取验证码图片（base64 编码）
2. 前端展示验证码，用户手动输入
3. 调用 `/api/login` 时传入 `captcha_code` 参数

## Token 缓存机制

登录成功后获取的 Token 会被缓存，避免重复登录：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `TOKEN_CACHE_TTL_SEC` | 900 | Token 缓存有效期（秒） |
| `TOKEN_PROFILE_TTL_SEC` | 3600 | 用户 Profile 缓存有效期（秒） |

- Token 缓存按用户名存储，线程安全
- 缓存命中时直接返回，无需重新登录
- 调用 `/api/logout` 可手动清除指定用户的 Token 缓存

## 登录实现

登录采用单一稳定路径 `cas_login_stable`，不存在 `CAS_LOGIN_STABLE_FIRST` 之类的策略开关（早期文档提及的双策略 / 备选回退已不存在）。流程如下：

1. 解析 CAS 登录页 URL，获取登录页面 HTML
2. 从页面提取 `execution` 令牌与事件顺序（`_stable_detect_event_order`）
3. 抓取验证码图片与一次性 token（验证码接口现已返回 JSON `{image, token, expiresAt}`）
4. OCR 识别或用户手输验证码后，提交用户名 + 密码 + 验证码 + execution
5. 跟随重定向完成 OAuth2 授权，从回调 URL fragment 提取 `access_token` + `id_token`

`login_with_retry` 在此基础上做 `max_retries × 3` 的整体重试。

> CAS 登录页已由 `cas.shmtu.edu.cn` 迁至 `sso.shmtu.edu.cn`，代码按登录页 host 推导同源验证码
> URL，无需手动维护两套地址，`.env` 中残留旧 `cas.` 地址也不会跨 host 抓取失败。

## 密码安全

密码在存储和传输过程中采用混淆处理：

- **XOR + Base64 混淆**：使用 `SECRET_KEY` 环境变量对密码进行 XOR 运算后 Base64 编码
- **非加密**：混淆仅防止明文暴露，不等于加密
- **安全警告**：使用默认 `SECRET_KEY` 时会发出警告，生产环境务必设置自定义密钥

## 登录错误处理

| 错误类型 | 说明 | 处理方式 |
|----------|------|----------|
| `captcha_error` | 验证码识别错误 | 前端提示手动输入验证码 |
| `password_error` | 用户名或密码错误 | 提示用户检查凭据 |
| `network_error` | 网络连接失败 | 提示检查网络连接 |
| `unknown_error` | 未知错误 | 查看服务端日志排查 |

## OAuth2 授权 URL 构建

系统自动构建 OAuth2 授权 URL，包含以下参数：

- `client_id`：OAuth 客户端标识（`OAUTH_CLIENT_ID`）
- `redirect_uri`：回调地址（WF 平台的 OIDC 回调）
- `response_type`：`id_token token`
- `scope`：`data openid process task app submit process_edit start profile`
- `state` / `nonce`：随机生成的安全参数
