"""Bounded, clinical-content-free Prometheus instrumentation for one process."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()
REQUESTS = Counter('medical_query_http_requests_total', 'Completed HTTP requests; probe routes identifiable',
                   ['method', 'route', 'status'], registry=REGISTRY)
DURATION = Histogram('medical_query_http_request_duration_seconds', 'HTTP handler elapsed time',
                     ['method', 'route'], buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30), registry=REGISTRY)
ERRORS = Counter('medical_query_http_errors_total', 'HTTP 5xx errors', ['route'], registry=REGISTRY)
DATABASE_READY = Gauge('medical_query_database_ready', 'Reachable database with imported encounter rows', registry=REGISTRY)
MODEL_CALLS = Counter('medical_query_model_calls_total', 'Acquired model stages; includes provider and local validation',
                      ['stage', 'outcome'], registry=REGISTRY)
MODEL_DURATION = Histogram('medical_query_model_duration_seconds', 'Acquired model stage duration including validation',
                          ['stage'], buckets=(.1, .5, 1, 2, 5, 10, 20, 30, 60), registry=REGISTRY)
TOOL_RESULTS = Counter('medical_query_tool_results_total', 'Local query tool outcomes', ['outcome'], registry=REGISTRY)


def observe_http(method, route, status, seconds):
    method = method if method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'} else 'OTHER'
    if route == '/metrics':
        return
    REQUESTS.labels(method, route, str(status)).inc()
    DURATION.labels(method, route).observe(seconds)
    if status >= 500:
        ERRORS.labels(route).inc()


def observe_model(stage, seconds, error):
    assert stage in {'query', 'diagnosis'}
    outcome = 'timeout' if error == 'model_timeout' else ('error' if error else 'success')
    MODEL_CALLS.labels(stage, outcome).inc()
    MODEL_DURATION.labels(stage).observe(seconds)


def observe_tool(outcome):
    assert outcome in {'success', 'clarification', 'refused', 'no_data', 'error'}
    TOOL_RESULTS.labels(outcome).inc()


def render_database_probe(connect):
    # Called on the synchronous /metrics handler's worker thread. connect has a
    # 2s connection timeout and 3s SQL timeout. The scrape budget is kept at 4s;
    # pathological queries can cause up=0, which is distinct from readiness=0.
    try:
        with connect() as connection:
            ready = connection.execute('SELECT EXISTS (SELECT 1 FROM query_api.encounter_overview) AS loaded').fetchone()['loaded']
        DATABASE_READY.set(int(ready))
    except Exception:
        DATABASE_READY.set(0)
    return generate_latest(REGISTRY)
