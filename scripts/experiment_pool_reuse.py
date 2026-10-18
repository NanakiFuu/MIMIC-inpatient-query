"""Does the pool reuse connections, and does it stay within max_size under load?

Runs the application in-process and reads the pool's own statistics. That avoids
`pg_stat_activity`, which turned out to be unusable here: a non-privileged role
only sees its own session, so it cannot be used to count another process's
connections.

Evidence: evidence/p2/12-connection-pool/pool-reuse.json
"""
import argparse
import json
import statistics
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import uvicorn

from medical_query import database
from medical_query.api import app

HOST = '127.0.0.1'
PORT = 18866
WORKERS = 4
SECONDS = 8
SAMPLE_INTERVAL = 0.05
IDLE_WINDOW = 1.5


def one(url, timeout=10):
    with urlopen(url, timeout=timeout) as response:
        response.read()


def spread(values):
    if not values:
        return None
    return {'samples': len(values), 'min': min(values),
            'median': statistics.median(values), 'max': max(values)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=WORKERS)
    parser.add_argument('--seconds', type=float, default=SECONDS)
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit('Refusing to overwrite evidence; pass --report with a new path')

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level='warning'))
    threading.Thread(target=server.run, daemon=True).start()
    base = f'http://{HOST}:{PORT}'
    for _ in range(100):
        try:
            one(f'{base}/health/live', timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    pool = database.pool()
    pool.wait(timeout=10)
    settings = database.pool_settings()

    # Serial reuse: consecutive borrows must land on the same backend.
    serial_pids = []
    for _ in range(3):
        with database.connect() as conn:
            serial_pids.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])

    samples, errors, stop = [], [], threading.Event()
    started = time.perf_counter()

    def sample():
        while not stop.is_set():
            stats = pool.get_stats()
            samples.append({'at_s': round(time.perf_counter() - started, 3),
                            'pool_size': stats['pool_size'],
                            'available': stats['pool_available'],
                            'waiting': stats['requests_waiting']})
            stop.wait(SAMPLE_INTERVAL)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    time.sleep(IDLE_WINDOW)
    idle_count = len(samples)

    deadline = time.perf_counter() + args.seconds

    def worker():
        while time.perf_counter() < deadline:
            try:
                one(f'{base}/api/summary')
            except Exception:
                errors.append(1)

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()
    sampler.join(timeout=5)
    server.should_exit = True

    sizes = [row['pool_size'] for row in samples]
    waiting = [row['waiting'] for row in samples]
    report = {
        'scope': 'Single WSL2 host, loopback, MIMIC-IV demo. Application runs in this '
                 'process, so the numbers are the pool object\'s own statistics.',
        'why_not_pg_stat_activity': 'A non-privileged role only sees its own session there, '
                                    'so it cannot count another process\'s connections.',
        'pool_settings': settings,
        'load': {'endpoint': '/api/summary', 'workers': args.workers, 'seconds': args.seconds},
        'serial_reuse': {'borrows': len(serial_pids), 'backend_pids': serial_pids,
                         'reused_one_backend': len(set(serial_pids)) == 1},
        'idle_window': spread(sizes[:idle_count]),
        'under_load': spread(sizes[idle_count:]),
        'requests_waiting': spread(waiting),
        'peak_within_pool_max': (max(sizes) <= settings['max_size']) if sizes else None,
        'request_errors': len(errors),
        'timeline': samples,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'timeline'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
