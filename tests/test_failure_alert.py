"""Exercise the failure-alert workflow's embedded mail program without sending mail."""
import contextlib
import io
import os
from pathlib import Path
import textwrap
import unittest
from unittest.mock import MagicMock, patch


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/renew-failure-notify.yml'


class FailureAlertTests(unittest.TestCase):
    def run_mail_program(self, variables, smtp):
        self.assertTrue(WORKFLOW.exists(), 'failure notification workflow is missing')
        source = WORKFLOW.read_text(encoding='utf-8')
        start = source.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n")
        end = source.index('\n          PY', start)
        code = textwrap.dedent(source[start:end])
        output = io.StringIO()
        self.output = output
        with patch.dict(os.environ, variables, clear=True), \
                patch('smtplib.SMTP_SSL', return_value=smtp) as connect, \
                patch('ssl.create_default_context', return_value=object()), \
                contextlib.redirect_stdout(output):
            exec(compile(code, str(WORKFLOW), 'exec'), {'__name__': '__main__'})
        return connect, output.getvalue()

    def test_sends_safe_failure_link_to_configured_recipient(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.send_message.return_value = {}
        variables = {'SMTP_USERNAME': 'sender@example.test', 'SMTP_PASSWORD': 'private-password',
                     'SMTP_TO': 'recipient@example.test', 'SOURCE_RUN_ID': '12345',
                     'GITHUB_REPOSITORY': 'owner/repository', 'GITHUB_SERVER_URL': 'https://github.com'}
        connect, output = self.run_mail_program(variables, smtp)
        connect.assert_called_once()
        smtp.login.assert_called_once_with('sender@example.test', 'private-password')
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'recipient@example.test')
        self.assertIn('https://github.com/owner/repository/actions/runs/12345', message.get_content())
        self.assertNotIn('private-password', message.as_string())
        self.assertNotIn('private-password', output)

    def test_rejected_recipient_fails_without_exposing_addresses(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.send_message.return_value = {'recipient@example.test': (550, b'private error')}
        variables = {'SMTP_USERNAME': 'sender@example.test', 'SMTP_PASSWORD': 'private-password',
                     'SMTP_TO': 'recipient@example.test', 'SOURCE_RUN_ID': '12345',
                     'GITHUB_REPOSITORY': 'owner/repository', 'GITHUB_SERVER_URL': 'https://github.com'}
        with self.assertRaises(SystemExit):
            self.run_mail_program(variables, smtp)
        self.assertEqual(smtp.send_message.call_count, 1)
        self.assertNotIn('recipient@example.test', self.output.getvalue())
        self.assertNotIn('private-password', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()

