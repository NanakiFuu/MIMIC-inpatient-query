"""TEST_ONLY fault responses; real DB checks covered by existing integration tests."""
import socket

from medical_query import api, model, telemetry


def test_patient_id_and_unknown_paths_not_metric_labels(client, target):
    client.get(f"/api/admissions/{target['hadm_id']}")
    client.get('/unknown/TEST_PRIVATE_TEXT')
    text = client.get('/metrics').text
    assert str(target['hadm_id']) not in text
    assert 'TEST_PRIVATE_TEXT' not in text
    assert 'route="/api/admissions/{hadm_id}"' in text
    assert 'route="unmatched"' in text
    assert 'medical_query_database_ready 1.0' in text


def test_scrape_survives_database_connection_failure(client, monkeypatch):
    def fail():
        raise ConnectionError('TEST_ONLY private exception text')
    monkeypatch.setattr(api, 'connect', fail)
    response = client.get('/metrics')
    assert response.status_code == 200
    assert 'medical_query_database_ready 0.0' in response.text
    assert 'private exception' not in response.text


def test_provider_timeout_records_bounded_metric(monkeypatch):
    monkeypatch.setattr(model, 'config', lambda: {'model': 'TEST_ONLY'})
    def fail(*args):
        raise model.ModelError('model_timeout', 504)
    monkeypatch.setattr(model, 'request_completion', fail)
    before = telemetry.REGISTRY.get_sample_value('medical_query_model_calls_total', {'stage':'query', 'outcome':'timeout'}) or 0
    try:
        model.plan('TEST_ONLY no clinical content')
    except model.ModelError:
        pass
    after = telemetry.REGISTRY.get_sample_value('medical_query_model_calls_total', {'stage':'query', 'outcome':'timeout'})
    assert after == before + 1


def test_no_scrape_self_count(client):
    client.get('/metrics')
    client.get('/metrics')
    assert 'route="/metrics"' not in client.get('/metrics').text
