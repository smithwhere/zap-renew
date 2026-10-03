import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check_weekly_schedule.py'
guard = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('weekly_schedule', SCRIPT)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)


class WeeklyScheduleTests(unittest.TestCase):
    def run_guard(self, runs=(), *, event_name='schedule', cron='0 0 * * 3',
                  current_created='2026-10-07T00:14:00Z', error=None, second_page=None):
        self.assertIsNotNone(guard, 'Weekly schedule guard is not implemented')
        requests = []
        current = {'id': 900, 'workflow_id': 42, 'head_branch': 'main',
                   'event': 'schedule', 'created_at': current_created}

        def reply(request, timeout):
            requests.append(request)
            if error:
                raise error
            if request.full_url.endswith('/actions/runs/900'):
                data = current
            else:
                query = parse_qs(urlparse(request.full_url).query)
                page_runs = second_page if query.get('page') == ['2'] else runs
                data = {'total_count': len(runs) + len(second_page or ()),
                        'workflow_runs': list(page_runs)}
            return io.BytesIO(json.dumps(data).encode())

        with TemporaryDirectory() as directory:
            event_file = Path(directory) / 'event.json'
            output_file = Path(directory) / 'output'
            event_file.write_text(json.dumps({'schedule': cron}), encoding='utf-8')
            env = {'GITHUB_EVENT_NAME': event_name, 'GITHUB_EVENT_PATH': str(event_file),
                   'GITHUB_REPOSITORY': 'fixture/repo', 'GITHUB_RUN_ID': '900',
                   'GITHUB_REF_NAME': 'main', 'GITHUB_API_URL': 'https://api.github.com',
                   'GH_TOKEN': 'fixture-token', 'GITHUB_OUTPUT': str(output_file)}
            log = io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch('urllib.request.urlopen', side_effect=reply), \
                    contextlib.redirect_stdout(log):
                code = guard.main()
            outputs = dict(line.split('=', 1) for line in output_file.read_text().splitlines())
        return code, outputs, requests, log.getvalue()

    def test_wednesday_runs_when_monday_schedule_is_missing(self):
        code, outputs, requests, _ = self.run_guard()
        self.assertEqual(code, 0)
        self.assertEqual(outputs['should_run'], 'true')
        query = parse_qs(urlparse(requests[-1].full_url).query)
        self.assertEqual(query['created'], ['2026-10-05T00:00:00Z..2026-10-06T23:59:59Z'])
        self.assertEqual(query['event'], ['schedule'])
        self.assertEqual(query['branch'], ['main'])
        self.assertIn('/actions/workflows/42/runs?', requests[-1].full_url)

    def test_monday_and_manual_runs_do_not_query_history(self):
        for event, cron in [('schedule', '0 0 * * 1'), ('workflow_dispatch', '0 0 * * 3')]:
            with self.subTest(event=event):
                code, outputs, requests, _ = self.run_guard(event_name=event, cron=cron)
                self.assertEqual((code, outputs['should_run']), (0, 'true'))
                self.assertEqual(requests, [])

    def test_successful_or_active_monday_schedule_prevents_makeup(self):
        for status, conclusion in [('completed', 'success'), ('queued', None), ('in_progress', None)]:
            with self.subTest(status=status, conclusion=conclusion):
                run = {'id': 800, 'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
                       'created_at': '2026-10-05T00:15:00Z', 'status': status, 'conclusion': conclusion}
                code, outputs, _, _ = self.run_guard([run])
                self.assertEqual((code, outputs['should_run']), (0, 'false'))

    def test_failed_cancelled_or_timed_out_monday_schedule_is_retried(self):
        for conclusion in ['failure', 'cancelled', 'timed_out', 'skipped']:
            with self.subTest(conclusion=conclusion):
                run = {'id': 800, 'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
                       'created_at': '2026-10-05T00:15:00Z', 'status': 'completed', 'conclusion': conclusion}
                code, outputs, _, _ = self.run_guard([run])
                self.assertEqual((code, outputs['should_run']), (0, 'true'))

    def test_a_successful_monday_record_overrides_other_failures(self):
        failed = {'id': 800, 'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
                  'created_at': '2026-10-05T00:15:00Z', 'status': 'completed', 'conclusion': 'failure'}
        success = dict(failed, id=801, conclusion='success')
        code, outputs, _, _ = self.run_guard([failed, success])
        self.assertEqual((code, outputs['should_run']), (0, 'false'))

    def test_unknown_completed_result_is_not_assumed_to_be_failure(self):
        run = {'id': 800, 'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
               'created_at': '2026-10-05T00:15:00Z', 'status': 'completed', 'conclusion': None}
        code, outputs, _, _ = self.run_guard([run])
        self.assertEqual((code, outputs['should_run']), (1, 'false'))

    def test_unrelated_runs_do_not_hide_a_missed_monday(self):
        variants = [
            {'id': 700, 'created_at': '2026-09-28T00:01:00Z'},
            {'id': 701, 'event': 'workflow_dispatch', 'created_at': '2026-10-05T01:00:00Z'},
            {'id': 702, 'created_at': '2026-10-07T00:00:00Z'},
            {'id': 703, 'created_at': '2026-10-04T23:59:59Z'},
            {'id': 704, 'head_branch': 'other', 'created_at': '2026-10-05T00:01:00Z'},
            {'id': 900, 'created_at': '2026-10-07T00:14:00Z'},
        ]
        runs = [dict({'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
                      'status': 'completed', 'conclusion': 'success'}, **v) for v in variants]
        code, outputs, _, _ = self.run_guard(runs)
        self.assertEqual((code, outputs['should_run']), (0, 'true'))

    def test_delayed_monday_trigger_on_tuesday_prevents_makeup(self):
        run = {'id': 800, 'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
               'created_at': '2026-10-06T01:00:00Z', 'status': 'completed', 'conclusion': 'success'}
        code, outputs, _, _ = self.run_guard([run])
        self.assertEqual((code, outputs['should_run']), (0, 'false'))

    def test_original_run_date_is_used_when_rechecking_a_past_week(self):
        run = {'id': 800, 'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
               'created_at': '2024-12-30T00:01:00Z', 'status': 'completed', 'conclusion': 'success'}
        code, outputs, requests, _ = self.run_guard([run], current_created='2025-01-01T00:09:00Z')
        self.assertEqual((code, outputs['should_run']), (0, 'false'))
        query = parse_qs(urlparse(requests[-1].full_url).query)
        self.assertEqual(query['created'], ['2024-12-30T00:00:00Z..2024-12-31T23:59:59Z'])

    def test_history_is_paginated_before_deciding_a_schedule_is_missing(self):
        failed = {'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
                  'created_at': '2026-10-05T01:00:00Z', 'status': 'completed', 'conclusion': 'failure'}
        run = {'id': 800, 'workflow_id': 42, 'event': 'schedule', 'head_branch': 'main',
               'created_at': '2026-10-05T00:00:00Z', 'status': 'completed', 'conclusion': 'success'}
        code, outputs, requests, _ = self.run_guard([dict(failed, id=i) for i in range(100)],
                                                  second_page=[run])
        self.assertEqual((code, outputs['should_run']), (0, 'false'))
        self.assertEqual(parse_qs(urlparse(requests[-1].full_url).query)['page'], ['2'])

    def test_api_failure_does_not_silently_start_a_duplicate_run(self):
        error = HTTPError('https://api.github.com/fixture', 403, 'fixture-private-token', {}, None)
        code, outputs, _, log = self.run_guard(error=error)
        self.assertEqual((code, outputs['should_run']), (1, 'false'))
        self.assertNotIn('fixture-private-token', log)


if __name__ == '__main__':
    unittest.main()
