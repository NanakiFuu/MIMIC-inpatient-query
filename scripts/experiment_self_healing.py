"""SIGKILL actual systemd MainPID; measure restart and query readiness."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.request import urlopen
from urllib.error import URLError


def properties():
    result=subprocess.run(['systemctl','show','medical-query.service','-p','MainPID','-p','Restart','-p','RestartUSec','-p','NRestarts'],capture_output=True,text=True,check=True)
    return dict(line.split('=',1) for line in result.stdout.splitlines())


def main():
    if os.getuid()!=0:
        raise SystemExit('Root required')
    output=Path(__file__).resolve().parents[1]/'evidence/p0/automation/self-healing.json'
    if output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    before=properties()
    original=int(before['MainPID'])
    assert original>1 and before['Restart']=='on-failure'
    with urlopen('http://127.0.0.1:8765/api/summary',timeout=5) as response:
        expected=json.load(response)['result']
    epoch=time.time()
    start=time.perf_counter()
    os.kill(original,signal.SIGKILL)
    observations=[]
    new_pid=None
    while time.perf_counter()-start<20:
        current=properties()
        elapsed=time.perf_counter()-start
        observations.append({'elapsed_ms':round(elapsed*1000,3),'pid':int(current['MainPID']),'restarts':int(current['NRestarts'])})
        if int(current['MainPID']) not in {0,original}:
            new_pid=int(current['MainPID'])
            try:
                with urlopen('http://127.0.0.1:8765/api/summary',timeout=2) as response:
                    actual=json.load(response)['result']
                if actual==expected:
                    break
            except (URLError,TimeoutError):
                pass
        time.sleep(.05)
    else:
        subprocess.run(['systemctl','start','medical-query.service'],check=True)
        raise RuntimeError('Auto-restart did not recover within experiment window')
    result={'scope':'One WSL2 host; actual application SIGKILL; systemd Restart=on-failure',
            'kill_utc_epoch':epoch,'before':before,'after':properties(),'new_pid':new_pid,
            'kill_to_query_ready_ms':round((time.perf_counter()-start)*1000,3),
            'same_summary':actual==expected,'observations':observations}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='observations'}))


if __name__=='__main__':
    main()
