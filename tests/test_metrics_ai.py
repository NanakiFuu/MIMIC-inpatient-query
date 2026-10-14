"""Unit tests for the Prometheus whitelist rule baseline and the model guard.

These run without Prometheus and without the provider: the model is stubbed, so
the deterministic parts (whitelist hygiene, rule baseline, consistency guard)
are covered in CI.
"""
import json

import pytest

from medical_query import metrics_ai


def snapshot(**overrides):
    metrics = {key: {'value': 0.0, 'description': ''} for key in metrics_ai.WHITELIST}
    for key, value in overrides.items():
        metrics[key] = {'value': value, 'description': ''}
    return {'source': 'test', 'observed_at_utc': '2026-01-01T00:00:00Z',
            'prometheus': 'test', 'metrics': metrics}


def healthy(**overrides):
    base = {'app_target_up': 1.0, 'compose_app_target_up': 1.0, 'node_target_up': 1.0,
            'prometheus_target_up': 1.0, 'database_ready': 1.0, 'http_requests_per_second': 1.0,
            'http_5xx_per_second': 0.0, 'http_5xx_ratio': 0.0, 'http_p95_seconds': 0.01,
            'cpu_busy_ratio': 0.1, 'load1_per_cpu': 0.1, 'memory_available_ratio': 0.5,
            'root_filesystem_available_ratio': 0.5, 'firing_alerts': 0.0}
    base.update(overrides)
    return snapshot(**base)


def fake_response(finding, evidence_ids, next_checks=('retry_same_query',),
                  model='deepseek-flash', finish='tool_calls', arguments=None):
    if arguments is None:
        arguments = json.dumps({'finding': finding, 'evidence_ids': list(evidence_ids),
                                'next_checks': list(next_checks)})
    return {'model': model,
            'choices': [{'finish_reason': finish,
                         'message': {'tool_calls': [{'type': 'function', 'function': {
                             'name': 'assess_metrics', 'arguments': arguments}}]}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


def stub_model(monkeypatch, response):
    monkeypatch.setattr(metrics_ai.model, 'config', lambda: {'model': 'deepseek-flash'})
    monkeypatch.setattr(metrics_ai.model, 'request_completion', lambda payload, settings: response)


def test_whitelist_expressions_avoid_clinical_identifiers():
    forbidden = ('hadm_id', 'subject_id', 'stay_id', 'icd_code', 'icd_version',
                 'valuenum', 'long_title', 'charttime', 'itemid', 'mimic_raw')
    for key, (expression, _description) in metrics_ai.WHITELIST.items():
        lowered = expression.lower()
        for token in forbidden:
            assert token not in lowered, f'{key} references clinical identifier {token}'


def test_whitelist_only_aggregates_or_whole_host_metrics():
    # every query must be an aggregate or a host-level gauge, never a raw row set
    for expression, _description in metrics_ai.WHITELIST.values():
        assert 'mimic_raw' not in expression
        assert 'query_api' not in expression


def test_rule_baseline_covers_every_finding():
    assert metrics_ai.rule_assessment(healthy()) == 'healthy'
    assert metrics_ai.rule_assessment(healthy(app_target_up=0)) == 'app_target_down'
    assert metrics_ai.rule_assessment(healthy(compose_app_target_up=0)) == 'compose_app_target_down'
    assert metrics_ai.rule_assessment(healthy(database_ready=0)) == 'database_unavailable'
    assert metrics_ai.rule_assessment(healthy(http_5xx_ratio=0.4)) == 'high_error_rate'
    assert metrics_ai.rule_assessment(healthy(http_p95_seconds=1.5)) == 'high_latency'
    assert metrics_ai.rule_assessment(healthy(cpu_busy_ratio=0.95)) == 'resource_saturation'
    assert metrics_ai.rule_assessment(healthy(load1_per_cpu=2.0)) == 'resource_saturation'
    assert metrics_ai.rule_assessment(healthy(memory_available_ratio=0.01)) == 'resource_saturation'
    assert metrics_ai.rule_assessment(healthy(root_filesystem_available_ratio=0.01)) == 'resource_saturation'
    assert metrics_ai.rule_assessment(healthy(firing_alerts=2.0)) == 'alert_firing'


def test_target_down_takes_priority_over_database_probe():
    # a dead app cannot report database_ready; the target signal must win
    assert metrics_ai.rule_assessment(healthy(app_target_up=0, database_ready=0)) == 'app_target_down'


def test_model_agreement_is_accepted(monkeypatch):
    stub_model(monkeypatch, fake_response('database_unavailable', ['database_ready'],
                                          next_checks=('check_database_service',)))
    result = metrics_ai.interpret(healthy(database_ready=0))
    assert result['rule_baseline'] == 'database_unavailable'
    assert result['model_original_judgment']['finding'] == 'database_unavailable'
    assert result['validated_finding'] == 'database_unavailable'
    assert result['consistency_check_passed'] is True


def test_model_disagreement_is_downgraded_but_raw_judgment_preserved(monkeypatch):
    stub_model(monkeypatch, fake_response('healthy', ['app_target_up']))
    result = metrics_ai.interpret(healthy(database_ready=0))
    assert result['rule_baseline'] == 'database_unavailable'
    assert result['model_original_judgment']['finding'] == 'healthy'
    assert result['validated_finding'] == 'insufficient_evidence'
    assert result['consistency_check_passed'] is False


def test_correct_finding_with_irrelevant_evidence_is_downgraded(monkeypatch):
    stub_model(monkeypatch, fake_response('database_unavailable', ['cpu_busy_ratio'],
                                          next_checks=('check_database_service',)))
    result = metrics_ai.interpret(healthy(database_ready=0))
    assert result['model_original_judgment']['finding'] == 'database_unavailable'
    assert result['validated_finding'] == 'insufficient_evidence'
    assert result['consistency_check_passed'] is False


def test_insufficient_evidence_from_model_is_accepted(monkeypatch):
    stub_model(monkeypatch, fake_response('insufficient_evidence', ['cpu_busy_ratio']))
    result = metrics_ai.interpret(healthy())
    assert result['validated_finding'] == 'insufficient_evidence'
    assert result['consistency_check_passed'] is True


def test_malformed_model_output_is_rejected_with_audit_evidence(monkeypatch):
    stub_model(monkeypatch, fake_response('healthy', ['app_target_up'],
                                          arguments=json.dumps({'finding': 'not_a_finding',
                                                                'evidence_ids': ['app_target_up'],
                                                                'next_checks': ['retry_same_query']})))
    with pytest.raises(metrics_ai.model.ModelError) as failure:
        metrics_ai.interpret(healthy())
    assert failure.value.code == 'model_assessment_rejected'
    assert failure.value.evidence['finish_reason'] == 'tool_calls'
    assert 'not_a_finding' in failure.value.evidence['raw_arguments']
