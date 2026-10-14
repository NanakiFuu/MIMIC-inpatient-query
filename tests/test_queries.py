from datetime import datetime
from decimal import Decimal
import json

import psycopg
import pytest

from medical_query.database import connect


def result(response):
    assert response.status_code==200,response.text
    payload = response.json()
    assert payload["source"]["version"]=="2.2"
    assert payload["source"]["ai_enabled"] is False
    assert payload["request_id"]==response.headers["X-Request-ID"]
    return payload["result"]


def test_summary_matches_csv(client,source):
    actual = result(client.get("/api/summary"))
    admissions = source["admissions"]
    icu_hadm = {row["hadm_id"] for row in source["icustays"]}
    hours = [(datetime.fromisoformat(row["dischtime"])-datetime.fromisoformat(row["admittime"])).total_seconds()/3600 for row in admissions]
    assert actual["admission_count"]==len(admissions)==275
    assert actual["patient_count"]==len({row["subject_id"] for row in admissions})==100
    assert actual["admissions_with_icu"]==len(icu_hadm)==128
    assert actual["icu_stay_count"]==len(source["icustays"])==140
    assert actual["hospital_duration_denominator"]==len(hours)
    assert actual["average_hospital_hours"]==pytest.approx(sum(hours)/len(hours))
    assert actual["minimum_hospital_hours"]==pytest.approx(min(hours))
    assert actual["maximum_hospital_hours"]==pytest.approx(max(hours))


def test_patient_admissions_and_pagination(client,source,target):
    expected = sorted([row for row in source["admissions"] if int(row["subject_id"])==target["subject_id"]],key=lambda row:(row["admittime"],int(row["hadm_id"])))
    actual = result(client.get("/api/admissions",params={"subject_id":target["subject_id"],"limit":1}))
    assert actual["total"]==len(expected)
    assert len(actual["rows"])==1
    assert actual["rows"][0]["hadm_id"]==int(expected[0]["hadm_id"])
    page = result(client.get("/api/admissions",params={"subject_id":target["subject_id"],"limit":1,"offset":1}))
    assert [row["hadm_id"] for row in page["rows"]]==[int(row["hadm_id"]) for row in expected[1:2]]


@pytest.mark.parametrize("has_icu",[True,False])
def test_icu_filter_no_join_multiplication(client,source,has_icu):
    ids = {row["hadm_id"] for row in source["icustays"]}
    expected = [row for row in source["admissions"] if (row["hadm_id"] in ids)==has_icu]
    actual = result(client.get("/api/summary",params={"has_icu":str(has_icu).lower()}))
    assert actual["admission_count"]==len(expected)
    assert actual["patient_count"]==len({row["subject_id"] for row in expected})


def test_encounter_detail_exact_source(client,source,target):
    actual = result(client.get(f"/api/admissions/{target['hadm_id']}"))
    expected = next(row for row in source["admissions"] if int(row["hadm_id"])==target["hadm_id"])
    patient = next(row for row in source["patients"] if int(row["subject_id"])==target["subject_id"])
    assert actual["subject_id"]==int(expected["subject_id"])
    assert actual["admittime"]==expected["admittime"].replace(" ","T")
    assert actual["anchor_age"]==int(patient["anchor_age"])
    assert actual["anchor_year_group"]==patient["anchor_year_group"]
    icu = [row for row in source["icustays"] if int(row["hadm_id"])==target["hadm_id"]]
    assert actual["icu_stay_count"]==len(icu)


def test_diagnoses_composite_dictionary(client,source,target):
    dictionary = {(row["icd_code"],row["icd_version"]):row["long_title"] for row in source["d_icd_diagnoses"]}
    expected = sorted([row for row in source["diagnoses_icd"] if int(row["hadm_id"])==target["hadm_id"]],key=lambda row:int(row["seq_num"]))
    actual = result(client.get(f"/api/admissions/{target['hadm_id']}/diagnoses"))
    assert len(actual)==len(expected)
    for item,row in zip(actual,expected,strict=True):
        assert item["icd_code"]==row["icd_code"]
        assert item["icd_version"]==int(row["icd_version"])
        assert item["long_title"]==dictionary[(row["icd_code"],row["icd_version"])]


@pytest.mark.parametrize("version",[9,10])
def test_diagnosis_filter_csv(client,source,version):
    rows = [row for row in source["diagnoses_icd"] if int(row["icd_version"])==version]
    code = rows[0]["icd_code"]
    ids = {row["hadm_id"] for row in rows if row["icd_code"]==code}
    actual = result(client.get("/api/admissions",params={"icd_code":code,"icd_version":version,"limit":200}))
    assert actual["total"]==len(ids)
    assert {str(row["hadm_id"]) for row in actual["rows"]}==ids


@pytest.mark.parametrize("concept,itemid,unit",[("creatinine","50912","mg/dL"),("potassium","50971","mEq/L"),("glucose","50931","mg/dL"),("hemoglobin","51222","g/dL")])
@pytest.mark.parametrize("mode",["all","source_flag","reference_range"])
def test_lab_rows_and_semantics_from_csv(client,source,target,concept,itemid,unit,mode):
    original = [row for row in source["labevents"] if row["hadm_id"]==str(target["hadm_id"]) and row["itemid"]==itemid]
    def outside(row):
        if not all(row[field] for field in ("valuenum","ref_range_lower","ref_range_upper")):
            return None
        value,low,high = [Decimal(row[field]) for field in ("valuenum","ref_range_lower","ref_range_upper")]
        if low>high:
            return None
        return value<low or value>high
    expected = sorted([row for row in original if mode=="all" or (mode=="source_flag" and row["flag"]=="abnormal") or (mode=="reference_range" and outside(row) is True)],key=lambda row:(row["charttime"],int(row["labevent_id"])))
    actual_rows = []
    for offset in range(0,len(expected)+1,200):
        actual = result(client.get(f"/api/admissions/{target['hadm_id']}/labs",params={"concept":concept,"abnormal":mode,"limit":200,"offset":offset}))
        assert actual["total"]==len(expected)
        actual_rows.extend(actual["rows"])
    assert [row["labevent_id"] for row in actual_rows]==[int(row["labevent_id"]) for row in expected]
    for actual,row in zip(actual_rows,expected,strict=True):
        assert actual["value"]==(row["value"] or None)
        assert actual["valueuom"]==(row["valueuom"] or None)
        assert actual["valuenum"]==pytest.approx(float(row["valuenum"])) if row["valuenum"] else actual["valuenum"] is None
        assert actual["outside_reference_range"]==outside(row)
        assert actual["source_abnormal"]==(True if row["flag"]=="abnormal" else None)
        assert actual["trend_eligible"]==bool(row["valuenum"] and row["valueuom"]==unit)


def test_empty_patient_is_empty_result(client):
    actual = result(client.get("/api/admissions",params={"subject_id":1}))
    assert actual["total"]==0 and actual["rows"]==[]
    summary = result(client.get("/api/summary",params={"subject_id":1}))
    assert summary["admission_count"]==0
    assert summary["average_hospital_hours"] is None


@pytest.mark.parametrize("suffix",["","/diagnoses","/labs?concept=creatinine"])
def test_unknown_encounter(client,suffix):
    response = client.get("/api/admissions/1"+suffix)
    assert response.status_code==404
    assert response.json()["error"]=="admission_not_found"


@pytest.mark.parametrize("url",[
    "/api/admissions?subject_id=-1", "/api/admissions?limit=201",
    "/api/admissions?offset=10001", "/api/admissions?icd_code=I10",
    "/api/summary?icd_version=10", "/api/summary?icd_code=I10&icd_version=11",
    "/api/admissions?icd_code=x%27%3Bdrop&icd_version=10",
    "/api/admissions/0", "/api/admissions/2147483648/diagnoses",
    "/api/admissions/1/labs?concept=urine_creatinine",
    "/api/admissions/1/labs?concept=creatinine&abnormal=normal",
])
def test_invalid_inputs_rejected(client,url):
    assert client.get(url).status_code==422


def test_app_db_permissions_and_session_limits():
    with connect() as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"]=="on"
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"]=="3s"
        assert conn.execute("SELECT current_user AS name").fetchone()["name"]=="medical_query_reader"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect() as conn:
            conn.execute("SELECT * FROM mimic_raw.patients LIMIT 1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect() as conn:
            conn.execute("SELECT * FROM project_audit.import_batches LIMIT 1")
    with connect() as conn:
        granted = conn.execute("SELECT has_table_privilege(current_user,'query_api.diagnosis_listing','INSERT,UPDATE,DELETE,TRUNCATE') AS writable").fetchone()
        assert granted["writable"] is False
    with pytest.raises((psycopg.errors.InsufficientPrivilege,psycopg.errors.ReadOnlySqlTransaction)):
        with connect() as conn:
            conn.execute("DELETE FROM mimic_raw.diagnoses_icd WHERE false")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect() as conn:
            # A user can override a read-only default; privileges must still deny writes.
            conn.execute("SET TRANSACTION READ WRITE")
            conn.execute("CREATE TEMP TABLE should_be_denied (id integer)")


def test_source_quality_preserved(source):
    # Owner read checks are independent of the restricted service role.
    with psycopg.connect(dbname="medical_query_demo",user="nanaki",host="/var/run/postgresql") as conn:
        missing = conn.execute("SELECT count(*) FROM mimic_raw.labevents WHERE hadm_id IS NULL").fetchone()[0]
        assert missing==sum(not row["hadm_id"] for row in source["labevents"])==28420
        assert conn.execute("SELECT count(*) FROM project_audit.import_batches").fetchone()[0]>=2
        assert conn.execute("SELECT count(*) FROM query_api.lab_observation").fetchone()[0]==len(source["labevents"])


def test_health_catalog_and_local_metrics(client,source):
    assert client.get("/health/live").json()["status"]=="up"
    assert client.get("/health/ready").json()["status"]=="ready"
    catalog = client.get("/api/catalog").json()
    dictionary = {int(row["itemid"]):row for row in source["d_labitems"]}
    for item in catalog["lab_concepts"].values():
        assert dictionary[item["itemid"]]["label"]==item["source_label"]
        assert dictionary[item["itemid"]]["fluid"]=="Blood"
    assert client.get("/ops/snapshot").json()["metrics"]["requests"]>0


def test_logs_redact_path_values_and_parameters(client,target,caplog):
    with caplog.at_level("INFO",logger="medical_query"):
        client.get(f"/api/admissions/{target['hadm_id']}?private=do-not-log")
    messages = [row.message for row in caplog.records if row.name=="medical_query"]
    assert messages
    for message in messages:
        assert str(target["hadm_id"]) not in message
        assert "do-not-log" not in message
        payload = json.loads(message)
        assert payload["route"]=="/api/admissions/{hadm_id}"


def test_unavailable_db_keeps_liveness_and_ops(client,monkeypatch,tmp_path):
    # Explicit configuration-failure test, not a claimed real PostgreSQL outage experiment.
    monkeypatch.setenv("MEDICAL_QUERY_DB_CONFIG",str(tmp_path/"missing-test-config.json"))
    response = client.get("/health/ready")
    assert response.status_code==503
    assert response.json()["error"]=="database_configuration_unavailable"
    assert client.get("/health/live").status_code==200
    assert client.get("/ops/snapshot").status_code==200
