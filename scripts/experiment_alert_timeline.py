"""Complete pending/firing/resolved alert timeline for a real DB stop/start.

Polls the Prometheus alerts API at 2s resolution and records the state of every
project alert, including 'inactive' when an alert is absent. Generates bounded
fixed-API traffic so the 5xx-ratio alert can also move through its states.
"""
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/p2/0c-alert-timeline/outage-alert-timeline.json'
ALERTS = ['DatabaseAccessNotReady', 'ApiErrorRateHigh', 'FixedApiLatencyHigh']
POLL = 2.0
TRAFFIC_INTERVAL = 0.2


def alerts_now():
    with urlopen('http://127.0.0.1:9090/api/v1/alerts', timeout=8) as response:
        data = json.load(response)
    assert data['status'] == 'success'
    states = {name: 'inactive' for name in ALERTS}
    for alert in data['data']['alerts']:
        name = alert['labels'].get('alertname')
        if name in states:
            states[name] = alert['state']
    return states


def main():
    if OUT.exists():
        raise SystemExit('Refusing to overwrite evidence')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result = {'scope': 'One WSL2 host; real postgresql@16-main stop/start; 2s alert-state polling; bounded fixed-API traffic',
              'poll_seconds': POLL, 'traffic_interval_seconds': TRAFFIC_INTERVAL,
              'alerts': ALERTS, 'transitions': [], 'samples': [], 'events': []}
    start = time.time()
    traffic = {'requests': 0, 'errors': 0}
    stop_traffic = threading.Event()

    def generate():
        while not stop_traffic.is_set():
            try:
                with urlopen('http://127.0.0.1:8765/api/summary', timeout=5) as response:
                    response.read()
                traffic['requests'] += 1
            except HTTPError:
                traffic['errors'] += 1
            except (URLError, TimeoutError):
                traffic['errors'] += 1
            stop_traffic.wait(TRAFFIC_INTERVAL)

    def event(name):
        result['events'].append({'event': name, 'utc_epoch': time.time()})

    previous = None

    def poll(duration):
        nonlocal previous
        deadline = time.time() + duration
        while time.time() < deadline:
            states = alerts_now()
            now = time.time()
            result['samples'].append({'utc_epoch': now, 'states': dict(states)})
            if previous is not None:
                for name in ALERTS:
                    if states[name] != previous[name]:
                        result['transitions'].append({'utc_epoch': now, 'alertname': name,
                                                      'from': previous[name], 'to': states[name]})
            previous = states
            time.sleep(POLL)
        return states

    thread = threading.Thread(target=generate, daemon=True)
    thread.start()
    try:
        event('baseline_started')
        poll(10)
        event('stop_command_started')
        subprocess.run(['systemctl', 'stop', 'postgresql@16-main'], check=True)
        event('database_stopped')
        deadline = time.time() + 120
        while time.time() < deadline:
            states = poll(POLL)
            if states['DatabaseAccessNotReady'] == 'firing' and states['ApiErrorRateHigh'] == 'firing':
                break
        event('target_alerts_firing')
        poll(10)
        event('start_command_started')
        subprocess.run(['systemctl', 'start', 'postgresql@16-main'], check=True)
        event('database_started')
        deadline = time.time() + 240
        while time.time() < deadline:
            states = poll(POLL)
            if all(state == 'inactive' for state in states.values()):
                break
        event('all_alerts_resolved')
        poll(6)
        event('finished')
    finally:
        stop_traffic.set()
        thread.join(timeout=5)
        subprocess.run(['systemctl', 'start', 'postgresql@16-main'], check=False)

    result['range'] = {'start': start, 'end': time.time(), 'poll_seconds': POLL}
    result['traffic'] = traffic
    result['final_states'] = previous
    result['reached_pending'] = sorted({t['alertname'] for t in result['transitions'] if t['to'] == 'pending'})
    result['reached_firing'] = sorted({t['alertname'] for t in result['transitions'] if t['to'] == 'firing'})
    result['never_fired'] = sorted(set(ALERTS) - set(result['reached_firing']))
    result['all_resolved'] = all(state == 'inactive' for state in previous.values())
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'reached_pending': result['reached_pending'], 'reached_firing': result['reached_firing'],
                      'never_fired': result['never_fired'], 'all_resolved': result['all_resolved'],
                      'transitions': result['transitions'], 'traffic': traffic}))
    assert 'DatabaseAccessNotReady' in result['reached_firing'] and result['all_resolved']


if __name__ == '__main__':
    main()
