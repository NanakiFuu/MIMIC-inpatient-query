"""Actual load and pg_stat_statements delta; index contrast on an isolated restored copy."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

import psycopg
from psycopg.rows import dict_row

from backup_database import RESTORE,MARKER

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'evidence/p1/database/statistics.json'
SQL="""SELECT labevent_id,charttime,valuenum,valueuom,ref_range_lower,ref_range_upper
FROM query_api.lab_observation WHERE hadm_id=%s AND itemid=%s AND fluid='Blood'
ORDER BY charttime,labevent_id LIMIT 200"""


def stats(connection,database):
    # Only code-owned query text is recorded; parameters/clinical rows are excluded.
    rows=connection.execute("""SELECT queryid::text,query,calls,total_exec_time,rows,shared_blks_hit,shared_blks_read
        FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=%s)
        AND query LIKE 'SELECT labevent_id,charttime%%'""",(database,)).fetchall()
    return {r['queryid']:dict(r) for r in rows}


def workload(connection,parameters,repetitions=150):
    fingerprint=None
    for _ in range(repetitions):
        rows=connection.execute(SQL,parameters).fetchall()
        value=hashlib.sha256(json.dumps(rows,default=str,sort_keys=True).encode()).hexdigest()
        if fingerprint is None:
            fingerprint=value
        assert value==fingerprint
    return {'calls':repetitions,'result_rows':len(rows),'fingerprint':fingerprint}


def delta(before,after):
    result=[]
    for key,row in after.items():
        old=before.get(key,{})
        change={k:row[k]-old.get(k,0) for k in ['calls','total_exec_time','rows','shared_blks_hit','shared_blks_read']}
        if change['calls']>0:
            result.append({'queryid':key,'normalized_sql':row['query'],**change,
                           'mean_exec_ms':change['total_exec_time']/change['calls']})
    return sorted(result,key=lambda r:r['total_exec_time'],reverse=True)


def explain(connection,parameters):
    runs=[connection.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+SQL,parameters).fetchone()['QUERY PLAN'][0] for _ in range(5)]
    return {'runs':runs,'median_execution_ms':statistics.median(r['Execution Time'] for r in runs)}


def main():
    if os.getuid()!=0 or OUTPUT.exists():
        raise SystemExit('Root required; evidence must not exist')
    user=os.environ.get('MEDICAL_QUERY_SERVICE_USER','nanaki')
    import pwd
    config=json.loads((Path(pwd.getpwnam(user).pw_dir)/'.config/medical-query/database.json').read_text())
    postgres=pwd.getpwnam('postgres')
    os.setegid(postgres.pw_gid)
    os.seteuid(postgres.pw_uid)
    admin=psycopg.connect(dbname='medical_query_demo',user='postgres',host='/var/run/postgresql',row_factory=dict_row,autocommit=True)
    with admin:
        # Credentials are loaded locally and never recorded.
        with psycopg.connect(**config,row_factory=dict_row) as reader:
            target=reader.execute("SELECT hadm_id FROM query_api.lab_observation WHERE hadm_id IS NOT NULL AND itemid=50912 ORDER BY hadm_id LIMIT 1").fetchone()['hadm_id']
            before=stats(admin,'medical_query_demo')
            live_workload=workload(reader,(target,50912))
            after=stats(admin,'medical_query_demo')
        marker=admin.execute("SELECT shobj_description(oid,'pg_database') AS marker FROM pg_database WHERE datname=%s",(RESTORE,)).fetchone()['marker']
        assert marker==MARKER
        # The existing isolated automated restore copy contains the same real data.
        with psycopg.connect(dbname=RESTORE,user='postgres',host='/var/run/postgresql',row_factory=dict_row,autocommit=True) as isolated:
            isolated.execute('CREATE EXTENSION IF NOT EXISTS pg_stat_statements')
            baseline_rows=isolated.execute(SQL,(target,50912)).fetchall()
            initial_stats=isolated.execute("SELECT relname,n_live_tup,n_dead_tup,last_vacuum,last_analyze FROM pg_stat_user_tables WHERE schemaname='mimic_raw'").fetchall()
            original_definition=isolated.execute("SELECT pg_get_indexdef('mimic_raw.labs_encounter_item_time_idx'::regclass) AS definition").fetchone()['definition']
            # All index changes roll back at transaction end, even on error.
            isolated.execute('BEGIN')
            try:
                isolated.execute('SET LOCAL lock_timeout=\'3s\'')
                isolated.execute('DROP INDEX mimic_raw.labs_encounter_item_time_idx')
                pre=stats(admin,RESTORE)
                unindexed_load=workload(isolated,(target,50912))
                post=stats(admin,RESTORE)
                unindexed=explain(isolated,(target,50912))
                isolated.execute(original_definition)
                pre_index=stats(admin,RESTORE)
                indexed_load=workload(isolated,(target,50912))
                post_index=stats(admin,RESTORE)
                indexed=explain(isolated,(target,50912))
                assert baseline_rows==isolated.execute(SQL,(target,50912)).fetchall()
            finally:
                isolated.execute('ROLLBACK')
            vacuum_before=explain(isolated,(target,50912))
            vacuum_start=time.perf_counter()
            isolated.execute('VACUUM (ANALYZE) mimic_raw.labevents')
            vacuum_seconds=time.perf_counter()-vacuum_start
            vacuum_after=explain(isolated,(target,50912))
            final_stats=isolated.execute("SELECT relname,n_live_tup,n_dead_tup,last_vacuum,last_analyze FROM pg_stat_user_tables WHERE schemaname='mimic_raw'").fetchall()
        result={'source':'Real MIMIC Demo2.2 query workload; no generated clinical rows',
                'observed_at_utc':datetime.now(timezone.utc).isoformat(),
                'scope':'Single WSL; application reader workload on main project DB; controlled index contrast and VACUUM on isolated restored copy',
                'method':'150 calls executed per stage; stats observed 149 per stage at sampling boundary; fixed query, warm cache; no global reset; one case, no capacity claim',
                'sql':SQL,'live_workload':live_workload,'live_top_sql':delta(before,after),
                'isolated_without_index':{'load':unindexed_load,'top_sql':delta(pre,post),'explain':unindexed},
                'isolated_with_index':{'load':indexed_load,'top_sql':delta(pre_index,post_index),'explain':indexed},
                'index_definition':original_definition,'index_restored':True,
                'vacuum':{'elapsed_seconds':vacuum_seconds,'statistics_before':initial_stats,'statistics_after':final_stats,
                          'explain_before':vacuum_before,'explain_after':vacuum_after},
                'same_results':unindexed_load['fingerprint']==indexed_load['fingerprint']==live_workload['fingerprint']}
        os.seteuid(0)
        OUTPUT.parent.mkdir(parents=True,exist_ok=True)
        OUTPUT.write_text(json.dumps(result,default=str,indent=2),encoding='utf-8')
        print(json.dumps({'same_results':result['same_results'],'unindexed_ms':unindexed['median_execution_ms'],
                          'indexed_ms':indexed['median_execution_ms'],'vacuum_before_ms':vacuum_before['median_execution_ms'],
                          'vacuum_after_ms':vacuum_after['median_execution_ms']}))


if __name__=='__main__':
    main()
