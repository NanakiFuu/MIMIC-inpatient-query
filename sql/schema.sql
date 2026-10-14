-- Raw tables mirror the seven actual MIMIC-IV Demo 2.2 CSV headers.
-- Empty CSV cells map to SQL NULL; dates stay shifted, timezone-free timestamps.
CREATE SCHEMA IF NOT EXISTS mimic_raw;
CREATE SCHEMA IF NOT EXISTS query_api;
CREATE SCHEMA IF NOT EXISTS project_audit;
COMMENT ON SCHEMA mimic_raw IS 'MIMIC-IV Clinical Database Demo 2.2 CSV import; not hospital live data';
COMMENT ON SCHEMA query_api IS 'Project-built query views, not native MIMIC tables';
COMMENT ON SCHEMA project_audit IS 'Project-generated import evidence, not MIMIC clinical data';

CREATE TABLE IF NOT EXISTS mimic_raw.patients (
 subject_id integer PRIMARY KEY, gender text NOT NULL, anchor_age integer NOT NULL,
 anchor_year integer NOT NULL, anchor_year_group text NOT NULL, dod date
);
CREATE TABLE IF NOT EXISTS mimic_raw.admissions (
 subject_id integer NOT NULL REFERENCES mimic_raw.patients,
 hadm_id integer PRIMARY KEY, admittime timestamp NOT NULL, dischtime timestamp NOT NULL,
 deathtime timestamp, admission_type text NOT NULL, admit_provider_id text,
 admission_location text, discharge_location text, insurance text, language text,
 marital_status text, race text, edregtime timestamp, edouttime timestamp,
 hospital_expire_flag smallint NOT NULL CHECK (hospital_expire_flag IN (0,1)),
 UNIQUE (hadm_id, subject_id)
);
CREATE TABLE IF NOT EXISTS mimic_raw.d_icd_diagnoses (
 icd_code text NOT NULL, icd_version smallint NOT NULL CHECK (icd_version IN (9,10)),
 long_title text NOT NULL, PRIMARY KEY (icd_code,icd_version)
);
CREATE TABLE IF NOT EXISTS mimic_raw.d_labitems (
 itemid integer PRIMARY KEY, label text, fluid text, category text
);
CREATE TABLE IF NOT EXISTS mimic_raw.diagnoses_icd (
 subject_id integer NOT NULL REFERENCES mimic_raw.patients, hadm_id integer NOT NULL,
 seq_num integer NOT NULL, icd_code text NOT NULL, icd_version smallint NOT NULL,
 PRIMARY KEY (hadm_id,seq_num),
 FOREIGN KEY (hadm_id,subject_id) REFERENCES mimic_raw.admissions(hadm_id,subject_id),
 FOREIGN KEY (icd_code,icd_version) REFERENCES mimic_raw.d_icd_diagnoses
);
CREATE TABLE IF NOT EXISTS mimic_raw.labevents (
 labevent_id integer PRIMARY KEY, subject_id integer NOT NULL REFERENCES mimic_raw.patients,
 hadm_id integer, specimen_id integer, itemid integer NOT NULL REFERENCES mimic_raw.d_labitems,
 order_provider_id text, charttime timestamp NOT NULL, storetime timestamp,
 value text, valuenum numeric, valueuom text, ref_range_lower numeric,
 ref_range_upper numeric, flag text, priority text, comments text,
 FOREIGN KEY (hadm_id,subject_id) REFERENCES mimic_raw.admissions(hadm_id,subject_id)
);
CREATE TABLE IF NOT EXISTS mimic_raw.icustays (
 subject_id integer NOT NULL REFERENCES mimic_raw.patients, hadm_id integer NOT NULL,
 stay_id integer PRIMARY KEY, first_careunit text, last_careunit text,
 intime timestamp NOT NULL, outtime timestamp NOT NULL, los numeric,
 FOREIGN KEY (hadm_id,subject_id) REFERENCES mimic_raw.admissions(hadm_id,subject_id)
);
CREATE TABLE IF NOT EXISTS project_audit.import_batches (
 batch_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 completed_at timestamptz NOT NULL DEFAULT now(), source_version text NOT NULL,
 source_url text NOT NULL, manifest_sha256 text NOT NULL,
 files jsonb NOT NULL, validation jsonb NOT NULL, elapsed_seconds numeric NOT NULL
);

-- Every view below is built by this project. ICU rows are aggregated first.
CREATE OR REPLACE VIEW query_api.encounter_overview AS
 SELECT a.*, p.gender,p.anchor_age,p.anchor_year,p.anchor_year_group,
 extract(epoch FROM (a.dischtime-a.admittime))/3600 AS hospital_hours,
 coalesce(i.icu_stay_count,0) AS icu_stay_count,
 coalesce(i.icu_stay_count,0)>0 AS has_icu,
 i.icu_hours
 FROM mimic_raw.admissions a JOIN mimic_raw.patients p USING(subject_id)
 LEFT JOIN (
   SELECT hadm_id,count(*) AS icu_stay_count,
     sum(extract(epoch FROM (outtime-intime))/3600) AS icu_hours
   FROM mimic_raw.icustays GROUP BY hadm_id
 ) i USING(hadm_id);

CREATE OR REPLACE VIEW query_api.diagnosis_listing AS
 SELECT d.subject_id,d.hadm_id,d.seq_num,d.icd_code,d.icd_version,c.long_title
 FROM mimic_raw.diagnoses_icd d
 JOIN mimic_raw.d_icd_diagnoses c USING(icd_code,icd_version);

CREATE OR REPLACE VIEW query_api.lab_observation AS
 SELECT l.*, d.label,d.fluid,d.category,
 CASE WHEN l.valuenum IS NOT NULL AND l.ref_range_lower IS NOT NULL
   AND l.ref_range_upper IS NOT NULL AND l.ref_range_lower<=l.ref_range_upper
   THEN (l.valuenum<l.ref_range_lower OR l.valuenum>l.ref_range_upper)
   ELSE NULL END AS outside_reference_range,
 CASE WHEN l.flag='abnormal' THEN true ELSE NULL END AS source_abnormal
 FROM mimic_raw.labevents l JOIN mimic_raw.d_labitems d USING(itemid);

COMMENT ON VIEW query_api.encounter_overview IS 'Project-built: one admission per row; anchor age is not exact admission age; shifted dates';
COMMENT ON VIEW query_api.diagnosis_listing IS 'Project-built: discharge billing ICD codes; seq_num is source order, not inferred clinical importance';
COMMENT ON VIEW query_api.lab_observation IS 'Project-built: original units; missing hadm_id preserved; NULL abnormal status is unknown';

-- These are query-support indexes; performance comparisons will be real experiments later.
CREATE INDEX IF NOT EXISTS admissions_subject_idx ON mimic_raw.admissions(subject_id,admittime,hadm_id);
CREATE INDEX IF NOT EXISTS diagnosis_code_idx ON mimic_raw.diagnoses_icd(icd_version,icd_code,hadm_id);
CREATE INDEX IF NOT EXISTS labs_encounter_item_time_idx ON mimic_raw.labevents(hadm_id,itemid,charttime,labevent_id);
CREATE INDEX IF NOT EXISTS icustays_hadm_idx ON mimic_raw.icustays(hadm_id);

REVOKE ALL ON SCHEMA mimic_raw,project_audit FROM PUBLIC,medical_query_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA mimic_raw,project_audit FROM PUBLIC,medical_query_reader;
REVOKE ALL ON SCHEMA query_api FROM PUBLIC;
GRANT USAGE ON SCHEMA query_api TO medical_query_reader;
GRANT SELECT ON query_api.encounter_overview,query_api.diagnosis_listing,query_api.lab_observation TO medical_query_reader;
