"""Install the project-only systemd service after query tests pass."""
import os
from pathlib import Path
import socket
import subprocess
import pwd
import argparse

ROOT = Path(__file__).resolve().parents[1]
UNIT = Path("/etc/systemd/system/medical-query.service")
CONTENT = """# Managed by MIMIC inpatient query project
[Unit]
Description=MIMIC Demo inpatient fixed query API
After=network.target postgresql@16-main.service

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={root}
Environment=PYTHONPATH={root}/src
ExecStart={python} -m uvicorn medical_query.api:app --host 127.0.0.1 --port 8765 --no-access-log
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--user',default=os.environ.get('MEDICAL_QUERY_SERVICE_USER',os.environ.get('SUDO_USER','nanaki')))
    parser.add_argument('--python',type=Path)
    args = parser.parse_args()
    if os.getuid()!=0:
        raise SystemExit("Run as WSL root")
    account = pwd.getpwnam(args.user)
    import grp
    group = grp.getgrgid(account.pw_gid).gr_name
    python = args.python or Path(account.pw_dir)/'environments/medical-query/.venv/bin/python'
    if any(c.isspace() for c in str(ROOT)+str(python)):
        raise SystemExit('systemd installer currently requires paths without whitespace')
    if UNIT.exists() and not UNIT.read_text().startswith("# Managed by MIMIC inpatient query project\n"):
        raise SystemExit("Existing service not owned by this project; refusing replacement")
    if not UNIT.exists():
        with socket.socket() as check:
            check.bind(("127.0.0.1",8765))
    UNIT.write_text(CONTENT.format(user=args.user,group=group,root=ROOT,python=python))
    subprocess.run(["systemd-analyze","verify",str(UNIT)],check=True)
    subprocess.run(["systemctl","daemon-reload"],check=True)
    subprocess.run(["systemctl","enable","--now","medical-query.service"],check=True)
    subprocess.run(["systemctl","is-active","medical-query.service"],check=True)
    print("Project service installed on http://127.0.0.1:8765; no Windows services changed")


if __name__=="__main__":
    main()
