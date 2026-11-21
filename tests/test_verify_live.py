"""Exercise live verification against browser fixtures without real credentials."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from playwright.async_api import async_playwright
from scripts.verify_live import DASHBOARD_URL, LOGIN_URL, LiveVerificationError, verify_vps


VPS_URL = 'https://legacy.zap-hosting.com/en/customer/vserver/show/123/overview/'
LOGIN_FORM = '''
<title>Login</title>
<form method="post" action="/en/customer/home/" class="{form_class}">
  <input name="username" aria-label="Username / E-Mail">
  <input name="password" type="password" aria-label="Password">
  <button type="submit">Login</button>
  {challenge}
</form>
'''


class LiveVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def setUp_browser(self, challenge='', submitted_page='<title>Dashboard</title>',
                            vps_page='<title>vServer #1 - Overview - ZAP-Hosting</title>',
                            form_class=''):
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        browser = await playwright.chromium.launch()
        self.addAsyncCleanup(browser.close)
        page = await browser.new_page()
        self.submitted = False
        self.visited_vps = False
        self.vps_visits = 0

        async def route_request(route):
            if route.request.url == LOGIN_URL:
                if isinstance(challenge, dict):
                    await route.fulfill(**challenge)
                    return
                body = LOGIN_FORM.format(challenge=challenge, form_class=form_class)
            elif route.request.url == DASHBOARD_URL and route.request.method == 'POST':
                self.submitted = True
                body = submitted_page
            elif route.request.url == VPS_URL:
                self.visited_vps = True
                self.vps_visits += 1
                body = vps_page
            else:
                await route.abort()
                return
            await route.fulfill(content_type='text/html', body=body)

        await page.route('**/*', route_request)
        return page

    async def test_visits_target_after_normal_login(self):
        page = await self.setUp_browser()
        with redirect_stdout(io.StringIO()) as log:
            await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        self.assertTrue(self.submitted)
        self.assertTrue(self.visited_vps)
        self.assertIn('Login 点击：已完成', log.getvalue())
        self.assertIn('登录提交 POST：1 次；响应：HTTP 200', log.getvalue())

    async def test_renew_refreshes_the_verified_vps_page(self):
        page = await self.setUp_browser()
        with redirect_stdout(io.StringIO()) as log:
            await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL, stay_duration=0)
        self.assertEqual(self.vps_visits, 2)
        self.assertIn('已刷新并确认目标 VPS 概览', log.getvalue())

    async def test_stops_before_filling_captcha_protected_form(self):
        page = await self.setUp_browser('<div class="g-recaptcha" hidden></div>')
        with self.assertRaisesRegex(LiveVerificationError, '验证码.*登录表单'):
            await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        self.assertEqual(await page.locator('input[name="username"]').input_value(), '')
        self.assertFalse(self.submitted)

    async def test_stops_when_classic_panel_marks_the_form_as_captcha_protected(self):
        page = await self.setUp_browser(form_class='header-login-bar contains-recaptcha')
        with redirect_stdout(io.StringIO()) as log:
            with self.assertRaisesRegex(LiveVerificationError, '验证码.*登录表单'):
                await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL, stay_duration=0)
        self.assertIn('登录页响应：HTTP 200', log.getvalue())
        self.assertIn('Login 点击：未尝试', log.getvalue())
        self.assertIn('登录提交 POST：不适用；响应：不适用', log.getvalue())
        self.assertEqual(await page.locator('input[name="username"]').input_value(), '')
        self.assertFalse(self.submitted)
        self.assertFalse(self.visited_vps)

    async def test_click_with_client_validation_and_no_login_request(self):
        page = await self.setUp_browser('''
            <script>document.forms[0].onsubmit = event => event.preventDefault()</script>
            <span class="error-message" hidden>Too many attempts</span>
            <span class="error-message">Invalid credentials for fixture@example.test</span>
            <span role="alert">PRIVATE-REFERENCE-123</span>
        ''')
        with redirect_stdout(io.StringIO()) as log, patch('scripts.verify_live.LOGIN_RESULT_TIMEOUT', 0.5, create=True):
            with self.assertRaisesRegex(LiveVerificationError, '未进入'):
                await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        output = log.getvalue()
        self.assertIn('Login 点击：已完成', output)
        self.assertIn('登录提交 POST：0 次；响应：无', output)
        self.assertIn('Invalid credentials', output)
        self.assertIn('其他可见错误（原文已隐藏）', output)
        for private in ('fixture@example.test', 'fixture-password', 'PRIVATE-REFERENCE-123', 'Too many attempts'):
            self.assertNotIn(private, output)

    async def test_ajax_login_401_is_separate_from_unrelated_post_and_page_error(self):
        page = await self.setUp_browser('''
            <div class="alert-danger"></div>
            <script>
              document.forms[0].onsubmit = async event => {
                event.preventDefault();
                await fetch('/metrics', {method: 'POST'});
                  await fetch('/interface/login/json_login.php?fixture=private-reference', {method: 'POST'});
                document.querySelector('.alert-danger').textContent = 'Too many attempts: fixture-password';
              };
            </script>
        ''')
        await page.route('**/metrics', lambda route: route.fulfill(status=204))
        await page.route('**/json_login.php?*', lambda route: route.fulfill(status=401, body='private body'))
        with redirect_stdout(io.StringIO()) as log, patch('scripts.verify_live.LOGIN_RESULT_TIMEOUT', 1, create=True):
            with self.assertRaisesRegex(LiveVerificationError, '未进入'):
                await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        output = log.getvalue()
        self.assertIn('登录提交 POST：1 次；响应：HTTP 401', output)
        self.assertIn('其他同源 POST：1 次；响应：HTTP 204', output)
        self.assertIn('Too many attempts', output)
        for private in ('private body', 'fixture-password', '/metrics', 'private-reference', 'json_login.php'):
            self.assertNotIn(private, output)

    async def test_aborted_post_is_reported_as_a_request_without_an_http_status(self):
        page = await self.setUp_browser('''
            <script>
              document.forms[0].onsubmit = event => {
                event.preventDefault();
                fetch(document.forms[0].action, {method: 'POST'}).catch(() => {});
              };
            </script>
        ''')
        await page.route(DASHBOARD_URL, lambda route: route.abort())
        with redirect_stdout(io.StringIO()) as log, patch('scripts.verify_live.LOGIN_RESULT_TIMEOUT', 1, create=True):
            with self.assertRaisesRegex(LiveVerificationError, '未进入'):
                await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        self.assertIn('登录提交 POST：1 次；响应：请求失败（无 HTTP 响应）', log.getvalue())

    async def test_stops_at_cloudflare_challenge(self):
        page = await self.setUp_browser({
            'status': 403, 'headers': {'cf-mitigated': 'challenge'},
            'content_type': 'text/html', 'body': '<title>Just a moment...</title>',
        })
        with self.assertRaisesRegex(LiveVerificationError, '验证码.*打开登录页'):
            await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        self.assertFalse(self.submitted)

    async def test_stops_at_captcha_after_form_submission(self):
        page = await self.setUp_browser(submitted_page='''
            <title>Verify</title><iframe title="reCAPTCHA challenge"></iframe>
        ''')
        with self.assertRaisesRegex(LiveVerificationError, '验证码.*提交登录后'):
            await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        self.assertTrue(self.submitted)
        self.assertFalse(self.visited_vps)

    async def test_overview_url_with_wrong_page_is_not_a_success(self):
        page = await self.setUp_browser(vps_page='<title>Server not found</title>')
        with self.assertRaisesRegex(LiveVerificationError, '未显示目标 VPS 概览'):
            await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
