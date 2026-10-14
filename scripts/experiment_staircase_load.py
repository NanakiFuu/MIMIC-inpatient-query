"""Fixed-API staircase load with client latency/error samples, Prometheus resource
series and concurrent `ss -s` connection-state samples.

One host, same-host client, real MIMIC Demo 2.2 data, no AI calls. Closed-loop
concurrency; this is a single-machine demonstration, not a capacity limit.
Evidence: evidence/p2/0b-load-curves/staircase-load.json (+ PNG curves)
"""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import math
from pathlib import Path
import statistics
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/p2/0b-load-curves/staircase-load.json'
STAGES = [1, 2, 4, 8, 16]
STAGE_SECONDS = 30
QUERY_PATH = '/api/summary'
PROM = {
    'cpu_busy_ratio': '1 - avg(rate(node_cpu_seconds_total{job="node",mode="idle"}[30s]))',
    'memory_available_bytes': 'node_memory_MemAvailable_bytes{job="node"}',
    'server_p95_seconds': 'histogram_quantile(.95,sum by(le)(rate(medical_query_http_request_duration_seconds_bucket{route="/api/summary"}[30s])))',
    'api_error_ratio': 'sum(rate(medical_query_http_requests_total{route="/api/summary",status=~"5.."}[30s]))/sum(rate(medical_query_http_requests_total{route="/api/summary"}[30s]))',
    'root_disk_available_bytes': 'node_filesystem_avail_bytes{job="node",mountpoint="/",fstype!~"tmpfs|overlay"}',
}


def http(path):
    start = time.perf_counter()
    try:
        with urlopen('http://127.0.0.1:8765' + path, timeout=8) as response:
            return response.status, json.load(response), round((time.perf_counter() - start) * 1000, 3)
    except HTTPError as error:
        return error.code, {}, round((time.perf_counter() - start) * 1000, 3)
    except (TimeoutError, URLError):
        return 0, {}, round((time.perf_counter() - start) * 1000, 3)


def prometheus(path, params=None):
    with urlopen('http://127.0.0.1:9090/api/v1/' + path + ('?' + urlencode(params) if params else ''), timeout=8) as response:
        data = json.load(response)
    assert data['status'] == 'success'
    return data['data']


def ss_summary():
    text = subprocess.run(['ss', '-s'], capture_output=True, text=True, check=True).stdout
    values = {}
    for line in text.splitlines():
        line = line.strip()
        for key in ['estab', 'timewait', 'closed', 'orphaned', 'synrecv', 'syn-sent', 'tw']:
            if line.lower().startswith(key):
                digits = [int(tok) for tok in line.replace(',', ' ').split() if tok.isdigit()]
                if digits:
                    values[key] = digits[0]
    return values


def percentile(sorted_values, fraction):
    return sorted_values[max(0, math.ceil(len(sorted_values) * fraction) - 1)]


def main():
    if OUT.exists():
        raise SystemExit('Refusing to overwrite evidence')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    status, body, _ = http(QUERY_PATH)
    assert status == 200
    expected = body['result']
    record = {'scope': 'Single WSL2 host; same-host closed-loop client; generated traffic; real MIMIC Demo 2.2; no AI calls',
              'method': 'Staircase concurrency; per-request client samples; Prometheus series and ss -s sampled during each stage',
              'stages': STAGES, 'stage_seconds': STAGE_SECONDS, 'queries': PROM, 'runs': []}
    start = time.time()
    with gzip.open(OUT.parent / 'staircase-requests.jsonl.gz', 'xt', encoding='utf-8') as stream:
        lock = threading.Lock()
        for concurrency in STAGES:
            run_start = time.time()
            end = time.monotonic() + STAGE_SECONDS
            rows = []
            ss_rows = []
            stop_ss = threading.Event()

            def sample_ss():
                while not stop_ss.is_set():
                    try:
                        ss_rows.append({'utc_epoch': time.time(), **ss_summary()})
                    except subprocess.CalledProcessError:
                        pass
                    stop_ss.wait(2)

            def worker():
                while time.monotonic() < end:
                    code, payload, elapsed = http(QUERY_PATH)
                    row = {'utc_epoch': time.time(), 'concurrency': concurrency, 'status': code,
                           'correct': code == 200 and payload.get('result') == expected, 'elapsed_ms': elapsed}
                    with lock:
                        rows.append(row)
                        stream.write(json.dumps(row) + '\n')

            ss_thread = threading.Thread(target=sample_ss, daemon=True)
            ss_thread.start()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(lambda _: worker(), range(concurrency)))
            stop_ss.set()
            ss_thread.join(timeout=5)
            finished = time.time()
            latencies = sorted(row['elapsed_ms'] for row in rows)
            failures = sum(not row['correct'] for row in rows)
            run = {'concurrency': concurrency, 'start_utc_epoch': run_start, 'end_utc_epoch': finished,
                   'requests': len(rows), 'elapsed_seconds': round(finished - run_start, 3),
                   'requests_per_second': round(len(rows) / (finished - run_start), 3),
                   'client_p50_ms': round(statistics.median(latencies), 3),
                   'client_p95_ms': round(percentile(latencies, .95), 3),
                   'client_p99_ms': round(percentile(latencies, .99), 3),
                   'failed_requests': failures, 'error_ratio': round(failures / len(rows), 6) if rows else None,
                   'ss_samples': ss_rows,
                   'series': {name: prometheus('query_range', {'query': q, 'start': run_start, 'end': finished, 'step': 5})
                              for name, q in PROM.items()}}
            record['runs'].append(run)
            print(json.dumps({k: v for k, v in run.items() if k not in ('series', 'ss_samples')}), flush=True)
            time.sleep(5)
    record['overall_range'] = {'start': start, 'end': time.time()}
    record['total_requests'] = sum(run['requests'] for run in record['runs'])
    record['total_failures'] = sum(run['failed_requests'] for run in record['runs'])
    record['passed'] = record['total_failures'] == 0
    OUT.write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps({'total_requests': record['total_requests'], 'total_failures': record['total_failures']}))


if __name__ == '__main__':
    main()
