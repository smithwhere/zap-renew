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
    async def test_recaptcha_form_selects_recaptcha_and_uses_its_site_key(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            submitted = []

            async def route_request(route):
                if route.request.method == 'POST':
                    submitted.append(route.request.post_data)
                    await route.fulfill(content_type='text/html', body='<title>Dashboard</title>')
                else:
                    await route.fulfill(content_type='text/html', body='''
                      <title>Login</title><form class="inline-login" method="post" action="/en/customer/home/">
                        <input name="username" autocomplete="username"><input type="password" name="password">
                        <div class="g-recaptcha" data-sitekey="fixture-recaptcha-sitekey"></div>
                        <textarea name="g-recaptcha-response" hidden></textarea>
                        <button type="submit">Login</button>
                      </form>
                    ''')
            await page.route('**/*', route_request)
            output = io.StringIO()
            with TemporaryDirectory() as temp, patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = unittest.mock.Mock()
                keeper.solver.solve.return_value = 'private-recaptcha-token'
                self.assertTrue(await keeper.login())
                keeper.solver.solve.assert_called_once_with('fixture-recaptcha-sitekey', renew.LOGIN_URL)
                keeper.solver.solve_turnstile.assert_not_called()
            self.assertIn('g-recaptcha-response=private-recaptcha-token', submitted[0])
            self.assertNotIn('private-recaptcha-token', output.getvalue())
            self.assertIn('reCAPTCHA', output.getvalue())

    async def test_login_without_captcha_does_not_call_a_solver(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()

            async def route_request(route):
                if route.request.method == 'POST':
                    await route.fulfill(content_type='text/html', body='<title>Dashboard</title>')
                else:
                    await route.fulfill(content_type='text/html', body='''
                      <title>Login</title><form class="inline-login" method="post" action="/en/customer/home/">
                        <input name="username" autocomplete="username"><input type="password" name="password">
                        <button type="submit">Login</button>
                      </form><div class="g-recaptcha" data-sitekey="unrelated"></div>
                    ''')
            await page.route('**/*', route_request)
            with TemporaryDirectory() as temp, patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), contextlib.redirect_stdout(io.StringIO()):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = unittest.mock.Mock()
                self.assertTrue(await keeper.login())
                keeper.solver.solve.assert_not_called()
                keeper.solver.solve_turnstile.assert_not_called()

    async def test_yescaptcha_token_reaches_turnstile_component_submission(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            submitted = []

            async def route_request(route):
                if route.request.method == 'POST':
                    submitted.append(route.request.post_data)
                    await route.fulfill(content_type='application/json', body='{}')
                else:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title><form class="inline-login">
                          <input name="username" autocomplete="username"><input type="password">
                          <div class="g-recaptcha" data-sitekey="unused-recaptcha"></div>
                          <div class="inline-login-turnstile"><input type="hidden"
                            id="cf-chl-widget-login1_response" name="cf-turnstile-response"></div>
                          <button type="submit">Login</button>
                        </form><script>
                        const form = document.querySelector('form');
                        form.__vue__ = {turnstileWidgetId: 'login1', turnstileSitekey: '0xfixture'};
                        window.turnstile = {getResponse: id => id === 'other' ? 'other-token' : ''};
                        form.onsubmit = event => {
                          event.preventDefault();
                          const token = window.turnstile.getResponse(form.__vue__.turnstileWidgetId);
                          const other = window.turnstile.getResponse('other');
                          fetch('/interface/customer/_ajax/json_login.php', {
                            method:'POST', body: new URLSearchParams({token, other})
                          });
                        };
                        </script>
                    ''')
            await page.route('**/*', route_request)
            output = io.StringIO()
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = unittest.mock.Mock()
                keeper.solver.solve_turnstile.return_value = 'private-fixture-token'
                self.assertFalse(await keeper.login())
                keeper.solver.solve_turnstile.assert_called_once_with('0xfixture', renew.LOGIN_URL)
                keeper.solver.solve.assert_not_called()
            self.assertEqual(submitted, ['token=private-fixture-token&other=other-token'])
            self.assertNotIn('private-fixture-token', output.getvalue())

    async def test_turnstile_login_uses_native_token_not_recaptcha_solver(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            submitted = []

            async def route_request(route):
                if route.request.method == 'POST':
                    submitted.append(route.request.post_data)
                    await route.fulfill(content_type='text/html', body='<title>Dashboard</title>')
                else:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title><form class="inline-login" method="post" action="/en/customer/home/">
                          <input name="username" autocomplete="username"><input name="password" type="password">
                          <div class="inline-login-turnstile"><input type="hidden"
                            name="cf-turnstile-response" value="native-test-token"></div>
                          <button type="submit">Login</button>
                        </form>
                    ''')
            await page.route('**/*', route_request)
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(io.StringIO()):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = unittest.mock.Mock()
                self.assertTrue(await keeper.login())
                keeper.solver.solve.assert_not_called()
            self.assertEqual(len(submitted), 1)
            self.assertIn('cf-turnstile-response=native-test-token', submitted[0])

    async def test_turnstile_login_does_not_submit_without_native_token(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            submitted = []

            async def route_request(route):
                if route.request.method == 'POST':
                    submitted.append(True)
                await route.fulfill(content_type='text/html', body='''
                    <title>Login</title><form class="inline-login" method="post">
                      <input name="username" autocomplete="username"><input type="password">
                      <div class="inline-login-turnstile"><input type="hidden" name="cf-turnstile-response"></div>
                      <button type="submit">Login</button>
                    </form>
                ''')
            await page.route('**/*', route_request)
            output = io.StringIO()
            with TemporaryDirectory() as temp, \
                    patch.object(renew, 'SESSION_DIR', Path(temp)), \
                    patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                    contextlib.redirect_stdout(output):
                keeper = renew.ZapKeepAlive('fixture@example.test', 'fixture-password')
                keeper.page = page
                keeper.solver = unittest.mock.Mock()
                self.assertFalse(await keeper.login())
                keeper.solver.solve.assert_not_called()
            self.assertFalse(submitted)
            self.assertIn('Turnstile', output.getvalue())

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





