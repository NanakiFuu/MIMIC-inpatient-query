#!/usr/bin/env bash
# PostgreSQL streaming-replication demonstration setup (SINGLE WSL2 host).
#
# Creates two NEW clusters and leaves the existing 16/main cluster untouched:
#   primary  16/replprimary  port 5532   <- business data restored from a dump
#   standby  16/replstandby  port 5533   <- physical replica (pg_basebackup)
#
# The reader role/password is taken from the application config so the app can
# be pointed at the promoted standby during the failover experiment. No secret
# is ever printed.
#
# Scope: one machine, one disk, synchronous-free (async) streaming replication.
# This is a local demonstration, not production HA.
#
# Usage: sudo bash scripts/setup_pg_replication_demo.sh <dump-file>
set -euo pipefail

PRIMARY_CLUSTER=${MEDICAL_QUERY_REPL_PRIMARY_CLUSTER:-replprimary}
STANDBY_CLUSTER=${MEDICAL_QUERY_REPL_STANDBY_CLUSTER:-replstandby}
PRIMARY_PORT=${MEDICAL_QUERY_REPL_PRIMARY_PORT:-5532}
STANDBY_PORT=${MEDICAL_QUERY_REPL_STANDBY_PORT:-5533}
REPL_USER=repl
APP_CONFIG=${MEDICAL_QUERY_APP_CONFIG:-/home/nanaki/.config/medical-query/database.json}
DUMP=${1:?usage: setup_pg_replication_demo.sh <path-to-dump>}

say() { printf '\n== %s ==\n' "$*"; }
as_postgres() { runuser -u postgres -- "$@"; }

# --- safety guard: never let an empty or mistyped name widen a wipe path -----
# Step 4 wipes the standby data directory. If a cluster name were empty the path
# would collapse to /var/lib/postgresql/16/ and the wipe would destroy every
# cluster on the host, including the live 16/main. Refuse anything that is not a
# plain cluster name before touching anything.
for name in "$PRIMARY_CLUSTER" "$STANDBY_CLUSTER"; do
  [[ "$name" =~ ^[a-z][a-z0-9_]{0,30}$ ]] \
    || { echo "unsafe cluster name: '$name' (expected ^[a-z][a-z0-9_]{0,30}\$)" >&2; exit 2; }
done
[ "$PRIMARY_CLUSTER" != "$STANDBY_CLUSTER" ] \
  || { echo 'primary and standby cluster names must differ' >&2; exit 2; }

READER_PW=$(python3 -c "import json;print(json.load(open('$APP_CONFIG'))['password'])")

# --- 1. primary cluster -----------------------------------------------------
say "primary cluster $PRIMARY_CLUSTER on $PRIMARY_PORT"
if ! pg_lsclusters | awk '{print $1, $2}' | grep -qx "16 $PRIMARY_CLUSTER"; then
  pg_createcluster 16 "$PRIMARY_CLUSTER" --port "$PRIMARY_PORT" >/dev/null
fi
pg_ctlcluster 16 "$PRIMARY_CLUSTER" start 2>/dev/null || true

# --- 2. replication-capable settings + pg_hba -------------------------------
say "primary settings and pg_hba"
CONF=/etc/postgresql/16/$PRIMARY_CLUSTER/postgresql.conf
HBA=/etc/postgresql/16/$PRIMARY_CLUSTER/pg_hba.conf
# wal_level=replica and max_wal_senders>=1 are already the PG16 defaults; assert them.
as_postgres psql -p "$PRIMARY_PORT" -v ON_ERROR_STOP=1 -q <<'SQL'
ALTER SYSTEM SET wal_level = 'replica';
ALTER SYSTEM SET max_wal_senders = 10;
ALTER SYSTEM SET wal_keep_size = '64MB';
-- the project dump contains CREATE EXTENSION pg_stat_statements, which fails
-- unless the library is preloaded (same requirement found during P1).
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';
SQL
if ! grep -q "replication $REPL_USER" "$HBA"; then
  printf 'host replication %s 127.0.0.1/32 scram-sha-256\n' "$REPL_USER" >> "$HBA"
fi
pg_ctlcluster 16 "$PRIMARY_CLUSTER" restart

# --- 3. roles and business database ----------------------------------------
say "roles and database"
# idempotent: adjust the password when the role already exists (a DROP would
# fail once the reader owns grants)
for role in "$REPL_USER" medical_query_reader; do
  if [ "$role" = "$REPL_USER" ]; then pw=replpass; else pw=$READER_PW; fi
  if as_postgres psql -p "$PRIMARY_PORT" -tAc "select 1 from pg_roles where rolname='$role'" | grep -q 1; then
    as_postgres psql -p "$PRIMARY_PORT" -v ON_ERROR_STOP=1 -q \
      -c "ALTER ROLE $role WITH LOGIN PASSWORD '$pw'"
  else
    if [ "$role" = "$REPL_USER" ]; then
      as_postgres psql -p "$PRIMARY_PORT" -v ON_ERROR_STOP=1 -q \
        -c "CREATE ROLE $role WITH REPLICATION LOGIN PASSWORD '$pw'"
    else
      as_postgres psql -p "$PRIMARY_PORT" -v ON_ERROR_STOP=1 -q \
        -c "CREATE ROLE $role WITH LOGIN PASSWORD '$pw'"
    fi
  fi
done

if ! as_postgres psql -p "$PRIMARY_PORT" -tAc "select 1 from pg_database where datname='medical_query_demo'" | grep -q 1; then
  as_postgres createdb -p "$PRIMARY_PORT" medical_query_demo
fi
# restore only when the business schema is still empty (re-runs skip this)
if ! as_postgres psql -p "$PRIMARY_PORT" -d medical_query_demo -tAc \
     "select 1 from information_schema.tables where table_schema='mimic_raw' limit 1" | grep -q 1; then
  # the backup file is 0600 under the app user's home; stage a postgres-readable copy
  STAGE=/tmp/pg-repl-demo
  mkdir -p "$STAGE"
  install -m 0640 -o postgres -g postgres "$DUMP" "$STAGE/source.dump"
  as_postgres pg_restore -p "$PRIMARY_PORT" -d medical_query_demo --no-owner --no-acl --exit-on-error "$STAGE/source.dump"
fi
as_postgres psql -p "$PRIMARY_PORT" -d medical_query_demo -v ON_ERROR_STOP=1 -q <<'SQL'
GRANT USAGE ON SCHEMA query_api TO medical_query_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA query_api TO medical_query_reader;
SQL

# --- 4. standby cluster + base backup --------------------------------------
say "standby cluster $STANDBY_CLUSTER on $STANDBY_PORT"
if ! pg_lsclusters | awk '{print $1, $2}' | grep -qx "16 $STANDBY_CLUSTER"; then
  pg_createcluster 16 "$STANDBY_CLUSTER" --port "$STANDBY_PORT" >/dev/null
fi
pg_ctlcluster 16 "$STANDBY_CLUSTER" stop 2>/dev/null || true
STANDBY_DATA=/var/lib/postgresql/16/$STANDBY_CLUSTER
# second guard: the wipe target must be a direct child of the 16/ cluster root,
# never the root itself (that is how an empty name becomes a host-wide wipe)
[ "$(dirname -- "$STANDBY_DATA")" = "/var/lib/postgresql/16" ] \
  || { echo "unsafe standby data dir: '$STANDBY_DATA'" >&2; exit 2; }
[ -d "$STANDBY_DATA" ] \
  || { echo "standby data dir missing: '$STANDBY_DATA'" >&2; exit 2; }
rm -rf "${STANDBY_DATA:?}"/*
chown postgres:postgres "$STANDBY_DATA"
export PGPASSWORD=replpass
as_postgres pg_basebackup -h 127.0.0.1 -p "$PRIMARY_PORT" -U "$REPL_USER" \
  -D "$STANDBY_DATA" -R -X stream -c fast -P 2>&1 | tail -3
unset PGPASSWORD

say "standby port override"
# the cluster config sets the port; pg_basebackup only wrote the data directory
grep -qE "^port = $STANDBY_PORT" /etc/postgresql/16/$STANDBY_CLUSTER/postgresql.conf \
  || sed -i "s/^port = .*/port = $STANDBY_PORT/" /etc/postgresql/16/$STANDBY_CLUSTER/postgresql.conf
pg_ctlcluster 16 "$STANDBY_CLUSTER" start

# --- 5. verify streaming ----------------------------------------------------
say "verify streaming replication"
sleep 3
as_postgres psql -p "$PRIMARY_PORT" -tAc \
  "select application_name, state, sync_state, sent_lsn, replay_lsn from pg_stat_replication"
as_postgres psql -p "$STANDBY_PORT" -tAc "select pg_is_in_recovery()"

say "clusters now"
pg_lsclusters
