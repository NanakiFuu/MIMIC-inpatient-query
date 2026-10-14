"""Real pg_dump/pg_restore into isolated temporary project DB; retain evidence."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"research/module3/backup-restore.json"
TARGET="medical_query_restore_check"
TABLES=["patients","admissions","d_icd_diagnoses","d_labitems","diagnoses_icd","labevents","icustays"]


def run(command,user="postgres"):
    result=subprocess.run(["/usr/sbin/runuser","-u",user,"--",*command],capture_output=True,text=True,cwd="/tmp")
    if result.returncode:
        raise RuntimeError("Backup/restore command failed: "+result.stderr[-1000:])
    return result.stdout.strip()


def sql(query,database="postgres"):
    return run(["psql","-X","-At","-v","ON_ERROR_STOP=1","-d",database,"-c",query])


def main():
    if os.getuid()!=0:
        raise SystemExit("Run as WSL root for temporary database creation")
    if OUT.exists() or sql(f"SELECT count(*) FROM pg_database WHERE datname='{TARGET}'")!="0":
        raise SystemExit("Refusing existing evidence or restore database")
    owner=os.environ.get('MEDICAL_QUERY_SERVICE_USER',os.environ.get('SUDO_USER','nanaki'))
    user=pwd.getpwnam(owner)
    backup_dir=Path(user.pw_dir)/'.local/state/medical-query/backups'
    run(["mkdir","-p",str(backup_dir)],user=owner)
    os.chown(backup_dir,user.pw_uid,user.pw_gid)
    os.chmod(backup_dir,0o700)
    dump=backup_dir/"mimic-demo-module3.dump"
    if dump.exists():
        raise SystemExit("Refusing existing dump file")
    started=time.perf_counter()
    run(["pg_dump","-Fc","--no-owner","--no-acl","-d","medical_query_demo","-f",str(dump)],user=owner)
    dump_seconds=time.perf_counter()-started
    os.chmod(dump,0o600)
    sql(f'CREATE DATABASE {TARGET} OWNER "'+owner.replace('"','""')+'"')
    sql(f"COMMENT ON DATABASE {TARGET} IS 'Project temporary backup verification; not application DB'")
    sql(f"REVOKE ALL ON DATABASE {TARGET} FROM PUBLIC")
    started=time.perf_counter()
    run(["pg_restore","--exit-on-error","--no-owner","--no-acl","-d",TARGET,str(dump)],user=owner)
    restore_seconds=time.perf_counter()-started
    checks={}
    for table in TABLES:
        query=f"SELECT count(*) FROM mimic_raw.{table}"
        original=int(sql(query,"medical_query_demo"))
        restored=int(sql(query,TARGET))
        checks[table]={"original":original,"restored":restored,"equal":original==restored}
    fingerprint="SELECT md5(string_agg(row_to_json(e)::text,'|' ORDER BY hadm_id)) FROM query_api.encounter_overview e"
    source_fingerprint=sql(fingerprint,"medical_query_demo")
    restored_fingerprint=sql(fingerprint,TARGET)
    with dump.open("rb") as stream:
        sha=hashlib.file_digest(stream,"sha256").hexdigest()
    record={"source":"本项目真实pg_dump/pg_restore验证，数据为MIMIC Demo2.2",
        "observed_at_utc":datetime.now(timezone.utc).isoformat(),"backup_path":str(dump),
        "backup_bytes":dump.stat().st_size,"backup_sha256":sha,"dump_seconds":round(dump_seconds,4),
        "restore_seconds":round(restore_seconds,4),"restore_database":TARGET,"checks":checks,
        "encounter_view_source_md5":source_fingerprint,"encounter_view_restored_md5":restored_fingerprint,
        "status":"passed" if all(item["equal"] for item in checks.values()) and source_fingerprint==restored_fingerprint else "failed",
        "limits":"数据备份恢复，不包含全局角色/密码/OS配置；不是集群容灾实验；临时库保留以便检查"}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({key:record[key] for key in ["status","backup_bytes","dump_seconds","restore_seconds","restore_database"]}))


if __name__=="__main__":
    main()
