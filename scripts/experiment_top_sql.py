"""Read-only fixed API mixed workload; actual pg_stat_statements delta ranking."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import time
from urllib.request import urlopen
import psycopg
from psycopg.rows import dict_row

ROOT=Path(__file__).resolve().parents[1]


def stats(connection):
    rows=connection.execute("""SELECT queryid::text,query,calls,total_exec_time,rows,shared_blks_hit,shared_blks_read
       FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname='medical_query_demo')
       AND userid=(SELECT oid FROM pg_roles WHERE rolname='medical_query_reader') AND query LIKE 'SELECT%%'""").fetchall()
    return {r['queryid']:dict(r) for r in rows}


def main():
    output=ROOT/'evidence/p1/database/top-sql.json'
    if os.getuid()!=0 or output.exists():
        raise SystemExit('Root required; evidence must not exist')
    account=pwd.getpwnam('postgres')
    os.setegid(account.pw_gid)
    os.seteuid(account.pw_uid)
    with psycopg.connect(dbname='medical_query_demo',user='postgres',host='/var/run/postgresql',row_factory=dict_row,autocommit=True) as connection:
        with urlopen('http://127.0.0.1:8765/api/admissions?limit=1',timeout=5) as response:
            target=json.load(response)['result']['rows'][0]['hadm_id']
        paths=['/api/summary','/api/summary?has_icu=true','/api/admissions?limit=20',
               f'/api/admissions/{target}/diagnoses',f'/api/admissions/{target}/labs?concept=creatinine']
        before=stats(connection)
        start=time.time()
        completed=0
        for _ in range(40):
            for path in paths:
                with urlopen('http://127.0.0.1:8765'+path,timeout=8) as response:
                    assert response.status==200
                    json.load(response)
                    completed+=1
        after=stats(connection)
    changes=[]
    for key,row in after.items():
        old=before.get(key,{})
        values={k:row[k]-old.get(k,0) for k in ['calls','total_exec_time','rows','shared_blks_hit','shared_blks_read']}
        if values['calls']>0:
            changes.append({'normalized_sql':row['query'],'queryid':key,**values,'mean_exec_ms':values['total_exec_time']/values['calls']})
    changes.sort(key=lambda r:r['total_exec_time'],reverse=True)
    result={'scope':'Single WSL systemd fixed API mixed test workload; MIMIC Demo2.2; generated 200 requests; no AI',
            'start_epoch':start,'end_epoch':time.time(),'completed_requests':completed,'top_by_total_exec_time':changes,
            'method':'Delta of project reader SQL only, no reset; monitor readiness probes may also appear; no clinical parameters stored',
            'observation':'Query ranking identifies workload cost, not necessarily slow SQL; Demo is small; controlled lab index contrast is a separate isolated experiment'}
    os.seteuid(0)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'completed_requests':completed,'sql_groups':len(changes),'largest_total_exec_ms':changes[0]['total_exec_time']}))


if __name__=='__main__':
    main()
