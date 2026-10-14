"""Seed the independent Compose DB, then restore the least-privilege reader.

Two import modes:
  csv  (default for external reproduction) - import the official MIMIC Demo CSVs
       directly into the container database. Requires only the downloaded data
       directory; no local dump and no private environment.
  dump - import a snapshot of the local systemd project database. Convenient on
       this machine but depends on local private state.

Both modes create the container reader role and apply identical grants, so the
application's least-privilege behaviour is the same either way.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psycopg
from prepare_compose import ROOT, compose, ENV_FILE, PRIVATE_DIR


def read_env():
    return dict(line.split('=',1) for line in ENV_FILE.read_text().splitlines() if line and not line.startswith('#'))


def connect(values, port):
    return psycopg.connect(host='127.0.0.1', port=port, dbname='medical_query_demo',
                           user='postgres', password=values['POSTGRES_PASSWORD'])


def wait_for_db(values, timeout=150):
    """Host-side connections are independent of Compose health ordering, so wait
    until the container database actually accepts connections before proceeding."""
    port=int(values.get('MEDICAL_QUERY_POSTGRES_PORT','15432'))
    deadline=time.monotonic()+timeout
    last=None
    while time.monotonic()<deadline:
        try:
            with connect(values, port):
                return
        except psycopg.OperationalError as error:
            last=error
            time.sleep(2)
    raise SystemExit(f'Container database not ready within {timeout}s: {last}')


def ensure_reader_role(conn, password):
    database_marker=conn.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()[0]
    raw_schema=conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname='mimic_raw'").fetchone()[0]
    if raw_schema and database_marker!='medical-query compose database v1':
        raise SystemExit('Existing container dataset lacks project marker; refusing replacement')
    exists=conn.execute("SELECT count(*) FROM pg_roles WHERE rolname='medical_query_reader'").fetchone()[0]
    if exists:
        marker=conn.execute("SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname='medical_query_reader'").fetchone()[0]
        if marker!='medical-query compose reader v1':
            raise SystemExit('Unrecognized container reader role')
        # Keep the role password in step with the env file to avoid drift.
        conn.execute("SELECT set_config('medical_query.reader_password',%s,false)",(password,))
        conn.execute("DO $$ BEGIN EXECUTE format('ALTER ROLE medical_query_reader PASSWORD %L',current_setting('medical_query.reader_password')); END $$")
    else:
        # Parameterized server-side function avoids logging password SQL.
        conn.execute("SELECT set_config('medical_query.reader_password',%s,false)",(password,))
        conn.execute("DO $$ BEGIN EXECUTE format('CREATE ROLE medical_query_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',current_setting('medical_query.reader_password')); END $$")
        conn.execute("COMMENT ON ROLE medical_query_reader IS 'medical-query compose reader v1'")
    conn.execute('REVOKE ALL ON DATABASE medical_query_demo FROM PUBLIC')
    conn.execute('GRANT CONNECT ON DATABASE medical_query_demo TO medical_query_reader')
    conn.execute("ALTER ROLE medical_query_reader SET default_transaction_read_only='on'")


def apply_grants(conn):
    """Applied after the data exists so the schema is present to lock down."""
    conn.execute("COMMENT ON DATABASE medical_query_demo IS 'medical-query compose database v1'")
    conn.execute('REVOKE ALL ON SCHEMA mimic_raw,project_audit FROM PUBLIC,medical_query_reader')
    conn.execute('REVOKE ALL ON ALL TABLES IN SCHEMA mimic_raw,project_audit FROM PUBLIC,medical_query_reader')
    conn.execute('REVOKE ALL ON SCHEMA query_api FROM PUBLIC')
    conn.execute('GRANT USAGE ON SCHEMA query_api TO medical_query_reader')
    conn.execute('GRANT SELECT ON query_api.encounter_overview,query_api.diagnosis_listing,query_api.lab_observation TO medical_query_reader')


def import_from_csv(values, port, source):
    if not (source/'SHA256SUMS.txt').exists():
        raise SystemExit(f'Official data directory not found: {source}. Run scripts/download_mimic.py first.')
    env=os.environ.copy()
    env.update({'MEDICAL_QUERY_DB_HOST':'127.0.0.1','MEDICAL_QUERY_DB_PORT':str(port),
                'MEDICAL_QUERY_DB_NAME':'medical_query_demo','MEDICAL_QUERY_DB_USER':'postgres',
                'MEDICAL_QUERY_DB_PASSWORD':values['POSTGRES_PASSWORD']})
    report=ROOT/'evidence/p1/docker/container-import-from-source.json'
    report.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run([sys.executable,str(ROOT/'scripts/import_mimic.py'),'--source',str(source),'--report',str(report)],
                   check=True,env=env,cwd='/tmp')
    return {'mode':'csv','source':str(source),'report':str(report)}


def import_from_dump(values, port, user):
    PRIVATE_DIR.mkdir(parents=True,exist_ok=True,mode=0o700)
    dump=PRIVATE_DIR/'systemd-seed.dump'
    # Source dump as its owner, using peer authentication and no password output.
    with dump.open('wb') as stream:
        subprocess.run(['runuser','-u',user,'--','pg_dump','-Fc','-d','medical_query_demo'],stdout=stream,check=True,cwd='/tmp')
    os.chmod(dump,0o600)
    compose('stop','app',stdout=subprocess.DEVNULL)
    with dump.open('rb') as stream:
        compose('exec','-T','db','pg_restore','--exit-on-error','--clean','--if-exists','--no-owner','--no-acl','-U','postgres','-d','medical_query_demo',stdin=stream,stdout=subprocess.DEVNULL)
    return {'mode':'dump','source':'Actual systemd project DB snapshot backed by verified MIMIC Demo2.2',
            'separate_container_volume':True,'bytes':dump.stat().st_size,
            'sha256':hashlib.file_digest(dump.open('rb'),'sha256').hexdigest()}


def main():
    if os.getuid()!=0:
        raise SystemExit('Run with root for Docker Engine access')
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['csv','dump'],default=os.environ.get('MEDICAL_QUERY_IMPORT_MODE','csv'))
    parser.add_argument('--source',type=Path,default=Path(os.environ.get('MEDICAL_QUERY_DATA_DIR',str(ROOT/'mimic-iv-clinical-database-demo-2.2'))))
    args=parser.parse_args()
    values=read_env()
    port=int(values.get('MEDICAL_QUERY_POSTGRES_PORT','15432'))
    compose('up','--build','-d',stdout=subprocess.DEVNULL)
    wait_for_db(values)
    with connect(values,port) as conn:
        ensure_reader_role(conn,values['MEDICAL_QUERY_DB_PASSWORD'])
    if args.mode=='csv':
        detail=import_from_csv(values,port,args.source)
    else:
        user=os.environ.get('MEDICAL_QUERY_SERVICE_USER','nanaki')
        detail=import_from_dump(values,port,user)
    with connect(values,port) as grants:
        apply_grants(grants)
    compose('start','app',stdout=subprocess.DEVNULL)
    output=ROOT/'evidence/p1/docker/seed.json'
    output.write_text(json.dumps({'import_mode':args.mode,**detail,
                                  'reader_role':'medical_query_reader','grants':'three query_api views only'},
                                 indent=2),encoding='utf-8')
    print(f'Compose project DB imported ({args.mode}); no source database modified')


if __name__=='__main__':
    main()
