"""Actual project backups exercise retention; does not touch legacy experiment dumps."""
import json
from pathlib import Path
from backup_database import backup


def main():
    output=Path(__file__).resolve().parents[1]/'evidence/p0/automation/retention.json'
    if output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    results=[backup(7) for _ in range(8)]
    directory=Path.home()/'.local/state/medical-query/backups'
    remaining=list(directory.glob('medical-query-*.dump'))
    legacy=list(directory.glob('mimic-demo-*.dump'))
    assert len(remaining)==7 and any(r['expired_backups_removed'] for r in results)
    result={'source':'Actual repeated project logical backups; seven-newest retention exercised',
            'runs':[{k:v for k,v in r.items() if k!='dump'} for r in results],
            'remaining_project_backups':len(remaining),'legacy_experiment_backups_preserved':len(legacy),'passed':True}
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'retained':len(remaining),'legacy_preserved':len(legacy),'passed':True}))


if __name__=='__main__':
    main()
