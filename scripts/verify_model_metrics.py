"""Two real bounded provider requests; record stages/outcomes without inputs or key."""
import json
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]


def call(path,body):
    request=Request('http://127.0.0.1:8765'+path,data=json.dumps(body).encode(),
                    headers={'Content-Type':'application/json'},method='POST')
    try:
        with urlopen(request,timeout=35) as response:
            return response.status,json.load(response)
    except HTTPError as error:
        return error.code,json.load(error)


def main():
    output=ROOT/'evidence/p1/model-metrics.json'
    if output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    query_status,query=call('/api/query',{'question':'统计整个Demo样本的住院次数'})
    diagnosis_status,diagnosis=call('/ops/diagnose',{})
    with urlopen('http://127.0.0.1:8765/metrics',timeout=8) as response:
        text=response.read().decode()
    lines=[line for line in text.splitlines() if line.startswith(('medical_query_model_calls_total','medical_query_model_duration_seconds_count','medical_query_tool_results_total'))]
    judged=diagnosis.get('diagnosis',{})
    result={'scope':'Two real DeepSeek requests, fixed Demo-wide query and runtime diagnosis; no patient question/data sent',
            'query_status':query_status,'diagnosis_status':diagnosis_status,
            'query_tool':query.get('plan',{}).get('tool'),'query_correct':query.get('result',{}).get('data',{}).get('admission_count')==275,
            'query_model_evidence':query.get('plan',{}).get('model_evidence'),
            'diagnosis_model_original_judgment':judged.get('model_original_judgment'),
            'diagnosis_rule_checked_finding':judged.get('finding'),'consistency_check_passed':judged.get('consistency_check_passed'),
            'model_evidence':judged.get('model_evidence'),'metrics':lines,
            'passed':query_status==diagnosis_status==200 and bool(judged.get('model_original_judgment'))}
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'query_status':query_status,'diagnosis_status':diagnosis_status}))
    assert result['passed'] and result['query_correct']


if __name__=='__main__':
    main()
