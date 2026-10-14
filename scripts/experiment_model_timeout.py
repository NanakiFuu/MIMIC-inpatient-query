"""Real HTTP delay/timeout in isolated test API process, no genuine key sent.

This is explicitly a local test proxy experiment, not a DeepSeek outage. The
production service/credential file stays unchanged. Fixed queries use real PG.
"""
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen

import uvicorn

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from medical_query import model
from medical_query.api import app

OUT=ROOT/"research/module3/model-timeout.json"


class SlowProxy(BaseHTTPRequestHandler):
    observations=[]
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length","0")))
        self.observations.append({"path":self.path,"observed_at_utc":datetime.now(timezone.utc).isoformat(),
                                  "test_key_only":self.headers.get("Authorization")=="Bearer TEST_ONLY_NOT_A_REAL_KEY"})
        time.sleep(2)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
        except (BrokenPipeError,ConnectionResetError):
            pass
    def log_message(self,*args):
        pass


def http(path,post=None):
    data=json.dumps(post).encode() if post is not None else None
    request=Request("http://127.0.0.1:8766"+path,data=data,headers={"Content-Type":"application/json"} if data else {},method="POST" if data else "GET")
    started=time.perf_counter()
    try:
        with urlopen(request,timeout=6) as response:
            body=json.load(response);status=response.status
    except HTTPError as error:
        body=json.loads(error.read());status=error.code
    return {"path":path,"status":status,"body":body,"elapsed_ms":round((time.perf_counter()-started)*1000,3)}


def main():
    if OUT.exists():
        raise SystemExit("Do not overwrite recorded experiment")
    with socket.socket() as check:
        check.bind(("127.0.0.1",8766))
    proxy=ThreadingHTTPServer(("127.0.0.1",0),SlowProxy)
    proxy_port=proxy.server_address[1]
    proxy_thread=threading.Thread(target=proxy.serve_forever,daemon=True)
    proxy_thread.start()
    original_config=model.config
    # Test-only adapter override in this isolated Python process, not saved config.
    model.config=lambda:{"provider":"TEST_LOCAL_PROXY","model":"deepseek-flash",
                         "base_url":f"http://127.0.0.1:{proxy_port}",
                         "api_key":"TEST_ONLY_NOT_A_REAL_KEY","timeout_seconds":1}
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=8766,access_log=False,log_level="warning"))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    try:
        for attempt in range(30):
            try:
                if http("/health/live")["status"]==200:
                    break
            except URLError:
                time.sleep(.1)
        before=http("/api/summary")
        failed=http("/api/query",{"question":"Demo有多少次住院？"})
        during_fixed=http("/api/summary")
        assert failed["status"]==504 and failed["body"]["error"]=="model_timeout"
        assert before["body"]["result"]==during_fixed["body"]["result"]
        assert all(item["test_key_only"] for item in SlowProxy.observations)
        model.config=original_config
        recovered=http("/api/query",{"question":"Demo有多少次住院？"})
        assert recovered["status"]==200 and recovered["body"]["result"]["data"]["admission_count"]==275
        record={"source":"隔离测试API进程与本地延迟代理真实HTTP实验；不是DeepSeek真实故障",
            "observed_at_utc":datetime.now(timezone.utc).isoformat(),"test_api_port":8766,
            "proxy_port":proxy_port,"proxy_delay_seconds":2,"adapter_timeout_seconds":1,
            "checks":[before,failed,during_fixed,recovered],"proxy_observations":SlowProxy.observations,
            "production_service_unchanged":True,"real_key_never_sent_to_proxy":True,"status":"passed"}
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"status":"passed","timeout_request_ms":failed["elapsed_ms"],"fixed_query_preserved":True,
                          "real_model_recovered":recovered["status"]==200}))
    finally:
        model.config=original_config
        server.should_exit=True
        thread.join(timeout=5)
        proxy.shutdown();proxy.server_close()


if __name__=="__main__":
    main()
