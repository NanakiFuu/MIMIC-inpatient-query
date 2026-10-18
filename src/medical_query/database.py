"""Fixed project database endpoint; secrets never enter logs or HTTP errors.

One connection pool per process. `connect()` returns a context manager that
borrows a pooled connection and hands it back on exit, so `with connect() as
conn:` keeps the same shape at every call site.
"""
import json
import logging
from pathlib import Path
import os
import threading

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Session defaults applied when a connection is created. They live in the
# conninfo, so every pooled connection carries the same read-only guarantees.
CONNECT_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000"

# Pool defaults are left alone for reconnection behaviour. `reconnect_timeout`
# is the deadline for one reconnection attempt loop, not a delay before the pool
# tries again, so shortening it only makes the pool abandon an attempt sooner;
# it does not make recovery faster. The outage drill verifies that the default
# recovers without restarting the application.


def endpoint():
    """Resolve the fixed endpoint; environment variables win over the file."""
    if 'MEDICAL_QUERY_DB_PASSWORD' in os.environ:
        config = {'host':os.environ.get('MEDICAL_QUERY_DB_HOST','127.0.0.1'),
                  'port':int(os.environ.get('MEDICAL_QUERY_DB_PORT','5432')),
                  'dbname':'medical_query_demo','user':'medical_query_reader',
                  'password':os.environ['MEDICAL_QUERY_DB_PASSWORD']}
        if not config['password']:
            raise ValueError('Database credential missing')
    else:
        config_path = Path(os.environ.get("MEDICAL_QUERY_DB_CONFIG", str(Path.home() / '.config/medical-query/database.json')))
        config = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = {"host", "port", "dbname", "user", "password"}
    if set(config) != allowed or config["dbname"] != "medical_query_demo" or config["user"] != "medical_query_reader":
        raise ValueError("Unexpected application database configuration")
    return config


def connection_settings():
    """Connection kwargs shared by every pooled connection.

    The endpoint is read when the pool is built, not on every request, so a
    credential file that disappears while the service is running no longer
    turns into 503s: the pool keeps the connections it already authenticated.
    """
    return {**endpoint(), 'connect_timeout':2, 'row_factory':dict_row,
            'application_name':'medical-query-api', 'options':CONNECT_OPTIONS}


def pool_settings():
    """Pool size and acquisition timeout; small by default and overridable.

    The budget matters: PostgreSQL `max_connections` is shared by every worker
    of every instance, so max_size is not a free knob. See the README.
    """
    return {'min_size':int(os.environ.get('MEDICAL_QUERY_POOL_MIN_SIZE','1')),
            'max_size':int(os.environ.get('MEDICAL_QUERY_POOL_MAX_SIZE','4')),
            'timeout':float(os.environ.get('MEDICAL_QUERY_POOL_TIMEOUT','2.0'))}


def instance_label():
    """Identify this process's pool in the journal.

    The default is the process id, so two instances of the same service never
    share a pool name and their connection logs can be told apart. Set
    `MEDICAL_QUERY_INSTANCE` to use a stable label (for example the port).
    """
    return os.environ.get('MEDICAL_QUERY_INSTANCE') or f'pid{os.getpid()}'


_pool = None
_pool_lock = threading.Lock()


def pool():
    """The process-wide pool, built on first use.

    Opening does not block when the database is down: the pool fills itself in
    background workers, and a request that cannot get a connection fails on its
    own acquisition timeout instead of hanging.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                settings = pool_settings()
                name = f'medical-query-{instance_label()}'

                def log_established(_conn):
                    # psycopg_pool logs a WARNING when a connection attempt
                    # fails but nothing at WARNING level when one succeeds, so a
                    # recovery is invisible in the journal without this. The
                    # pool name carries the process id, so a recovery can be
                    # attributed to one instance.
                    logging.getLogger('medical_query').info(
                        json.dumps({'event':'pool_connection_established','pool':name}))

                candidate = ConnectionPool(
                    kwargs=connection_settings(),
                    min_size=settings['min_size'], max_size=settings['max_size'],
                    timeout=settings['timeout'],
                    check=ConnectionPool.check_connection,
                    configure=log_established,
                    name=name, open=False)
                candidate.open()
                _pool = candidate
    return _pool


def connect():
    """Borrow a pooled connection; leaving the `with` block returns it."""
    return pool().connection(timeout=pool_settings()['timeout'])


def close():
    """Release every pooled connection. Called from the shutdown hook."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
