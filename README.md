# ZAP-Hosting VPS 保活脚本

自动登录 ZAP-Hosting 并访问 VPS 页面以保持 Lifetime VPS 活跃。

## 功能

- 支持多账号批量处理
- 通过 YesCaptcha 处理登录表单验证码
- 自动处理 Cloudflare 验证
- 会话持久化
- Telegram 通知支持

## GitHub Actions 使用

在本仓库的 Settings → Secrets and variables → Actions 中添加 Repository secrets：

| Secret 名称 | 内容 |
|-------------|------|
| `ZAP_ACCOUNT` | `邮箱:密码`，多账号用逗号分隔 |
| `YESCAPTCHA_API_KEY` | 登录表单验证码使用的 YesCaptcha API 密钥 |
| `ZAP_PROXY_URL` | 可选浏览器代理地址；不配置时直接连接 |
| `SMTP_USERNAME` | QQ 发件邮箱 |
| `SMTP_PASSWORD` | QQ 邮箱 SMTP 服务授权码 |
| `SMTP_TO` | 通知收件邮箱 |

账号、密码和 API 密钥仅保存到 Secrets，不要提交到公开仓库。
工作流直接将 Secrets 传入进程环境，不生成包含凭据的文件，也不缓存登录会话。
在 Actions 中启用工作流后，选择 **ZAP Renew → Run workflow** 可手动运行。
定时任务为每周一北京时间 08:00；每周三北京时间 08:00 检查本周周一定时任务的执行结果，
如果没有定时运行记录，或周一已触发但失败、取消、超时，则执行相同保活流程补跑。
周一已成功时，周三跳过保活；周一仍在排队或运行时也跳过，避免重复执行。
手动运行不计作周一定时任务，仍可随时手动执行。
检查会包含延迟到周二创建的周一定时记录，查询失败时报告错误，不将未知状态当作漏跑。
周三跳过时不会登录、安装浏览器或发送保活结果邮件，原因可在 Actions 日志中查看。
工作流使用自动提供的 `GITHUB_TOKEN` 和 `actions: read` 权限查询记录，无需新增 Secret。
GitHub 定时触发可能延迟；周三检查也是 GitHub 定时任务，不保证在 08:00 准时启动。
首次登录及验证码解决需要实际运行验证。
当前工作流使用 Classic Panel (`legacy.zap-hosting.com`)，登录成功后等待 3 秒，
访问指定 VPS 详情页、停留 10 秒并刷新。Cookie 提示出现时自动选择 **Accept all**。
登录提交后，`Run renew script` 中的“登录诊断”会记录登录 POST 和其他同源 POST 的数量及 HTTP 状态，
并记录可见错误的已知通用提示；未识别提示只报告存在，不输出原文。没有 HTTP 响应或没有发出请求也会注明。
诊断不记录请求地址、请求体、响应体或验证码，但这些信息仍可能不足以确定站点拒绝登录的原因。
登录请求返回 HTTP 422 时，诊断会读取 JSON 响应中的错误字段，并只输出预设类别；
未识别、非 JSON 或无法读取的响应不会输出原文。类别仅代表相关线索，并非已确定的根因。
成功与失败结果均通过 QQ SMTP SSL (465) 发邮件。
邮件显示“服务器接受”表示 SMTP 已接受投递，最终到达收件箱由邮件服务商处理。
另有独立的 `ZAP Renew Failure Alert` 工作流，在主工作流失败或超时时发送包含运行链接的提醒，
覆盖依赖安装等脚本启动前的失败。脚本已发出失败邮件时，可能额外收到一封提醒。
该提醒仍依赖 GitHub Actions 和同一 SMTP 配置；任务没有被 GitHub 创建、Actions 整体不可用或
SMTP 服务器不可用时不能保证邮件送达。
保活成功表示已访问并刷新 VPS 页面，不保证服务商已延长 Lifetime VPS 有效期。

### Cloudflare 验证

脚本沿用上游的 CDP 鼠标事件方式，定位实际 Turnstile iframe（包括 closed Shadow DOM），
不会按整个页面容器的固定位置点击，也不会在同一控件验证过程中反复点击。
浏览器使用原生 User-Agent，先等待页面自动验证，再在需要时点击实际控件。
整页 Cloudflare 验证在浏览器中处理，不调用 YesCaptcha API，也不注入第三方返回的验证 Cookie。
最后一次点击后会重新检查页面，验证失败仍会终止任务；本地测试通过不保证每次实站验证都能通过。
登录表单若使用 Turnstile，脚本先等待页面自身完成验证；未完成时使用 YesCaptcha 的
`TurnstileTaskProxyless` 处理该表单（每次成功任务可能产生费用）。脚本按登录表单内的控件自动识别
Turnstile 或 reCAPTCHA；两种控件同时存在时优先 Turnstile。reCAPTCHA 使用原接口，优先读取表单的
sitekey；没有验证码时不调用 YesCaptcha。网站变更控件结构时可能需要更新识别逻辑。

代理格式为 `http://username:password@host:port`、`https://host:port` 或 `socks5://host:port`。
带认证的 SOCKS5 不受支持。代理只用于浏览器连接，不是 Cloudflare 验证的必需配置。
代理凭据中的特殊字符应进行 URL 编码。

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






