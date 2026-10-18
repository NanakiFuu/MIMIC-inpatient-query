import csv
import gzip
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from medical_query import database
from medical_query.api import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_connection_pool():
    """The pool is a process-wide singleton; give every test a fresh one."""
    yield
    database.close()


@pytest.fixture(scope="session")
def source():
    """Independent expected results from original CSV, not production query code."""
    result = {}
    for area,name in [("hosp","patients"),("hosp","admissions"),("hosp","diagnoses_icd"),
                      ("hosp","d_icd_diagnoses"),("hosp","labevents"),("hosp","d_labitems"),("icu","icustays")]:
        path = ROOT / "mimic-iv-clinical-database-demo-2.2" / area / f"{name}.csv.gz"
        with gzip.open(path,"rt",newline="",encoding="utf-8") as stream:
            result[name] = list(csv.DictReader(stream))
    return result


@pytest.fixture
def client():
    with TestClient(app,base_url="http://localhost") as instance:
        yield instance


@pytest.fixture(scope="session")
def target(source):
    candidates = sorted({int(row["hadm_id"]) for row in source["labevents"]
                         if row["hadm_id"] and row["itemid"]=="50912"})
    hadm = candidates[0]
    admission = next(row for row in source["admissions"] if int(row["hadm_id"])==hadm)
    return {"hadm_id":hadm,"subject_id":int(admission["subject_id"])}
