import json
import pytest

from medical_query import diagnostics,model


def snapshot(database=True,db_error=None,model_error=None):
    return {"source":"TEST_ONLY synthetic observations","observations":[
      {"id":"db_probe","reachable":database},{"id":"request_metrics","requests":10,"server_errors":2},
      {"id":"recent_db_error","last_error":db_error},
      {"id":"model_state","last_error":model_error}]}


@pytest.mark.parametrize("input,expected",[
    (snapshot(),"service_healthy"),(snapshot(False),"database_unreachable"),
    (snapshot(db_error={"code":"database_query_timeout"}),"recent_query_error"),
    (snapshot(model_error={"code":"model_timeout"}),"model_dependency_error"),
])
def test_rule_baseline(input,expected):
    assert diagnostics.rule_finding(input)==expected


def test_unfounded_diagnosis_is_downgraded(monkeypatch):
    monkeypatch.setattr(model,"config",lambda:{"model":"deepseek-flash"})
    arguments={"finding":"database_unreachable","evidence_ids":["db_probe"],"next_checks":["check_database_service"]}
    monkeypatch.setattr(model,"request_completion",lambda *args:{"model":"deepseek-flash","choices":[{"finish_reason":"tool_calls","message":{
        "tool_calls":[{"function":{"name":"diagnose_snapshot","arguments":json.dumps(arguments)}}]}}],"usage":{}})
    before_calls=model.state["calls"]
    outcome=diagnostics.interpret(snapshot(True))
    assert outcome["finding"]=="insufficient_evidence"
    assert outcome["consistency_check_passed"] is False
    assert model.state["calls"]==before_calls+1
    assert model.state["last_success_at"] is not None


def test_runtime_evidence_has_no_credentials(client):
    response=client.get("/ops/evidence")
    assert response.status_code==200
    text=response.text
    assert "api_key" not in text and "password" not in text
    assert response.json()["observations"][0]["reachable"] is True
