"""Browser regression tests for Classic Panel login."""
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
    async def test_login_failure_reports_http_status_without_logging_page_contents(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            rejected_url = f'{renew.LOGIN_URL}?reference=private-reference'

            async def route_request(route):
                if route.request.method == 'GET' and route.request.url == renew.LOGIN_URL:
                    await route.fulfill(content_type='text/html', body='''
                        <title>Login</title>
                        <form class="inline-login" method="post" action="?reference=private-reference">
                          <input name="username" autocomplete="username">
                          <input name="password" type="password">
                          <button type="submit">Login</button>
                        </form>
                    ''')
                elif route.request.method == 'POST' and route.request.url == rejected_url:
                    await route.fulfill(status=429, content_type='text/html', body='''
                        <title>Login</title>
                        <div class="modal-content" hidden>SOON...</div>
                        <div class="modal">
                          <div class="login-error">Try again later. Private account reference.</div>
                          <button class="close" onclick="this.parentElement.hidden = true">Close</button>
                        </div>
                    ''')
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

            self.assertIn('HTTP 429', output.getvalue())
            self.assertIn('页面显示登录错误', output.getvalue())
            self.assertTrue(await page.locator('.login-error').is_visible())
            self.assertNotIn('SOON...', output.getvalue())
            self.assertNotIn('private-reference', output.getvalue())
            self.assertNotIn('Private account reference', output.getvalue())

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
