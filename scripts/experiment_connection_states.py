"""Connection-state sampling during load + TIME_WAIT decay observation.

Runs a burst of short-lived (Connection: close) HTTP requests through nginx and
samples the kernel socket tables with `ss` while the burst runs and for a while
afterwards, so the TIME_WAIT build-up and its decay can be shown with numbers.

Single WSL2 host, same-host client. Evidence:
evidence/p2/3-connection-states/connection-states.json
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/p2/3-connection-states/connection-states.json'
TARGET = 'http://127.0.0.1:8080/health/live'
WORKERS = 8
REQUESTS_PER_WORKER = 300
SAMPLE_INTERVAL = 0.5
DECAY_SECONDS = 75  # > Linux fixed TIME_WAIT (60s) so the drop is visible
STATES = ['ESTAB', 'TIME-WAIT', 'CLOSE-WAIT', 'FIN-WAIT-1', 'FIN-WAIT-2', 'SYN-SENT', 'LAST-ACK']


def ss_summary():
    """Parse `ss -s` TCP line into a dict of counters."""
    text = subprocess.run(['ss', '-s'], capture_output=True, text=True).stdout
    line = next((l for l in text.splitlines() if l.startswith('TCP:')), '')
    numbers = dict(re.findall(r'(\w+)\s+(\d+)', line))
    numbers.pop('TCP', None)
    return {k: int(v) for k, v in numbers.items()}


def state_counts():
    """Per-state socket counts via `ss -tanH` (state is the first column)."""
    text = subprocess.run(['ss', '-tanH'], capture_output=True, text=True).stdout
    counts = {state: 0 for state in STATES}
    for line in text.splitlines():
        parts = line.split()
        if parts:
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    counts['total'] = sum(v for k, v in counts.items() if k != 'total')
    return counts


def sysctl(name):
    return subprocess.run(['sysctl', '-n', name], capture_output=True, text=True).stdout.strip()


def one_request():
    try:
        with urlopen(Request(TARGET, headers={'Connection': 'close'}), timeout=8) as response:
            response.read()
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, OSError):
        return None


def main():
    if OUT.exists():
        raise SystemExit('Refusing to overwrite evidence')
    OUT.parent.mkdir(parents=True, exist_ok=True)

    result = {
        'scope': 'Single WSL2 host; same-host client; short-lived HTTP connections through nginx.',
        'kernel': {
            'ip_local_port_range': sysctl('net.ipv4.ip_local_port_range'),
            'tcp_tw_reuse': sysctl('net.ipv4.tcp_tw_reuse'),
            'tcp_fin_timeout': sysctl('net.ipv4.tcp_fin_timeout'),
        },
        'config': {'target': TARGET, 'workers': WORKERS,
                   'requests': WORKERS * REQUESTS_PER_WORKER,
                   'sample_interval_s': SAMPLE_INTERVAL, 'decay_seconds': DECAY_SECONDS},
    }

    samples = []
    t0 = time.perf_counter()
    stop = threading.Event()
    load_done = threading.Event()

    def sampler():
        while not stop.is_set():
            samples.append({
                't': round(time.perf_counter() - t0, 2),
                'ss_s': ss_summary(),
                'states': state_counts(),
                'phase': 'load' if not load_done.is_set() else 'decay',
            })
            time.sleep(SAMPLE_INTERVAL)

    statuses = []
    lock = threading.Lock()

    def worker():
        local = [one_request() for _ in range(REQUESTS_PER_WORKER)]
        with lock:
            statuses.extend(local)

    sampler_thread = threading.Thread(target=sampler)
    sampler_thread.start()
    load_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(lambda _: worker(), range(WORKERS)))
    load_seconds = time.perf_counter() - load_start
    load_done.set()

    # keep sampling while TIME_WAIT drains
    time.sleep(DECAY_SECONDS)
    stop.set()
    sampler_thread.join()

    peak_timewait = max(s['states'].get('TIME-WAIT', 0) for s in samples)
    result['load'] = {
        'requests': len(statuses),
        'failed': sum(1 for s in statuses if s != 200),
        'duration_s': round(load_seconds, 3),
        'requests_per_s': round(len(statuses) / load_seconds, 1),
        'new_connections_approx': len(statuses),
    }
    result['time_wait'] = {
        'peak': peak_timewait,
        'at_load_end': next((s['states'].get('TIME-WAIT', 0) for s in reversed(samples)
                             if s['phase'] == 'load'), 0),
        'after_decay_s': samples[-1]['states'].get('TIME-WAIT', 0),
        'decay_seconds_observed': DECAY_SECONDS,
        'samples': [{'t': s['t'], 'phase': s['phase'],
                     'time_wait': s['states'].get('TIME-WAIT', 0),
                     'estab': s['states'].get('ESTAB', 0),
                     'ss_s_tcp': s['ss_s']} for s in samples],
    }
    result['final_state_counts'] = samples[-1]['states']

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'requests': result['load']['requests'], 'failed': result['load']['failed'],
                      'rps': result['load']['requests_per_s'], 'peak_time_wait': peak_timewait,
                      'time_wait_at_load_end': result['time_wait']['at_load_end'],
                      'time_wait_after_decay': result['time_wait']['after_decay_s']}))


if __name__ == '__main__':
    main()
