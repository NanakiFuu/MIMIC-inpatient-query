"""Nginx dual-instance failover demonstration (single WSL2 host).

Topology (all on ONE machine, ONE shared PostgreSQL database):

    client -> nginx 127.0.0.1:8080
                  |-- upstream 127.0.0.1:8865  (medical-query@8865)
                  '-- upstream 127.0.0.1:8866  (medical-query@8866)
                              both -> PostgreSQL 127.0.0.1:5432 medical_query_demo

Closed-loop load is sent through nginx while one instance is SIGKILLed. The
script records, per request, the HTTP status, the upstream(s) nginx used
(X-Upstream-Addr / X-Upstream-Status) and the client latency, so client-visible
failures and the retry-to-healthy-node behaviour can be separated.

Single-machine demonstration only: no shared-nothing scaling, nginx and the
database remain single points of failure, and the client runs on the same host.
Evidence: evidence/p2/1-nginx-dual-instance/nginx-failover.json
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/p2/1-nginx-dual-instance/nginx-failover.json'
LB = 'http://127.0.0.1:8080'
PATH = '/api/summary'
WORKERS = 8
BASELINE_S = 15
OBSERVE_S = 45
RECOVERY_S = 25
KILL_INSTANCE = 'medical-query@8865'
KILL_PORT = 8865
SURVIVOR_PORT = 8866


def systemctl(*args):
    return subprocess.run(['systemctl', *args], capture_output=True, text=True)


def main_pid(unit):
    out = systemctl('show', '-p', 'MainPID', unit).stdout.strip()
    return int(out.split('=', 1)[1]) if '=' in out else 0


def request():
    """One request through nginx; returns a record dict."""
    start = time.perf_counter()
    record = {'t': round(time.perf_counter() - T0, 3), 'status': None,
              'upstream': None, 'upstream_status': None, 'error': None, 'ms': None}
    try:
        with urlopen(LB + PATH, timeout=10) as response:
            record['status'] = response.status
            record['upstream'] = response.headers.get('X-Upstream-Addr')
            record['upstream_status'] = response.headers.get('X-Upstream-Status')
            response.read()
    except HTTPError as error:
        record['status'] = error.code
        record['upstream'] = error.headers.get('X-Upstream-Addr')
        record['upstream_status'] = error.headers.get('X-Upstream-Status')
        record['error'] = f'HTTPError {error.code}'
    except (URLError, OSError) as error:
        record['error'] = f'{type(error).__name__}: {error}'
    record['ms'] = round((time.perf_counter() - start) * 1000, 3)
    return record


def worker(stop, sink, lock):
    while not stop.is_set():
        record = request()
        with lock:
            sink.append(record)


def http_status(path):
    try:
        with urlopen(LB + path, timeout=4) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, OSError):
        return 0


def main():
    if OUT.exists():
        raise SystemExit('Refusing to overwrite evidence')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    global T0

    result = {
        'scope': 'Single WSL2 host; nginx + 2 app instances + 1 shared PostgreSQL; '
                 'same-host client. Local failover demo, not production HA.',
        'topology': {
            'load_balancer': f'nginx 127.0.0.1:8080 (round-robin, passive health checks)',
            'upstreams': ['127.0.0.1:8865', '127.0.0.1:8866'],
            'shared_database': 'PostgreSQL 127.0.0.1:5432 medical_query_demo (single instance)',
            'systemd_units': ['medical-query@8865.service', 'medical-query@8866.service'],
            'retry_policy': 'proxy_next_upstream error timeout http_502/503/504',
        },
        'config': {'workers': WORKERS, 'path': PATH, 'baseline_s': BASELINE_S,
                   'observe_s': OBSERVE_S, 'recovery_s': RECOVERY_S,
                   'killed_unit': KILL_INSTANCE},
    }

    # --- preconditions -------------------------------------------------
    result['preconditions'] = {
        'health_8865': http_status('/health/ready'),
        'health_8866': http_status('/health/ready'),
        'pid_8865': main_pid(KILL_INSTANCE),
    }

    # --- run load, kill at the baseline boundary -----------------------
    records, lock, stop = [], threading.Lock(), threading.Event()
    T0 = time.perf_counter()
    kill_info = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker, stop, records, lock) for _ in range(WORKERS)]

        time.sleep(BASELINE_S)
        kill_at = time.perf_counter() - T0
        pid_before = main_pid(KILL_INSTANCE)
        kill_info['pid_before'] = pid_before
        kill_info['kill_issued_at_s'] = round(kill_at, 3)
        subprocess.run(['kill', '-9', str(pid_before)], check=False)
        kill_ts = time.time()

        # poll until systemd has restarted the instance (new MainPID) and the
        # port answers again
        restart_detected = None
        port_back = None
        deadline = time.time() + OBSERVE_S
        while time.time() < deadline:
            pid_now = main_pid(KILL_INSTANCE)
            if pid_now and pid_now != pid_before and restart_detected is None:
                restart_detected = time.time() - kill_ts
            if port_back is None and http_status_direct(KILL_PORT) == 200:
                port_back = time.time() - kill_ts
            time.sleep(0.2)
        kill_info['systemd_restart_after_s'] = round(restart_detected, 3) if restart_detected else None
        kill_info['instance_port_back_after_s'] = round(port_back, 3) if port_back else None
        kill_info['pid_after'] = main_pid(KILL_INSTANCE)

        time.sleep(RECOVERY_S)
        stop.set()
        for future in futures:
            future.result()

    # --- summarise -----------------------------------------------------
    records.sort(key=lambda r: r['t'])
    failures = [r for r in records if (r['status'] is None or r['status'] >= 500)]
    retried = [r for r in records if r['upstream'] and ',' in r['upstream']]

    def share(lo, hi):
        subset = [r for r in records if lo <= r['t'] < hi]
        counts = {}
        for r in subset:
            key = r['upstream'] or (r['error'] or 'unknown')
            counts[key] = counts.get(key, 0) + 1
        return {'requests': len(subset), 'by_upstream': counts}

    kill_t = kill_info['kill_issued_at_s']
    result['kill'] = kill_info
    result['totals'] = {
        'requests': len(records),
        'client_visible_failures': len(failures),
        'failure_rate': round(len(failures) / len(records), 5) if records else None,
        'requests_retried_to_second_upstream': len(retried),
        'latency_ms_p50': round(statistics.median([r['ms'] for r in records]), 3),
        'latency_ms_p95': round(sorted(r['ms'] for r in records)[int(len(records) * 0.95) - 1], 3),
        'latency_ms_max': round(max(r['ms'] for r in records), 3),
    }
    result['upstream_share'] = {
        'baseline': share(0, kill_t),
        'failover_window': share(kill_t, kill_t + 12),
        'recovery_window': share(kill_t + OBSERVE_S, kill_t + OBSERVE_S + RECOVERY_S),
    }
    result['failover_evidence'] = {
        'note': 'Requests where nginx retried the next upstream show a comma-separated '
                'X-Upstream-Addr / X-Upstream-Status, e.g. "8865, 8866" / "502, 200".',
        'samples': retried[:5],
        'first_success_after_kill_ms': round(
            (min((r['t'] for r in records if r['t'] >= kill_t and r['status'] == 200),
                 default=None) - kill_t) * 1000, 3) if any(
                r['t'] >= kill_t and r['status'] == 200 for r in records) else None,
        'failures_detail': failures[:5],
    }
    # per-second upstream/error breakdown around the kill
    per_second = []
    for second in range(int(records[-1]['t']) + 1):
        subset = [r for r in records if second <= r['t'] < second + 1]
        if not subset:
            continue
        per_second.append({
            'second': second,
            'requests': len(subset),
            'errors': sum(1 for r in subset if r['status'] is None or r['status'] >= 500),
            'served_8865': sum(1 for r in subset if r['upstream'] and '8865' in r['upstream']),
            'served_8866': sum(1 for r in subset if r['upstream'] and '8866' in r['upstream']),
            'p95_ms': round(sorted(r['ms'] for r in subset)[max(0, int(len(subset) * 0.95) - 1)], 3),
        })
    result['per_second'] = per_second

    # nginx error log slice (shows the passive health check acting)
    tail = subprocess.run(['tail', '-n', '40', '/var/log/nginx/medical-query-lb.error.log'],
                          capture_output=True, text=True).stdout.splitlines()
    result['nginx_error_log_tail'] = tail[-20:]

    result['postconditions'] = {
        'health_8865': http_status_direct(8865),
        'health_8866': http_status_direct(8866),
        'through_lb': http_status('/health/ready'),
    }

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'requests': result['totals']['requests'],
                      'failures': result['totals']['client_visible_failures'],
                      'retried': result['totals']['requests_retried_to_second_upstream'],
                      'restart_after_s': kill_info['systemd_restart_after_s'],
                      'port_back_after_s': kill_info['instance_port_back_after_s']}))


def http_status_direct(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/health/ready', timeout=3) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, OSError):
        return 0


if __name__ == '__main__':
    main()
