"""Install project-only backup and isolated restore timers; no global cron."""
import argparse
import os
from pathlib import Path
import pwd
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--user',default=os.environ.get('MEDICAL_QUERY_SERVICE_USER','nanaki'))
    args=parser.parse_args()
    if os.getuid()!=0:
        raise SystemExit('Root required')
    account=pwd.getpwnam(args.user)
    python=Path(account.pw_dir)/'environments/medical-query/.venv/bin/python'
    for name,mode,schedule,run_user in [('medical-query-backup','backup','*-*-* 02:00:00',args.user),
                                        ('medical-query-backup-verify','verify','Sun *-*-* 03:00:00','root')]:
        files={f'{name}.service':f'''# Managed by medical-query operations
[Unit]
Description=Project database {mode}
After=postgresql@16-main.service
[Service]
Type=oneshot
User={run_user}
WorkingDirectory={ROOT}
ExecStart={python} {ROOT}/scripts/backup_database.py --mode {mode} --user {args.user} --retain 7
UMask=0077
PrivateTmp=true
''',f'{name}.timer':f'''# Managed by medical-query operations
[Unit]
Description=Project scheduled database {mode}
[Timer]
OnCalendar={schedule}
Persistent=true
RandomizedDelaySec=60
[Install]
WantedBy=timers.target
'''}
        for filename,content in files.items():
            path=Path('/etc/systemd/system')/filename
            if path.exists() and not path.read_text().startswith('# Managed by medical-query operations'):
                raise SystemExit('Refusing to overwrite unrelated systemd unit')
            path.write_text(content)
            subprocess.run(['systemd-analyze','verify',str(path)],check=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','medical-query-backup.timer','medical-query-backup-verify.timer'],check=True)
    subprocess.run(['systemctl','start','medical-query-backup.service'],check=True)
    subprocess.run(['systemctl','start','medical-query-backup-verify.service'],check=True)
    subprocess.run(['systemctl','list-timers','medical-query-*','--no-pager'],check=True)


if __name__=='__main__':
    main()
