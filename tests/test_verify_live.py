"""Exercise live verification against browser fixtures without real credentials."""
import unittest

from playwright.async_api import async_playwright
from scripts.verify_live import DASHBOARD_URL, LOGIN_URL, LiveVerificationError, verify_vps


VPS_URL = 'https://legacy.zap-hosting.com/en/customer/vserver/show/123/overview/'
LOGIN_FORM = '''
<title>Login</title>
<form method="post" action="/en/customer/home/">
  <input name="username" aria-label="Username / E-Mail">
  <input name="password" type="password" aria-label="Password">
  <button type="submit">Login</button>
  {challenge}
</form>
'''


class LiveVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def setUp_browser(self, challenge='', submitted_page='<title>Dashboard</title>',
                            vps_page='<title>vServer #1 - Overview - ZAP-Hosting</title>'):
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        browser = await playwright.chromium.launch()
        self.addAsyncCleanup(browser.close)
        page = await browser.new_page()
        self.submitted = False
        self.visited_vps = False

        async def route_request(route):
            if route.request.url == LOGIN_URL:
                if isinstance(challenge, dict):
                    await route.fulfill(**challenge)
                    return
                body = LOGIN_FORM.format(challenge=challenge)
            elif route.request.url == DASHBOARD_URL and route.request.method == 'POST':
                self.submitted = True
                body = submitted_page
            elif route.request.url == VPS_URL:
                self.visited_vps = True
                body = vps_page
            else:
                await route.abort()
                return
            await route.fulfill(content_type='text/html', body=body)

        await page.route('**/*', route_request)
        return page

    async def test_visits_target_after_normal_login(self):
        page = await self.setUp_browser()
        await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        self.assertTrue(self.submitted)
        self.assertTrue(self.visited_vps)

    async def test_stops_before_filling_captcha_protected_form(self):
        page = await self.setUp_browser('<div class="g-recaptcha" hidden></div>')
        with self.assertRaisesRegex(LiveVerificationError, '验证码.*登录表单'):
            await verify_vps(page, 'fixture@example.test', 'fixture-password', VPS_URL)
        self.assertEqual(await page.locator('input[name="username"]').input_value(), '')
        self.assertFalse(self.submitted)

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
