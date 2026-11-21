"""Login must proceed when optional resources keep the load event pending."""
import asyncio
import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from test_renew import renew


class LoginBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_422_json_error_is_categorized_without_exposing_response(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()

            async def route_request(route):
                if route.request.method == 'POST':
                    await route.fulfill(status=422, content_type='application/json', body='''
                        {"error": {"code": "CAPTCHA_INVALID", "message": "Captcha rejected: private-token"},
                         "password": "fixture-password", "email": "fixture@example.test"}
                    ''')
                else:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title><form class="inline-login">
                          <input name="username" autocomplete="username"><input type="password">
                          <button type="submit">Login</button>
                        </form><script>document.querySelector('form').onsubmit = event => {
                          event.preventDefault();
                          fetch('/interface/login/json_login.php', {method: 'POST'});
                        };</script>
                    ''')
            await page.route('**/*', route_request)
            output = io.StringIO()
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = None
                self.assertFalse(await keeper.login())
            log = output.getvalue()
            self.assertIn('登录提交 POST：1 次；响应：HTTP 422', log)
            self.assertIn('HTTP 422 响应类别：验证码相关', log)
            for private in ('private-token', 'fixture-password', 'fixture@example.test', 'Captcha rejected'):
                self.assertNotIn(private, log)

    async def test_422_unknown_response_never_prints_untrusted_text(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()

            async def route_request(route):
                if route.request.method == 'POST':
                    await route.fulfill(status=422, content_type='application/json',
                                        body='{"error":{"code":"PRIVATE-123","message":"private response"}}')
                else:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title><form class="inline-login">
                          <input name="username" autocomplete="username"><input type="password">
                          <button type="submit">Login</button>
                        </form><script>document.querySelector('form').onsubmit = event => {
                          event.preventDefault();
                          fetch('/interface/login/json_login.php', {method: 'POST'});
                        };</script>
                    ''')
            await page.route('**/*', route_request)
            output = io.StringIO()
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = None
                self.assertFalse(await keeper.login())
            self.assertIn('HTTP 422 响应类别：未识别（原文已隐藏）', output.getvalue())
            self.assertNotIn('PRIVATE-123', output.getvalue())
            self.assertNotIn('private response', output.getvalue())

    async def test_failed_ajax_login_reports_safe_status_and_visible_errors(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()

            async def route_request(route):
                if route.request.url == renew.LOGIN_URL:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title>
                        <form class="inline-login" action="/interface/login/" method="post">
                          <input name="username" autocomplete="username">
                          <input name="password" type="password">
                          <button type="submit">Login</button>
                        </form>
                        <div class="modal-content" hidden>SOON...</div>
                        <div class="alert-danger" hidden>Wrong password hidden</div>
                        <div class="login-error" role="alert"></div>
                        <script>
                        document.querySelector('form').onsubmit = async event => {
                          event.preventDefault();
                          await fetch('/metrics', {method: 'POST'});
                          await fetch('/interface/login/json_login.php?secret=private-token', {method: 'POST'});
                          document.querySelector('.login-error').textContent =
                            'Invalid credentials for fixture@example.test fixture-password private-token';
                        };
                        </script>
                    ''')
                elif '/json_login.php?' in route.request.url:
                    await route.fulfill(status=401, body='private response')
                elif route.request.url.endswith('/metrics'):
                    await route.fulfill(status=204)
                else:
                    await route.abort()

            await page.route('**/*', route_request)
            output = io.StringIO()
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = None
                self.assertFalse(await keeper.login())

            log = output.getvalue()
            self.assertIn('登录提交 POST：1 次；响应：HTTP 401', log)
            self.assertIn('其他同源 POST：1 次；响应：HTTP 204', log)
            self.assertIn('Invalid credentials', log)
            for private in ('fixture@example.test', 'fixture-password', 'private-token',
                            'private response', 'SOON...', 'Wrong password hidden'):
                self.assertNotIn(private, log)

    async def test_client_side_rejection_reports_no_post_and_unknown_error(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <title>Login</title><form class="inline-login" onsubmit="return false">
                  <input name="username" autocomplete="username"><input type="password">
                  <button type="submit">Login</button>
                </form><div class="alert-danger">Secret reference 123</div>
            '''))
            output = io.StringIO()
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = None
                self.assertFalse(await keeper.login())
            self.assertIn('登录提交 POST：0 次；响应：无', output.getvalue())
            self.assertIn('其他可见错误（原文已隐藏）', output.getvalue())
            self.assertNotIn('Secret reference 123', output.getvalue())

    async def test_failed_login_request_reports_no_http_response(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()

            async def route_request(route):
                if route.request.method == 'POST':
                    await route.abort()
                else:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title><form class="inline-login">
                          <input name="username" autocomplete="username"><input type="password">
                          <button type="submit">Login</button>
                        </form><script>document.querySelector('form').onsubmit = event => {
                          event.preventDefault();
                          fetch('/interface/login/json_login.php', {method: 'POST'}).catch(() => {});
                        };</script>
                    ''')
            await page.route('**/*', route_request)
            output = io.StringIO()
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = None
                self.assertFalse(await keeper.login())
            self.assertIn('登录提交 POST：1 次；响应：请求失败（无 HTTP 响应）', output.getvalue())

    async def test_login_proceeds_while_optional_image_is_still_loading(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            page.set_default_navigation_timeout(1500)
            image_requested = asyncio.Event()
            release_image = asyncio.Event()
            image_finished = asyncio.Event()
            submitted = []

            async def route_request(route):
                if route.request.url.endswith('/slow-image.png'):
                    image_requested.set()
                    await release_image.wait()
                    try:
                        await route.fulfill(status=204)
                    finally:
                        image_finished.set()
                elif route.request.url == renew.LOGIN_URL:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title>
                        <form class="inline-login" action="/en/customer/home/" method="post">
                          <input name="username" autocomplete="username">
                          <input name="password" type="password">
                          <button type="submit">Login</button>
                        </form>
                        <img src="/slow-image.png">
                    ''')
                elif route.request.url == renew.DASHBOARD_URL:
                    submitted.append(route.request.post_data)
                    await route.fulfill(content_type='text/html', body='<title>Dashboard</title>')
                else:
                    await route.abort()

            await page.route('**/*', route_request)
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(io.StringIO()):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = None
                try:
                    self.assertTrue(await keeper.login())
                    self.assertTrue(image_requested.is_set())
                    self.assertFalse(release_image.is_set())
                    self.assertEqual(page.url, renew.DASHBOARD_URL)
                    self.assertEqual(submitted, ['username=fixture%40example.test&password=fixture-password'])
                finally:
                    release_image.set()
                    if image_requested.is_set():
                        await asyncio.wait_for(image_finished.wait(), timeout=2)


if __name__ == '__main__':
    unittest.main()


