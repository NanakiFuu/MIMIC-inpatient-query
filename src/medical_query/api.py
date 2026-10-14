import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
import psycopg

from . import queries
from .database import connect
from . import model, planning, diagnostics, telemetry, metrics_ai
from . import __version__

app = FastAPI(title="MIMIC Demo 住院查询",version=__version__,
              description="MIMIC-IV Clinical Database Demo 2.2；项目自建只读视图；受限工具规划与固定查询。")
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static",StaticFiles(directory=STATIC),name="static")
app.add_middleware(TrustedHostMiddleware,allowed_hosts=["localhost","127.0.0.1","[::1]"])
log = logging.getLogger("medical_query")
logging.basicConfig(level=logging.INFO,format="%(message)s")
state_lock = threading.Lock()
state = {"requests":0,"errors":0,"total_elapsed_ms":0.0,"last_error":None}
SOURCE = {"dataset":"MIMIC-IV Clinical Database Demo","version":"2.2",
          "url":"https://physionet.org/content/mimic-iv-demo/2.2/",
          "query_layer":"项目自建 query_api 视图","mode":"fixed_queries","ai_enabled":False}
SEMANTICS = ["数据仅描述 Demo 样本，不推断真实医院业务量或患病率。",
             "日期为按患者移位的脱敏日期；跨患者日历年份不可解释为真实医院年度。",
             "anchor_age 是锚定年龄，不是精确住院年龄；高龄按源规则处理。",
             "诊断为出院计费 ICD 编码；seq_num 为源排序。",
             "空异常标记不等于正常；数值缺失不补零；保留原单位。"]
PositiveId = Annotated[int,Query(gt=0,le=2147483647)]
IcdCode = Annotated[str,Query(min_length=1,max_length=12,pattern=r"^[A-Za-z0-9]+$")]
IcdVersion = Annotated[int,Query(ge=9,le=10)]
Limit = Annotated[int,Query(ge=1,le=200)]
Offset = Annotated[int,Query(ge=0,le=10000)]
LabConcept = Literal["creatinine","potassium","glucose","hemoglobin"]
AbnormalMode = Literal["all","source_flag","reference_range"]


def evidence(request,template_id,conditions,result):
    return {"source":SOURCE,"template_id":template_id,
            "request_id":request.state.request_id,"conditions":conditions,"result":result}


def paired_icd(code,version):
    if (code is None) != (version is None):
        raise HTTPException(422,detail="icd_code 与 icd_version 必须同时提供；不猜测编码版本")


def db_error(exc):
    if isinstance(exc,psycopg.errors.QueryCanceled):
        return 504,"database_query_timeout"
    if isinstance(exc,psycopg.errors.LockNotAvailable):
        return 503,"database_lock_timeout"
    if isinstance(exc,(psycopg.OperationalError,psycopg.InterfaceError)):
        return 503,"database_unavailable"
    return 500,"database_error"


@app.middleware("http")
async def request_metrics(request,call_next):
    request.state.request_id = uuid.uuid4().hex
    started = time.perf_counter()
    status = 500
    try:
        # Local AI endpoints incur provider usage. Reject cross-site browser POSTs.
        origin=request.headers.get("origin")
        if request.method=="POST" and origin is not None and origin!=str(request.base_url).rstrip("/"):
            status=403
            return JSONResponse(status_code=403,content={"error":"cross_origin_request_denied"})
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request.state.request_id
        return response
    finally:
        elapsed = round((time.perf_counter()-started)*1000,3)
        route = request.scope.get("route")
        # Never log URL/query/body, patient IDs, values, credentials or error strings.
        route_name = getattr(route,"path","unmatched")
        telemetry.observe_http(request.method, route_name, status, (time.perf_counter()-started))
        with state_lock:
            state["requests"] += 1
            state["total_elapsed_ms"] += elapsed
            if status>=500:
                state["errors"] += 1
        log.info(json.dumps({"event":"request","request_id":request.state.request_id,
                             "method":request.method,"route":route_name,
                             "status":status,"elapsed_ms":elapsed}))


@app.exception_handler(psycopg.Error)
async def handle_database(request,exc):
    status, code = db_error(exc)
    with state_lock:
        state["last_error"] = {"code":code,"request_id":request.state.request_id,"timestamp":time.time()}
    return JSONResponse(status_code=status,content={"error":code,"request_id":request.state.request_id})


@app.exception_handler(queries.EncounterNotFound)
async def handle_missing(request,exc):
    return JSONResponse(status_code=404,content={"error":"admission_not_found","request_id":request.state.request_id})


@app.exception_handler(FileNotFoundError)
@app.exception_handler(PermissionError)
@app.exception_handler(ValueError)
async def handle_configuration(request,exc):
    # Safe response for missing/unreadable/malformed local credential files.
    return JSONResponse(status_code=503,content={"error":"database_configuration_unavailable","request_id":request.state.request_id})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health/live")
def live():
    return {"status":"up","model_status_endpoint":"/api/model/status"}


@app.get('/metrics', include_in_schema=False)
def prometheus_metrics():
    return Response(telemetry.render_database_probe(connect),
                    media_type='text/plain; version=0.0.4; charset=utf-8')


@app.get("/health/ready")
def ready():
    with connect() as conn:
        check = conn.execute("SELECT EXISTS (SELECT 1 FROM query_api.encounter_overview) AS loaded").fetchone()
    if not check["loaded"]:
        raise HTTPException(503,detail="dataset_not_loaded")
    return {"status":"ready","database":"reachable","dataset":"MIMIC Demo 2.2",
            "model_status_endpoint":"/api/model/status"}


@app.get("/api/catalog")
def catalog():
    return {"source":SOURCE,"lab_concepts":queries.LAB_CONCEPTS,"semantics":SEMANTICS,
            "abnormal_modes":{"all":"所有源记录","source_flag":"源 flag=abnormal",
                              "reference_range":"数值超出有效的完整源参考区间"}}


@app.get("/api/admissions")
def list_admissions(request:Request,subject_id:PositiveId|None=None,
                    has_icu:bool|None=None,icd_code:IcdCode|None=None,
                    icd_version:IcdVersion|None=None,limit:Limit=50,offset:Offset=0):
    paired_icd(icd_code,icd_version)
    conditions = {"subject_id":subject_id,"has_icu":has_icu,"icd_code":icd_code,"icd_version":icd_version}
    return evidence(request,"admissions.v1",conditions,
                    queries.admissions(**conditions,limit=limit,offset=offset))


@app.get("/api/admissions/{hadm_id}")
def admission_detail(request:Request,hadm_id:int):
    if not 0<hadm_id<=2147483647:
        raise HTTPException(422,detail="hadm_id 必须是正整数")
    return evidence(request,"encounter.v1",{"hadm_id":hadm_id},queries.encounter(hadm_id))


@app.get("/api/admissions/{hadm_id}/diagnoses")
def admission_diagnoses(request:Request,hadm_id:int):
    if not 0<hadm_id<=2147483647:
        raise HTTPException(422,detail="hadm_id 必须是正整数")
    return evidence(request,"diagnoses.v1",{"hadm_id":hadm_id},queries.diagnoses(hadm_id))


@app.get("/api/admissions/{hadm_id}/labs")
def admission_labs(request:Request,hadm_id:int,concept:LabConcept,
                   abnormal:AbnormalMode="all",limit:Limit=100,offset:Offset=0):
    if not 0<hadm_id<=2147483647:
        raise HTTPException(422,detail="hadm_id 必须是正整数")
    conditions = {"hadm_id":hadm_id,"concept":concept,"abnormal":abnormal}
    return evidence(request,"labs.v1",conditions,queries.labs(**conditions,limit=limit,offset=offset))


@app.get("/api/summary")
def admission_summary(request:Request,subject_id:PositiveId|None=None,
                      has_icu:bool|None=None,icd_code:IcdCode|None=None,
                      icd_version:IcdVersion|None=None):
    paired_icd(icd_code,icd_version)
    conditions = {"subject_id":subject_id,"has_icu":has_icu,"icd_code":icd_code,"icd_version":icd_version}
    result = queries.summary(**conditions)
    result["duration_definition"] = "各住院 dischtime-admittime 的小时数；平均值分母见 hospital_duration_denominator"
    result["icu_definition"] = "含至少一条真实 icustays 记录的住院数；不按住院边界截断 ICU 时间"
    return evidence(request,"summary.v1",conditions,result)


@app.get("/ops/snapshot")
def operations_snapshot():
    with state_lock:
        current = dict(state)
    return {"source":"本项目运行实测，进程启动后累计；非 MIMIC 数据",
            "metrics":current,"model":model.status(),"scope":"仅本机维护"}


class NaturalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    question: Annotated[str,Field(min_length=1,max_length=1000)]


@app.exception_handler(model.ModelError)
async def handle_model_error(request,exc):
    return JSONResponse(status_code=exc.status,content={"error":exc.code,
                        "request_id":request.state.request_id,"model_evidence":exc.evidence,
                        "fallback":"固定查询仍可用"})


@app.get("/api/model/status")
def model_status():
    return model.status()


@app.post("/api/query")
def natural_query(request:Request,body:NaturalQuery):
    question = body.question.strip()
    if not question:
        raise HTTPException(422,detail="请输入查询问题")
    decision = model.plan(question)
    # Never send query results back to the remote model. Interpret deterministically.
    source = {**SOURCE,"mode":"model_tool_plan","ai_enabled":True}
    try:
        outcome = planning.execute(decision["tool"],decision["arguments"])
    except queries.EncounterNotFound:
        telemetry.observe_tool('no_data')
        return JSONResponse(status_code=404,content={"error":"admission_not_found",
                    "source":source,"request_id":request.state.request_id,
                    "template_id":decision["tool"]+".v1","conditions":decision["arguments"],
                    "plan":decision,"result":{"status":"no_data"}})
    except Exception:
        telemetry.observe_tool('error')
        raise
    telemetry.observe_tool({'answered':'success','clarification':'clarification','refused':'refused'}[outcome['status']])
    return {"source":source,"request_id":request.state.request_id,
            "template_id":decision["tool"]+".v1","conditions":decision["arguments"],
            "plan":decision,"result":outcome}


@app.get("/ops/evidence")
def operations_evidence():
    with state_lock:
        metrics=dict(state)
    return diagnostics.observe(metrics)


@app.post("/ops/diagnose")
def operations_diagnosis(request:Request):
    with state_lock:
        metrics=dict(state)
    snapshot=diagnostics.observe(metrics)
    return {"request_id":request.state.request_id,"evidence":snapshot,
            "diagnosis":diagnostics.interpret(snapshot)}


@app.post("/ops/metrics-diagnosis")
def operations_metrics_diagnosis(request:Request):
    """Read-only Prometheus whitelist -> model judgment + local rule validation."""
    snapshot=metrics_ai.collect()
    return {"request_id":request.state.request_id,"evidence":snapshot,
            "diagnosis":metrics_ai.interpret(snapshot)}
