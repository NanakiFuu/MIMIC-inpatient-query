"""Live API evaluation of frozen cases; saves failures, actual tokens, no keys."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--split",choices=["development","holdout","all"],default="all")
    args=parser.parse_args()
    if args.report.exists():
        raise SystemExit("Do not overwrite a real evaluation record; choose a new report filename")
    case_path=ROOT/"research/module2/query-cases-v1.json"
    suite=json.loads(case_path.read_text())
    cases=[case for case in suite["cases"] if args.split=="all" or case["split"]==args.split]
    record={"source":"本项目真实模型/API运行实测；测试问题为自编，查询数据为MIMIC Demo 2.2",
            "started_at_utc":datetime.now(timezone.utc).isoformat(),"case_sha256":hashlib.sha256(case_path.read_bytes()).hexdigest(),
            "split":args.split,"cases":[],"summary":{}}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    def save():
        args.report.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    save()
    for case in cases:
        started=time.perf_counter()
        request=Request("http://127.0.0.1:8765/api/query",data=json.dumps({"question":case["question"]}).encode(),
                        headers={"Content-Type":"application/json"},method="POST")
        try:
            try:
                with urlopen(request,timeout=35) as response:
                    body=json.load(response)
                    status=response.status
            except HTTPError as error:
                body=json.loads(error.read())
                status=error.code
            projection=case["expected_projection"] or {}
            actual_plan=body.get("plan",{})
            if projection.get("http_status"):
                correct=status==projection["http_status"] and body.get("error")=="admission_not_found"
                # For 404, the API hides plan details; classification isn't observable.
                plan_correct=None
            else:
                plan_correct=actual_plan.get("tool")==case["expected_tool"] and all(actual_plan.get("arguments",{}).get(k)==v for k,v in case["expected_arguments"].items())
                data=body.get("result",{}).get("data")
                projection_correct=True
                for key,value in projection.items():
                    actual=len(data) if key=="row_count" and isinstance(data,list) else data.get(key) if isinstance(data,dict) else None
                    projection_correct=projection_correct and actual==value
                correct=status==200 and bool(plan_correct) and projection_correct
            item={"id":case["id"],"split":case["split"],"category":case["category"],
                  "http_status":status,"correct":correct,"plan_correct":plan_correct,
                  "elapsed_ms":round((time.perf_counter()-started)*1000,3),"request_id":body.get("request_id"),
                  "actual_plan":actual_plan,"error":body.get("error"),"model_error_evidence":body.get("model_evidence"),
                  "result_status":body.get("result",{}).get("status")}
        except (URLError,TimeoutError,ValueError) as error:
            item={"id":case["id"],"split":case["split"],"category":case["category"],"correct":False,
                  "plan_correct":False,"error":type(error).__name__,"elapsed_ms":round((time.perf_counter()-started)*1000,3)}
        record["cases"].append(item)
        completed=record["cases"]
        record["summary"]={"planned":len(cases),"completed":len(completed),"correct":sum(row["correct"] for row in completed),
                           "failures":sum(not row["correct"] for row in completed),
                           "reported_tokens":sum((row.get("actual_plan",{}).get("model_evidence") or row.get("model_error_evidence") or {}).get("usage",{}).get("total_tokens",0) for row in completed)}
        save()
        print(json.dumps({"case":case["id"],"correct":item["correct"],"error":item.get("error")}),flush=True)
        if item.get("error") in {"model_not_configured","model_authentication_failed","model_balance_insufficient"}:
            print("Stopping evaluation: local key/provider account action needed",flush=True)
            break
    record["finished_at_utc"]=datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps(record["summary"]))


if __name__=="__main__":
    main()
