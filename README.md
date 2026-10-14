# MIMIC-IV Demo 住院信息查询服务

一个跑在本机的查询服务，数据来自 PhysioNet 公开的 MIMIC-IV Clinical Database Demo 2.2
（100 名患者、275 次住院、140 次 ICU 入住）。

MIMIC 是 Medical Information Mart for Intensive Care 的缩写，MIT 和波士顿贝斯以色列女执事
医疗中心（BIDMC）发布的公开重症监护数据集。完整版要申请，这里用的是可以自由下载的 Demo
小样本。

可以问"这个病人住院期间肌酐怎么变化的""有糖尿病诊断的住院有哪些"这类中文问题，服务把它
映射到一组预先写好的参数化查询上，返回结果和检验趋势图。

模型在这里只做一件事：把问题翻译成"用哪个模板 + 哪些参数"。它不生成 SQL，也拿不到数据库
连接。所有 SQL 都是固定模板、参数化传入，越界或含糊的问题会返回澄清提示或直接拒绝。

## 跑起来

Windows PowerShell 里执行，窗口保持打开（WSL 需要一个前台进程活着）：

```powershell
wsl -d Ubuntu-24.04 --exec bash /mnt/d/Project/New-AI-Medical-Agent-Platform/scripts/run_demo.sh
```

然后打开 <http://localhost:8765>。API 文档在 <http://localhost:8765/docs>，样本统计可以直接看
<http://localhost:8765/api/summary>。

服务只监听 127.0.0.1，没有认证，别往公网放。

### 从零部署

```bash
cd /mnt/d/Project/New-AI-Medical-Agent-Platform

# Python 环境
python3 -m venv ~/environments/medical-query/.venv
~/environments/medical-query/.venv/bin/pip install -c requirements.lock.txt -e ".[test]"
source ~/environments/medical-query/.venv/bin/activate

# 数据：从 PhysioNet 下 7 张表，逐张校验官方 SHA256
python3 scripts/download_mimic.py

# 建库 + 建只读角色
sudo python3 scripts/bootstrap_database.py

# 导入（一个事务内替换 7 张表的快照，失败整体回滚）
python scripts/import_mimic.py

# 自检
bash scripts/check_module1.sh
```

前两步要联网。下载那一步如果卡在 403，多半是出口 IP 被 PhysioNet 拦了，挂个代理再跑。

模型是可选的。要用的话在本人的终端（不要 sudo）执行 `python3 scripts/configure_model.py`，
它会用不回显的方式让你输入 DeepSeek 的 API Key，写到 `~/.config/medical-query/model.json`。
不配也能用，固定查询不依赖模型。

## 接口

| 路径 | 说明 |
|---|---|
| `/api/catalog` | 可查的检验项目、单位，以及各自的限制 |
| `/api/admissions` | 按 subject_id / 是否 ICU / ICD 版本与代码筛住院，分页 |
| `/api/admissions/{hadm_id}` | 单次住院详情 |
| `/api/admissions/{hadm_id}/diagnoses` | 按版本和代码查诊断，联合字典 |
| `/api/admissions/{hadm_id}/labs?concept=creatinine` | 四项血液检验之一，带异常判定和分页 |
| `/api/summary` | 患者数、住院数、ICU 次数、住院时长及分母 |
| `/health/live`、`/health/ready` | 存活探测、数据库就绪探测（分开的） |
| `/ops/snapshot` | 当前进程的请求数、错误数、耗时合计、最近的数据库错误 |

检验的 `concept` 支持 `creatinine`、`potassium`、`glucose`、`hemoglobin`。
`abnormal` 支持 `all`、`source_flag`（用源数据的 flag）、`reference_range`（用参考区间越界）。
每页最多 200 行，响应里有 total / limit / offset。要画趋势的话只取 `trend_eligible` 的记录，
并且自己翻页，别把一页当成完整趋势。

## 数据

MIMIC-IV Clinical Database Demo 2.2，PhysioNet 公开发布：
<https://physionet.org/content/mimic-iv-demo/2.2/>，许可为 Open Data Commons ODbL 1.0
（<https://opendatacommons.org/licenses/odbl/1-0/>）。

仓库里不含原始 CSV、数据库 dump 或备份，`scripts/download_mimic.py` 从官方来源现取现校验。

Demo 官方引用：Johnson, A., Bulgarelli, L., Pollard, T., Horng, S., Celi, L. A., & Mark, R.
(2023). MIMIC-IV Clinical Database Demo (version 2.2). PhysioNet. RRID:SCR_007345.
<https://doi.org/10.13026/dp1f-ex47>

完整数据集的背景引用：Johnson, A. E. W., Bulgarelli, L., Shen, L. et al. MIMIC-IV, a freely
accessible electronic health record dataset. Scientific Data 10, 1 (2023).
<https://doi.org/10.1038/s41597-022-01899-x>

几点数据上的坑，用之前要知道：

- 日期是脱敏移位过的，`anchor_age` 是锚定年龄，ICD 是出院计费编码。不能拿来算真实年度指标，
  也不能当临床诊断用。
- 有 28,420 条检验记录没有 `hadm_id`，它们留在原始表和视图里，不会自动归到某次住院。
- Demo 只有 2.2 这一个版本在用，不和 1.0 合并。

## 数据库与权限

库名 `medical_query_demo`。导入用的 owner 是当前 Linux 用户（本地 peer 认证）；应用连库用的是
`medical_query_reader`（TCP 认证，随机密码存在 `~/.config/medical-query/database.json`，
文件 0600、目录 0700，不要提交、不要截图、不要贴进聊天）。

`reader` 只能 SELECT 三张自建视图，读不到原始层和审计层，也不能建表或写数据。连接超时 2 秒，
SQL 超时 3 秒，事务默认只读。这三张视图在 `query_api` schema 下，原始表在 `mimic_raw`，
项目自己的审计记录在 `project_audit`。

## 运维部分

systemd 服务 `medical-query.service` 跑在 8765：

```bash
bash scripts/service.sh start|stop|health
systemctl status medical-query.service
journalctl -u medical-query.service -n 30 --no-pager
```

监控用 Prometheus + node_exporter，配置在 `deploy/prometheus/`，安装脚本
`scripts/install_monitoring.sh`（要 root）。告警页面 <http://localhost:9090/alerts>，
应用指标 <http://localhost:8765/metrics>。指标标签里不含临床内容。

备份是每日 dump + 每周在隔离库做一次恢复校验，由 systemd timer 驱动，脚本在
`scripts/backup_database.sh` 和 `scripts/install_backup_timers.py`。备份文件不进仓库。
timer 依赖 Windows 开着、WSL 活着。

### 跑过的一些实验

单机 WSL2、回环端口、Demo 小样本，下面这些是当时的实测值：

- 停掉 PostgreSQL：数据库不可用告警和 5xx 告警实际触发，恢复后解除；从恢复命令到同一条查询
  成功 2246 ms。
- `kill -9` 应用进程：systemd 自动拉起，PID 变化、NRestarts 增加，到查询就绪 4098 ms。
- 并发 1 / 4 / 8 各跑约一分钟，26,242 次固定查询全部正确，实测约 71 / 178 / 175 请求每秒。
- 备份保留策略：真实生成多份备份，验证保留最新 7 份。
- 容器网络：Compose 自定义 bridge，应用用服务名 `db:5432` 连库，容器访问不到宿主机上
  只绑 127.0.0.1 的服务，发布端口只绑回环。

这些都是单机小样本上的观测，不是 SLA、容量上限或多机高可用。

## 容器版

容器版和 systemd 版并存，各自用独立数据库和独立端口，互不影响。

```bash
# 官方数据路径（可复现，只需要 Docker 和官方 Demo 数据）
python3 scripts/download_mimic.py --directory .local/download-validation
sudo bash scripts/install_compose.sh --from-source

# 验证：容器健康 + 两种部署的固定查询结果一致
curl --fail http://127.0.0.1:18765/health/ready
python scripts/verify_deployments.py
```

日常操作：`bash scripts/compose.sh start|stop|health`。

端口默认 `127.0.0.1:18765`（应用）和 `127.0.0.1:15432`（数据库），只绑回环。应用以 UID 10001
运行，容器 `read_only` + `cap_drop: ALL` + `no-new-privileges`。基础镜像按 RepoDigest 固定
（容器内 PostgreSQL 16.15、Python 3.12.15）。密钥放在 Linux 私有目录（默认
`/var/lib/medical-query/compose/.env`，目录 0700、文件 0600），不放 Windows 挂载目录，因为
DrvFs 上的 chmod 不生效。默认不往容器里注入模型密钥。

项目名、卷名、端口、私有目录、数据目录都能用环境变量覆盖，可以并行开多套，不用改文件：

```bash
export MEDICAL_QUERY_COMPOSE_PROJECT=mq-iso
export MEDICAL_QUERY_HTTP_PORT=28765
export MEDICAL_QUERY_POSTGRES_PORT=25432
sudo bash scripts/install_compose.sh --from-source
```

## 项目结构

```
src/medical_query/      应用代码（FastAPI + psycopg）
  queries.py            固定查询模板
  planning.py           模型工具规划与本地校验
  database.py           连接与只读角色
  static/               查询页面
tests/                  pytest，102 项
scripts/                部署、导入、实验、备份、检查
sql/schema.sql          视图定义
deploy/                 systemd unit、Prometheus 配置、Nginx
.github/workflows/ci.yml
```

## 说明

- 没有 LICENSE 文件，默认保留所有权利。
- 第一次从零部署会比较折腾，主要是 WSL 和 PostgreSQL 权限那几关。
- 这套东西是为校招作品和毕业设计做的，跑通和能讲清楚是主要目标，不是生产系统。
