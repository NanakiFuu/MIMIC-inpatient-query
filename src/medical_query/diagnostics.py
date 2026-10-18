"""Read-only interpretation of this project's runtime observations.

The model receives no fault labels, clinical rows, credentials, or shell access.
It chooses a constrained finding and citations; explanation/actions are local.
"""
from datetime import datetime, timezone
import json
import time
from typing import Annotated, Literal

from pydantic import BaseModel,ConfigDict,Field,ValidationError
import psycopg
from psycopg_pool import PoolTimeout

from .database import connect
from . import model, telemetry

VERSION="ops-evidence-v1"
FINDINGS={
 "database_unreachable":"数据库探测失败，当前证据只能确认连接或数据库依赖异常，不能直接认定数据库服务已停止。",
 "service_healthy":"本次数据库探测通过。历史错误计数仍保留，不能据此认定所有依赖或长期运行都健康。",
 "recent_query_error":"最近记录有数据库查询错误，需要结合错误类型与查询范围进一步检查。",
 "model_dependency_error":"记录中存在模型依赖错误，需检查连接、超时或本地配置；固定查询可独立使用。",
 "insufficient_evidence":"当前证据不足以定位原因，先核对依赖探测及最近项目日志。",
}
STEPS={
 "check_database_service":"在 Ubuntu 查看 systemctl status postgresql@16-main；状态确认后再决定恢复动作。",
 "inspect_project_logs":"查看 journalctl -u medical-query.service -n 30 --no-pager，并按请求标识定位错误。",
 "check_model_config":"检查本地模型配置、账户可用性及官方端点连通性；不要输出密钥。",
 "retry_fixed_query":"用固定条件重复同一查询，区分模型依赖与数据库问题。",
 "narrow_query":"缩小查询范围并查看指定模板的 EXPLAIN 记录，当前证据尚不足以断言索引缺失。",
}


class Diagnosis(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    finding:Literal["database_unreachable","service_healthy","recent_query_error","model_dependency_error","insufficient_evidence"]
    evidence_ids:Annotated[list[Literal["db_probe","request_metrics","recent_db_error","model_state"]],Field(min_length=1,max_length=4)]
    next_checks:Annotated[list[Literal["check_database_service","inspect_project_logs","check_model_config","retry_fixed_query","narrow_query"]],Field(min_length=1,max_length=3)]


def observe(metrics):
    database={"id":"db_probe","reachable":False,"error":None}
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        database["reachable"]=True
    except PoolTimeout:
        # No connection could be borrowed in time. On its own that does not prove
        # the database is down, so it must not be labelled like a connection
        # error: PoolTimeout subclasses OperationalError.
        database["error"]="pool_timeout"
    except psycopg.Error as error:
        database["error"]="connection_error" if isinstance(error,psycopg.OperationalError) else "database_error"
    except (OSError,ValueError):
        database["error"]="configuration_error"
    model_state=model.status()
    observations=[database,
       {"id":"request_metrics","requests":metrics["requests"],"server_errors":metrics["errors"],
        "scope":"current_application_process"},
       {"id":"recent_db_error","last_error":metrics["last_error"]},
       {"id":"model_state","configured":model_state["configured"],"calls":model_state["metrics"]["calls"],
        "failures":model_state["metrics"]["failures"],"last_error":model_state["metrics"]["last_error"]}]
    return {"source":"本项目运行实测，非MIMIC数据","observed_at_utc":datetime.now(timezone.utc).isoformat(),"observations":observations}


def rule_finding(snapshot):
    by_id={item["id"]:item for item in snapshot["observations"]}
    if not by_id["db_probe"]["reachable"]:
        return "database_unreachable"
    if by_id["recent_db_error"]["last_error"]:
        return "recent_query_error"
    if by_id["model_state"]["last_error"]:
        return "model_dependency_error"
    return "service_healthy"


def interpret(snapshot):
    settings=model.config()
    if not model.capacity.acquire(blocking=False):
        raise model.ModelError("model_capacity_busy",429)
    started=time.perf_counter()
    metric_error = 'stage_failed'
    with model.state_lock:
        model.state["calls"]+=1
    try:
        response=model.request_completion({"model":settings["model"],"thinking":{"type":"disabled"},
           "temperature":0,"max_tokens":600,"stream":False,"tool_choice":"required",
           "messages":[{"role":"system","content":"你是本项目只读运维证据解释器。选择一个诊断工具调用，只依据提供的观测引用证据ID。数据库探测失败时用database_unreachable，不能推断服务已停止、网络故障或磁盘损坏。数据库通过且没有任何历史错误用service_healthy；数据库通过但最近数据库错误记录非空用recent_query_error；只有模型错误时用model_dependency_error。无法支持则insufficient_evidence。请求累计计数不能证明性能瓶颈。所有恢复动作仅人工核查，不执行任何命令。"},
                       {"role":"user","content":json.dumps(snapshot,ensure_ascii=False)}],
           "tools":[{"type":"function","function":{"name":"diagnose_snapshot","description":"选择有证据的判断与下一步人工检查","parameters":Diagnosis.model_json_schema()}}]},settings)
        try:
            choice=response["choices"][0]
            calls=choice["message"]["tool_calls"]
            if choice["finish_reason"]!="tool_calls" or len(calls)!=1 or calls[0]["function"]["name"]!="diagnose_snapshot":
                raise ValueError("Invalid diagnostic tool")
            diagnosis=Diagnosis.model_validate_json(calls[0]["function"]["arguments"],strict=True)
            if response.get("model") not in {"deepseek-flash","deepseek-v4.1-flash"}:
                raise ValueError("Unrecognized model")
            expected=rule_finding(snapshot)
            relevant={"database_unreachable":"db_probe","service_healthy":"db_probe","recent_query_error":"recent_db_error",
                      "model_dependency_error":"model_state","insufficient_evidence":None}[diagnosis.finding]
            accepted=diagnosis.finding in {expected,"insufficient_evidence"} and (relevant is None or relevant in diagnosis.evidence_ids)
            finding=diagnosis.finding if accepted else "insufficient_evidence"
            raw_usage=response.get("usage",{})
            usage={key:raw_usage[key] for key in ("prompt_tokens","completion_tokens","total_tokens") if type(raw_usage.get(key)) is int}
        except (KeyError,ValueError,TypeError,ValidationError):
            raise model.ModelError("model_diagnosis_rejected",502) from None
        with model.state_lock:
            model.state["successes"]+=1
            model.state["last_success_at"]=time.time()
        metric_error = None
        return {"finding":finding,"explanation":FINDINGS[finding],"evidence_ids":diagnosis.evidence_ids,
                "model_original_judgment":diagnosis.model_dump(),
                "next_checks":[STEPS[key] for key in diagnosis.next_checks],"consistency_check_passed":accepted,
                "rule_baseline":expected,"model_evidence":{"model":settings["model"],"prompt_version":VERSION,
                    "usage":usage,"elapsed_ms":round((time.perf_counter()-started)*1000,3)},
                "action_policy":"仅解释与人工检查建议，不自动恢复服务"}
    except model.ModelError as error:
        metric_error = error.code
        with model.state_lock:
            model.state["failures"]+=1
            model.state["last_error"]={"code":error.code,"timestamp":time.time()}
        raise
    finally:
        telemetry.observe_model('diagnosis', time.perf_counter()-started, metric_error)
        model.capacity.release()
