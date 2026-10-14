"""DeepSeek adapter: one bounded request, locally validated tools, no result upload."""
import hashlib
from http.client import HTTPException as HTTPClientException
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from pydantic import ValidationError

from .planning import SYSTEM_PROMPT, PROMPT_VERSION, tools, parse_call
from . import telemetry

ADAPTER_VERSION = "single-plan-v2"

CONFIG = Path.home() / '.config/medical-query/model.json'
capacity = threading.BoundedSemaphore(2)
state_lock = threading.Lock()
state = {"calls":0,"successes":0,"failures":0,"last_success_at":None,"last_error":None}


class ModelError(Exception):
    def __init__(self,code,status=503,evidence=None):
        self.code,self.status,self.evidence = code,status,evidence
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def config():
    try:
        if 'DEEPSEEK_API_KEY' in os.environ:
            data = {'provider':'deepseek','base_url':'https://api.deepseek.com','model':'deepseek-flash',
                    'api_key':os.environ['DEEPSEEK_API_KEY'],'timeout_seconds':25}
            if not data['api_key'] or any(c.isspace() for c in data['api_key']):
                raise ValueError('Invalid key format')
            return data
        path = Path(os.environ.get("MEDICAL_QUERY_MODEL_CONFIG",str(CONFIG)))
        info = path.stat()
        if path.is_symlink() or info.st_mode & 0o077:
            raise ValueError("Private permissions required")
        data = json.loads(path.read_text())
        if set(data)!={"provider","base_url","model","api_key","timeout_seconds"}:
            raise ValueError("Unexpected config fields")
        if data["provider"]!="deepseek" or data["base_url"]!="https://api.deepseek.com" or data["model"]!="deepseek-flash":
            raise ValueError("Unsupported endpoint")
        if not isinstance(data["api_key"],str) or not data["api_key"] or any(c.isspace() for c in data["api_key"]):
            raise ValueError("Invalid key format")
        if type(data["timeout_seconds"]) is not int or not 1<=data["timeout_seconds"]<=30:
            raise ValueError("Invalid timeout")
        return data
    except (OSError,ValueError,TypeError):
        raise ModelError("model_not_configured") from None


def status():
    try:
        config()
        configured = True
    except ModelError:
        configured = False
    with state_lock:
        snapshot = dict(state)
    return {"provider":"deepseek","model":"deepseek-flash","display_model":"DeepSeek-V4.1-Flash",
            "configured":configured,"connection_verified_this_process":snapshot["last_success_at"] is not None,
            "metrics":snapshot,"mode":"single_tool_plan","prompt_version":PROMPT_VERSION,
            "adapter_version":ADAPTER_VERSION,
            "data_sent":"查询问题与工具结构，或本项目运行观测；不发送CSV或查询结果"}


def request_completion(payload,settings):
    request = Request(settings["base_url"]+"/chat/completions",
                      data=json.dumps(payload).encode(),method="POST",
                      headers={"Content-Type":"application/json","Authorization":"Bearer "+settings["api_key"]})
    try:
        # Redirects rejected so an Authorization header cannot reach another host.
        with build_opener(NoRedirect()).open(request,timeout=settings["timeout_seconds"]) as response:
            raw = response.read(262145)
        if len(raw)>262144:
            raise ModelError("model_response_too_large",502)
        return json.loads(raw)
    except HTTPError as exc:
        code = {401:"model_authentication_failed",402:"model_balance_insufficient",
                429:"model_rate_limited",400:"model_request_rejected",404:"model_endpoint_not_found"}.get(exc.code,"model_provider_error")
        exc.close()
        raise ModelError(code,502) from None
    except (TimeoutError,socket.timeout):
        raise ModelError("model_timeout",504) from None
    except URLError as exc:
        if isinstance(exc.reason,(TimeoutError,socket.timeout)):
            raise ModelError("model_timeout",504) from None
        raise ModelError("model_unreachable") from None
    except (json.JSONDecodeError,UnicodeDecodeError):
        raise ModelError("model_invalid_response",502) from None
    except (OSError,HTTPClientException):
        raise ModelError("model_unreachable") from None


def plan(question):
    settings = config()
    if not capacity.acquire(blocking=False):
        raise ModelError("model_capacity_busy",429)
    started = time.perf_counter()
    metric_error = 'stage_failed'
    with state_lock:
        state["calls"]+=1
    try:
        payload = {"model":settings["model"],"messages":[{"role":"system","content":SYSTEM_PROMPT},
                   {"role":"user","content":question}],"tools":tools(),"tool_choice":"required",
                   "thinking":{"type":"disabled"},"temperature":0,"max_tokens":1000,"stream":False}
        response = request_completion(payload,settings)
        raw_usage = response.get("usage",{}) if isinstance(response,dict) else {}
        usage = {key:raw_usage[key] for key in ("prompt_tokens","completion_tokens","total_tokens")
                 if isinstance(raw_usage,dict) and type(raw_usage.get(key)) is int and raw_usage[key]>=0}
        metadata = {"provider":"deepseek","model":settings["model"],
                    "prompt_version":PROMPT_VERSION,"adapter_version":ADAPTER_VERSION,"usage":usage,
                    "elapsed_ms":round((time.perf_counter()-started)*1000,3),
                    "question_sha256":hashlib.sha256(question.encode()).hexdigest()}
        try:
            choices = response["choices"]
            if len(choices)!=1 or choices[0]["finish_reason"]!="tool_calls":
                raise ValueError("Incomplete or non-tool response")
            calls = choices[0]["message"]["tool_calls"]
            if not isinstance(calls,list) or not calls:
                raise ValueError("Function call required")
            if len(calls)>1:
                # Multiple model calls never execute. Return one deterministic
                # clarification decision instead of choosing one and dropping another.
                function = {"name":"clarify"}
                arguments = {"reason":"multiple_queries"}
                metadata["guard_action"]="multiple_calls_clarified"
            else:
                if calls[0]["type"]!="function":
                    raise ValueError("Function call required")
                function = calls[0]["function"]
                arguments = parse_call(function["name"],function["arguments"],question)
            if response.get("model") not in {"deepseek-flash","deepseek-v4.1-flash"}:
                raise ValueError("Unexpected response model")
            metadata["returned_model"]=response["model"]
        except (KeyError,TypeError,ValueError,ValidationError):
            raise ModelError("model_plan_rejected",502,evidence=metadata) from None
        with state_lock:
            state["successes"]+=1
            state["last_success_at"]=time.time()
        metric_error = None
        return {"tool":function["name"],"arguments":arguments,"model_evidence":metadata}
    except ModelError as exc:
        metric_error = exc.code
        with state_lock:
            state["failures"]+=1
            state["last_error"]={"code":exc.code,"timestamp":time.time()}
        raise
    finally:
        telemetry.observe_model('query', time.perf_counter()-started, metric_error)
        capacity.release()
