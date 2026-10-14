"""PostgreSQL streaming-replication failover demonstration (SINGLE WSL2 host).

Two NEW clusters are used; the existing 16/main cluster is never touched:
    primary  16/replprimary  port 5532  <- restored from a project backup
    standby  16/replstandby  port 5533  <- physical replica (streaming, async)

Steps: baseline stats -> commit-to-visible replication lag -> pre-failover data
fingerprints -> stop primary + promote standby -> point an application instance
at the promoted node and re-run the fixed queries -> post-failover consistency.

Scope: one machine, one disk, asynchronous streaming replication, manual
promotion. This is a local demonstration, NOT production HA (no automatic
failover, no quorum, single point of failure on the shared disk).

Evidence: evidence/p2/2-pg-replication/pg-replication-failover.json
"""
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/p2/2-pg-replication/pg-replication-failover.json'
PRIMARY_PORT = 5532
STANDBY_PORT = 5533
PRIMARY_CLUSTER = 'replprimary'
STANDBY_CLUSTER = 'replstandby'
PROBE_SAMPLES = 50
APP_PORT = 8867
APP_CONFIG = Path('/home/nanaki/.config/medical-query/database.json')
APP_VENV = '/home/nanaki/environments/medical-query/.venv/bin/python'
STAGE = Path('/tmp/pg-repl-demo')
ADMIN = {'host': '127.0.0.1', 'user': 'demo_admin', 'password': 'demoadmin',
         'dbname': 'medical_query_demo', 'connect_timeout': 5, 'row_factory': dict_row}


def connect(port):
    return psycopg.connect(**{**ADMIN, 'port': port})


def fingerprints(port):
    """count + ordered md5 per relation, for the source tables and the views."""
    out = {}
    with connect(port) as conn:
        relations = [r['rel'] for r in conn.execute("""
            select n.nspname||'.'||c.relname as rel
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname in ('mimic_raw','query_api') and c.relkind in ('r','v')
            order by 1""").fetchall()]
        for rel in relations:
            row = conn.execute(
                f"select count(*) as n, coalesce(md5(string_agg(x, '|' order by x)), '') as h "
                f"from (select t::text as x from {rel} t) s").fetchone()
            out[rel] = f"{row['n']}:{row['h']}"
    return out


def replication_stats(port):
    with connect(port) as conn:
        rows = conn.execute("""
            select application_name, state, sync_state,
                   pg_wal_lsn_diff(sent_lsn, replay_lsn) as replay_behind_bytes,
                   write_lag::text, flush_lag::text, replay_lag::text
            from pg_stat_replication""").fetchall()
    return [{k: (str(v) if v is not None else None) for k, v in r.items()} for r in rows]


def lag_samples(primary, standby, count):
    """commit-to-visible-on-standby latency, in milliseconds."""
    primary.execute("create table if not exists public.repl_probe(id serial primary key, note text)")
    primary.execute("truncate public.repl_probe restart identity")
    primary.commit()
    # wait until the standby shows the table AND has replayed the truncate
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if standby.execute("select count(*) as n from public.repl_probe").fetchone()['n'] == 0:
                break
        except psycopg.errors.UndefinedTable:
            standby.rollback()
        time.sleep(0.05)
    samples = []
    for i in range(count):
        start = time.perf_counter()
        primary.execute("insert into public.repl_probe(note) values ('probe')")
        primary.commit()
        while True:
            seen = standby.execute("select count(*) as n from public.repl_probe").fetchone()['n']
            if seen >= i + 1:
                break
            time.sleep(0.0005)
        samples.append(round((time.perf_counter() - start) * 1000, 3))
    return samples


def http_status(url):
    try:
        with urlopen(url, timeout=4) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, OSError):
        return 0


def main():
    if OUT.exists():
        raise SystemExit('Refusing to overwrite evidence')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)

    result = {
        'scope': 'Single WSL2 host; two new clusters (5532 primary / 5533 standby); '
                 'asynchronous streaming replication; manual promotion. Local demo, not HA.',
        'existing_cluster_untouched': '16/main on 5432 remains the live project database',
        'clusters': {'primary': f'16/{PRIMARY_CLUSTER}:{PRIMARY_PORT}',
                     'standby': f'16/{STANDBY_CLUSTER}:{STANDBY_PORT}'},
    }

    primary = connect(PRIMARY_PORT)
    standby = connect(STANDBY_PORT)
    standby.autocommit = True  # each poll must see the newest replayed data

    result['baseline'] = {
        'primary_in_recovery': primary.execute('select pg_is_in_recovery() as v').fetchone()['v'],
        'standby_in_recovery': standby.execute('select pg_is_in_recovery() as v').fetchone()['v'],
        'pg_stat_replication': replication_stats(PRIMARY_PORT),
    }

    # --- 1. replication lag ---------------------------------------------
    samples = lag_samples(primary, standby, PROBE_SAMPLES)
    result['lag'] = {
        'method': 'commit on primary -> row visible on standby (same host clock)',
        'samples_ms': samples,
        'p50_ms': round(statistics.median(samples), 3),
        'p95_ms': round(sorted(samples)[int(len(samples) * 0.95) - 1], 3),
        'max_ms': max(samples),
        'stats_after_load': replication_stats(PRIMARY_PORT),
    }

    # --- 2. pre-failover consistency ------------------------------------
    fp_primary = fingerprints(PRIMARY_PORT)
    fp_standby = fingerprints(STANDBY_PORT)
    result['consistency_before'] = {
        'primary': fp_primary, 'standby': fp_standby,
        'identical': fp_primary == fp_standby,
        'relations': len(fp_primary),
    }

    # --- 3. failover: stop primary, promote standby ---------------------
    stop_start = time.perf_counter()
    subprocess.run(['pg_ctlcluster', '16', PRIMARY_CLUSTER, 'stop', '--force'],
                   capture_output=True, text=True)
    primary.close()
    stop_s = time.perf_counter() - stop_start
    promote_start = time.perf_counter()
    subprocess.run(['pg_ctlcluster', '16', STANDBY_CLUSTER, 'promote'],
                   capture_output=True, text=True)
    promoted_at = None
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with connect(STANDBY_PORT) as probe:
                if probe.execute('select pg_is_in_recovery() as v').fetchone()['v'] is False:
                    promoted_at = time.perf_counter() - promote_start
                    break
        except psycopg.OperationalError:
            pass
        time.sleep(0.2)
    result['failover'] = {
        'primary_stop_s': round(stop_s, 3),
        'promotion_s': round(promoted_at, 3) if promoted_at is not None else None,
        'promoted_node_accepts_writes': None,
    }
    # write test on the promoted node
    if promoted_at is not None:
        with connect(STANDBY_PORT) as promoted:
            promoted.execute('create table if not exists public.failover_marker(id int primary key, at timestamptz default now())')
            promoted.execute('insert into public.failover_marker(id) values (1) on conflict do nothing')
            promoted.commit()
            result['failover']['promoted_node_accepts_writes'] = True
            result['failover']['promoted_in_recovery'] = promoted.execute(
                'select pg_is_in_recovery() as v').fetchone()['v']

    # --- 4. application switch to the promoted node ---------------------
    config = json.loads(APP_CONFIG.read_text(encoding='utf-8'))
    config['port'] = STANDBY_PORT
    staged_config = STAGE / 'app-standby.json'
    staged_config.write_text(json.dumps(config), encoding='utf-8')
    os.chmod(staged_config, 0o600)
    subprocess.run(['chown', 'nanaki:nanaki', str(staged_config)], check=False)

    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'),
           'MEDICAL_QUERY_DB_CONFIG': str(staged_config)}
    app = subprocess.Popen(
        ['runuser', '-u', 'nanaki', '--', APP_VENV, '-m', 'uvicorn', 'medical_query.api:app',
         '--host', '127.0.0.1', '--port', str(APP_PORT), '--no-access-log'],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    health = 0
    for _ in range(40):
        health = http_status(f'http://127.0.0.1:{APP_PORT}/health/ready')
        if health == 200:
            break
        time.sleep(0.5)
    result['app_switch'] = {
        'instance_port': APP_PORT, 'pointed_at_db_port': STANDBY_PORT,
        'config_override': 'MEDICAL_QUERY_DB_CONFIG (staged copy, port changed)',
        'health_ready': health,
    }
    parity = subprocess.run(
        [APP_VENV, str(ROOT / 'scripts/verify_deployments.py'), '--first', 'http://127.0.0.1:8765',
         '--second', f'http://127.0.0.1:{APP_PORT}', '--report',
         str(ROOT / 'evidence/p2/2-pg-replication/parity-after-failover.json')],
        capture_output=True, text=True)
    result['app_switch']['parity_stdout'] = parity.stdout.strip().splitlines()[-3:]
    result['app_switch']['parity_rc'] = parity.returncode

    # --- 5. post-failover consistency -----------------------------------
    fp_after = fingerprints(STANDBY_PORT)
    result['consistency_after'] = {
        'promoted': fp_after,
        'matches_pre_failover_primary': fp_after == fp_primary,
        'diff': {k: (fp_primary.get(k), fp_after.get(k))
                 for k in set(fp_primary) | set(fp_after) if fp_primary.get(k) != fp_after.get(k)},
    }

    # --- 6. cleanup the temporary app instance --------------------------
    app.terminate()
    try:
        app.wait(timeout=10)
    except subprocess.TimeoutExpired:
        app.kill()
    result['app_switch']['instance_stopped'] = app.poll() is not None

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'lag_p50_ms': result['lag']['p50_ms'], 'lag_max_ms': result['lag']['max_ms'],
                      'consistent_before': result['consistency_before']['identical'],
                      'promotion_s': result['failover']['promotion_s'],
                      'writes_ok': result['failover']['promoted_node_accepts_writes'],
                      'app_health': health, 'parity_rc': parity.returncode,
                      'consistent_after': result['consistency_after']['matches_pre_failover_primary']}))


if __name__ == '__main__':
    main()
