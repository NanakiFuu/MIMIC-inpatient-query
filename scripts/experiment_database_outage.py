"""Real project PostgreSQL outage/recovery, always attempt restoration in finally."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"research/module3/database-outage.json"


def http(path,method="GET"):
    started=time.perf_counter()
    request=Request("http://127.0.0.1:8765"+path,method=method)
    try:
        with urlopen(request,timeout=35) as response:
            status=response.status
            body=json.load(response)
    except HTTPError as error:
        status=error.code
        body=json.loads(error.read())
    return {"path":path,"method":method,"status":status,"body":body,
            "elapsed_ms":round((time.perf_counter()-started)*1000,3),
            "observed_at_utc":datetime.now(timezone.utc).isoformat()}


def main():
    if os.getuid()!=0:
        raise SystemExit("Run as WSL root to control only postgresql@16-main")
    if OUT.exists():
        raise SystemExit("Experiment already recorded; do not overwrite")
    listing=subprocess.run(["/usr/sbin/runuser","-u","postgres","--","psql","-X","-At","-d","postgres",
                            "-c","SELECT datname FROM pg_database ORDER BY datname"],capture_output=True,text=True,check=True,cwd="/tmp")
    if set(listing.stdout.splitlines())!={"postgres","template0","template1","medical_query_demo"}:
        raise SystemExit("Unexpected database in cluster; refusing outage experiment")
    subprocess.run(["systemctl","is-active","postgresql@16-main"],check=True)
    record={"source":"本项目真实故障操作与HTTP观测，非MIMIC故障数据",
            "scope":"WSL PostgreSQL16/main only","events":[],"checks":[]}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    def save():
        OUT.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    def command(action):
        started=time.perf_counter()
        result=subprocess.run(["systemctl",action,"postgresql@16-main"],capture_output=True,text=True)
        record["events"].append({"command":["systemctl",action,"postgresql@16-main"],"exit_code":result.returncode,
                    "elapsed_ms":round((time.perf_counter()-started)*1000,3),"observed_at_utc":datetime.now(timezone.utc).isoformat()})
        save()
        if result.returncode:
            raise RuntimeError("Project PostgreSQL service operation failed")
    before=http("/api/summary")
    record["checks"].append(before)
    if before["status"]!=200 or before["body"]["result"]["admission_count"]!=275:
        raise SystemExit("Precondition query failed; no service stopped")
    save()
    try:
        command("stop")
        for path,expected in [("/health/live",200),("/health/ready",503),("/api/summary",503),("/ops/snapshot",200)]:
            check=http(path)
            record["checks"].append(check)
            save()
            if check["status"]!=expected:
                raise RuntimeError(f"Unexpected outage status for {path}")
        check=http("/ops/diagnose","POST")
        record["checks"].append(check)
        # A model failure remains evidence, never prevents database restoration.
        record["ai_diagnosis_passed"]=(check["status"]==200 and check["body"]["diagnosis"]["finding"]=="database_unreachable")
        save()
    finally:
        recovery_started=time.perf_counter()
        command("start")
        after=None
        for attempt in range(10):
            after=http("/api/summary")
            if after["status"]==200:
                break
            time.sleep(.5)
        record["checks"].append(after)
        record["recovery_from_start_to_success_ms"]=round((time.perf_counter()-recovery_started)*1000,3)
        record["query_result_restored"]=(after["status"]==200 and after["body"]["result"]==before["body"]["result"])
        record["status"]="passed" if record["query_result_restored"] and record.get("ai_diagnosis_passed") else "incomplete"
        save()
        subprocess.run(["systemctl","is-active","postgresql@16-main"],check=True)
    print(json.dumps({"status":record["status"],"query_result_restored":record["query_result_restored"],
                      "ai_diagnosis_passed":record["ai_diagnosis_passed"],
                      "recovery_from_start_to_success_ms":record["recovery_from_start_to_success_ms"]}))


if __name__=="__main__":
    main()
