"""pg_stat_statements case: explicit window -> real Top SQL -> plan -> index -> re-test.

Run as root. The script drops its effective uid to the postgres account so peer
authentication works (the postgres account cannot traverse the venv's home dir).

Read-only against the business database. The index experiment runs only in a
separate database created from a pg_dump copy, so the project database is not
altered. Evidence: evidence/p2/0a-pg-stat-statements/query-case.json
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import time
from urllib.request import urlopen

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/p2/0a-pg-stat-statements/query-case.json'
VERIFY_DB = 'medical_query_case_verify'
PAGE_SQL = 'SELECT e.* FROM query_api.encounter_overview e ORDER BY e.admittime,e.hadm_id LIMIT %s OFFSET %s'
WINDOW_REQUESTS = 200
PATHS = ['/api/summary', '/api/summary?has_icu=true', '/api/admissions?limit=20',
         '/api/admissions/{h}/diagnoses', '/api/admissions/{h}/labs?concept=creatinine']


def psql(database, sql, check=True):
    return subprocess.run(['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-d', database, '-c', sql],
                          capture_output=True, text=True, check=check)


def stats(conn):
    rows = conn.execute("""SELECT queryid::text, query, calls, total_exec_time, rows,
                                  shared_blks_hit, shared_blks_read
                             FROM pg_stat_statements
                            WHERE dbid=(SELECT oid FROM pg_database WHERE datname='medical_query_demo')
                              AND userid=(SELECT oid FROM pg_roles WHERE rolname='medical_query_reader')
                              AND query LIKE 'SELECT%%'""").fetchall()
    return {r['queryid']: dict(r) for r in rows}


def explain(conn, limit, offset):
    plan = conn.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + PAGE_SQL % (limit, offset)).fetchone()['QUERY PLAN'][0]
    return plan


def fingerprint(conn, limit, offset):
    rows = conn.execute(PAGE_SQL, (limit, offset)).fetchall()
    payload = json.dumps([[str(v) for v in row.values()] for row in rows], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest(), len(rows)


def walk(node):
    yield node
    for child in node.get('Plans', []):
        yield from walk(child)


def summarize(plan):
    node = plan['Plan']
    return {'planning_ms': plan['Planning Time'], 'execution_ms': plan['Execution Time'],
            'top_node': node['Node Type'],
            'sort_node_present': any('Sort' in n.get('Node Type', '') for n in walk(node)),
            'total_rows_returned_by_top_node': node.get('Actual Rows')}


def main():
    if os.getuid() != 0:
        raise SystemExit('Run as root; the script drops euid to postgres for peer auth')
    if OUT.exists():
        raise SystemExit('Refusing to overwrite evidence')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result = {'scope': 'Single WSL2 host; real fixed-API window; index experiment in an isolated copy database',
              'business_db_modified': False}
    account = pwd.getpwnam('postgres')
    os.setegid(account.pw_gid)
    os.seteuid(account.pw_uid)

    with psycopg.connect(dbname='medical_query_demo', user='postgres', host='/var/run/postgresql',
                         row_factory=dict_row, autocommit=True) as conn:
        with urlopen('http://127.0.0.1:8765/api/admissions?limit=1', timeout=5) as response:
            hadm = json.load(response)['result']['rows'][0]['hadm_id']
        paths = [p.format(h=hadm) for p in PATHS]
        before = stats(conn)
        window_start = time.time()
        completed = 0
        for _ in range(WINDOW_REQUESTS // len(paths)):
            for path in paths:
                with urlopen('http://127.0.0.1:8765' + path, timeout=8) as response:
                    assert response.status == 200
                    json.load(response)
                    completed += 1
        window_end = time.time()
        after = stats(conn)
        info = conn.execute('SELECT stats_reset, dealloc FROM pg_stat_statements_info').fetchone()
        result['window'] = {'start_epoch': window_start, 'end_epoch': window_end,
                            'seconds': round(window_end - window_start, 3), 'completed_requests': completed,
                            'stats_reset': str(info['stats_reset']), 'dealloc': info['dealloc'],
                            'method': 'pg_stat_statements delta over the window; no global reset'}

    ranking = []
    for key, row in after.items():
        old = before.get(key, {})
        delta = {k: row[k] - old.get(k, 0) for k in ['calls', 'total_exec_time', 'rows', 'shared_blks_hit', 'shared_blks_read']}
        if delta['calls'] > 0:
            ranking.append({'normalized_sql': ' '.join(row['query'].split()), 'queryid': key, **delta,
                            'mean_exec_ms': delta['total_exec_time'] / delta['calls']})
    ranking.sort(key=lambda r: r['total_exec_time'], reverse=True)
    result['top_by_total_exec_time'] = ranking
    result['chosen_problem'] = {
        'sql': PAGE_SQL,
        'why': 'Deep OFFSET pagination on an ordered view. mimic_raw.admissions has no (admittime, hadm_id) '
               'index, so the plan must sort the whole ordered set even though OFFSET discards most rows.',
    }

    psql('postgres', f'DROP DATABASE IF EXISTS {VERIFY_DB}', check=False)
    with open('/tmp/case-verify.dump', 'wb') as stream:
        subprocess.run(['pg_dump', '-Fp', '-d', 'medical_query_demo'], stdout=stream, check=True, cwd='/tmp')
    psql('postgres', f'CREATE DATABASE {VERIFY_DB}')
    with open('/tmp/case-verify.dump', 'rb') as stream:
        subprocess.run(['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-d', VERIFY_DB],
                       stdin=stream, check=True, cwd='/tmp')

    with psycopg.connect(dbname=VERIFY_DB, user='postgres', host='/var/run/postgresql',
                         row_factory=dict_row, autocommit=True) as conn:
        fp_before = {off: fingerprint(conn, 20, off) for off in (0, 100)}
        plans_before = {off: explain(conn, 20, off) for off in (0, 100)}
        conn.execute('CREATE INDEX admissions_admittime_idx ON mimic_raw.admissions(admittime, hadm_id)')
        conn.execute('ANALYZE mimic_raw.admissions')
        fp_after = {off: fingerprint(conn, 20, off) for off in (0, 100)}
        plans_after = {off: explain(conn, 20, off) for off in (0, 100)}
        index_size = conn.execute("SELECT pg_size_pretty(pg_relation_size('mimic_raw.admissions_admittime_idx'))").fetchone()['pg_size_pretty']
        # Same page (rows 101-120) reached two ways, with the index present.
        anchor = conn.execute('SELECT admittime, hadm_id FROM query_api.encounter_overview '
                              'ORDER BY admittime, hadm_id LIMIT 1 OFFSET 99').fetchone()
        offset_plan = conn.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + PAGE_SQL % (20, 100)).fetchone()['QUERY PLAN'][0]
        keyset_sql = ("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT e.* FROM query_api.encounter_overview e "
                      f"WHERE (e.admittime,e.hadm_id) > (TIMESTAMP '{anchor['admittime']}', {anchor['hadm_id']}) "
                      "ORDER BY e.admittime,e.hadm_id LIMIT 20")
        keyset_plan = conn.execute(keyset_sql).fetchone()['QUERY PLAN'][0]

    result['plan_experiment'] = {
        'database': VERIFY_DB,
        'index_created': 'admissions_admittime_idx ON mimic_raw.admissions(admittime, hadm_id)',
        'index_size': index_size,
        'before': {str(o): summarize(plans_before[o]) for o in (0, 100)},
        'after': {str(o): summarize(plans_after[o]) for o in (0, 100)},
        'result_fingerprint_before': {str(o): fp_before[o] for o in (0, 100)},
        'result_fingerprint_after': {str(o): fp_after[o] for o in (0, 100)},
        'results_identical': fp_before == fp_after,
    }
    result['pagination_comparison'] = {
        'same_page': 'rows 101-120 (LIMIT 20 OFFSET 100)',
        'offset_plan': summarize(offset_plan),
        'keyset_plan': summarize(keyset_plan),
        'keyset_sql': 'WHERE (admittime,hadm_id) > (last row of previous page) ORDER BY admittime,hadm_id LIMIT 20',
        'note': 'Keyset pagination avoids OFFSET skipping; it changes the query shape, so the fixed API template '
                'would have to adopt it before it can be used in production.',
    }
    result['caveats'] = [
        'MIMIC Demo 2.2 is a small sample; absolute times are tiny and single-run.',
        'The index was created only in the isolated copy database; the project database was not modified.',
        'A rank by total_exec_time reflects workload cost, not a production slow-query diagnosis.',
    ]
    os.seteuid(0)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'completed_requests': completed, 'top_sql_groups': len(ranking),
                      'top1_ms': ranking[0]['total_exec_time'] if ranking else None,
                      'results_identical': result['plan_experiment']['results_identical'],
                      'before': result['plan_experiment']['before'],
                      'after': result['plan_experiment']['after'],
                      'pagination': result['pagination_comparison']}))


if __name__ == '__main__':
    main()
