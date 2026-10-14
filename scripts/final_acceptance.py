"""Final real deployed acceptance; no credential contents, no fabricated metrics."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"research/module3/final-acceptance.json"


def get(path,post=False,data=None):
    payload=json.dumps(data).encode() if data is not None else None
    request=Request("http://127.0.0.1:8765"+path,data=payload,method="POST" if post else "GET",
                    headers={"Content-Type":"application/json"} if payload else {})
    with urlopen(request,timeout=35) as response:
        assert response.status==200
        return json.load(response)


def main():
    source=json.loads((ROOT/"research/module1/import-final-result.json").read_text())
    verified=[]
    for entry in source["files"]:
        path=ROOT/"mimic-iv-clinical-database-demo-2.2"/entry["path"]
        with path.open("rb") as stream:
            checksum=hashlib.file_digest(stream,"sha256").hexdigest()
        assert checksum==entry["sha256"]
        verified.append({"path":entry["path"],"sha256":checksum})
    ready=get("/health/ready")
    query=get("/api/query",post=True,data={"question":"Demo一共有多少名患者、多少次住院？"})
    assert query["plan"]["tool"]=="summary"
    assert query["result"]["data"]["admission_count"]==275
    assert query["result"]["data"]["patient_count"]==100
    diagnosis=get("/ops/diagnose",post=True)
    assert diagnosis["diagnosis"]["finding"]=="service_healthy"
    assert diagnosis["diagnosis"]["consistency_check_passed"] is True
    status=get("/api/model/status")
    assert status["connection_verified_this_process"] is True
    assert status["metrics"]["calls"]>=2
    result={"status":"passed","observed_at_utc":datetime.now(timezone.utc).isoformat(),
            "source_files_verified":verified,"ready":ready,"real_model_query":query,
            "real_readonly_diagnosis":diagnosis,"model_status":status}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":"passed","verified_source_files":len(verified),"model":status["model"],
                      "model_calls_this_process":status["metrics"]["calls"],"diagnosis":diagnosis["diagnosis"]["finding"]}))


if __name__=="__main__":
    main()
