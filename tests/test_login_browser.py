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
    async def test_login_proceeds_while_optional_image_is_still_loading(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            self.addAsyncCleanup(browser.close)
            page = await browser.new_page()
            page.set_default_navigation_timeout(1500)
            image_requested = asyncio.Event()
            release_image = asyncio.Event()
            submitted = []

            async def route_request(route):
                if route.request.url.endswith('/slow-image.png'):
                    image_requested.set()
                    await release_image.wait()
                    await route.fulfill(status=204)
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


if __name__ == '__main__':
    unittest.main()
