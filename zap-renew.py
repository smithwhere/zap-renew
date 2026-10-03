#!/usr/bin/env python3
"""
ZAP-Hosting Lifetime VPS 保活脚本

cron: 0 8 1 * *
new Env('zap-renew')

功能:
1. 支持多账号
2. 自动登录 ZAP-Hosting (如果会话过期)
3. 进入 Dashboard
4. 找到并进入 VPS 详情页
5. 停留指定时间后刷新
6. 保存会话供下次使用

环境变量:
    ZAP_ACCOUNT: 账号配置，格式: 邮箱:密码,邮箱2:密码2
    YESCAPTCHA_API_KEY: YesCaptcha API密钥
    STAY_DURATION: 停留时间(秒)，默认10
"""

import os
import asyncio
import json
import time
import requests
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# ==================== 从环境变量加载配置 ====================
YESCAPTCHA_API_KEY = os.environ.get('YESCAPTCHA_API_KEY', '')
YESCAPTCHA_API_URL = "https://api.yescaptcha.com"

# 兼容旧版文档和工作流使用的变量名，优先使用 ZAP_ACCOUNT。
ACCOUNTS_STR = (os.environ.get('ZAP_ACCOUNT') or
                os.environ.get('ACCOUNTS_ZAP') or
                os.environ.get('ACCOUNTS', ''))
STAY_DURATION = int(os.environ.get('STAY_DURATION', '10'))

LOGIN_URL = "https://zap-hosting.com/en/#login"
DASHBOARD_URL = "https://zap-hosting.com/en/customer/home/"
SESSION_DIR = Path(__file__).parent / "sessions"
RECAPTCHA_SITEKEY = "6Lc8WwosAAAAABY42gdwB6ShcYBPW_YHTQeIhjav"


def parse_accounts(accounts_str: str) -> list:
    accounts = []
    if not accounts_str:
        return accounts
    for item in accounts_str.split(','):
        item = item.strip()
        if ':' in item:
            email, password = item.split(':', 1)
            accounts.append({'email': email.strip(), 'password': password.strip()})
    return accounts


def get_session_file(email: str) -> Path:
    SESSION_DIR.mkdir(exist_ok=True)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    return SESSION_DIR / f"{safe_name}.json"


# 青龙通知
try:
    from notify import send as notify_send
except ImportError:
    def notify_send(title, content): print(f"[通知] {title}: {content}")


class Logger:
    @staticmethod
    def log(step: str, msg: str, status: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        symbols = {"INFO": "ℹ", "OK": "✓", "WARN": "⚠", "ERROR": "✗", "WAIT": "⏳"}
        symbol = symbols.get(status, "•")
        print(f"[{timestamp}] [{step}] {symbol} {msg}")


class YesCaptchaSolver:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = YESCAPTCHA_API_URL
    
    def create_task(self, site_key: str, page_url: str) -> str:
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "softID": "26129",
            }
        }
        response = requests.post(f"{self.base_url}/createTask", json=payload, timeout=30)
        result = response.json()
        if result.get("errorId") == 0:
            return result.get("taskId")
        raise Exception(f"YesCaptcha 创建任务失败: {result.get('errorDescription')}")
    
    def get_result(self, task_id: str, max_wait: int = 120) -> str:
        payload = {"clientKey": self.api_key, "taskId": task_id}
        start_time = time.time()
        while time.time() - start_time < max_wait:
            response = requests.post(f"{self.base_url}/getTaskResult", json=payload, timeout=30)
            result = response.json()
            if result.get("errorId") != 0:
                raise Exception(f"YesCaptcha 错误: {result.get('errorDescription')}")
            if result.get("status") == "ready":
                return result.get("solution", {}).get("gRecaptchaResponse")
            time.sleep(3)
        raise Exception("YesCaptcha 超时")
    
    def solve(self, site_key: str, page_url: str) -> str:
        Logger.log("验证码", "创建 YesCaptcha 任务...", "WAIT")
        task_id = self.create_task(site_key, page_url)
        Logger.log("验证码", f"任务 ID: {task_id}")
        Logger.log("验证码", "等待验证码解决...", "WAIT")
        token = self.get_result(task_id)
        Logger.log("验证码", "验证码已解决!", "OK")
        return token


class ZapKeepAlive:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.session_file = get_session_file(email)
        self.solver = YesCaptchaSolver(YESCAPTCHA_API_KEY) if YESCAPTCHA_API_KEY else None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None
    
    async def handle_cloudflare(self, max_attempts: int = 20) -> bool:
        for attempt in range(max_attempts):
            try:
                await self.page.wait_for_load_state('domcontentloaded', timeout=5000)
                title = await self.page.title()
                if "Just a moment" not in title:
                    return True
            except:
                await asyncio.sleep(1)
                continue
            wrapper = await self.page.query_selector('.main-wrapper')
            if wrapper:
                rect = await wrapper.bounding_box()
                if rect:
                    x, y = int(rect['x'] + 25), int(rect['y'] + rect['height'] / 2)
                    await self.cdp.send('Input.dispatchMouseEvent', {
                        'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1
                    })
                    await asyncio.sleep(0.1)
                    await self.cdp.send('Input.dispatchMouseEvent', {
                        'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1
                    })
            await asyncio.sleep(2)
        return False
    
    async def close_modals(self):
        try:
            dont_show = await self.page.query_selector('button:has-text("Don\'t show again")')
            if dont_show and await dont_show.is_visible():
                await dont_show.click()
                await asyncio.sleep(1)
            close_btns = await self.page.query_selector_all('.modal .close, button.close, [data-dismiss="modal"]')
            for btn in close_btns:
                try:
                    if await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(0.5)
                except:
                    pass
            await self.page.keyboard.press('Escape')
            await asyncio.sleep(0.5)
        except:
            pass
    
    async def login(self) -> bool:
        Logger.log("登录", f"开始登录 {self.email}...", "WAIT")
        Logger.log("登录", "导航到登录页面...")
        await self.page.goto(LOGIN_URL)
        await asyncio.sleep(3)
        
        Logger.log("登录", "处理 Cloudflare 验证...", "WAIT")
        if not await self.handle_cloudflare():
            Logger.log("登录", "Cloudflare 验证超时", "ERROR")
            return False
        Logger.log("登录", "Cloudflare 验证通过!", "OK")
        await asyncio.sleep(2)
        
        try:
            btn = await self.page.query_selector('button:has-text("Accept all")')
            if btn:
                await btn.click()
                Logger.log("登录", "已接受 cookies", "OK")
        except:
            pass
        await asyncio.sleep(1)
        
        Logger.log("登录", "打开登录对话框...")
        login_link = await self.page.query_selector('text="Log in!"') or \
                     await self.page.query_selector('text="Already registered"') or \
                     await self.page.query_selector('a:has-text("Log in")')
        if login_link:
            await login_link.click()
            Logger.log("登录", "已点击登录链接", "OK")
        
        # 新版顶部登录栏需先展开邮箱登录，输入框存在不代表可以操作。
        email_trigger = self.page.locator('#hlbEmailTrigger')
        if await email_trigger.is_visible():
            await email_trigger.click(timeout=10000)
            Logger.log("登录", "已展开邮箱登录表单", "OK")
        
        await asyncio.sleep(2)  # 等待对话框加载
        
        Logger.log("登录", "填写登录表单...")
        
        # 查找用户名输入框
        email_input = None
        for selector in ['#headerLoginForm #hlbUsername', 'input[placeholder*="E-Mail"]', 'input[placeholder*="e-mail"]',
                         'input[placeholder*="Username"]', '.modal input[type="text"]']:
            email_input = await self.page.query_selector(selector)
            if email_input and await email_input.is_visible():
                break
            email_input = None
        
        if not email_input:
            all_inputs = await self.page.query_selector_all('input[type="text"], input[type="email"]')
            for inp in all_inputs:
                if await inp.is_visible():
                    placeholder = await inp.get_attribute('placeholder') or ''
                    if 'search' not in placeholder.lower():
                        email_input = inp
                        break
        
        if email_input:
            await email_input.fill(self.email)
            Logger.log("登录", f"用户名: {self.email}", "OK")
        else:
            Logger.log("登录", "找不到用户名输入框", "ERROR")
            return False
        
        # 查找密码输入框
        password_input = None
        # 密码与邮箱必须来自同一个表单，避免命中页面上其他登录/注册框。
        form_handle = await email_input.evaluate_handle('(input) => input.closest("form")')
        login_form = form_handle.as_element()
        if not login_form:
            Logger.log("登录", "找不到登录表单", "ERROR")
            return False
        all_passwords = await login_form.query_selector_all('input[type="password"]')
        for pwd in all_passwords:
            if await pwd.is_visible():
                password_input = pwd
                break
        
        if password_input:
            await password_input.fill(self.password)
            Logger.log("登录", "密码: ********", "OK")
        else:
            Logger.log("登录", "找不到密码输入框", "ERROR")
            return False
        
        # 等待 reCAPTCHA 结果并注入
        if self.solver:
            Logger.log("登录", "等待 reCAPTCHA 结果...", "WAIT")
            try:
                recaptcha_token = await asyncio.to_thread(
                    self.solver.solve, RECAPTCHA_SITEKEY, LOGIN_URL
                )
                Logger.log("登录", "reCAPTCHA 已解决", "OK")
                
                # 注入 token
                await self.page.evaluate('''
                    (token) => {
                        const textareas = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
                        textareas.forEach(ta => { ta.style.display = 'block'; ta.value = token; });
                        
                        if (typeof ___grecaptcha_cfg !== 'undefined') {
                            const clients = ___grecaptcha_cfg.clients;
                            for (const key in clients) {
                                const client = clients[key];
                                for (const prop in client) {
                                    const val = client[prop];
                                    if (val && typeof val === 'object' && val.callback) {
                                        try { val.callback(token); } catch(e) {}
                                    }
                                }
                            }
                        }
                        return true;
                    }
                ''', recaptcha_token)
                Logger.log("登录", "reCAPTCHA token 已注入", "OK")
            except Exception as e:
                Logger.log("登录", f"reCAPTCHA 错误: {e}", "ERROR")
                return False
        else:
            Logger.log("登录", "未配置 YESCAPTCHA_API_KEY", "WARN")
        
        # 立即点击登录按钮
        Logger.log("登录", "点击 Login 按钮...")
        login_btn = None
        for selector in ['#headerLoginForm button[type="submit"]', '.modal button:has-text("Login")', '.modal button:has-text("Log in")',
                         'button:has-text("Login")', 'button:has-text("Log in")', 
                         '.modal button[type="submit"]']:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    login_btn = btn
                    break
            except:
                continue
        
        if login_btn:
            await login_btn.click()
            Logger.log("登录", "点击了登录按钮", "OK")
        else:
            await password_input.press('Enter')
            Logger.log("登录", "未找到按钮，按 Enter", "WARN")
        
        Logger.log("登录", "等待登录结果...", "WAIT")
        await asyncio.sleep(5)
        
        # 调试: 检查页面上是否有错误提示
        try:
            modal_content = await self.page.evaluate('() => document.querySelector(".modal-body, .modal-content")?.innerText || ""')
            if modal_content:
                Logger.log("登录", f"Modal 内容: {modal_content[:150]}", "INFO")
        except:
            pass
        
        # 关闭可能的弹窗
        await self.close_modals()
        
        # 等待并检查多次
        for i in range(15):
            await asyncio.sleep(2)
            
            # 每次都尝试关闭弹窗
            await self.close_modals()
            
            url = self.page.url
            if 'customer' in url:
                Logger.log("登录", "登录成功!", "OK")
                return True
            
            # 检查是否有错误提示
            try:
                error_text = await self.page.evaluate('() => document.querySelector(".alert-danger, .error-message, .login-error, .text-danger")?.innerText || ""')
                if error_text and 'wrong' in error_text.lower():
                    Logger.log("登录", f"错误提示: {error_text[:100]}", "ERROR")
                    break
            except:
                pass
        
        url = self.page.url
        if 'customer' in url:
            Logger.log("登录", "登录成功!", "OK")
            return True
        
        # 调试信息
        Logger.log("登录", f"登录失败 - 当前URL: {url}", "ERROR")
        try:
            page_text = await self.page.evaluate('() => document.body.innerText.substring(0, 500)')
            Logger.log("登录", f"页面内容: {page_text[:200]}...", "INFO")
        except:
            pass
        return False
    
    async def visit_vps_detail(self) -> bool:
        Logger.log("VPS", "访问 Dashboard...", "WAIT")
        await self.page.goto(DASHBOARD_URL, wait_until='domcontentloaded')
        await asyncio.sleep(3)
        
        if not await self.handle_cloudflare():
            Logger.log("VPS", "Cloudflare 验证超时", "ERROR")
            return False
        Logger.log("VPS", "Cloudflare 验证通过!", "OK")
        await asyncio.sleep(2)
        
        await self.close_modals()
        
        Logger.log("VPS", "查找 My VPS 入口...")
        vps_link = None
        for selector in ['a:has-text("My VPS")', 'a[href*="vserver"]', 'text=My VPS']:
            try:
                link = await self.page.query_selector(selector)
                if link and await link.is_visible():
                    vps_link = link
                    break
            except:
                continue
        
        if vps_link:
            await vps_link.click()
            Logger.log("VPS", "点击了 My VPS", "OK")
            await asyncio.sleep(3)
        
        await self.handle_cloudflare(10)
        await asyncio.sleep(2)
        
        Logger.log("VPS", "查找 VPS 详情页...")
        links = await self.page.evaluate('''
            () => {
                const links = document.querySelectorAll('a');
                return Array.from(links).map(a => ({
                    text: a.innerText.trim().substring(0, 100),
                    href: a.href
                })).filter(l => l.href && l.href.includes('vserver'));
            }
        ''')
        
        for link in links:
            if '/id/' in link['href'] or '/show/' in link['href']:
                await self.page.goto(link['href'])
                Logger.log("VPS", f"进入 VPS 详情页", "OK")
                break
        
        await asyncio.sleep(3)
        await self.handle_cloudflare(10)
        await asyncio.sleep(2)
        await self.close_modals()
        
        current_url = self.page.url
        Logger.log("VPS", f"当前页面: {current_url}")
        
        try:
            page_text = await self.page.evaluate('() => document.body.innerText')
            if 'ONLINE' in page_text:
                Logger.log("VPS", "VPS 状态: ONLINE", "OK")
            elif 'OFFLINE' in page_text:
                Logger.log("VPS", "VPS 状态: OFFLINE", "WARN")
        except:
            pass
        
        return 'vserver' in current_url
    
    async def stay_and_refresh(self):
        Logger.log("保活", f"在 VPS 详情页停留 {STAY_DURATION} 秒...", "WAIT")
        for i in range(STAY_DURATION, 0, -1):
            print(f"\r[{datetime.now().strftime('%H:%M:%S')}] [保活] ⏳ 剩余 {i} 秒...", end='', flush=True)
            await asyncio.sleep(1)
        print()
        Logger.log("保活", "停留完成", "OK")
        
        Logger.log("保活", "刷新页面 (F5)...", "WAIT")
        await self.page.reload()
        await asyncio.sleep(5)
        await self.handle_cloudflare(10)
        await asyncio.sleep(2)
        Logger.log("保活", "页面已刷新", "OK")
    
    async def save_session(self):
        cookies = await self.context.cookies()
        with open(self.session_file, 'w') as f:
            json.dump(cookies, f, indent=2)
        Logger.log("会话", f"会话已保存到 {self.session_file.name}", "OK")
    
    async def load_session(self) -> bool:
        if self.session_file.exists():
            try:
                with open(self.session_file) as f:
                    cookies = json.load(f)
                await self.context.add_cookies(cookies)
                Logger.log("会话", "已加载保存的会话", "OK")
                return True
            except Exception as e:
                Logger.log("会话", f"加载会话失败: {e}", "WARN")
        return False
    
    async def run(self) -> bool:
        print()
        print("-" * 60)
        Logger.log("账号", f"开始处理: {self.email}", "WAIT")
        print("-" * 60)
        
        async with async_playwright() as p:
            Logger.log("启动", "启动浏览器...")
            self.browser = await p.chromium.launch(
                headless=False,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled']
            )
            self.context = await self.browser.new_context(
                viewport={'width': 1280, 'height': 900},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            self.page = await self.context.new_page()
            self.cdp = await self.context.new_cdp_session(self.page)
            Logger.log("启动", "浏览器已启动", "OK")
            
            await self.load_session()
            
            Logger.log("检查", "检查登录状态...", "WAIT")
            await self.page.goto(DASHBOARD_URL, wait_until='domcontentloaded')
            await asyncio.sleep(5)
            
            cf_passed = await self.handle_cloudflare()
            if cf_passed:
                Logger.log("检查", "Cloudflare 验证通过", "OK")
            await asyncio.sleep(2)
            
            current_url = self.page.url
            need_login = 'login' in current_url.lower() or '#login' in current_url or 'customer' not in current_url
            
            if need_login:
                Logger.log("检查", "需要登录", "WARN")
                if not await self.login():
                    Logger.log("结果", "登录失败，任务终止", "ERROR")
                    await self.browser.close()
                    return False
            else:
                Logger.log("检查", "会话有效，已登录", "OK")
            
            if not await self.visit_vps_detail():
                Logger.log("结果", "访问 VPS 详情页失败", "ERROR")
                await self.browser.close()
                return False
            
            await self.stay_and_refresh()
            await self.save_session()
            
            Logger.log("结果", f"{self.email} 保活完成!", "OK")
            
            await self.browser.close()
            return True


async def main():
    if not YESCAPTCHA_API_KEY:
        print("警告: 未设置 YESCAPTCHA_API_KEY 环境变量，登录时可能无法自动解决验证码")
    
    if not ACCOUNTS_STR:
        print("错误: 未设置 ZAP_ACCOUNT 环境变量")
        exit(1)
    
    accounts = parse_accounts(ACCOUNTS_STR)
    if not accounts:
        print("错误: 无有效账号配置")
        exit(1)
    
    
    print()
    print("=" * 60)
    print("  ZAP-Hosting Lifetime VPS 保活脚本")
    print("=" * 60)
    print(f"  账号数量: {len(accounts)}")
    print(f"  停留时间: {STAY_DURATION} 秒")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    results = []
    for i, account in enumerate(accounts, 1):
        print(f"\n[进度] 处理账号 {i}/{len(accounts)}")
        keeper = ZapKeepAlive(account['email'], account['password'])
        success = await keeper.run()
        results.append({'email': account['email'], 'success': success})
    
    print()
    print("=" * 60)
    print("  📊 任务汇总")
    print("=" * 60)
    success_count = sum(1 for r in results if r['success'])
    for r in results:
        status = "✓ 成功" if r['success'] else "✗ 失败"
        print(f"  {status}: {r['email']}")
    print("-" * 60)
    print(f"  总计: {success_count}/{len(results)} 成功")
    print(f"  完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print()
    
    # 发送汇总通知
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if success_count == len(results):
        notify_title = "ZAP 保活成功"
        emoji = "✅"
    elif success_count > 0:
        notify_title = "ZAP 保活部分成功"
        emoji = "⚠️"
    else:
        notify_title = "ZAP 保活失败"
        emoji = "❌"
    
    msg_lines = [f"{emoji} {notify_title}", ""]
    for r in results:
        status = "✅" if r['success'] else "❌"
        msg_lines.append(f"{status} {r['email']}")
    msg_lines.append("")
    msg_lines.append(f"📊 结果: {success_count}/{len(results)} 成功")
    msg_lines.append(f"🕒 时间: {now}")
    
    message = "\n".join(msg_lines)
    notify_send(notify_title, message)
    
    return success_count == len(results)


if __name__ == '__main__':
    result = asyncio.run(main())
    exit(0 if result else 1)
