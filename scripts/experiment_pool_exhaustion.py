"""Pool exhaustion at the HTTP layer: bounded 503, then recovery.

Runs the application in-process with a one-connection pool, holds that
connection, and checks that requests fail with 503 database_pool_timeout inside
the configured acquisition timeout instead of hanging. Then it releases the
connection and checks that requests succeed again. The database stays up the
whole time, so this is pool exhaustion, not a database outage.

Evidence: evidence/p2/12-connection-pool/pool-exhaustion.json
"""
import argparse
import json
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

# Must be set before the pool is first built.
os.environ['MEDICAL_QUERY_POOL_MIN_SIZE'] = '1'
os.environ['MEDICAL_QUERY_POOL_MAX_SIZE'] = '1'
os.environ['MEDICAL_QUERY_POOL_TIMEOUT'] = '0.5'

import uvicorn  # noqa: E402

from medical_query import database  # noqa: E402
from medical_query.api import app  # noqa: E402

HOST = '127.0.0.1'
PORT = 18867


def call(path, timeout=10):
    started = time.perf_counter()
    try:
        with urlopen(f'http://{HOST}:{PORT}{path}', timeout=timeout) as response:
            status, body = response.status, json.load(response)
    except HTTPError as error:
        status, body = error.code, json.loads(error.read())
    except Exception as error:  # noqa: BLE001
        status, body = None, {'transport_error': type(error).__name__}
    return {'path': path, 'status': status, 'body': body,
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 1)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit('Refusing to overwrite evidence; pass --report with a new path')

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level='warning'))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        probe = call('/health/live', timeout=1)
        if probe['status'] == 200:
            break
        time.sleep(0.1)

    settings = database.pool_settings()
    database.pool().wait(timeout=10)
    baseline = call('/api/summary')

    # Hold the only pooled connection, so every database-backed request has to wait.
    with database.connect() as held:
        held.execute('SELECT 1')
        exhausted = [call('/api/summary'), call('/health/ready'),
                     call('/health/live'), call('/ops/snapshot')]

    recovered = call('/api/summary')
    server.should_exit = True

    report = {
        'scope': 'Single WSL2 host, loopback, MIMIC-IV demo. The database stays up for the '
                 'whole run, so this measures pool exhaustion and not a database outage.',
        'pool_settings': settings,
        'baseline_before_holding': baseline,
        'while_pool_exhausted': exhausted,
        'after_release': recovered,
        'database_backed_requests_bounded':
            all(row['status'] == 503 for row in exhausted[:2]),
        'non_database_requests_unaffected':
            all(row['status'] == 200 for row in exhausted[2:]),
        'error_code_reported': [row['body'].get('error') for row in exhausted[:2]],
        'recovered_after_release': recovered['status'] == 200,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
