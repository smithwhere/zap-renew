#!/usr/bin/env python3
"""
ZAP-Hosting Lifetime VPS 保活脚本

cron: 0 8 * * 1
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
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlparse, unquote
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
LOGIN_WAIT_DURATION = int(os.environ.get('LOGIN_WAIT_DURATION', '30'))

BASE_URL = os.environ.get('ZAP_BASE_URL', 'https://legacy.zap-hosting.com').rstrip('/')
LOGIN_URL = f"{BASE_URL}/interface/login/"
DASHBOARD_URL = f"{BASE_URL}/en/customer/home/"
VPS_URL = os.environ.get('ZAP_VPS_URL', '')
PROXY_URL = os.environ.get('ZAP_PROXY_URL', '').strip()
SESSION_DIR = Path(__file__).parent / "sessions"
RECAPTCHA_SITEKEY = "6Lc8WwosAAAAABY42gdwB6ShcYBPW_YHTQeIhjav"


def browser_proxy(proxy_url: str):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if (parsed.scheme not in ('http', 'https', 'socks5') or not parsed.hostname
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('ZAP_PROXY_URL 必须是 HTTP/HTTPS 或无密码 SOCKS5 代理地址')
    try:
        parsed.port  # 检查端口格式；错误信息不包含凭据。
    except ValueError:
        raise ValueError('ZAP_PROXY_URL 的端口无效') from None
    if parsed.scheme == 'socks5' and (parsed.username or parsed.password):
        raise ValueError('浏览器不支持带认证的 SOCKS5 代理')
    config = {'server': f'{parsed.scheme}://{parsed.netloc.rsplit("@", 1)[-1]}'}
    if parsed.username is not None:
        config['username'] = unquote(parsed.username)
        config['password'] = unquote(parsed.password or '')
    return config


def is_customer_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.netloc == urlparse(BASE_URL).netloc
            and parsed.path.startswith('/en/customer/'))


def is_vps_detail_url(url: str) -> bool:
    if not is_customer_url(url):
        return False
    path = urlparse(url).path
    if VPS_URL:
        target = urlparse(VPS_URL)
        return (urlparse(url).netloc == target.netloc and
                path.rstrip('/') == target.path.rstrip('/'))
    return '/vserver/show/' in path or '/vserver/id/' in path


def send_email(title: str, content: str) -> bool:
    username = os.environ.get('SMTP_USERNAME', '')
    password = os.environ.get('SMTP_PASSWORD', '')
    recipient = os.environ.get('SMTP_TO', '')
    if not any((username, password, recipient)):
        return True  # 邮件通知为可选功能。
    if not all((username, password, recipient)):
        Logger.log('邮件', 'SMTP_USERNAME、SMTP_PASSWORD、SMTP_TO 配置不完整', 'ERROR')
        return False
    message = EmailMessage()
    message['Subject'] = title
    message['From'] = username
    message['To'] = recipient
    message.set_content(content)
    try:
        with smtplib.SMTP_SSL(
            os.environ.get('SMTP_HOST', 'smtp.qq.com'),
            int(os.environ.get('SMTP_PORT', '465')),
            context=ssl.create_default_context(), timeout=30,
        ) as smtp:
            smtp.login(username, password)
            refused = smtp.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        Logger.log('邮件', '通知已被邮件服务器接受', 'OK')
        return True
    except (OSError, smtplib.SMTPException, ValueError) as error:
        Logger.log('邮件', f'发送失败: {type(error).__name__}', 'ERROR')
        return False


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
    
    def _submit_task(self, task: dict) -> str:
        payload = {'clientKey': self.api_key, 'task': task}
        response = requests.post(f"{self.base_url}/createTask", json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        if result.get('errorId') != 0:
            # 不回显 errorDescription，它可能包含请求参数或代理凭据。
            raise RuntimeError(f"YesCaptcha 创建任务失败: {result.get('errorCode', 'API_ERROR')}")
        task_id = result.get('taskId')
        if not task_id:
            raise RuntimeError('YesCaptcha 未返回任务 ID')
        return task_id

    def create_task(self, site_key: str, page_url: str) -> str:
        return self._submit_task({
            "type": "NoCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "softID": "26129",
        })
    
    def _get_solution(self, task_id: str, max_wait: int = 120) -> dict:
        payload = {"clientKey": self.api_key, "taskId": task_id}
        start_time = time.monotonic()
        while time.monotonic() - start_time < max_wait:
            response = requests.post(f"{self.base_url}/getTaskResult", json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
            if result.get("errorId") != 0:
                raise RuntimeError(f"YesCaptcha 任务失败: {result.get('errorCode', 'API_ERROR')}")
            if result.get("status") == "ready":
                solution = result.get('solution')
                if not isinstance(solution, dict):
                    raise RuntimeError('YesCaptcha 未返回有效 solution')
                return solution
            time.sleep(3)
        raise RuntimeError("YesCaptcha 超时")

    def get_result(self, task_id: str, max_wait: int = 120) -> str:
        token = self._get_solution(task_id, max_wait).get('gRecaptchaResponse')
        if not token:
            raise RuntimeError('YesCaptcha 未返回 reCAPTCHA token')
        return token

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
    
    async def click_cloudflare_widget(self, clicked_nodes: set) -> bool:
        """沿用上游的 CDP 鼠标事件，定位实际控件而非整块页面容器。"""
        try:
            document = await self.cdp.send('DOM.getDocument', {'depth': -1, 'pierce': True})
            pending = [document['root']]
            while pending:
                node = pending.pop()
                pending.extend(node.get('children', []))
                pending.extend(node.get('shadowRoots', []))
                if node.get('nodeName', '').upper() != 'IFRAME':
                    continue
                attrs = node.get('attributes', [])
                attributes = dict(zip(attrs[::2], attrs[1::2]))
                source = urlparse(attributes.get('src', ''))
                if (source.scheme != 'https' or source.hostname != 'challenges.cloudflare.com'
                        or '/turnstile/' not in source.path):
                    continue
                node_id = node['backendNodeId']
                if node_id in clicked_nodes:
                    continue  # 验证正在处理时，不重复点击同一个控件。
                await self.cdp.send('DOM.scrollIntoViewIfNeeded', {'backendNodeId': node_id})
                box = await self.cdp.send('DOM.getBoxModel', {'backendNodeId': node_id})
                model = box['model']
                if model['width'] < 100 or model['height'] < 40:
                    continue  # 自动验证/隐藏的 iframe 不需要点击。
                border = model['border']
                x = min(border[::2]) + 30
                y = (min(border[1::2]) + max(border[1::2])) / 2
                await self.cdp.send('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved', 'x': x, 'y': y,
                })
                await self.cdp.send('Input.dispatchMouseEvent', {
                    'type': 'mousePressed', 'x': x, 'y': y,
                    'button': 'left', 'buttons': 1, 'clickCount': 1,
                })
                await asyncio.sleep(0.1)
                await self.cdp.send('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased', 'x': x, 'y': y,
                    'button': 'left', 'buttons': 0, 'clickCount': 1,
                })
                clicked_nodes.add(node_id)
                Logger.log('Cloudflare', '已定位并点击验证控件，等待验证结果', 'WAIT')
                return True
        except Exception as error:
            # 验证页面可能正在跳转或重新创建 iframe，下次轮询重新定位。
            Logger.log('Cloudflare', f'控件定位/点击异常: {type(error).__name__}', 'WARN')
        return False

    async def handle_cloudflare(self, max_attempts: int = 20) -> bool:
        last_state = '页面尚未加载'
        last_title = ''
        clicked_nodes = set()
        for attempt in range(max_attempts):
            try:
                await self.page.wait_for_load_state('domcontentloaded', timeout=5000)
                title = await self.page.title()
                last_title = title
                last_state = f'页面标题: {title[:100]}'
                if title.strip() and "just a moment" not in title.lower():
                    return True
            except Exception as error:
                last_state = f'页面检查异常: {type(error).__name__}'
                await asyncio.sleep(1)
                continue
            clicked = await self.click_cloudflare_widget(clicked_nodes)
            if attempt == 0 and not clicked:
                Logger.log('Cloudflare', '未发现可点击控件，等待页面自动验证', 'WAIT')
            await asyncio.sleep(2)
        # 最后一次点击/自动验证可能已成功，报告超时前重新读取页面状态。
        try:
            await self.page.wait_for_load_state('domcontentloaded', timeout=5000)
            last_title = await self.page.title()
            last_state = f'页面标题: {last_title[:100]}'
            if last_title.strip() and 'just a moment' not in last_title.lower():
                return True
        except Exception as error:
            last_title = ''
            last_state = f'页面检查异常: {type(error).__name__}'
        # 验证页仍保留目标 URL，不能将它当成登录或访问成功。
        parsed = urlparse(self.page.url)
        safe_url = parsed._replace(query='', fragment='').geturl()
        Logger.log('Cloudflare', f'验证超时 ({max_attempts} 次检查); '
                   f'{last_state}; 已点击控件: {len(clicked_nodes)}; URL: {safe_url}', 'ERROR')
        return False
    
    async def accept_cookies(self):
        try:
            button = self.page.get_by_role('button', name='Accept all', exact=True)
            if await button.is_visible():
                await button.click(timeout=5000)
                Logger.log('Cookies', '已选择 Accept all', 'OK')
        except Exception as error:
            Logger.log('Cookies', f'处理 Cookie 提示失败: {type(error).__name__}', 'WARN')

    async def close_modals(self):
        await self.accept_cookies()
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
        
        await self.accept_cookies()
        await asyncio.sleep(1)
        
        Logger.log("登录", "打开登录对话框...")
        login_link = await self.page.query_selector('text="Log in!"') or \
                     await self.page.query_selector('text="Already registered"') or \
                     await self.page.query_selector('a:has-text("Log in")')
        inline_form = self.page.locator('form.inline-login')
        if login_link and not await inline_form.is_visible():
            await login_link.click()
            Logger.log("登录", "已点击登录链接", "OK")
        
        await asyncio.sleep(2)  # 等待对话框加载
        
        Logger.log("登录", "填写登录表单...")
        
        # 查找用户名输入框
        email_input = None
        # 首页登录链接展开的是首页表单；顶部栏在折叠时仍可能被判定为 visible。
        for selector in ['form.inline-login input[autocomplete="username"]',
                         '#post-8036 form input[name="username"]', '#recaptcha-login-name',
                         '.modal input[name="username"]', '#hlbUsername',
                         'input[placeholder*="E-Mail"]', 'input[placeholder*="e-mail"]',
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
        
        if not email_input:
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
        if await login_form.get_attribute('id') == 'headerLoginForm':
            await login_form.hover()
            email_trigger = self.page.locator('#hlbEmailTrigger')
            if await email_trigger.is_visible():
                await email_trigger.click(timeout=10000)
                Logger.log("登录", "已展开邮箱登录表单", "OK")
        await email_input.fill(self.email)
        Logger.log("登录", f"用户名: {self.email}", "OK")
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
                    self.solver.solve, RECAPTCHA_SITEKEY, self.page.url
                )
                Logger.log("登录", "reCAPTCHA 已解决", "OK")
                
                # 注入 token
                await self.page.evaluate('''
                    (token) => {
                        const textareas = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
                        textareas.forEach(ta => {
                            ta.value = token;
                            ta.dispatchEvent(new Event('input', {bubbles: true}));
                            ta.dispatchEvent(new Event('change', {bubbles: true}));
                        });
                        
                        if (typeof ___grecaptcha_cfg !== 'undefined') {
                            const clients = ___grecaptcha_cfg.clients;
                            const visited = new WeakSet();
                            const applyToken = (object, depth) => {
                                if (!object || typeof object !== 'object' || depth > 8 ||
                                    visited.has(object) || object instanceof Element) return;
                                visited.add(object);
                                for (const key of Object.keys(object)) {
                                    const value = object[key];
                                    if (key === 'callback' && typeof value === 'function') {
                                        try { value.call(object, token); } catch(e) {}
                                    } else if (value && typeof value === 'object') {
                                        applyToken(value, depth + 1);
                                    }
                                }
                            };
                            applyToken(clients, 0);
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
        for selector in ['button[type="submit"]', 'button:has-text("Login")',
                         'button:has-text("Log in")', 'input[type="submit"]']:
            try:
                btn = await login_form.query_selector(selector)
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
            if is_customer_url(url):
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
        if is_customer_url(url):
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
        if VPS_URL:
            Logger.log('VPS', '访问指定 VPS 详情页...', 'WAIT')
            await self.page.goto(VPS_URL, wait_until='domcontentloaded')
            if not await self.handle_cloudflare():
                return False
            await self.close_modals()
            return is_vps_detail_url(self.page.url)
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
        
        if not await self.handle_cloudflare(10):
            return False
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
        if not await self.handle_cloudflare(10):
            return False
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
        
        return is_vps_detail_url(current_url)
    
    async def stay_and_refresh(self):
        Logger.log("保活", f"在 VPS 详情页停留 {STAY_DURATION} 秒...", "WAIT")
        for i in range(STAY_DURATION, 0, -1):
            print(f"\r[{datetime.now().strftime('%H:%M:%S')}] [保活] ⏳ 剩余 {i} 秒...", end='', flush=True)
            await asyncio.sleep(1)
        print()
        Logger.log("保活", "停留完成", "OK")
        
        Logger.log("保活", "刷新页面 (F5)...", "WAIT")
        await self.page.reload(wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(5)
        if not await self.handle_cloudflare(10):
            Logger.log('保活', '刷新后的 Cloudflare 验证未通过', 'ERROR')
            return False
        await asyncio.sleep(2)
        if not is_vps_detail_url(self.page.url):
            Logger.log('保活', '刷新后未停留在 VPS 详情页', 'ERROR')
            return False
        Logger.log("保活", "页面已刷新", "OK")
        return True
    
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
                proxy=browser_proxy(PROXY_URL),
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled']
            )
            self.context = await self.browser.new_context(
                viewport={'width': 1280, 'height': 900},
            )
            self.page = await self.context.new_page()
            self.cdp = await self.context.new_cdp_session(self.page)
            Logger.log("启动", "浏览器已启动", "OK")
            
            await self.load_session()
            
            Logger.log("检查", "检查登录状态...", "WAIT")
            await self.page.goto(DASHBOARD_URL, wait_until='domcontentloaded')
            await asyncio.sleep(5)
            
            cf_passed = await self.handle_cloudflare()
            if not cf_passed:
                Logger.log('检查', 'Cloudflare 验证未通过，无法确认登录状态，任务终止', 'ERROR')
                await self.browser.close()
                return False
            Logger.log("检查", "Cloudflare 验证通过", "OK")
            await asyncio.sleep(2)
            
            current_url = self.page.url
            need_login = not is_customer_url(current_url)
            
            if need_login:
                Logger.log("检查", "需要登录", "WARN")
                if not await self.login():
                    Logger.log("结果", "登录失败，任务终止", "ERROR")
                    await self.browser.close()
                    return False
            else:
                Logger.log("检查", "会话有效，已登录", "OK")

            Logger.log('登录', f'登录成功后等待 {LOGIN_WAIT_DURATION} 秒...', 'WAIT')
            await asyncio.sleep(LOGIN_WAIT_DURATION)
            
            if not await self.visit_vps_detail():
                Logger.log("结果", "访问 VPS 详情页失败", "ERROR")
                await self.browser.close()
                return False
            
            if not await self.stay_and_refresh():
                await self.browser.close()
                return False
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
        try:
            success = await keeper.run()
        except Exception as error:
            Logger.log('结果', f'账号运行失败: {type(error).__name__}: {error}', 'ERROR')
            success = False
        finally:
            if keeper.browser and keeper.browser.is_connected():
                await keeper.browser.close()
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
    mail_sent = await asyncio.to_thread(send_email, notify_title, message)
    
    return success_count == len(results) and mail_sent


if __name__ == '__main__':
    result = asyncio.run(main())
    exit(0 if result else 1)
