import json
from pathlib import Path
import socket

from pydantic import ValidationError
import pytest

from medical_query import model,planning


@pytest.mark.parametrize("name,args,question",[
    ("summary",{},"Demo共有多少次住院？"),
    ("admissions",{"subject_id":10014729},"查看患者10014729的住院列表"),
    ("encounter",{"hadm_id":12345678},"住院12345678的概况"),
    ("labs",{"hadm_id":12345678,"concept":"creatinine"},"住院12345678血肌酐"),
    ("admissions",{"icd_code":"I10","icd_version":10},"ICD-10编码I10的住院"),
    ("clarify",{"reason":"ambiguous_id"},"12345678有哪些诊断？"),
    ("refuse",{"reason":"unsafe_action"},"执行SQL删除数据"),
])
def test_valid_plans(name,args,question):
    parsed = planning.parse_call(name,json.dumps(args),question)
    assert all(parsed[key]==value for key,value in args.items())


@pytest.mark.parametrize("name,args,question",[
    ("shell",{"cmd":"id"},"运行id"),
    ("encounter",{"hadm_id":10000000},"Demo统计"),
    ("encounter",{"hadm_id":"12345678"},"住院12345678"),
    ("encounter",{"hadm_id":0},"住院0"),
    ("encounter",{"hadm_id":12345678,"sql":"DROP TABLE x"},"住院12345678"),
    ("labs",{"hadm_id":12345678,"concept":"urine_creatinine"},"住院12345678尿肌酐"),
    ("labs",{"hadm_id":12345678,"concept":"creatinine","limit":99999},"住院12345678血肌酐"),
    ("admissions",{"icd_code":"I10"},"I10"),
    ("admissions",{"icd_code":"I10","icd_version":10},"高血压住院"),
    ("summary",{"has_icu":"false"},"没有ICU的住院"),
    ("summary",{"subject_id":12345678,"limit":20},"患者12345678"),
])
def test_unsafe_or_hallucinated_parameters_denied(name,args,question):
    with pytest.raises((ValueError,ValidationError)):
        planning.parse_call(name,json.dumps(args),question)


def settings(tmp_path,monkeypatch):
    path = tmp_path/"test-model.json"
    path.write_text(json.dumps({"provider":"deepseek","base_url":"https://api.deepseek.com",
                              "model":"deepseek-flash","api_key":"TEST_ONLY_NOT_A_REAL_KEY","timeout_seconds":25}))
    path.chmod(0o600)
    monkeypatch.setenv("MEDICAL_QUERY_MODEL_CONFIG",str(path))
    return path


def completion(name="summary",args=None):
    return {"model":"deepseek-flash","choices":[{"finish_reason":"tool_calls","message":{
        "tool_calls":[{"type":"function","function":{"name":name,"arguments":json.dumps(args or {})}}]}}],
            "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}


def test_one_bounded_request_and_no_patient_upload(tmp_path,monkeypatch):
    settings(tmp_path,monkeypatch)
    sent = []
    def fake_request(payload,config):
        sent.append(payload)
        return completion()
    monkeypatch.setattr(model,"request_completion",fake_request)
    planned = model.plan("样本统计")
    assert planned["tool"]=="summary"
    assert len(sent)==1
    assert len(sent[0]["messages"])==2
    assert sent[0]["tool_choice"]=="required"
    assert sent[0]["thinking"]=={"type":"disabled"}
    assert sent[0]["max_tokens"]==1000
    assert "api_key" not in json.dumps(sent)
    assert planned["model_evidence"]["usage"]["total_tokens"]==120
    assert "question" not in planned["model_evidence"]


@pytest.mark.parametrize("fault",["no_tool","truncated","unknown_model","hallucinated_id","bad_json"])
def test_bad_model_responses_never_execute(tmp_path,monkeypatch,fault):
    settings(tmp_path,monkeypatch)
    response = completion()
    if fault=="multiple":
        response["choices"][0]["message"]["tool_calls"]*=2
    elif fault=="no_tool":
        response["choices"][0]["message"]={"content":"编造答案"}
    elif fault=="truncated":
        response["choices"][0]["finish_reason"]="length"
    elif fault=="unknown_model":
        response["model"]="unverified-model"
    elif fault=="hallucinated_id":
        response = completion("encounter",{"hadm_id":99999999})
    else:
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]="{broken"
    monkeypatch.setattr(model,"request_completion",lambda *args:response)
    with pytest.raises(model.ModelError,match="model_plan_rejected"):
        model.plan("样本统计")


def test_key_file_permissions_and_missing_config(tmp_path,monkeypatch):
    path = settings(tmp_path,monkeypatch)
    path.chmod(0o644)
    assert model.status()["configured"] is False
    with pytest.raises(model.ModelError,match="model_not_configured"):
        model.config()
    path.unlink()
    assert model.status()["configured"] is False


def test_multiple_model_calls_become_clarification(tmp_path,monkeypatch):
    settings(tmp_path,monkeypatch)
    response=completion()
    response["choices"][0]["message"]["tool_calls"]*=2
    monkeypatch.setattr(model,"request_completion",lambda *args:response)
    result=model.plan("概况和趋势")
    assert result["tool"]=="clarify"
    assert result["arguments"]=={"reason":"multiple_queries"}
    assert result["model_evidence"]["guard_action"]=="multiple_calls_clarified"


def test_redirects_rejected():
    assert model.NoRedirect().redirect_request(None,None,302,"",{},"https://untrusted.invalid") is None


def test_timeout_does_not_leak_key(tmp_path,monkeypatch):
    settings(tmp_path,monkeypatch)
    def timeout(*args,**kwargs):
        raise socket.timeout("TEST_ONLY_NOT_A_REAL_KEY")
    class Opener:
        open = staticmethod(timeout)
    monkeypatch.setattr(model,"build_opener",lambda *args:Opener())
    with pytest.raises(model.ModelError) as caught:
        model.plan("样本统计")
    assert caught.value.code=="model_timeout"
    assert "TEST_ONLY" not in str(caught.value)


def test_capacity_denied_without_model_call(tmp_path,monkeypatch):
    settings(tmp_path,monkeypatch)
    model.capacity.acquire()
    model.capacity.acquire()
    try:
        with pytest.raises(model.ModelError,match="model_capacity_busy"):
            model.plan("样本统计")
    finally:
        model.capacity.release()
        model.capacity.release()
