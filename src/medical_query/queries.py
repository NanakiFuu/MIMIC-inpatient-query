"""Bounded, parameterized query templates. No model-generated SQL is accepted."""
from .database import connect

LAB_CONCEPTS = {
    "creatinine": {"itemid":50912,"label":"血肌酐","source_label":"Creatinine","fluid":"Blood","unit":"mg/dL"},
    "potassium": {"itemid":50971,"label":"血钾","source_label":"Potassium","fluid":"Blood","unit":"mEq/L"},
    "glucose": {"itemid":50931,"label":"血糖","source_label":"Glucose","fluid":"Blood","unit":"mg/dL"},
    "hemoglobin": {"itemid":51222,"label":"血红蛋白","source_label":"Hemoglobin","fluid":"Blood","unit":"g/dL"},
}


class EncounterNotFound(Exception):
    pass


def require_encounter(conn, hadm_id):
    if not conn.execute("SELECT 1 FROM query_api.encounter_overview WHERE hadm_id=%s",(hadm_id,)).fetchone():
        raise EncounterNotFound()


def filters(subject_id=None, has_icu=None, icd_code=None, icd_version=None):
    # Identifiers and predicates are code-owned constants; user values are bind parameters.
    predicates, params = [], []
    if subject_id is not None:
        predicates.append("e.subject_id=%s")
        params.append(subject_id)
    if has_icu is not None:
        predicates.append("e.has_icu=%s")
        params.append(has_icu)
    if icd_code is not None:
        predicates.append("EXISTS (SELECT 1 FROM query_api.diagnosis_listing d WHERE d.hadm_id=e.hadm_id AND d.icd_code=%s AND d.icd_version=%s)")
        params.extend([icd_code,icd_version])
    return (" WHERE " + " AND ".join(predicates) if predicates else ""), params


def admissions(subject_id=None, has_icu=None, icd_code=None, icd_version=None, limit=50, offset=0):
    where, params = filters(subject_id,has_icu,icd_code,icd_version)
    with connect() as conn:
        # One MVCC snapshot for count + page when an import runs concurrently.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        total = conn.execute("SELECT count(*) AS n FROM query_api.encounter_overview e"+where,params).fetchone()["n"]
        rows = conn.execute("SELECT e.* FROM query_api.encounter_overview e"+where+" ORDER BY e.admittime,e.hadm_id LIMIT %s OFFSET %s",[*params,limit,offset]).fetchall()
    return {"total":total,"rows":rows,"limit":limit,"offset":offset}


def encounter(hadm_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM query_api.encounter_overview WHERE hadm_id=%s",(hadm_id,)).fetchone()
        if row is None:
            raise EncounterNotFound()
        return row


def diagnoses(hadm_id):
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        require_encounter(conn,hadm_id)
        return conn.execute("SELECT * FROM query_api.diagnosis_listing WHERE hadm_id=%s ORDER BY seq_num,icd_version,icd_code",(hadm_id,)).fetchall()


def labs(hadm_id, concept, abnormal="all", limit=100, offset=0):
    item = LAB_CONCEPTS[concept]
    where = " WHERE hadm_id=%s AND itemid=%s AND fluid=%s"
    params = [hadm_id,item["itemid"],item["fluid"]]
    if abnormal == "source_flag":
        where += " AND source_abnormal IS TRUE"
    elif abnormal == "reference_range":
        where += " AND outside_reference_range IS TRUE"
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        require_encounter(conn,hadm_id)
        total = conn.execute("SELECT count(*) AS n FROM query_api.lab_observation"+where,params).fetchone()["n"]
        # Different or missing units remain visible, but are explicitly excluded from numeric trends.
        rows = conn.execute("SELECT l.*, (valuenum IS NOT NULL AND valueuom=%s) IS TRUE AS trend_eligible FROM query_api.lab_observation l"+where+" ORDER BY charttime,labevent_id LIMIT %s OFFSET %s",[item["unit"],*params,limit,offset]).fetchall()
    return {"concept":concept,"item":item,"abnormal":abnormal,"total":total,
            "rows":rows,"limit":limit,"offset":offset,
            "missing_hadm_policy":"只包含明确归属于该 hadm_id 的源记录；未按时间补归属",
            "trend_policy":"仅 trend_eligible=true 的数值可按注册单位绘制趋势；其余记录保留原值与单位"}


def summary(subject_id=None, has_icu=None, icd_code=None, icd_version=None):
    where, params = filters(subject_id,has_icu,icd_code,icd_version)
    with connect() as conn:
        return conn.execute("""SELECT count(*) AS admission_count,
          count(DISTINCT subject_id) AS patient_count,
          count(*) FILTER (WHERE has_icu) AS admissions_with_icu,
          coalesce(sum(icu_stay_count),0) AS icu_stay_count,
          count(hospital_hours) AS hospital_duration_denominator,
          avg(hospital_hours) AS average_hospital_hours,
          min(hospital_hours) AS minimum_hospital_hours,
          max(hospital_hours) AS maximum_hospital_hours
          FROM query_api.encounter_overview e"""+where,params).fetchone()
