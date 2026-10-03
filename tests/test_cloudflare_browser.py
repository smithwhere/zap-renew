"""Local browser fixtures exercise CDP mouse targeting without external requests."""
import contextlib
import io
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from test_renew import renew


WIDGET_URL = 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/test'
PANEL_URL = 'https://panel.example.test/'


class CloudflareBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(viewport={'width': 1280, 'height': 900})
        self.page = await self.context.new_page()
        with patch.object(renew, 'SESSION_DIR', Path(self.temp.name)):
            self.keeper = renew.ZapKeepAlive('fixture@example.test', 'unused')
        self.keeper.page = self.page
        self.keeper.solver = None
        self.keeper.cdp = await self.context.new_cdp_session(self.page)

    async def load_fixture(self, *, widget=True, resolve=True):
        title_change = "document.title = 'VPS Overview';" if resolve else ''
        widget_script = f'''
            const shadow = document.querySelector('#widget').attachShadow({{mode: 'closed'}});
            const frame = document.createElement('iframe');
            frame.src = '{WIDGET_URL}';
            frame.style = 'width:300px;height:65px;border:0';
            shadow.append(frame);
        ''' if widget else ''
        main_html = f'''<!doctype html><title>Just a moment...</title>
            <style>body{{margin:0}} .main-wrapper{{position:relative;width:1000px;height:500px}}
            #widget{{position:absolute;left:300px;top:200px;width:300px;height:65px}}
            #unrelated{{position:absolute;left:10px;top:230px;width:100px;height:50px}}</style>
            <div class="main-wrapper">
                <button id="unrelated" onclick="document.body.dataset.unrelated='clicked'">Unrelated</button>
                <div id="widget"></div>
            </div>
            <script>
                document.body.dataset.clicks = '0';
                window.addEventListener('message', event => {{
                    if (event.origin !== 'https://challenges.cloudflare.com' || event.data !== 'checked') return;
                    document.body.dataset.clicks = String(Number(document.body.dataset.clicks) + 1);
                    {title_change}
                }});
                {widget_script}
            </script>'''
        widget_html = '''<!doctype html><style>body{margin:0}
            button{position:absolute;left:10px;top:10px;width:30px;height:45px}</style>
            <button onclick="parent.postMessage('checked','*')">✓</button>'''

        async def route_request(route):
            if route.request.url == PANEL_URL:
                await route.fulfill(body=main_html, content_type='text/html')
            elif route.request.url == WIDGET_URL:
                await route.fulfill(body=widget_html, content_type='text/html')
            else:
                await route.abort()
        await self.context.route('**/*', route_request)
        await self.page.goto(PANEL_URL)

    async def test_closed_shadow_widget_is_clicked_instead_of_outer_wrapper(self):
        await self.load_fixture()
        self.assertTrue(await self.keeper.handle_cloudflare(max_attempts=2))
        self.assertEqual(await self.page.title(), 'VPS Overview')
        self.assertIsNone(await self.page.get_attribute('body', 'data-unrelated'))

    async def test_last_widget_attempt_is_checked_before_timeout(self):
        await self.load_fixture()
        with patch.object(renew, 'PROXY_URL', 'http://proxy.example.test:8080'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(await self.keeper.handle_cloudflare(max_attempts=1))

    async def test_missing_widget_never_clicks_an_unrelated_control(self):
        await self.load_fixture(widget=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(await self.keeper.handle_cloudflare(max_attempts=1))
        self.assertIsNone(await self.page.get_attribute('body', 'data-unrelated'))

    async def test_cloudflare_never_calls_yescaptcha_even_with_key_and_proxy(self):
        await self.load_fixture(widget=False)
        self.keeper.solver = renew.YesCaptchaSolver('fixture-key')
        api_urls = []

        def forbid_api(url, **kwargs):
            api_urls.append(url)
            raise RuntimeError('Cloudflare must not call a paid solver')

        with patch.object(renew, 'PROXY_URL', 'http://proxy.example.test:8080'), \
                patch.object(renew.requests, 'post', side_effect=forbid_api), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(await self.keeper.handle_cloudflare(max_attempts=1))
        self.assertEqual(api_urls, [])

    async def test_same_widget_is_not_clicked_again_while_verifying(self):
        await self.load_fixture(resolve=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(await self.keeper.handle_cloudflare(max_attempts=3))
        self.assertEqual(await self.page.get_attribute('body', 'data-clicks'), '1')

    async def test_run_uses_native_user_agent_and_configured_proxy(self):
        native_user_agent = await self.page.evaluate('navigator.userAgent')
        received_user_agents = []

        async def capture_request(route):
            received_user_agents.append(route.request.headers.get('user-agent'))
            await route.fulfill(body='<title>Just a moment...</title>', content_type='text/html')

        async def new_context(**kwargs):
            context = await self.browser.new_context(**kwargs)
            await context.route('**/*', capture_request)
            return context

        browser_adapter = SimpleNamespace(new_context=new_context, close=self.browser.close)
        playwright = AsyncMock()
        playwright.__aenter__.return_value = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser_adapter))
        )
        with patch.object(renew, 'async_playwright', return_value=playwright), \
                patch.object(renew, 'PROXY_URL', 'http://user:password@proxy.example.test:8080'), \
                patch.object(renew.asyncio, 'sleep', new=AsyncMock()), \
                patch.object(self.keeper, 'handle_cloudflare', new=AsyncMock(return_value=False)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(await self.keeper.run())
        self.assertEqual(received_user_agents, [native_user_agent])
        self.assertEqual(playwright.__aenter__.return_value.chromium.launch.call_args.kwargs['proxy'],
                         {'server': 'http://proxy.example.test:8080', 'username': 'user', 'password': 'password'})



if __name__ == '__main__':
    unittest.main()
