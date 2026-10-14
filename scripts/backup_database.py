"""Snapshot-consistent project backup, bounded retention, isolated restore verification.

Backup runs as the database owner. Restore orchestration requires root and only
touches a database bearing the exact project-owned verification marker.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import time

import psycopg

DATABASE = 'medical_query_demo'
RESTORE = 'medical_query_backup_verify'
MARKER = 'medical-query isolated automated restore verification v1'
TABLES = ['patients','admissions','d_icd_diagnoses','d_labitems','diagnoses_icd','labevents','icustays']
FINGERPRINT = "SELECT md5(string_agg(row_to_json(v)::text, E'\\n' ORDER BY hadm_id)) FROM query_api.encounter_overview v"


def checks(connection):
    return {'tables': {table:connection.execute(f'SELECT count(*) FROM mimic_raw.{table}').fetchone()[0] for table in TABLES},
            'encounter_rows':connection.execute('SELECT count(*) FROM query_api.encounter_overview').fetchone()[0],
            'encounter_fingerprint':connection.execute(FINGERPRINT).fetchone()[0]}


def admin(sql):
    result = subprocess.run(['runuser','-u','postgres','--','psql','-X','-v','ON_ERROR_STOP=1','-At','-d','postgres'],
                            input=sql, capture_output=True, text=True, cwd='/tmp')
    if result.returncode:
        raise RuntimeError('Isolated restore administration failed')
    return result.stdout.strip()


def backup(retain):
    directory = Path.home() / '.local/state/medical-query/backups'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    with (directory / 'backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        final = directory / f'medical-query-{stamp}.dump'
        temporary = final.with_suffix('.partial')
        started = time.perf_counter()
        with psycopg.connect(dbname=DATABASE, host='/var/run/postgresql', user=getpass.getuser()) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            snapshot = connection.execute('SELECT pg_export_snapshot()').fetchone()[0]
            expected = checks(connection)
            subprocess.run(['pg_dump','-Fc','--snapshot',snapshot,'-d',DATABASE,'-f',str(temporary)],check=True)
        os.chmod(temporary, 0o600)
        temporary.rename(final)
        manifest = {'scope':'Single-host logical backup of project DB; original data unchanged',
                    'backup_utc':stamp,'bytes':final.stat().st_size,'sha256':hashlib.file_digest(final.open('rb'),'sha256').hexdigest(),
                    'dump_seconds':round(time.perf_counter()-started,4),'snapshot_checks':expected,'dump':str(final)}
        final.with_suffix('.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        os.chmod(final.with_suffix('.json'),0o600)
        # Only this script's completed backup prefix, never older experiment dumps.
        expired = sorted(directory.glob('medical-query-*.dump'),reverse=True)[retain:]
        for path in expired:
            if path.is_symlink() or path.resolve().parent != directory.resolve():
                raise RuntimeError('Unsafe retention target')
            path.unlink()
            path.with_suffix('.json').unlink(missing_ok=True)
        manifest['expired_backups_removed'] = len(expired)
        manifest['retention_count'] = retain
        return manifest


def verify(user, retain):
    if os.getuid() != 0:
        raise SystemExit('Restore verification requires root')
    result = subprocess.run(['runuser','-u',user,'--',str(Path(pwd.getpwnam(user).pw_dir)/'environments/medical-query/.venv/bin/python'),
                             str(Path(__file__).resolve()),'--mode','backup','--retain',str(retain)],check=True,capture_output=True,text=True)
    manifest = json.loads(result.stdout)
    dump = Path(manifest.pop('dump'))
    directory = Path(pwd.getpwnam(user).pw_dir)/'.local/state/medical-query/backups'
    if dump.is_symlink() or dump.resolve().parent != directory.resolve() or not dump.name.startswith('medical-query-'):
        raise RuntimeError('Invalid project backup path')
    current = admin(f"SELECT coalesce(shobj_description(oid,'pg_database'),'') FROM pg_database WHERE datname='{RESTORE}'")
    exists = admin(f"SELECT count(*) FROM pg_database WHERE datname='{RESTORE}'") == '1'
    if exists and current != MARKER:
        raise RuntimeError('Existing restore DB lacks project marker; refusing change')
    if not exists:
        admin(f"CREATE DATABASE {RESTORE}; COMMENT ON DATABASE {RESTORE} IS '{MARKER}';")
    started = time.perf_counter()
    with dump.open('rb') as stream:
        subprocess.run(['runuser','-u','postgres','--','pg_restore','--exit-on-error','--clean','--if-exists','--no-owner','-d',RESTORE],
                       stdin=stream,check=True,cwd='/tmp',capture_output=True)
    # CLI return values contain only aggregate counts/fingerprint, no clinical rows.
    query = "SELECT json_build_object('tables', json_build_object(" + ','.join(f"'{t}',(SELECT count(*) FROM mimic_raw.{t})" for t in TABLES) + "), 'encounter_rows',(SELECT count(*) FROM query_api.encounter_overview), 'encounter_fingerprint',("+FINGERPRINT+"));"
    output = subprocess.run(['runuser','-u','postgres','--','psql','-X','-v','ON_ERROR_STOP=1','-At','-d',RESTORE],
                            input=query,capture_output=True,text=True,check=True,cwd='/tmp')
    actual = json.loads(output.stdout)
    manifest.update({'restore_database':RESTORE,'restore_seconds':round(time.perf_counter()-started,4),
                     'restored_checks':actual,'restore_matches_snapshot':actual==manifest['snapshot_checks']})
    if not manifest['restore_matches_snapshot']:
        raise RuntimeError('Restored backup failed snapshot validation')
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['backup','verify'],default='backup')
    parser.add_argument('--user',default=os.environ.get('MEDICAL_QUERY_SERVICE_USER','nanaki'))
    parser.add_argument('--retain',type=int,default=7)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if not 2 <= args.retain <= 30:
        raise SystemExit('Retention must be 2..30')
    os.umask(0o077)
    result=backup(args.retain) if args.mode=='backup' else verify(args.user,args.retain)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        with args.report.open('x',encoding='utf-8') as stream:
            json.dump({k:v for k,v in result.items() if k!='dump'},stream,indent=2)
    print(json.dumps(result))


if __name__=='__main__':
    main()
