import unittest
from unittest.mock import Mock, patch

from test_renew import renew


class YesCaptchaTests(unittest.TestCase):
    def test_unsupported_browser_proxy_is_rejected_without_exposing_credentials(self):
        for proxy in ['socks5://user:private-password@proxy.example.test:1080',
                      'ftp://proxy.example.test:8080', 'http://proxy.example.test:bad-port']:
            with self.subTest(proxy=proxy), patch.object(renew.requests, 'post') as post:
                with self.assertRaises(ValueError) as error:
                    renew.browser_proxy(proxy)
                self.assertNotIn('private-password', str(error.exception))
                post.assert_not_called()

    def test_proxy_credentials_are_decoded_for_browser_only(self):
        self.assertEqual(renew.browser_proxy('https://user%40example:p%3Aa%24%24@proxy.example.test:8080'),
                         {'server': 'https://proxy.example.test:8080', 'username': 'user@example', 'password': 'p:a$$'})
        self.assertIsNone(renew.browser_proxy(''))

    def test_api_rejection_never_returns_sensitive_error_description(self):
        response = {'errorId': 1, 'errorCode': 'ERROR_PROXY_CONNECT', 'errorDescription': 'fixture-private-key'}
        with patch.object(renew.requests, 'post', return_value=Mock(json=Mock(return_value=response))):
            with self.assertRaises(RuntimeError) as error:
                renew.YesCaptchaSolver('fixture-key').create_task(
                    'fixture-sitekey', 'https://panel.example.test/')
        self.assertIn('ERROR_PROXY_CONNECT', str(error.exception))
        self.assertNotIn('fixture-private-key', str(error.exception))

    def test_polling_stops_at_deadline(self):
        response = {'errorId': 0, 'status': 'processing'}
        with patch.object(renew.requests, 'post', return_value=Mock(json=Mock(return_value=response))) as post, \
                patch.object(renew.time, 'monotonic', side_effect=[0, 0, 121]), \
                patch.object(renew.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '超时'):
                renew.YesCaptchaSolver('fixture-key').get_result('fixture-task')
        self.assertEqual(post.call_count, 1)

    def test_recaptcha_still_returns_its_response_token(self):
        responses = [{'errorId': 0, 'taskId': 'fixture-task'},
                     {'errorId': 0, 'status': 'ready', 'solution': {'gRecaptchaResponse': 'fixture-token'}}]
        with patch.object(renew.requests, 'post', side_effect=[Mock(json=Mock(return_value=r)) for r in responses]):
            solver = renew.YesCaptchaSolver('fixture-key')
            self.assertEqual(solver.solve('fixture-sitekey', 'https://panel.example.test/'), 'fixture-token')


if __name__ == '__main__':
    unittest.main()
