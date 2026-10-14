"""Small real localhost fixed-API load, no model calls or synthetic clinical rows."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
import math
from pathlib import Path
import resource
import statistics
import subprocess
import time
from urllib.request import urlopen
from urllib.error import URLError,HTTPError

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"research/module3/api-load.json"


def service_resources():
    result=subprocess.run(["systemctl","show","medical-query.service","-p","MemoryCurrent",
                           "-p","CPUUsageNSec","-p","TasksCurrent"],capture_output=True,text=True,check=True)
    values={}
    for line in result.stdout.splitlines():
        key,value=line.split("=",1)
        values[key]=int(value) if value.isdigit() else value
    return values


def request(index):
    # Test traffic, real MIMIC-backed fixed query paths. AI path is excluded.
    path=["/api/summary","/api/admissions?limit=20","/api/summary?has_icu=true"][index%3]
    started=time.perf_counter()
    try:
        with urlopen("http://127.0.0.1:8765"+path,timeout=10) as response:
            body=json.load(response)
            correct=response.status==200 and (body["result"].get("admission_count") in (275,128) if path.startswith("/api/summary") else body["result"]["total"]==275)
            status=response.status
    except (URLError,HTTPError,TimeoutError):
        correct,status=False,0
    return {"path":path,"status":status,"correct":correct,"elapsed_ms":round((time.perf_counter()-started)*1000,3)}


def main():
    if OUT.exists():
        raise SystemExit("Do not overwrite recorded experiment")
    record={"source":"本项目真实固定API请求；请求流量为测试生成，临床记录来自MIMIC Demo2.2",
            "observed_at_utc":datetime.now(timezone.utc).isoformat(),
            "location":"客户端及服务端均在同一WSL2实例；单Uvicorn进程；真实数据，无AI请求",
            "scope":"短时小规模观测，不是容量上限、真实用户并发或大规模系统结论","runs":[]}
    for concurrency in (1,4,8):
        for index in range(6):
            assert request(index)["correct"]
        before=resource.getrusage(resource.RUSAGE_SELF)
        server_before=service_resources()
        started=time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            rows=list(executor.map(request,range(120)))
        elapsed=time.perf_counter()-started
        after=resource.getrusage(resource.RUSAGE_SELF)
        server_after=service_resources()
        latencies=sorted(row["elapsed_ms"] for row in rows)
        failed=sum(not row["correct"] for row in rows)
        record["runs"].append({"concurrency":concurrency,"requests":len(rows),"elapsed_seconds":round(elapsed,4),
          "requests_per_second":round(len(rows)/elapsed,3),"p50_ms":statistics.median(latencies),
          "p95_ms":latencies[math.ceil(len(latencies)*.95)-1],"failed":failed,"error_rate":failed/len(rows),
          "client_cpu_seconds":round((after.ru_utime+after.ru_stime)-(before.ru_utime+before.ru_stime),4),
          "client_maxrss_kib":after.ru_maxrss,"service_resources_before":server_before,
          "service_resources_after":server_after,"samples":rows})
        print(json.dumps({key:record["runs"][-1][key] for key in ["concurrency","requests_per_second","p50_ms","p95_ms","failed"]}),flush=True)
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
