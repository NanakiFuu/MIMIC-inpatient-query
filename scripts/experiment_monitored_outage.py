"""Actual project DB stop/start, bounded test traffic, Prometheus timelines."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def http(path):
    start = time.perf_counter()
    try:
        with urlopen('http://127.0.0.1:8765' + path, timeout=6) as response:
            return response.status, json.load(response), round((time.perf_counter()-start)*1000, 3)
    except HTTPError as error:
        return error.code, json.load(error), round((time.perf_counter()-start)*1000, 3)
    except (TimeoutError, URLError):
        return 0, {}, round((time.perf_counter()-start)*1000, 3)


def prometheus(path, params=None):
    with urlopen('http://127.0.0.1:9090/api/v1/' + path + ('?' + urlencode(params) if params else ''), timeout=8) as response:
        data = json.load(response)
    assert data['status'] == 'success'
    return data['data']


QUERIES = {
    'database_ready': 'medical_query_database_ready{job="medical-query"}',
    'app_scrape_up': 'up{job="medical-query"}',
    'error_ratio': 'sum(rate(medical_query_http_requests_total{route="/api/summary",status=~"5.."}[1m])) / sum(rate(medical_query_http_requests_total{route="/api/summary"}[1m]))',
    'cpu_busy_ratio': '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m]))',
    'memory_available_bytes': 'node_memory_MemAvailable_bytes',
    'alerts': 'ALERTS{alertstate="firing"}',
}


def main():
    output = ROOT / 'evidence/p0/monitoring/outage.json'
    if output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    assert subprocess.run(['systemctl', 'is-active', '--quiet', 'postgresql@16-main']).returncode == 0
    ready_deadline = time.monotonic() + 15
    initial = http('/api/summary')
    while initial[0] != 200 and time.monotonic() < ready_deadline:
        time.sleep(.25)
        initial = http('/api/summary')
    assert initial[0] == 200
    result = {'source': 'Real project DB stop/start; generated test traffic; MIMIC Demo 2.2 records',
              'scope': 'One WSL2 Linux instance; no clinical records in metrics', 'events': [], 'samples': [], 'alerts': [], 'queries': QUERIES}
    start_epoch = time.time()
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            status, body, elapsed = http('/api/summary')
            result['samples'].append({'utc_epoch': time.time(), 'status': status, 'elapsed_ms': elapsed})
            stop.wait(.1)

    def event(name):
        result['events'].append({'event': name, 'utc_epoch': time.time()})
        print(name, flush=True)

    thread = threading.Thread(target=sample)
    thread.start()
    try:
        event('healthy_baseline_started')
        time.sleep(25)
        event('stop_command_started')
        subprocess.run(['sudo', '-n', 'systemctl', 'stop', 'postgresql@16-main'], check=True)
        event('database_stopped')
        assert http('/health/live')[0] == 200
        assert http('/health/ready')[0] == 503
        # Hold long enough for scrape/evaluation/for timers; gather exact states.
        for _ in range(12):
            alerts = prometheus('alerts')['alerts']
            result['alerts'].append({'utc_epoch': time.time(), 'alerts': alerts})
            time.sleep(5)
        event('start_command_started')
        restore_started = time.perf_counter()
        subprocess.run(['sudo', '-n', 'systemctl', 'start', 'postgresql@16-main'], check=True)
        while time.perf_counter()-restore_started < 15:
            restored = http('/api/summary')
            if restored[0] == 200:
                break
            time.sleep(.1)
        assert restored[0] == 200 and restored[1]['result'] == initial[1]['result']
        result['command_to_success_ms'] = round((time.perf_counter()-restore_started)*1000, 3)
        event('query_recovered')
        for _ in range(18):
            result['alerts'].append({'utc_epoch': time.time(), 'alerts': prometheus('alerts')['alerts']})
            time.sleep(5)
        event('recovery_observation_finished')
    finally:
        subprocess.run(['sudo', '-n', 'systemctl', 'start', 'postgresql@16-main'], check=True)
        stop.set()
        thread.join(timeout=10)
        end_epoch = time.time()
        result['range'] = {'start': start_epoch, 'end': end_epoch, 'step': 5}
        result['series'] = {key: prometheus('query_range', {'query':query, **result['range']}) for key, query in QUERIES.items()}
        result['restored_same_summary'] = http('/api/summary')[1].get('result') == initial[1]['result']
        result['targets'] = prometheus('targets')
        firing = {a['labels']['alertname'] for sample in result['alerts'] for a in sample['alerts'] if a['state'] == 'firing'}
        result['observed_firing_alerts'] = sorted(firing)
        result['final_active_alerts'] = prometheus('alerts')['alerts']
        result['passed'] = {'DatabaseAccessNotReady', 'ApiErrorRateHigh'} <= firing and result['restored_same_summary'] and not result['final_active_alerts']
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    assert result['passed']
    print(json.dumps({'passed': True, 'firing_alerts': sorted(firing), 'command_to_success_ms': result['command_to_success_ms']}))


if __name__ == '__main__':
    main()
