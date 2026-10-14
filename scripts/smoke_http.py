"""Real local HTTP probe, run inside WSL or Windows; emits aggregate evidence."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from urllib.request import urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--base-url",default="http://127.0.0.1:8765")
    args = parser.parse_args()
    checks = []
    startup_attempts = []
    # WSL may have just booted. Wait at most 20s for app + database readiness.
    for attempt in range(20):
        try:
            with urlopen(args.base_url+"/health/ready",timeout=2) as response:
                assert response.status==200
                json.load(response)
            startup_attempts.append({"attempt":attempt+1,"status":"ready"})
            break
        except URLError as exc:
            startup_attempts.append({"attempt":attempt+1,"status":"not_ready","error":type(exc).__name__})
            time.sleep(1)
    else:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps({"status":"failed","phase":"startup","startup_attempts":startup_attempts},indent=2)+"\n")
        raise SystemExit("API did not become ready; inspect medical-query.service")
    for path in ["/health/live","/health/ready","/api/summary","/api/admissions?limit=1","/ops/snapshot","/openapi.json"]:
        started = time.perf_counter()
        with urlopen(args.base_url+path,timeout=5) as response:
            body = json.load(response)
            request_id = response.headers.get("X-Request-ID")
            assert response.status==200 and request_id
        if path=="/api/summary":
            assert body["result"]["admission_count"]==275
            assert body["result"]["patient_count"]==100
            assert body["result"]["admissions_with_icu"]==128
        if path=="/api/admissions?limit=1":
            assert len(body["result"]["rows"])==1 and body["result"]["total"]==275
        checks.append({"path":path,"status":200,"request_id":request_id,
                       "elapsed_ms":round((time.perf_counter()-started)*1000,3)})
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps({"status":"passed","observed_at_utc":datetime.now(timezone.utc).isoformat(),
                                      "base_url":args.base_url,"startup_attempts":startup_attempts,"checks":checks},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":"passed","http_checks":len(checks),"report":str(args.report)}))


if __name__=="__main__":
    main()
