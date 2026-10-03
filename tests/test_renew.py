import contextlib
import importlib.util
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError


spec = importlib.util.spec_from_file_location(
    'zap_renew', Path(__file__).resolve().parents[1] / 'zap-renew.py'
)
renew = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renew)


class RenewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session_patch = patch.object(renew, 'SESSION_DIR', Path(self.temp.name))
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.sleep_patch = patch.object(renew.asyncio, 'sleep', new=AsyncMock())
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.keeper = renew.ZapKeepAlive('test@example.com', 'test-password')
        self.page = SimpleNamespace(
            url=renew.BASE_URL + '/en/customer/vserver/show/123/overview/',
            wait_for_load_state=AsyncMock(),
            title=AsyncMock(return_value='VPS Overview'),
            query_selector=AsyncMock(return_value=None),
            goto=AsyncMock(),
            reload=AsyncMock(),
        )
        self.keeper.page = self.page

    async def test_challenge_at_customer_url_never_counts_as_logged_in(self):
        self.page.url = renew.DASHBOARD_URL
        self.page.title.return_value = 'Just a moment...'
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=self.page),
            new_cdp_session=AsyncMock(),
            cookies=AsyncMock(return_value=[]),
        )
        browser = SimpleNamespace(
            new_context=AsyncMock(return_value=context), close=AsyncMock()
        )
        playwright = AsyncMock()
        playwright.__aenter__.return_value = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
        )
        output = io.StringIO()
        with patch.object(renew, 'async_playwright', return_value=playwright), \
                patch.object(self.keeper, 'visit_vps_detail', new=AsyncMock(return_value=True)), \
                patch.object(self.keeper, 'stay_and_refresh', new=AsyncMock(return_value=True)), \
                contextlib.redirect_stdout(output):
            result = await self.keeper.run()
        self.assertFalse(result)
        self.assertNotIn('会话有效，已登录', output.getvalue())
        self.assertFalse(self.keeper.session_file.exists())
        self.assertIn('Cloudflare', output.getvalue())

    async def test_empty_title_during_navigation_is_not_verification_success(self):
        self.page.title.return_value = ''
        self.assertFalse(await self.keeper.handle_cloudflare(max_attempts=1))

    async def test_challenge_timeout_explains_failure_without_query_tokens(self):
        self.page.title.return_value = 'Just a moment...'
        self.page.url += '?token=private-token'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = await self.keeper.handle_cloudflare(max_attempts=1)
        self.assertFalse(result)
        self.assertIn('Cloudflare', output.getvalue())
        self.assertIn('Just a moment', output.getvalue())
        self.assertNotIn('private-token', output.getvalue())

    async def test_challenge_can_complete_during_polling(self):
        self.page.title.side_effect = ['Just a moment...', 'VPS Overview']
        self.assertTrue(await self.keeper.handle_cloudflare(max_attempts=2))

    async def test_page_loading_error_is_reported(self):
        self.page.wait_for_load_state.side_effect = PlaywrightTimeoutError('Not loaded')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = await self.keeper.handle_cloudflare(max_attempts=1)
        self.assertFalse(result)
        self.assertIn('TimeoutError', output.getvalue())

    async def test_vps_discovery_stops_when_verification_fails(self):
        self.page.evaluate = AsyncMock(return_value=[{'href': self.page.url}])
        with patch.object(renew, 'VPS_URL', ''), \
                patch.object(self.keeper, 'close_modals', new=AsyncMock()), \
                patch.object(self.keeper, 'handle_cloudflare',
                             new=AsyncMock(side_effect=[True, False, True])):
            self.assertFalse(await self.keeper.visit_vps_detail())

    async def test_vps_detail_does_not_accept_a_challenge_at_target_url(self):
        self.page.evaluate = AsyncMock(return_value=[{'href': self.page.url}])
        with patch.object(renew, 'VPS_URL', ''), \
                patch.object(self.keeper, 'close_modals', new=AsyncMock()), \
                patch.object(self.keeper, 'handle_cloudflare',
                             new=AsyncMock(side_effect=[True, True, False])):
            self.assertFalse(await self.keeper.visit_vps_detail())

    async def test_refresh_does_not_wait_for_slow_third_party_load_event(self):
        async def reload(**kwargs):
            if kwargs.get('wait_until') != 'domcontentloaded':
                raise PlaywrightTimeoutError('Slow third-party resource')
        self.page.reload.side_effect = reload
        with patch.object(renew, 'STAY_DURATION', 0), patch.object(renew, 'VPS_URL', ''):
            self.assertTrue(await self.keeper.stay_and_refresh())

    async def test_refresh_redirect_to_login_is_failure(self):
        self.page.url = renew.LOGIN_URL
        self.page.title.return_value = 'Login'
        with patch.object(renew, 'STAY_DURATION', 0):
            self.assertFalse(await self.keeper.stay_and_refresh())


if __name__ == '__main__':
    unittest.main()
