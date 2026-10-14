"""One-minute fixed-query stages with client samples and concurrent Prometheus observations."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import math
from pathlib import Path
import statistics
import threading
import time

from experiment_monitored_outage import http, prometheus

ROOT=Path(__file__).resolve().parents[1]
QUERIES={
 'cpu_busy_ratio':'1 - avg(rate(node_cpu_seconds_total{job="node",mode="idle"}[30s]))',
 'logical_cpus':'count(node_cpu_seconds_total{job="node",mode="idle"})',
 'memory_available_bytes':'node_memory_MemAvailable_bytes{job="node"}',
 'memory_total_bytes':'node_memory_MemTotal_bytes{job="node"}',
 'root_disk_available_bytes':'node_filesystem_avail_bytes{job="node",mountpoint="/",fstype!~"tmpfs|overlay"}',
 'read_bytes_per_second':'rate(node_disk_read_bytes_total{job="node"}[30s])',
 'write_bytes_per_second':'rate(node_disk_written_bytes_total{job="node"}[30s])',
 'server_p95_seconds':'histogram_quantile(.95,sum by(le)(rate(medical_query_http_request_duration_seconds_bucket{route="/api/summary"}[30s])))',
 'api_error_ratio':'sum(rate(medical_query_http_requests_total{route="/api/summary",status=~"5.."}[30s]))/sum(rate(medical_query_http_requests_total{route="/api/summary"}[30s]))',
}


def main():
    directory=ROOT/'evidence/p0/monitoring'
    output=directory/'load.json'
    if output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    status,body,_=http('/api/summary')
    assert status==200
    expected=body['result']
    record={'scope':'Single WSL2 host; same-host client; generated test traffic; real MIMIC Demo 2.2 data; no AI calls',
            'method':'Closed-loop concurrency; each stage 60s; no claim of capacity limit; histogram p95 differs from exact client p95',
            'queries':QUERIES,'runs':[]}
    start=time.time()
    with gzip.open(directory/'load-requests.jsonl.gz','xt',encoding='utf-8') as stream:
        lock=threading.Lock()
        for concurrency in (1,4,8):
            end=time.monotonic()+60
            run_start=time.time()
            rows=[]
            def worker(index):
                while time.monotonic()<end:
                    status,body,elapsed=http('/api/summary')
                    row={'utc_epoch':time.time(),'concurrency':concurrency,'status':status,
                         'correct':status==200 and body.get('result')==expected,'elapsed_ms':elapsed}
                    with lock:
                        rows.append(row)
                        stream.write(json.dumps(row)+'\n')
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(worker,range(concurrency)))
            finished=time.time()
            latencies=sorted(row['elapsed_ms'] for row in rows)
            run={'concurrency':concurrency,'start_utc_epoch':run_start,'end_utc_epoch':finished,'requests':len(rows),
                 'elapsed_seconds':round(finished-run_start,3),'requests_per_second':round(len(rows)/(finished-run_start),3),
                 'client_p50_ms':statistics.median(latencies),'client_p95_ms':latencies[math.ceil(len(rows)*.95)-1],
                 'incorrect_or_failed':sum(not row['correct'] for row in rows),
                 'series':{name:prometheus('query_range',{'query':query,'start':run_start,'end':finished,'step':5}) for name,query in QUERIES.items()}}
            record['runs'].append(run)
            print(json.dumps({k:v for k,v in run.items() if k!='series'}),flush=True)
            time.sleep(10)
    record['overall_range']={'start':start,'end':time.time()}
    record['passed']=all(run['incorrect_or_failed']==0 for run in record['runs'])
    output.write_text(json.dumps(record,indent=2),encoding='utf-8')
    assert record['passed']


if __name__=='__main__':
    main()
