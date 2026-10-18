"""Connection pool: bounded acquisition, reuse, and the read-only guarantees.

These need the project database, like the rest of the suite.
"""
import json
import logging
import os
import time

import psycopg
import psycopg_pool
import pytest

from medical_query import api, database


def test_pool_settings_defaults_are_small_and_overridable(monkeypatch):
    assert database.pool_settings()=={'min_size':1,'max_size':4,'timeout':2.0}
    monkeypatch.setenv('MEDICAL_QUERY_POOL_MIN_SIZE','2')
    monkeypatch.setenv('MEDICAL_QUERY_POOL_MAX_SIZE','8')
    monkeypatch.setenv('MEDICAL_QUERY_POOL_TIMEOUT','0.5')
    assert database.pool_settings()=={'min_size':2,'max_size':8,'timeout':0.5}


def test_instance_label_defaults_to_pid_and_honours_override(monkeypatch):
    monkeypatch.delenv('MEDICAL_QUERY_INSTANCE',raising=False)
    assert database.instance_label()==f'pid{os.getpid()}'
    monkeypatch.setenv('MEDICAL_QUERY_INSTANCE','8865')
    assert database.instance_label()=='8865'


def test_pool_logs_a_successful_connection_with_its_process_label(caplog):
    # psycopg_pool logs a WARNING on connection failure but nothing at WARNING
    # level on success, so without an explicit record a recovery cannot be seen
    # in the journal. The record has to name this process's pool.
    database.close()
    caplog.set_level(logging.INFO,logger='medical_query')
    database.pool().wait(timeout=5)
    events=[json.loads(record.getMessage()) for record in caplog.records
            if record.getMessage().startswith('{')]
    assert {'event':'pool_connection_established','pool':f'medical-query-{database.instance_label()}'} in events


def test_pool_exhaustion_is_reported_as_503():
    # PoolTimeout subclasses psycopg.OperationalError, so this mapping has to be
    # checked first: an exhausted pool is not the same failure as a database
    # that is down, and neither may turn into an unbounded wait.
    assert api.db_error(psycopg_pool.PoolTimeout('pool timeout'))==(503,'database_pool_timeout')


def test_read_only_session_defaults_travel_with_every_pooled_connection():
    assert 'default_transaction_read_only=on' in database.CONNECT_OPTIONS
    assert 'statement_timeout=3000' in database.CONNECT_OPTIONS
    assert 'lock_timeout=1000' in database.CONNECT_OPTIONS


def test_connect_reuses_a_pooled_connection_instead_of_one_per_request():
    # Warm the pool first. While it is still filling, a borrow can race the
    # background worker and pick up a second connection, which is normal pool
    # behaviour; what matters is that steady-state borrowing does not open a
    # backend per request.
    database.pool().wait(timeout=5)
    pids = []
    for _ in range(5):
        with database.connect() as conn:
            pids.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
    assert len(set(pids))==1, f"expected one reused backend, saw {sorted(set(pids))}"


def test_a_pooled_connection_still_cannot_write():
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction,psycopg.errors.InsufficientPrivilege)):
        with database.connect() as conn:
            conn.execute("CREATE TEMP TABLE pool_probe_should_be_denied (id integer)")


def test_a_failed_statement_returns_a_usable_connection():
    # A statement that errors inside the `with` block must not poison the pool.
    # The connection has to go back and the next borrow has to still work: the
    # `with connect()` shape is compatible, but that alone proves nothing about
    # exception and transaction handling.
    database.pool().wait(timeout=5)
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction,psycopg.errors.InsufficientPrivilege)):
        with database.connect() as conn:
            first = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            conn.execute("CREATE TEMP TABLE pool_error_probe (id integer)")
    with database.connect() as conn:
        second = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        assert conn.execute("SELECT 1 AS ok").fetchone()["ok"]==1
    assert first==second, "the connection that errored should have gone back to the pool"


def test_an_exhausted_pool_fails_on_its_own_timeout(monkeypatch):
    # Pool exhaustion must not turn into an unbounded wait.
    monkeypatch.setenv('MEDICAL_QUERY_POOL_MIN_SIZE','1')
    monkeypatch.setenv('MEDICAL_QUERY_POOL_MAX_SIZE','1')
    monkeypatch.setenv('MEDICAL_QUERY_POOL_TIMEOUT','0.5')
    database.close()
    with database.connect() as held:
        held.execute("SELECT 1")
        started = time.perf_counter()
        with pytest.raises(psycopg_pool.PoolTimeout):
            with database.connect():
                pass
        elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"waited {elapsed:.2f}s; it should fail on the acquisition timeout"
