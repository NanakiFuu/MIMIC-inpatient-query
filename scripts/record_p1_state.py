"""Read-only environment/publication state; no credentials or private contents."""
import json
from pathlib import Path
import subprocess
from datetime import datetime,timezone
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]


def main():
    output=ROOT/'evidence/p1/environment-state.json'
    result={'observed_at_utc':datetime.now(timezone.utc).isoformat(),
            'container_deployment_verified':False,'git_identity_pending':True,'code_license_pending':True,
            'next_gate':'P1 container verification; P2 requires explicit user confirmation'}
    for name,args in {
        'services':['systemctl','is-active','medical-query','postgresql@16-main','prometheus','prometheus-node-exporter','docker'],
        'docker_version':['docker','version','--format','{{.Server.Version}}'],
        'compose_version':['docker','compose','version'],
        'timer_state':['systemctl','list-timers','medical-query-*','--no-pager'],
        'disk':['df','-h','/'],
    }.items():
        command=subprocess.run(args,capture_output=True,text=True)
        result[name]={'returncode':command.returncode,'stdout':command.stdout.strip()}
    result['health']={}
    for endpoint in ['http://127.0.0.1:8765/health/ready','http://127.0.0.1:9090/-/ready']:
        with urlopen(endpoint,timeout=8) as response:
            result['health'][endpoint]=response.status
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'services':result['services'],'health':result['health']}))


if __name__=='__main__':
    main()
