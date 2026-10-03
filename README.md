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

账号、密码和 API 密钥仅保存到 Secrets，不要提交到公开仓库。
工作流直接将 Secrets 传入进程环境，不生成包含凭据的文件，也不缓存登录会话。
在 Actions 中启用工作流后，选择 **ZAP Renew → Run workflow** 可手动运行。
定时任务保持每月 1 日北京时间 11:00；首次登录及验证码解决需要实际运行验证。
保活成功表示已访问并刷新 VPS 页面，不保证服务商已延长 Lifetime VPS 有效期。

## 青龙面板使用

### 1. 添加订阅

在青龙面板的「订阅管理」中添加：

- **名称**: zap-renew
- **链接**: `https://github.com/smithwhere/zap-renew.git`
- **分支**: main
- **定时规则**: `0 8 * * *`

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

建议每月执行一次:
- 定时规则: `0 8 1 * *` (每月1号 8:00)

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
