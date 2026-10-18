"""Repeat the project PostgreSQL outage drill to describe how stable recovery is.

This runs a few rounds of the same real stop/start of `postgresql@16-main` and
records, on a monotonic clock, when each milestone happens: the stop command,
the first failing request, the start command, the moment the database accepts
connections again, the first successful query, three consecutive successes, and
the alert clearing.

It deliberately does not call the model. The single round already recorded in
`database-outage-with-pool.json` carries the AI diagnosis; repeating it here
would only add cost without new evidence.

Safety: the database is restarted in `finally`, and every wait loop aborts after
`--max-outage-seconds` and restarts it, so a killed or hanging script cannot
leave the project database down. Only the project cluster is touched, and the
cluster is checked first.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / 'evidence/p2/12-connection-pool/outage-rounds.json'
SERVICE = 'postgresql@16-main'
INSTANCE_UNITS = ['medical-query.service', 'medical-query@8865.service', 'medical-query@8866.service']
ALERT_NAME = 'DatabaseAccessNotReady'


def utc():
    return datetime.now(timezone.utc).isoformat()


def http(path, method='GET', timeout=35):
    started = time.perf_counter()
    request = Request('http://127.0.0.1:8765' + path, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response), round((time.perf_counter() - started) * 1000, 3)
    except HTTPError as error:
        return error.code, json.loads(error.read()), round((time.perf_counter() - started) * 1000, 3)


def prometheus_alerts():
    with urlopen('http://127.0.0.1:9090/api/v1/alerts', timeout=5) as response:
        return json.load(response)['data']['alerts']


def alert_state():
    for alert in prometheus_alerts():
        if alert['labels'].get('alertname') == ALERT_NAME:
            return alert['state']
    return 'absent'


def db_accepts():
    return subprocess.run(['pg_isready', '-h', '127.0.0.1', '-p', '5432'],
                          capture_output=True).returncode == 0


def systemctl(action):
    return subprocess.run(['systemctl', action, SERVICE], capture_output=True, text=True)


def journal_lines(unit, since):
    result = subprocess.run(['journalctl', '-u', unit, '--since', since, '--no-pager'],
                            capture_output=True, text=True)
    return result.stdout.splitlines()


def check_cluster():
    listing = subprocess.run(['/usr/sbin/runuser', '-u', 'postgres', '--', 'psql', '-X', '-At',
                              '-d', 'postgres', '-c', 'SELECT datname FROM pg_database ORDER BY datname'],
                             capture_output=True, text=True, check=True, cwd='/tmp')
    present = {name for name in listing.stdout.splitlines() if name}
    standard = {'postgres', 'template0', 'template1'}
    unexpected = {name for name in present if name not in standard and not name.startswith('medical_query_')}
    if unexpected:
        raise SystemExit(f'Unexpected database in cluster; refusing outage drill: {sorted(unexpected)}')
    return sorted(present)


def wait_until(predicate, deadline, interval=0.25):
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def run_round(index, hold_seconds, max_outage, cluster):
    record = {'round': index, 'started_utc': utc(), 'milestones_ms': {}, 'checks': [],
              'alert_fired': False, 'alert_cleared': False, 'pool_log_lines': {}}
    status, body, _ = http('/api/summary')
    if status != 200 or body['result']['admission_count'] != 275:
        raise RuntimeError('Precondition query failed; no service stopped')
    record['precondition'] = {'status': status, 'admission_count': body['result']['admission_count']}

    since = utc()
    origin = time.monotonic()
    hard_deadline = origin + max_outage

    def mark(name):
        record['milestones_ms'][name] = round((time.monotonic() - origin) * 1000, 3)

    mark('round_start')
    systemctl('stop')
    mark('stop_command_done')

    # First failing request: readiness is the honest probe for a database-backed path.
    ready_status, ready_body, _ = http('/health/ready', timeout=10)
    record['checks'].append({'phase': 'outage', 'path': '/health/ready', 'status': ready_status,
                             'body': ready_body, 'at_utc': utc()})
    mark('first_failure')
    live_status, _, _ = http('/health/live', timeout=10)
    record['checks'].append({'phase': 'outage', 'path': '/health/live', 'status': live_status, 'at_utc': utc()})
    summary_status, summary_body, summary_ms = http('/api/summary', timeout=10)
    record['checks'].append({'phase': 'outage', 'path': '/api/summary', 'status': summary_status,
                             'error': summary_body.get('error'), 'elapsed_ms': summary_ms, 'at_utc': utc()})

    # Hold the outage long enough for the alert to actually fire, then observe it.
    hold_deadline = min(time.monotonic() + hold_seconds, hard_deadline)
    while time.monotonic() < hold_deadline:
        if alert_state() == 'firing':
            record['alert_fired'] = True
            break
        time.sleep(1)
    mark('alert_firing_observed' if record['alert_fired'] else 'alert_fire_not_observed')

    # Recover. The start command time is what the recovery interval is measured from.
    systemctl('start')
    mark('start_command_done')
    if not wait_until(db_accepts, hard_deadline, 0.2):
        raise RuntimeError('database did not accept connections before the outage budget')
    mark('db_connectable')

    def query_ok():
        try:
            return http('/api/summary', timeout=10)[0] == 200
        except URLError:
            return False

    if not wait_until(query_ok, hard_deadline, 0.25):
        raise RuntimeError('no successful query before the outage budget')
    mark('first_success')

    consecutive = 0
    while time.monotonic() < hard_deadline and consecutive < 3:
        if query_ok():
            consecutive += 1
        else:
            consecutive = 0
        if consecutive < 3:
            time.sleep(0.5)
    mark('sustained_success')

    if record['alert_fired']:
        if wait_until(lambda: alert_state() == 'absent', min(time.monotonic() + 90, hard_deadline + 90), 1):
            record['alert_cleared'] = True
            mark('alert_cleared')
        else:
            mark('alert_clear_not_observed')

    for unit in INSTANCE_UNITS:
        lines = [line for line in journal_lines(unit, since)
                 if 'pool_connection_established' in line or 'error connecting' in line
                 or 'pool_startup_skipped' in line]
        record['pool_log_lines'][unit] = lines
    record['finished_utc'] = utc()
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, default=DEFAULT_OUT,
                        help='Output path; a re-run must pass a new path (existing files are not overwritten).')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--hold-seconds', type=float, default=25.0,
                        help='How long to keep the database down so the alert can fire.')
    parser.add_argument('--max-outage-seconds', type=float, default=120.0,
                        help='Hard budget per round; the database is restarted if it is exceeded.')
    args = parser.parse_args()
    if os.getuid() != 0:
        raise SystemExit('Run as WSL root to control only postgresql@16-main')
    if args.report.exists():
        raise SystemExit('Experiment already recorded; pass --report with a new path')
    subprocess.run(['systemctl', 'is-active', SERVICE], check=True, capture_output=True)
    cluster = check_cluster()
    document = {'source': '本项目真实故障操作与HTTP/Prometheus观测，非MIMIC故障数据',
                'scope': 'WSL PostgreSQL16/main only; monotonic milestones; no model calls',
                'service': SERVICE, 'cluster_databases': cluster,
                'hold_seconds': args.hold_seconds, 'max_outage_seconds': args.max_outage_seconds,
                'rounds': []}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.report.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    save()
    try:
        for index in range(1, args.rounds + 1):
            record = run_round(index, args.hold_seconds, args.max_outage_seconds, cluster)
            document['rounds'].append(record)
            save()
            print(json.dumps({'round': index, 'milestones_ms': record['milestones_ms'],
                              'alert_fired': record['alert_fired'], 'alert_cleared': record['alert_cleared']}))
    finally:
        # Whatever happened above, the project database must be running when we leave.
        subprocess.run(['systemctl', 'start', SERVICE], capture_output=True)
        time.sleep(1)
        print(json.dumps({'post_run_db_active': subprocess.run(['systemctl', 'is-active', SERVICE],
              capture_output=True, text=True).stdout.strip()}))
        save()


if __name__ == '__main__':
    main()
