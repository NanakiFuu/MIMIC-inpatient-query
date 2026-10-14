"""Verified, atomic, repeatable snapshot import; never modifies source CSVs."""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import time
import getpass
import os

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
TABLES = [
    ("hosp", "patients", 100), ("hosp", "admissions", 275),
    ("hosp", "d_icd_diagnoses", 109775), ("hosp", "d_labitems", 1622),
    ("hosp", "diagnoses_icd", 4506), ("hosp", "labevents", 107727),
    ("icu", "icustays", 140),
]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_source(base):
    manifest = base / "SHA256SUMS.txt"
    expected = {}
    for line in manifest.read_text().splitlines():
        if line.strip():
            checksum, name = line.split(maxsplit=1)
            expected[name.lstrip("*")] = checksum
    files = []
    for area, table, rows in TABLES:
        relative = f"{area}/{table}.csv.gz"
        actual = digest(base / relative)
        if actual != expected.get(relative):
            raise ValueError(f"Official SHA256 mismatch: {relative}")
        with gzip.open(base / relative, "rt", newline="", encoding="utf-8") as stream:
            columns = next(csv.reader(stream))
        files.append({"path": relative, "table": table, "sha256": actual,
                      "expected_rows": rows, "columns": columns})
    return digest(manifest), files


def connection_config():
    """Peer auth by default; TCP with an explicit password for container targets.

    Default behaviour is unchanged (local socket, current user). When
    MEDICAL_QUERY_DB_PASSWORD is set the import targets the configured TCP
    endpoint instead, so the same verified import can seed the Compose database
    from the official CSVs without any private dump.
    """
    if os.environ.get("MEDICAL_QUERY_DB_PASSWORD"):
        return {"dbname": os.environ.get("MEDICAL_QUERY_DB_NAME", "medical_query_demo"),
                "user": os.environ.get("MEDICAL_QUERY_DB_USER", "postgres"),
                "host": os.environ.get("MEDICAL_QUERY_DB_HOST", "127.0.0.1"),
                "port": int(os.environ.get("MEDICAL_QUERY_DB_PORT", "5432")),
                "password": os.environ["MEDICAL_QUERY_DB_PASSWORD"]}
    return {"dbname": "medical_query_demo", "user": getpass.getuser(), "host": "/var/run/postgresql"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=ROOT / "research/module1/import-result.json")
    parser.add_argument('--source',type=Path,default=Path(os.environ.get('MEDICAL_QUERY_DATA_DIR',str(ROOT/'mimic-iv-clinical-database-demo-2.2'))))
    args = parser.parse_args()
    started = time.perf_counter()
    base = args.source
    manifest_hash, files = verify_source(base)
    with psycopg.connect(**connection_config()) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(71422001)")
        conn.execute("SET LOCAL lock_timeout='10s'")
        conn.execute("SET LOCAL statement_timeout='60s'")
        conn.execute((ROOT / "sql/schema.sql").read_text(encoding="utf-8"))
        names = [sql.Identifier("mimic_raw", entry["table"]) for entry in files]
        conn.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(",").join(names)))
        for entry in files:
            target = sql.Identifier("mimic_raw", entry["table"])
            actual_columns = [row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='mimic_raw' AND table_name=%s ORDER BY ordinal_position",
                (entry["table"],))]
            if actual_columns != entry["columns"]:
                raise ValueError(f"CSV/database schema mismatch: {entry['table']}")
            column_names = sql.SQL(",").join(sql.Identifier(name) for name in actual_columns)
            with conn.cursor().copy(sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV, HEADER true, NULL '', FORCE_NULL ({}))").format(target, column_names)) as copy:
                with gzip.open(base / entry["path"], "rb") as stream:
                    while block := stream.read(1024 * 1024):
                        copy.write(block)
            count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(target)).fetchone()[0]
            if count != entry["expected_rows"]:
                raise ValueError(f"Row count mismatch: {entry['table']}")
            entry["imported_rows"] = count
            print(f"Imported {entry['table']}: {count}", flush=True)
        checks = {
            "missing_lab_hadm_id": conn.execute("SELECT count(*) FROM mimic_raw.labevents WHERE hadm_id IS NULL").fetchone()[0],
            "numeric_labs": conn.execute("SELECT count(*) FROM mimic_raw.labevents WHERE valuenum IS NOT NULL").fetchone()[0],
            "admissions_with_icu": conn.execute("SELECT count(*) FROM query_api.encounter_overview WHERE has_icu").fetchone()[0],
            "encounter_view_rows": conn.execute("SELECT count(*) FROM query_api.encounter_overview").fetchone()[0],
            "diagnosis_view_rows": conn.execute("SELECT count(*) FROM query_api.diagnosis_listing").fetchone()[0],
            "lab_view_rows": conn.execute("SELECT count(*) FROM query_api.lab_observation").fetchone()[0],
        }
        expected_checks = {"missing_lab_hadm_id":28420,"numeric_labs":95246,
                           "admissions_with_icu":128,"encounter_view_rows":275,
                           "diagnosis_view_rows":4506,"lab_view_rows":107727}
        if checks != expected_checks:
            raise ValueError(f"Audit reconciliation failed: {checks}")
        for target in names:
            conn.execute(sql.SQL("ANALYZE {}").format(target))
        expected_source = (manifest_hash, [{k:v for k,v in entry.items() if k != "imported_rows"} for entry in files])
        if verify_source(base) != expected_source:
            raise ValueError("Source changed during import; transaction rolled back")
        elapsed = round(time.perf_counter()-started, 4)
        batch_id = conn.execute(
            "INSERT INTO project_audit.import_batches(source_version,source_url,manifest_sha256,files,validation,elapsed_seconds) VALUES (%s,%s,%s,%s,%s,%s) RETURNING batch_id",
            ("2.2","https://physionet.org/content/mimic-iv-demo/2.2/",manifest_hash,Jsonb(files),Jsonb(checks),elapsed),
        ).fetchone()[0]
    # Check again after commit. Report success only after both commit and source verification.
    if verify_source(base) != (manifest_hash, [{k:v for k,v in entry.items() if k != "imported_rows"} for entry in files]):
        raise ValueError("Source changed during import")
    result = {"status":"passed","batch_id":batch_id,"source_version":"2.2",
              "source_url":"https://physionet.org/content/mimic-iv-demo/2.2/",
              "manifest_sha256":manifest_hash,"files":files,"validation":checks,
              "elapsed_seconds":elapsed,"source_files_unchanged":True}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":"passed","batch_id":batch_id,"elapsed_seconds":elapsed}))


if __name__ == "__main__":
    main()
