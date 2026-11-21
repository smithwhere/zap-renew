"""Verify Classic Panel access through the normal login form."""

import asyncio
import re
from time import monotonic
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


CLASSIC_HOST = 'legacy.zap-hosting.com'
DASHBOARD_URL = f'https://{CLASSIC_HOST}/en/customer/home/'
LOGIN_URL = f'https://{CLASSIC_HOST}/interface/login/?redirect=%2Fen%2Fcustomer%2Fhome%2F'
VISIBLE_CHALLENGE = (
    'iframe[title*="captcha" i]:visible, iframe[src*="/recaptcha/"]:visible, '
    'iframe[src*="challenges.cloudflare.com"]:visible, '
    '.g-recaptcha:visible, .h-captcha:visible, .cf-turnstile:visible'
)
FORM_CHALLENGE = ('.contains-recaptcha, .g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], '
                  '[name="g-recaptcha-response"], [name="h-captcha-response"], '
                  '[name="cf-turnstile-response"]')


class LiveVerificationError(RuntimeError):
    """A fixed diagnostic message that can be shown in public Actions logs."""


def is_classic_url(url):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and parsed.netloc == CLASSIC_HOST


async def check_challenge(page, stage, response=None):
    if response and response.headers.get('cf-mitigated') == 'challenge':
        raise LiveVerificationError(f'检测到 Cloudflare 验证码（{stage}），已停止。')
    if (await page.title()).lower().startswith('just a moment'):
        raise LiveVerificationError(f'检测到 Cloudflare 验证页（{stage}），已停止。')
    if await page.locator(VISIBLE_CHALLENGE).count():
        raise LiveVerificationError(f'检测到验证码（{stage}），已停止。')
    if await page.locator('input[autocomplete="one-time-code"]:visible').count():
        raise LiveVerificationError(f'面板要求一次性验证码（{stage}），已停止。')


async def check_form_challenge(form):
    if await form.evaluate('(form, selector) => form.matches(selector) || !!form.querySelector(selector)',
                           FORM_CHALLENGE):
        raise LiveVerificationError('检测到验证码（登录表单要求人机验证），已停止。')


async def verify_vps(page, email, password, vps_url):
    target = urlparse(vps_url)
    if (not is_classic_url(vps_url) or target.query or target.fragment or
            not re.fullmatch(r'/en/customer/vserver/show/\d+/overview/?', target.path)):
        raise LiveVerificationError('ZAP_VPS_URL 必须是 Classic Panel 的 VPS 概览地址。')

    print('[实站验证] 打开 Classic Panel 登录页。', flush=True)
    response = await page.goto(LOGIN_URL, wait_until='domcontentloaded')
    await check_challenge(page, '打开登录页', response)
    if not is_classic_url(page.url) or urlparse(page.url).path != '/interface/login/':
        raise LiveVerificationError('打开登录页时跳转到非预期页面。')
    if response and not response.ok:
        raise LiveVerificationError(f'打开登录页失败（HTTP {response.status}）。')

    only_necessary = page.get_by_role('button', name='Only necessary', exact=True)
    if await only_necessary.is_visible():
        await only_necessary.click()

    username = page.locator('input[name="username"]:visible')
    password_field = page.locator('input[type="password"]:visible')
    forms = page.locator('form:visible', has=username).filter(has=password_field)
    deadline = monotonic() + 10
    while True:
        await check_challenge(page, '等待登录表单')
        if await forms.count() == 1:
            break
        if monotonic() >= deadline:
            raise LiveVerificationError('未找到唯一的可见登录表单。')
        await asyncio.sleep(0.25)

    await check_form_challenge(forms)
    submit = forms.get_by_role('button', name='Login', exact=True)
    action = await submit.get_attribute('formaction') or await forms.get_attribute('action') or ''
    method = await submit.get_attribute('formmethod') or await forms.get_attribute('method') or 'get'
    if (not is_classic_url(page.url) or not is_classic_url(urljoin(page.url, action))
            or method.lower() != 'post'):
        raise LiveVerificationError('登录表单提交地址或方法不符合预期。')

    await forms.locator('input[name="username"]:visible').fill(email)
    await forms.locator('input[type="password"]:visible').fill(password)
    await check_challenge(page, '提交登录前')
    await check_form_challenge(forms)

    print('[实站验证] 提交普通登录表单。', flush=True)
    try:
        await submit.click()
    except PlaywrightTimeoutError:
        await check_challenge(page, '点击登录时')
        raise LiveVerificationError('点击登录按钮时等待超时。') from None
    deadline = monotonic() + 20
    while True:
        await check_challenge(page, '提交登录后')
        current = urlparse(page.url)
        if is_classic_url(page.url) and current.path == urlparse(DASHBOARD_URL).path:
            break
        if not is_classic_url(page.url):
            raise LiveVerificationError('提交登录后跳转到非预期站点。')
        if monotonic() >= deadline:
            raise LiveVerificationError('提交登录后未进入 Classic Panel 客户区。')
        await asyncio.sleep(0.25)

    print('[实站验证] 已进入 Classic Panel 客户区，正在打开目标 VPS。', flush=True)
    response = await page.goto(vps_url, wait_until='domcontentloaded')
    await check_challenge(page, '打开 VPS', response)
    if (not response or not response.ok or not is_classic_url(page.url) or
            urlparse(page.url).path.rstrip('/') != target.path.rstrip('/') or
            not re.search(r'vServer.*Overview.*ZAP-Hosting', await page.title(), re.IGNORECASE)):
        raise LiveVerificationError('未显示目标 VPS 概览。')
    print('[实站验证] 已打开并确认目标 VPS 概览。', flush=True)


async def run(accounts, vps_url):
    if not accounts or any(not account['email'] or not account['password'] for account in accounts):
        print('::error::[实站验证] 缺少有效的 ZAP_ACCOUNT 配置。', flush=True)
        return False

    try:
        async with async_playwright() as playwright:
            async with await playwright.chromium.launch() as browser:
                for index, account in enumerate(accounts, 1):
                    print(f'[实站验证] 账号 {index}/{len(accounts)}', flush=True)
                    async with await browser.new_context() as context:
                        page = await context.new_page()
                        page.set_default_timeout(10_000)
                        page.set_default_navigation_timeout(20_000)
                        await verify_vps(page, account['email'], account['password'], vps_url)
    except (LiveVerificationError, PlaywrightError) as error:
        detail = str(error) if isinstance(error, LiveVerificationError) else type(error).__name__
        print(f'::error::[实站验证] {detail}', flush=True)
        return False
    return True
