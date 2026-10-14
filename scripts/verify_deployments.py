"""Compare exact normalized fixed-query results from real local deployments."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.request import urlopen
from urllib.error import URLError


def get(base,path):
    with urlopen(base+path,timeout=8) as response:
        return response.status,json.load(response)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--first',default='http://127.0.0.1:8765')
    parser.add_argument('--second',default='http://127.0.0.1:18765')
    parser.add_argument('--report',type=Path,default=Path(__file__).resolve().parents[1]/'evidence/p1/docker/query-parity.json')
    args=parser.parse_args()
    if args.report.exists():
        raise SystemExit('Refusing to overwrite evidence')
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            if all(get(base,'/health/ready')[0]==200 for base in (args.first,args.second)):
                break
        except (URLError,TimeoutError):
            pass
        time.sleep(1)
    listing=get(args.first,'/api/admissions?limit=1')[1]['result']['rows'][0]
    hadm=listing['hadm_id']
    patient=listing['subject_id']
    paths=['/api/summary','/api/summary?has_icu=true','/api/admissions?limit=20&offset=10',
           f'/api/admissions?subject_id={patient}',f'/api/admissions/{hadm}',
           f'/api/admissions/{hadm}/diagnoses',f'/api/admissions/{hadm}/labs?concept=creatinine',
           f'/api/admissions/{hadm}/labs?concept=potassium&abnormal=source_flag',
           f'/api/admissions/{hadm}/labs?concept=glucose&abnormal=reference_range',
           f'/api/admissions/{hadm}/labs?concept=hemoglobin&limit=1&offset=1']
    checks=[]
    for path in paths:
        first=get(args.first,path)
        second=get(args.second,path)
        # Request UUIDs necessarily differ. Compare actual result/source/conditions/template.
        normalized=[{key:body[key] for key in ['source','result','conditions','template_id']} for _,body in [first,second]]
        fingerprints=[hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest() for body in normalized]
        equal=first[0]==second[0]==200 and fingerprints[0]==fingerprints[1]
        # Store templates and fingerprints only; patient/clinical payload not recorded.
        checks.append({'template':normalized[0]['template_id'],'statuses':[first[0],second[0]],
                       'fingerprints':fingerprints,'equal':equal})
    result={'scope':'Real systemd and Compose queries against same MIMIC Demo2.2 snapshot; UUIDs excluded; no AI calls',
            'deployment_ports':[args.first,args.second],'checks':checks,'passed':all(c['equal'] for c in checks)}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'fixed_query_checks':len(checks)}))
    assert result['passed']


if __name__=='__main__':
    main()
