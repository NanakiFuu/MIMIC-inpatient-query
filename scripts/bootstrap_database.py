"""Run once as WSL root; provision only this project's DB and roles.

Credentials remain in the Linux user's private config directory, never the repo.
Normal import uses nanaki peer authentication; API uses a restricted TCP role.
"""
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import argparse
import re

DB = "medical_query_demo"
READER = "medical_query_reader"
MARKER = "mimic-inpatient-query project database v1"


def admin(query, database="postgres"):
    result = subprocess.run(
        ["/usr/sbin/runuser", "-u", "postgres", "--", "/usr/bin/psql", "-X",
         "-v", "ON_ERROR_STOP=1", "-At", "-d", database],
        input=query, text=True, capture_output=True, cwd="/tmp", check=False,
    )
    if result.returncode:
        # SQL may include a password. Never echo command input or error text.
        raise RuntimeError("PostgreSQL provisioning command failed; no secret output emitted")
    return result.stdout.strip()


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--user',default=os.environ.get('MEDICAL_QUERY_SERVICE_USER',os.environ.get('SUDO_USER','nanaki')))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,30}',args.user):
        raise SystemExit('User must be a simple Unix/database identifier')
    if os.getuid() != 0:
        raise SystemExit("Run with sudo or wsl -u root")
    owner = args.user
    user = pwd.getpwnam(owner)
    CONFIG = Path(user.pw_dir)/'.config/medical-query/database.json'
    db_status = admin(f"SELECT pg_get_userbyid(datdba)||'|'||coalesce(shobj_description(oid,'pg_database'),'') FROM pg_database WHERE datname='{DB}';")
    if db_status and db_status != f"{owner}|{MARKER}":
        raise SystemExit("Refusing to change an existing unrecognized database")
    reader_status = admin(f"SELECT coalesce(shobj_description(oid,'pg_authid'),'') FROM pg_roles WHERE rolname='{READER}';")
    reader_exists = admin(f"SELECT count(*) FROM pg_roles WHERE rolname='{READER}';") == "1"
    if reader_exists and reader_status != MARKER:
        raise SystemExit("Refusing to change an existing unrecognized reader role")
    owner_status = admin(f"SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname='{owner}';")
    if owner_status == "t":
        raise SystemExit("Unexpected elevated database privileges for nanaki")
    if not owner_status:
        admin(f"CREATE ROLE {owner} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;")
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text())
        if config.get("dbname") != DB or config.get("user") != READER:
            raise SystemExit("Unexpected existing credential file")
    else:
        config = {"host": "127.0.0.1", "port": 5432, "dbname": DB,
                  "user": READER, "password": secrets.token_urlsafe(32)}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        os.chown(CONFIG.parent, user.pw_uid, user.pw_gid)
        os.chmod(CONFIG.parent, 0o700)
        fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream)
        os.chown(CONFIG, user.pw_uid, user.pw_gid)
    # The first bootstrap may have created ~/.config as root. Repair only that
    # project-created parent when its sole entry is this project's directory.
    parent = CONFIG.parent.parent
    if parent.stat().st_uid == 0 and {p.name for p in parent.iterdir()} == {"medical-query"}:
        os.chown(parent, user.pw_uid, user.pw_gid)
    os.chown(CONFIG, user.pw_uid, user.pw_gid)
    os.chmod(CONFIG, 0o600)
    password = literal(config["password"])
    if not reader_exists:
        admin(f"CREATE ROLE {READER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {password}; COMMENT ON ROLE {READER} IS {literal(MARKER)};")
    else:
        admin(f"ALTER ROLE {READER} PASSWORD {password};")
    if not db_status:
        admin(f"CREATE DATABASE {DB} OWNER {owner};")
        admin(f"COMMENT ON DATABASE {DB} IS {literal(MARKER)};")
    admin(f"REVOKE ALL ON DATABASE {DB} FROM PUBLIC; GRANT CONNECT ON DATABASE {DB} TO {READER}; ALTER ROLE {READER} IN DATABASE {DB} SET default_transaction_read_only=on; ALTER ROLE {READER} IN DATABASE {DB} SET statement_timeout='3s'; ALTER ROLE {READER} IN DATABASE {DB} SET lock_timeout='1s';", DB)
    admin("REVOKE CREATE ON SCHEMA public FROM PUBLIC;", DB)
    print(json.dumps({"database": DB, "owner": owner, "reader": READER,
                      "credentials": str(CONFIG), "status": "ready"}))


if __name__ == "__main__":
    main()
