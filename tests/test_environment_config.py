"""TEST_ONLY credentials; environment routes for portable Compose deployment."""
import json
import pytest
from medical_query import database,model


def test_environment_db_config_does_not_open_private_file(monkeypatch):
    # The endpoint is resolved when the pool is built, so this asserts on the
    # settings a new pool would be created with. MEDICAL_QUERY_DB_CONFIG points
    # at a path that must never be opened.
    monkeypatch.setenv('MEDICAL_QUERY_DB_HOST','TEST_ONLY_DATABASE')
    monkeypatch.setenv('MEDICAL_QUERY_DB_PORT','15432')
    monkeypatch.setenv('MEDICAL_QUERY_DB_PASSWORD','TEST_ONLY_NOT_A_REAL_PASSWORD')
    monkeypatch.setenv('MEDICAL_QUERY_DB_CONFIG','TEST_ONLY_MUST_NOT_OPEN')
    settings = database.connection_settings()
    assert settings['host']=='TEST_ONLY_DATABASE' and settings['port']==15432
    assert settings['dbname']=='medical_query_demo' and settings['user']=='medical_query_reader'
    assert 'default_transaction_read_only=on' in settings['options']
    assert settings['connect_timeout']==2


def test_empty_environment_db_password_is_rejected(monkeypatch):
    monkeypatch.setenv('MEDICAL_QUERY_DB_PASSWORD','')
    with pytest.raises(ValueError):
        database.connect()


def test_model_environment_key_preserves_fixed_provider(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY_NOT_A_REAL_KEY')
    monkeypatch.setenv('MEDICAL_QUERY_MODEL_CONFIG','TEST_ONLY_MUST_NOT_OPEN')
    settings=model.config()
    assert settings['base_url']=='https://api.deepseek.com'
    assert settings['model']=='deepseek-flash'
    assert settings['timeout_seconds']==25
    assert 'TEST_ONLY_NOT_A_REAL_KEY' not in json.dumps(model.status())


def test_empty_model_environment_key_is_unconfigured(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','')
    with pytest.raises(model.ModelError):
        model.config()
