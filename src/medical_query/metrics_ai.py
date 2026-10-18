"""Read-only Prometheus whitelist -> model fault assessment with local rule validation.

The model receives only aggregated numeric metrics from a fixed whitelist: no
clinical labels, no per-request identifiers, no credentials, no shell access and
no metric name outside the list. It returns a bounded finding plus the evidence
ids it used. A deterministic local rule engine computes its own baseline from
the same numbers; when the model's finding disagrees with that baseline the
validated output falls back to ``insufficient_evidence`` while the raw model
judgment is preserved for review.

Scope: single WSL2 host. The rule thresholds are demonstration values, not
production SLOs.
"""
from datetime import datetime, timezone
import json
import time
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import model, telemetry

VERSION = 'ops-metrics-ai-v1'
DEFAULT_PROMETHEUS = 'http://127.0.0.1:9090'
# Rate window for the counter/histogram queries. One minute keeps the fault
# demonstration responsive (scrape interval is 5s) while still smoothing noise.
WINDOW = '1m'

# Fixed whitelist. Every query is an aggregate over the whole host; none selects
# a clinical label (no hadm_id / subject_id / icu_stay_id) and none returns a
# raw label set. `or vector(0)` keeps a missing series as an explicit zero.
WHITELIST = {
    'app_target_up': ('up{job="medical-query"}', 'Prometheus can scrape the systemd application (1) or not (0).'),
    'compose_app_target_up': ('up{job="medical-query-compose"}', 'Prometheus can scrape the container application (1) or not (0).'),
    'node_target_up': ('up{job="node"}', 'node_exporter reachable (1) or not (0).'),
    'prometheus_target_up': ('up{job="prometheus"}', 'Prometheus scraping itself (1) or not (0).'),
    'database_ready': ('min(medical_query_database_ready) or vector(0)', 'Whether each application instance could serve a database-backed request at the last scrape (1) or not (0); min across all scraped app instances. 0 covers both an unreachable database and a connection pool that could not lend a connection in time, so it is not proof that the database is down.'),
    'http_requests_per_second': (f'sum(rate(medical_query_http_requests_total[{WINDOW}])) or vector(0)', f'Completed HTTP requests per second over {WINDOW}.'),
    'http_5xx_per_second': (f'sum(rate(medical_query_http_requests_total{{status=~"5.."}}[{WINDOW}])) or vector(0)', f'HTTP 5xx responses per second over {WINDOW}.'),
    'http_5xx_ratio': (f'(sum(rate(medical_query_http_requests_total{{status=~"5.."}}[{WINDOW}])) or vector(0)) / clamp_min(sum(rate(medical_query_http_requests_total[{WINDOW}])) or vector(0), 0.001)', f'Share of requests that returned 5xx over {WINDOW}.'),
    'http_p95_seconds': (f'histogram_quantile(0.95, sum by (le) (rate(medical_query_http_request_duration_seconds_bucket[{WINDOW}]))) or vector(0)', f'Estimated 95th percentile HTTP handler time in seconds over {WINDOW}.'),
    'cpu_busy_ratio': (f'1 - avg(rate(node_cpu_seconds_total{{mode="idle"}}[{WINDOW}]))', f'Fraction of CPU time not idle across all cores over {WINDOW}.'),
    'load1_per_cpu': ('scalar(node_load1) / scalar(count(count by (cpu) (node_cpu_seconds_total{mode="idle"})))', 'One-minute load average divided by the number of CPUs.'),
    'memory_available_ratio': ('node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes', 'Fraction of memory currently available.'),
    'root_filesystem_available_ratio': ('min(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"})', 'Fraction of the root filesystem still available.'),
    'firing_alerts': ('count(ALERTS{alertstate="firing"}) or vector(0)', 'Number of Prometheus alerts currently firing (aggregate count only).'),
}

FINDINGS = {
    'healthy': '白名单指标均在演示阈值内，未发现故障证据。累计计数本身不能证明性能问题。',
    'app_target_down': 'Prometheus 无法抓取 systemd 应用实例，说明该实例未提供 /metrics；不等于数据库故障。',
    'compose_app_target_down': 'Prometheus 无法抓取容器应用实例；systemd 实例是否正常需另看对应指标。',
    'database_unavailable': '应用数据库访问路径不就绪（database_ready=0）：可能是数据库不可达，也可能是连接池在超时内没借出连接。当前证据只能确认访问路径异常，不能直接认定数据库服务已停止。',
    'high_error_rate': 'HTTP 5xx 比例超过演示阈值，需要结合日志与依赖状态定位，不能仅凭比例判断根因。',
    'high_latency': 'HTTP p95 超过演示阈值，可能是负载、资源或下游依赖，当前证据不足以确定根因。',
    'resource_saturation': 'CPU、内存或磁盘可用量达到演示阈值，属于资源压力信号，不是应用逻辑错误的直接证据。',
    'alert_firing': '有告警处于 firing，但白名单指标未显示对应根因，需要人工核对告警内容。',
    'insufficient_evidence': '模型判断与本地规则基线不一致或证据不足，以本地规则基线为准并转人工核对。',
}

STEPS = {
    'check_prometheus_targets': '在 Prometheus 目标页核对 up 与最近抓取错误，确认是目标不可达还是抓取超时。',
    'check_database_service': '在 Ubuntu 查看 systemctl status postgresql@16-main，状态确认后再决定恢复动作。',
    'check_application_unit': '查看 systemctl status medical-query 与 journalctl -u medical-query -n 30 --no-pager。',
    'check_container_runtime': '查看 docker ps 与容器日志，确认容器是停止、重启还是仅健康检查失败。',
    'inspect_request_errors': '按路由模板查看 5xx 与日志，区分依赖故障与请求本身被拒绝。',
    'inspect_resources': '查看 top/vmstat 与磁盘占用，确认是持续压力还是采样瞬间。',
    'review_firing_alerts': '打开 Prometheus 告警页，逐条核对 firing 告警的表达式与持续时间。',
    'retry_same_query': '用同一固定条件重复查询，区分瞬时抖动与稳定故障。',
}

class Assessment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    finding: Literal['healthy', 'app_target_down', 'compose_app_target_down', 'database_unavailable',
                     'high_error_rate', 'high_latency', 'resource_saturation', 'alert_firing',
                     'insufficient_evidence']
    evidence_ids: Annotated[list[Literal[
        'app_target_up', 'compose_app_target_up', 'node_target_up', 'prometheus_target_up',
        'database_ready', 'http_requests_per_second', 'http_5xx_per_second', 'http_5xx_ratio',
        'http_p95_seconds', 'cpu_busy_ratio', 'load1_per_cpu', 'memory_available_ratio',
        'root_filesystem_available_ratio', 'firing_alerts']], Field(min_length=1, max_length=4)]
    next_checks: Annotated[list[Literal[
        'check_prometheus_targets', 'check_database_service', 'check_application_unit',
        'check_container_runtime', 'inspect_request_errors', 'inspect_resources',
        'review_firing_alerts', 'retry_same_query']], Field(min_length=1, max_length=3)]


RELEVANT = {
    'healthy': {'app_target_up', 'compose_app_target_up', 'database_ready'},
    'app_target_down': {'app_target_up'},
    'compose_app_target_down': {'compose_app_target_up'},
    'database_unavailable': {'database_ready'},
    'high_error_rate': {'http_5xx_ratio', 'http_5xx_per_second'},
    'high_latency': {'http_p95_seconds'},
    'resource_saturation': {'cpu_busy_ratio', 'load1_per_cpu', 'memory_available_ratio',
                            'root_filesystem_available_ratio'},
    'alert_firing': {'firing_alerts'},
    'insufficient_evidence': set(),
}

# Demonstration thresholds (single host, small demo). Not production SLOs.
# cpu_busy_ratio keeps headroom above the observed idle band (0.05-0.44) while
# still catching a real saturation; load1_per_cpu is the secondary signal.
THRESHOLDS = {'http_5xx_ratio': 0.05, 'http_p95_seconds': 0.5, 'cpu_busy_ratio': 0.90,
              'load1_per_cpu': 1.0, 'memory_available_ratio': 0.05,
              'root_filesystem_available_ratio': 0.05}


def _query(base, expression):
    url = f'{base}/api/v1/query?' + urlencode({'query': expression})
    try:
        with urlopen(url, timeout=8) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError) as error:
        raise RuntimeError(f'prometheus_unreachable: {type(error).__name__}') from None
    if payload.get('status') != 'success':
        raise RuntimeError('prometheus_query_failed')
    data = payload['data']
    # `scalar()` expressions return resultType "scalar": [timestamp, value]
    if data.get('resultType') == 'scalar':
        return float(data['result'][1])
    result = data['result']
    if not result:
        return None
    return float(result[0]['value'][1])


def collect(base=DEFAULT_PROMETHEUS):
    """Run the whitelist and return numeric metrics only."""
    metrics = {}
    for key, (expression, description) in WHITELIST.items():
        value = _query(base, expression)
        metrics[key] = {'value': value, 'description': description}
    return {'source': 'Prometheus read-only whitelist; aggregated numbers only, no clinical labels',
            'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'prometheus': base, 'metrics': metrics}


def rule_assessment(snapshot):
    """Deterministic baseline computed from the same numbers the model sees."""
    value = {key: item['value'] for key, item in snapshot['metrics'].items()}

    def at_least(key, threshold):
        return value.get(key) is not None and value[key] >= threshold

    def at_most(key, threshold):
        return value.get(key) is not None and value[key] <= threshold

    if value.get('app_target_up') == 0:
        return 'app_target_down'
    if value.get('database_ready') == 0:
        return 'database_unavailable'
    if value.get('compose_app_target_up') == 0:
        return 'compose_app_target_down'
    if at_least('http_5xx_ratio', THRESHOLDS['http_5xx_ratio']):
        return 'high_error_rate'
    if at_least('http_p95_seconds', THRESHOLDS['http_p95_seconds']):
        return 'high_latency'
    if at_least('cpu_busy_ratio', THRESHOLDS['cpu_busy_ratio']) or \
            at_least('load1_per_cpu', THRESHOLDS['load1_per_cpu']) or \
            at_most('memory_available_ratio', THRESHOLDS['memory_available_ratio']) or \
            at_most('root_filesystem_available_ratio', THRESHOLDS['root_filesystem_available_ratio']):
        return 'resource_saturation'
    if value.get('firing_alerts', 0) and value['firing_alerts'] > 0:
        return 'alert_firing'
    return 'healthy'


SYSTEM_PROMPT = (
    '你是本项目只读运维证据解释器，只能使用给定的 Prometheus 白名单指标（均为聚合数字）。'
    '规则：应用目标抓取失败用 app_target_down；容器应用目标抓取失败用 compose_app_target_down；'
    'database_ready 为 0 用 database_unavailable（含义是应用数据库访问路径不就绪，可能是连接池借连接超时或数据库连接失败；不能推断服务已停止、磁盘损坏或网络故障）；'
    '5xx 比例超过 0.05 用 high_error_rate；p95 超过 0.5 秒用 high_latency；'
    'CPU 忙比例超过 0.90 或每核 load1 超过 1.0 或可用内存低于 5% 或根分区可用低于 5% 用 resource_saturation；'
    'firing_alerts 大于 0 且以上都不成立用 alert_firing；全部正常用 healthy；'
    '证据不足用 insufficient_evidence。累计计数不能证明性能瓶颈，不得提出自动执行命令。'
    '必须只依据所提供的指标引用证据 id。')


def interpret(snapshot, settings=None):
    """Model raw judgment + local rule validation, both returned."""
    settings = settings or model.config()
    if not model.capacity.acquire(blocking=False):
        raise model.ModelError('model_capacity_busy', 429)
    started = time.perf_counter()
    metric_error = 'stage_failed'
    with model.state_lock:
        model.state['calls'] += 1
    try:
        payload = {'model': settings['model'], 'thinking': {'type': 'disabled'}, 'temperature': 0,
                   'max_tokens': 600, 'stream': False, 'tool_choice': 'required',
                   'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                                {'role': 'user', 'content': json.dumps(snapshot, ensure_ascii=False)}],
                   'tools': [{'type': 'function', 'function': {
                       'name': 'assess_metrics', 'description': '选择一个有证据的运维判断与下一步人工检查',
                       'parameters': Assessment.model_json_schema()}}]}
        response = model.request_completion(payload, settings)
        finish_reason = None
        raw_arguments = None
        try:
            choice = response['choices'][0]
            finish_reason = choice.get('finish_reason')
            calls = choice['message']['tool_calls']
            if finish_reason != 'tool_calls' or len(calls) != 1 or \
                    calls[0]['function']['name'] != 'assess_metrics':
                raise ValueError('Invalid assessment tool')
            raw_arguments = calls[0]['function']['arguments']
            assessment = Assessment.model_validate_json(raw_arguments, strict=True)
            if response.get('model') not in {'deepseek-flash', 'deepseek-v4.1-flash'}:
                raise ValueError('Unrecognized model')
            baseline = rule_assessment(snapshot)
            relevant = RELEVANT[assessment.finding]
            accepted = (assessment.finding in {baseline, 'insufficient_evidence'}
                        and (not relevant or bool(relevant & set(assessment.evidence_ids))))
            finding = assessment.finding if accepted else 'insufficient_evidence'
            raw_usage = response.get('usage', {})
            usage = {key: raw_usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                     if type(raw_usage.get(key)) is int}
        except (KeyError, ValueError, TypeError, ValidationError) as failure:
            # Keep what the provider actually returned so a rejection is auditable.
            raise model.ModelError('model_assessment_rejected', 502, evidence={
                'finish_reason': finish_reason,
                'raw_arguments': (raw_arguments or '')[:400],
                'detail': f'{type(failure).__name__}: {failure}'[:300]}) from None
        with model.state_lock:
            model.state['successes'] += 1
            model.state['last_success_at'] = time.time()
        metric_error = None
        return {'finding': finding, 'explanation': FINDINGS[finding],
                'model_original_judgment': assessment.model_dump(),
                'validated_finding': finding,
                'consistency_check_passed': accepted,
                'rule_baseline': baseline,
                'next_checks': [STEPS[key] for key in assessment.next_checks],
                'model_evidence': {'model': settings['model'], 'prompt_version': VERSION,
                                   'usage': usage,
                                   'elapsed_ms': round((time.perf_counter() - started) * 1000, 3)},
                'action_policy': '仅解释与人工检查建议，不自动执行任何恢复动作'}
    except model.ModelError as error:
        metric_error = error.code
        with model.state_lock:
            model.state['failures'] += 1
            model.state['last_error'] = {'code': error.code, 'timestamp': time.time()}
        raise
    finally:
        telemetry.observe_model('diagnosis', time.perf_counter() - started, metric_error)
        model.capacity.release()
