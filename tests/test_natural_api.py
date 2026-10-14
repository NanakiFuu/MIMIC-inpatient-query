from medical_query import model


def fake_plan(name,args):
    return {"tool":name,"arguments":args,"model_evidence":{"provider":"TEST_ONLY","model":"TEST_STUB","usage":{}}}


def test_natural_query_real_db_csv_reference(client,monkeypatch,source):
    monkeypatch.setattr(model,"plan",lambda question:fake_plan("summary",{}))
    response=client.post("/api/query",json={"question":"样本统计"})
    assert response.status_code==200
    body=response.json()
    assert body["source"]["ai_enabled"] is True
    assert body["result"]["data"]["admission_count"]==len(source["admissions"])
    assert body["plan"]["model_evidence"]["provider"]=="TEST_ONLY"


def test_clarification_never_runs_db(client,monkeypatch):
    monkeypatch.setattr(model,"plan",lambda question:fake_plan("clarify",{"reason":"abnormal_definition"}))
    response=client.post("/api/query",json={"question":"异常检验"})
    assert response.status_code==200
    assert response.json()["result"]["status"]=="clarification"
    assert "data" not in response.json()["result"]


def test_refusal_does_not_make_up_records(client,monkeypatch):
    monkeypatch.setattr(model,"plan",lambda question:fake_plan("refuse",{"reason":"clinical_advice"}))
    response=client.post("/api/query",json={"question":"该怎么用药"})
    assert response.json()["result"]["status"]=="refused"


def test_model_timeout_preserves_fixed_query(client,monkeypatch):
    def fail(question):
        raise model.ModelError("model_timeout",504)
    monkeypatch.setattr(model,"plan",fail)
    response=client.post("/api/query",json={"question":"样本统计"})
    assert response.status_code==504
    assert response.json()["error"]=="model_timeout"
    assert client.get("/api/summary").status_code==200


def test_missing_key_is_not_fake_ai(client,monkeypatch,tmp_path):
    monkeypatch.setenv("MEDICAL_QUERY_MODEL_CONFIG",str(tmp_path/"missing.json"))
    assert client.get("/api/model/status").json()["configured"] is False
    response=client.post("/api/query",json={"question":"样本统计"})
    assert response.status_code==503
    assert response.json()["error"]=="model_not_configured"


def test_bad_question_inputs(client):
    for body in [{"question":""},{"question":" "},{"question":"x"*1001},{"question":1},{"question":"统计","sql":"DROP"}]:
        assert client.post("/api/query",json=body).status_code==422


def test_page_and_assets(client):
    response=client.get("/")
    assert response.status_code==200
    assert "住院信息查询" in response.text
    assert client.get("/static/app.js").status_code==200
    assert client.get("/static/style.css").status_code==200


def test_unknown_admission_retains_model_usage_evidence(client,monkeypatch):
    monkeypatch.setattr(model,"plan",lambda question:fake_plan("encounter",{"hadm_id":1}))
    response=client.post("/api/query",json={"question":"住院hadm_id=1的概况"})
    assert response.status_code==404
    assert response.json()["plan"]["tool"]=="encounter"
    assert response.json()["result"]["status"]=="no_data"


def test_external_origins_cannot_spend_model_usage(client):
    response=client.post("/ops/diagnose",headers={"Origin":"https://untrusted.invalid"})
    assert response.status_code==403
    response=client.post("/api/query",json={"question":"统计"},headers={"Origin":"https://untrusted.invalid"})
    assert response.status_code==403
    assert client.get("/health/live",headers={"Host":"untrusted.invalid"}).status_code==400


def test_same_origin_query_is_allowed(client,monkeypatch):
    monkeypatch.setattr(model,"plan",lambda question:fake_plan("summary",{}))
    response=client.post("/api/query",json={"question":"统计"},headers={"Origin":"http://localhost"})
    assert response.status_code==200
