# ZAP-Hosting VPS 保活脚本

自动登录 ZAP-Hosting 并访问 VPS 页面以保持 Lifetime VPS 活跃。

## 功能

- 支持多账号批量处理
- 自动解决 reCAPTCHA 验证 (需要 YesCaptcha)
- 自动处理 Cloudflare 验证
- 会话持久化
- Telegram 通知支持

## GitHub Actions 使用

在本仓库的 Settings → Secrets and variables → Actions 中添加 Repository secrets：

| Secret 名称 | 内容 |
|-------------|------|
| `ZAP_ACCOUNT` | `邮箱:密码`，多账号用逗号分隔 |
| `YESCAPTCHA_API_KEY` | YesCaptcha API 密钥 |
| `ZAP_PROXY_URL` | 可选公网代理地址；调用 CloudFlareTaskS2 时必需，浏览器同步使用 |
| `SMTP_USERNAME` | QQ 发件邮箱 |
| `SMTP_PASSWORD` | QQ 邮箱 SMTP 服务授权码 |
| `SMTP_TO` | 通知收件邮箱 |

账号、密码和 API 密钥仅保存到 Secrets，不要提交到公开仓库。
工作流直接将 Secrets 传入进程环境，不生成包含凭据的文件，也不缓存登录会话。
在 Actions 中启用工作流后，选择 **ZAP Renew → Run workflow** 可手动运行。
定时任务为每周一北京时间 08:00；首次登录及验证码解决需要实际运行验证。
当前工作流使用 Classic Panel (`legacy.zap-hosting.com`)，登录成功后等待 30 秒，
访问指定 VPS 详情页、停留 10 秒并刷新。Cookie 提示出现时自动选择 **Accept all**。
成功与失败结果均通过 QQ SMTP SSL (465) 发邮件。
邮件显示“服务器接受”表示 SMTP 已接受投递，最终到达收件箱由邮件服务商处理。
保活成功表示已访问并刷新 VPS 页面，不保证服务商已延长 Lifetime VPS 有效期。

### Cloudflare 验证

脚本沿用上游的 CDP 鼠标事件方式，定位实际 Turnstile iframe（包括 closed Shadow DOM），
不会按整个页面容器的固定位置点击，也不会在同一控件验证过程中反复点击。
浏览器使用原生 User-Agent；验证失败仍会终止任务。

若整页 `Just a moment...` 挑战持续不通过，配置 `YESCAPTCHA_API_KEY` 和 `ZAP_PROXY_URL`
后，脚本会通过 YesCaptcha `CloudFlareTaskS2` 获取 `cf_clearance` 和匹配的 User-Agent，
应用到浏览器并重新验证。它与登录表单使用的 reCAPTCHA 接口是不同的任务类型。
未配置代理时不会创建此 API 任务；一次验证最多创建一个任务，失败后不会反复扣费重试。

代理格式为 `http://username:password@host:port`、`https://host:port` 或 `socks5://host:port`。
带认证的 SOCKS5 不受支持。代理需要能被 YesCaptcha 服务器和运行器共同访问，出口必须稳定一致；
不能使用只有本机可访问的 localhost 代理。代理凭据中的特殊字符应进行 URL 编码。
接口接入不保证服务商放行，缺少有效代理时仅能使用页面自身验证和 CDP 点击方式。
接口要求见 [YesCaptcha 官方文档](https://yescaptcha.atlassian.net/wiki/spaces/YESCAPTCHA/pages/389382145/CloudFlareTask%2BCloudFlare5S)。

本地检查：安装依赖并执行 `playwright install chromium` 后，运行
`python -m unittest discover -s tests -v`。浏览器测试使用本地响应夹具，
不会发送真实账号或创建真实 YesCaptcha 付费任务。

## 青龙面板使用

### 1. 添加订阅

在青龙面板的「订阅管理」中添加：

- **名称**: zap-renew
- **链接**: `https://github.com/smithwhere/zap-renew.git`
- **分支**: main
- **定时规则**: `0 8 * * 1` (面板时区设为 Asia/Shanghai)

### 2. 配置环境变量

在青龙面板的「环境变量」中添加：

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `ZAP_ACCOUNT` | 账号配置 | `邮箱:密码,邮箱2:密码2` |
| `YESCAPTCHA_API_KEY` | YesCaptcha API密钥 | `your_api_key` |
| `STAY_DURATION` | 停留时间(秒) | `10` |
| `TELEGRAM_BOT_TOKEN` | TG机器人Token | (可选) |
| `TELEGRAM_CHAT_ID` | TG聊天ID | (可选) |

**账号格式**: `邮箱:密码`，多账号用逗号分隔

### 3. 安装依赖

在青龙面板的「依赖管理」→「Python3」中安装：

```
playwright
requests
```

### 4. 系统依赖

需要在容器中安装 xvfb:

```bash
apt-get update && apt-get install -y xvfb xauth
```

### 5. 定时任务

每周一早上执行一次（面板时区设为 Asia/Shanghai）:
- 定时规则: `0 8 * * 1` (每周一 08:00)

## 手动运行

```bash
export ZAP_ACCOUNT='your@email.com:password'
export YESCAPTCHA_API_KEY="your_api_key"
xvfb-run python3 zap-renew.py
```

脚本读取系统环境变量，兼容旧名称 `ACCOUNTS_ZAP` 和 `ACCOUNTS`，优先使用 `ZAP_ACCOUNT`。
`.env.example` 是配置模板；使用 `.env` 时需要先将其中的变量导出到环境。

## 许可

MIT License
