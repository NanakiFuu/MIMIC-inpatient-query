"""AI fault assessment from a read-only Prometheus whitelist, across reproducible faults.

For each case the script injects a real fault on this host, waits for Prometheus
to reflect it, takes the whitelisted metric snapshot, computes the deterministic
local rule baseline and asks the model for its own judgment. Both are recorded:
the raw model judgment is kept verbatim and the validated output is what survives
the local consistency check.

Cases: healthy baseline, systemd app stopped, container app stopped, PostgreSQL
stopped, CPU saturation. Every case is reverted and re-verified before the next.

Scope: single WSL2 host, demonstration thresholds. Evidence:
evidence/p2/6-ai-prometheus/fault-assessments.json
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from medical_query import metrics_ai, model  # noqa: E402

OUT = ROOT / 'evidence/p2/6-ai-prometheus/fault-assessments.json'
APP_URL = 'http://127.0.0.1:8765/health/ready'
CONTAINER = 'medical-query-demo-app-1'
BUSY_PROCESSES = 60


def run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def http_ok(url, timeout=4):
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (HTTPError, URLError, OSError):
        return False


def wait_until(predicate, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(1)
    print(f'  WARNING: {label} not confirmed within {timeout}s')
    return False


def app_up():
    return http_ok(APP_URL)


def container_healthy():
    out = run('docker', 'inspect', '-f', '{{.State.Health.Status}}', CONTAINER).stdout.strip()
    return out == 'healthy'


def database_up():
    return run('systemctl', 'is-active', 'postgresql@16-main').stdout.strip() == 'active'


def stop_app():
    run('systemctl', 'stop', 'medical-query')


def start_app():
    run('systemctl', 'start', 'medical-query')
    wait_until(app_up, 60, 'systemd app healthy')


def stop_container():
    run('docker', 'stop', CONTAINER)


def start_container():
    run('docker', 'start', CONTAINER)
    wait_until(container_healthy, 90, 'container healthy')


def stop_database():
    run('systemctl', 'stop', 'postgresql@16-main')


def start_database():
    run('systemctl', 'start', 'postgresql@16-main')
    wait_until(database_up, 60, 'database active')


_busy = []


def saturate_cpu():
    devnull = open(os.devnull, 'wb')
    for _ in range(BUSY_PROCESSES):
        _busy.append(subprocess.Popen(['yes'], stdout=devnull, stderr=devnull))


def calm_cpu():
    for process in _busy:
        process.kill()
    for process in _busy:
        process.wait()
    _busy.clear()


CASES = [
    {'name': 'healthy_baseline', 'injected': 'none', 'expected_rule': 'healthy',
     'settle_s': 10, 'inject': None, 'recover': None},
    {'name': 'systemd_app_stopped', 'injected': 'systemctl stop medical-query',
     'expected_rule': 'app_target_down', 'settle_s': 14, 'inject': stop_app, 'recover': start_app},
    {'name': 'container_app_stopped', 'injected': f'docker stop {CONTAINER}',
     'expected_rule': 'compose_app_target_down', 'settle_s': 14, 'inject': stop_container,
     'recover': start_container},
    {'name': 'postgresql_stopped', 'injected': 'systemctl stop postgresql@16-main',
     'expected_rule': 'database_unavailable', 'settle_s': 14, 'inject': stop_database,
     'recover': start_database},
    {'name': 'cpu_saturation', 'injected': f'{BUSY_PROCESSES} busy processes for ~90s',
     'expected_rule': 'resource_saturation', 'settle_s': 90, 'inject': saturate_cpu,
     'recover': calm_cpu},
]


def main():
    if OUT.exists():
        raise SystemExit('Refusing to overwrite evidence')
    OUT.parent.mkdir(parents=True, exist_ok=True)

    result = {
        'scope': 'Single WSL2 host; read-only Prometheus whitelist; model sees aggregated numbers only',
        'whitelist': {key: expression for key, (expression, _) in metrics_ai.WHITELIST.items()},
        'thresholds': metrics_ai.THRESHOLDS,
        'prompt_version': metrics_ai.VERSION,
        'model': {'configured': model.status()['configured'], 'model': 'deepseek-flash'},
        'cases': [],
    }
    if not result['model']['configured']:
        raise SystemExit('Model is not configured; cannot run')

    for case in CASES:
        print(f'== {case["name"]} ==', flush=True)
        # The WSL session can stop units between runs; restore the baseline
        # before injecting so a case is never measured on a dirty host.
        if not database_up():
            run('systemctl', 'start', 'postgresql@16-main')
            wait_until(database_up, 60, 'database active (precondition)')
        if not app_up():
            run('systemctl', 'start', 'medical-query')
            wait_until(app_up, 60, 'app healthy (precondition)')
        if not container_healthy():
            run('docker', 'start', CONTAINER)
            wait_until(container_healthy, 90, 'container healthy (precondition)')
        time.sleep(6)

        if case['inject']:
            case['inject']()
        time.sleep(case['settle_s'])

        snapshot = metrics_ai.collect()
        baseline = metrics_ai.rule_assessment(snapshot)
        try:
            diagnosis = metrics_ai.interpret(snapshot)
            error, error_evidence = None, None
        except model.ModelError as failure:
            diagnosis, error = None, failure.code
            error_evidence = failure.evidence

        record = {
            'name': case['name'], 'injected': case['injected'],
            'expected_rule': case['expected_rule'],
            'metrics': {key: item['value'] for key, item in snapshot['metrics'].items()},
            'rule_baseline': baseline,
            'model_error': error,
            'model_error_evidence': error_evidence,
            'model_original_judgment': diagnosis['model_original_judgment'] if diagnosis else None,
            'validated_finding': diagnosis['validated_finding'] if diagnosis else None,
            'consistency_check_passed': diagnosis['consistency_check_passed'] if diagnosis else None,
            'model_agrees_with_rule': (diagnosis['model_original_judgment']['finding'] == baseline)
                                      if diagnosis else None,
            'next_checks': diagnosis['next_checks'] if diagnosis else None,
            'model_evidence': diagnosis['model_evidence'] if diagnosis else None,
            'rule_matches_expected': baseline == case['expected_rule'],
        }
        result['cases'].append(record)
        print(f'  rule={baseline} expected={case["expected_rule"]} '
              f'model_raw={record["model_original_judgment"]["finding"] if diagnosis else error} '
              f'validated={record["validated_finding"]}', flush=True)

        if case['recover']:
            case['recover']()
        wait_until(app_up, 60, 'app healthy')
        time.sleep(3)

    result['summary'] = {
        'cases': len(result['cases']),
        'rule_matches_expected': sum(1 for c in result['cases'] if c['rule_matches_expected']),
        'model_agrees_with_rule': sum(1 for c in result['cases'] if c['model_agrees_with_rule']),
        'consistency_check_passed': sum(1 for c in result['cases'] if c['consistency_check_passed']),
        'model_errors': sum(1 for c in result['cases'] if c['model_error']),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
