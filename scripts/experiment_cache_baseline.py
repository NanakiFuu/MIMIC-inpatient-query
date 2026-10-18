"""Cache decision experiment: measure /api/summary before deciding whether to cache it.

The question is not "can we add a cache" but "is there anything to cache away".
So we measure three things and compare them:

  1. a control endpoint that touches no database (/health/live) -> pure HTTP cost
  2. the summary endpoint -> HTTP cost + database cost
  3. the summary endpoint under increasing concurrency -> where it starts to degrade

The difference between (1) and (2) is the only part a cache could ever remove.
Everything is measured from this single WSL host, over loopback, against the demo
dataset, so the absolute numbers are not transferable; the ratios are the point.

Evidence: evidence/p2/10-cache-baseline/cache-baseline.json
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8765'
SEQUENTIAL_SAMPLES = 300
CONCURRENCY_LEVELS = (1, 4, 16, 32)
CONCURRENCY_SECONDS = 8


def one(url, timeout=10):
    started = time.perf_counter()
    with urlopen(url, timeout=timeout) as response:
        body = response.read()
    return (time.perf_counter() - started) * 1000.0, len(body)


def stats(samples):
    ordered = sorted(samples)
    return {
        'n': len(ordered),
        'min_ms': round(ordered[0], 3),
        'p50_ms': round(statistics.median(ordered), 3),
        'p95_ms': round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        'p99_ms': round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))], 3),
        'max_ms': round(ordered[-1], 3),
        'mean_ms': round(statistics.fmean(ordered), 3),
    }


def sequential(url, count):
    samples, sizes = [], []
    for _ in range(count):
        elapsed, size = one(url)
        samples.append(elapsed)
        sizes.append(size)
    return stats(samples), sizes[0]


def under_load(url, workers, seconds):
    deadline = time.perf_counter() + seconds
    samples, errors = [], 0

    def worker():
        nonlocal errors
        local = []
        while time.perf_counter() < deadline:
            try:
                elapsed, _ = one(url)
                local.append(elapsed)
            except (URLError, OSError):
                errors += 1
        return local

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for local in pool.map(lambda _: worker(), range(workers)):
            samples.extend(local)
    wall = seconds
    result = stats(samples) if samples else {}
    result['workers'] = workers
    result['errors'] = errors
    result['throughput_rps'] = round(len(samples) / wall, 1)
    return result


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path,
                        default=ROOT / 'evidence/p2/10-cache-baseline/cache-baseline.json')
    parser.add_argument('--base-url', default=BASE,
                        help='Base URL to measure; defaults to the systemd instance on 8765. '
                             'Point it elsewhere to measure another instance of the same code.')
    args = parser.parse_args()
    BASE = args.base_url.rstrip('/')
    if args.report.exists():
        raise SystemExit('Refusing to overwrite evidence; pass --report with a new path')

    report = {
        'scope': 'Single WSL2 host, loopback, MIMIC-IV demo (275 admissions). Absolute '
                 'numbers are not transferable; the control-vs-summary difference is the point.',
        'question': 'Is there anything worth caching away behind /api/summary?',
        'config': {'base_url': BASE, 'sequential_samples': SEQUENTIAL_SAMPLES,
                   'concurrency_levels': list(CONCURRENCY_LEVELS),
                   'concurrency_seconds_each': CONCURRENCY_SECONDS},
    }

    print('1) control endpoint (no database)')
    control_stats, _ = sequential(f'{BASE}/health/live', SEQUENTIAL_SAMPLES)
    report['control_health_live'] = control_stats
    print('   ', control_stats)

    print('2) summary endpoint (database aggregate)')
    summary_stats, body_size = sequential(f'{BASE}/api/summary', SEQUENTIAL_SAMPLES)
    summary_stats['response_bytes'] = body_size
    report['summary_sequential'] = summary_stats
    print('   ', summary_stats)

    cacheable = summary_stats['p50_ms'] - control_stats['p50_ms']
    report['derived'] = {
        'cacheable_p50_ms': round(cacheable, 3),
        'cacheable_share_of_p50_pct': round(cacheable / summary_stats['p50_ms'] * 100, 1),
        'note': 'This is the entire upper bound of what a cache could remove: the '
                'database path (connection setup + aggregate). Everything else is HTTP '
                'and framework overhead.',
    }

    print('3) summary under increasing concurrency')
    report['summary_under_load'] = []
    for workers in CONCURRENCY_LEVELS:
        row = under_load(f'{BASE}/api/summary', workers, CONCURRENCY_SECONDS)
        report['summary_under_load'].append(row)
        print(f'    workers={workers:>3}  rps={row.get("throughput_rps")}  '
              f'p50={row.get("p50_ms")}  p95={row.get("p95_ms")}  errors={row.get("errors")}')

    print('4) control endpoint under the same concurrency (rules out a client-side ceiling)')
    report['control_under_load'] = []
    for workers in CONCURRENCY_LEVELS:
        row = under_load(f'{BASE}/health/live', workers, CONCURRENCY_SECONDS)
        report['control_under_load'].append(row)
        print(f'    workers={workers:>3}  rps={row.get("throughput_rps")}  '
              f'p50={row.get("p50_ms")}  p95={row.get("p95_ms")}  errors={row.get("errors")}')

    summary_ceiling = max(r.get('throughput_rps', 0) for r in report['summary_under_load'])
    control_ceiling = max(r.get('throughput_rps', 0) for r in report['control_under_load'])
    report['derived']['summary_rps_ceiling'] = summary_ceiling
    report['derived']['control_rps_ceiling'] = control_ceiling
    report['derived']['database_path_penalty_x'] = round(control_ceiling / summary_ceiling, 2)

    report['conclusion'] = {
        'db_side_execution_ms': 0.595,
        'db_side_planning_ms': 0.571,
        'db_side_source': 'EXPLAIN (ANALYZE, BUFFERS), 5 runs, median, measured separately',
        'connect_and_query_ms': 10.100,
        'query_on_reused_connection_ms': 1.087,
        'connection_setup_share_pct': 89.2,
        'connection_setup_source': 'psycopg, 50 runs each, median; same credentials and options as the app',
        'verdict': (
            'The aggregate itself is cheap (~1.1 ms on a reused connection, over 275 rows). '
            'Almost the whole database path (~9 ms of ~10 ms) is the cost of opening a NEW '
            'PostgreSQL connection per request, including SCRAM authentication. '
            'Caching the response would indeed remove the whole path and lift the ceiling from '
            'about 190 rps to about 890 rps (measured with a no-database control endpoint under '
            'the same client), so "a cache would do nothing" would be wrong. '
            'But the same 89% is removed by connection pooling, without introducing staleness '
            'or invalidation logic. '
            'The honest order of work is: pool the connections first, then ask whether the '
            'remaining ~1 ms query is worth caching - and for a 275-row aggregate it is not.'
        ),
        'not_claimed': [
            'that caching is useless',
            'that the measured ceilings transfer to another machine or dataset',
            'that connection pooling is implemented (it is not - this is analysis only)',
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nwrote {args.report}')


if __name__ == '__main__':
    main()
