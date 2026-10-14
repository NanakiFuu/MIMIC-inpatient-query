"""Real index comparison on original data; restore original index in transaction."""
from datetime import datetime,timezone
import json
from pathlib import Path
import statistics
import getpass

import psycopg

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"research/module3/query-plan.json"
QUERY="""SELECT labevent_id,charttime,valuenum,valueuom,ref_range_lower,ref_range_upper
 FROM query_api.lab_observation WHERE hadm_id=%s AND itemid=%s AND fluid='Blood'
 ORDER BY charttime,labevent_id LIMIT 200"""


def main():
    if OUT.exists():
        raise SystemExit("Do not overwrite recorded experiment")
    with psycopg.connect(dbname="medical_query_demo",user=getpass.getuser(),host="/var/run/postgresql") as conn:
        conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute("SET LOCAL statement_timeout='30s'")
        conn.execute("SELECT pg_advisory_xact_lock(71422001)")
        definition=conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='mimic_raw' AND indexname='labs_encounter_item_time_idx'").fetchone()
        if not definition:
            raise SystemExit("Expected project index missing")
        # pg_indexes is trusted local catalog; this restores only a known project index.
        if "ON mimic_raw.labevents USING btree (hadm_id, itemid, charttime, labevent_id)" not in definition[0]:
            raise SystemExit("Unexpected index definition; no change made")
        parameters=conn.execute("SELECT hadm_id,itemid FROM mimic_raw.labevents WHERE hadm_id IS NOT NULL AND itemid=50912 GROUP BY hadm_id,itemid ORDER BY count(*) DESC,hadm_id LIMIT 1").fetchone()
        conn.execute("ANALYZE mimic_raw.labevents")
        expected=conn.execute(QUERY,parameters).fetchall()
        def plans():
            conn.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "+QUERY,parameters).fetchone()
            return [conn.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "+QUERY,parameters).fetchone()[0][0] for _ in range(5)]
        conn.execute("DROP INDEX mimic_raw.labs_encounter_item_time_idx")
        before=plans()
        conn.execute(definition[0])
        after=plans()
        assert conn.execute(QUERY,parameters).fetchall()==expected
        record={"source":"本项目PostgreSQL实际EXPLAIN ANALYZE，临床记录来自MIMIC Demo2.2",
          "observed_at_utc":datetime.now(timezone.utc).isoformat(),"sql":QUERY,"parameters":parameters,
          "data_rows":conn.execute("SELECT count(*) FROM mimic_raw.labevents").fetchone()[0],
          "returned_rows":len(expected),"index_definition":definition[0],"before":before,"after":after,
          "cache_conditions":"两组各一次预热后五次测量；未清操作系统或数据库缓存，非冷缓存对照；建索引也会预热",
          "before_execution_median_ms":statistics.median(plan["Execution Time"] for plan in before),
          "after_execution_median_ms":statistics.median(plan["Execution Time"] for plan in after),
          "result_identical":True,"original_index_restored":True}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({key:record[key] for key in ["data_rows","returned_rows","before_execution_median_ms","after_execution_median_ms","result_identical"]}))


if __name__=="__main__":
    main()
