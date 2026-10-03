"""Wednesday guard: make up a missing or unsuccessful Monday scheduled run."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlencode


def fetch_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'zap-renew-schedule-check',
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError('GitHub API response must be an object')
    return data


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def check_schedule() -> tuple[bool, str]:
    if os.environ['GITHUB_EVENT_NAME'] != 'schedule':
        return True, 'manual_run'
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    if event.get('schedule') != '0 0 * * 3':
        return True, 'regular_schedule'

    token = os.environ['GH_TOKEN']
    if not token:
        raise ValueError('Missing workflow token')
    base = f"{os.environ['GITHUB_API_URL'].rstrip('/')}/repos/{os.environ['GITHUB_REPOSITORY']}"
    run_id = int(os.environ['GITHUB_RUN_ID'])
    branch = os.environ['GITHUB_REF_NAME']
    current = fetch_json(f'{base}/actions/runs/{run_id}', token)
    workflow_id = int(current['workflow_id'])
    # Use the original run's creation date, so a later re-run checks the original week.
    created = parse_time(current['created_at'])
    monday = (created - timedelta(days=created.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    wednesday = monday + timedelta(days=2)
    # UTC Monday 00:00 == Beijing Monday 08:00. Include delayed Monday triggers
    # through Tuesday, while excluding this Wednesday check and previous weeks.
    date_range = f'{monday:%Y-%m-%dT%H:%M:%SZ}..{wednesday - timedelta(seconds=1):%Y-%m-%dT%H:%M:%SZ}'
    failed_run_exists = False
    for page in range(1, 11):  # GitHub limits filtered searches to 1,000 results.
        query = urlencode({'event': 'schedule', 'branch': branch, 'created': date_range,
                           'per_page': 100, 'page': page})
        data = fetch_json(f'{base}/actions/workflows/{workflow_id}/runs?{query}', token)
        runs = data.get('workflow_runs')
        if not isinstance(runs, list):
            raise ValueError('GitHub API did not return workflow_runs')
        for run in runs:
            if (run['id'] != run_id and run['event'] == 'schedule'
                    and run['head_branch'] == branch
                    and monday <= parse_time(run['created_at']) < wednesday):
                if run['status'] != 'completed':
                    print(f"周一定时任务 {run['id']} 仍在排队或运行，跳过周三补跑。")
                    return False, 'monday_schedule_active'
                if run['conclusion'] == 'success':
                    print(f"周一定时任务 {run['id']} 已成功，跳过周三补跑。")
                    return False, 'monday_schedule_succeeded'
                if run['conclusion'] is None:
                    raise ValueError('Completed workflow has no conclusion')
                failed_run_exists = True
        if len(runs) < 100:
            if failed_run_exists:
                print('本周周一定时任务未成功，执行周三补跑。')
                return True, 'monday_schedule_failed'
            print('未发现本周周一定时运行记录，执行周三补跑。')
            return True, 'monday_schedule_missing'
    # Do not interpret incomplete history as an absent run.
    raise RuntimeError('GitHub workflow history exceeded the search limit')


def main() -> int:
    try:
        should_run, reason = check_schedule()
        code = 0
    except Exception as error:
        detail = f'HTTP {error.code}' if isinstance(error, HTTPError) else type(error).__name__
        print(f'::error::补跑检查失败 ({detail})，不执行保活；请检查 Actions 运行记录。')
        should_run, reason, code = False, 'check_failed', 1
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'should_run={str(should_run).lower()}\nreason={reason}\n')
    if should_run and reason in ('manual_run', 'regular_schedule'):
        print('周一定时任务或手动运行，执行保活。')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
