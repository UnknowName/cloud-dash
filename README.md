# Cloud Dash

多云平台 ECS 监控指标 Prometheus Exporter，支持阿里云和华为云，可一键接入 Prometheus + Grafana 监控体系。

## 功能特性

- **多云平台支持** — 同时接入阿里云、华为云，统一暴露 Prometheus 格式指标
- **ECS 全量指标** — CPU 利用率、内存利用率、磁盘利用率（按挂载点拆分）、网络出入速率
- **实例列表本地缓存** — 避免频繁调用云 API 获取实例列表，降低 API 限流风险
- **优先级线程池** — 按指标优先级调度采集任务，支持动态扩缩容和失败重试
- **可配置采集间隔** — 支持 seconds / minutes / hours 灵活配置，运行时动态调整
- **实例名称过滤** — 通过 `include_name` 只采集指定名称的实例
- **Grafana 仪表盘** — 内置开箱即用的监控仪表盘 JSON，含概览、趋势图、仪表盘、实例详情表
- **RESTful 状态接口** — 提供 `/health` 健康检查和 `/api/v1/status` 运行状态查询

## 环境要求

| 依赖 | 最低版本 |
|------|---------|
| Python | >= 3.14 |
| [uv](https://docs.astral.sh/uv/) (推荐) | 最新版 |

> 项目使用 `uv` 作为包管理器，也可使用 `pip` 安装依赖。

## 快速开始

### 1. 克隆项目

```bash
git clone <repository-url>
cd cloud-dash
```

### 2. 安装依赖

使用 uv（推荐）：

```bash
uv sync
```

使用 pip：

```bash
pip install -e ".[dev]"
```

### 3. 编辑配置文件

复制并修改配置文件，填入你的云平台凭证：

```bash
cp config.yaml config.local.yaml
```

编辑 `config.local.yaml`，参考下方 [配置指南](#配置指南) 填写必要信息。

### 4. 启动服务

```bash
# 使用默认配置文件 ./config.yaml
python main.py

# 指定配置文件路径
python main.py /path/to/config.yaml
```

服务默认监听 `0.0.0.0:9100`，启动后即可访问：

- 指标端点：`http://localhost:9100/metrics`
- 健康检查：`http://localhost:9100/health`
- 运行状态：`http://localhost:9100/api/v1/status`

### 5. 配置 Prometheus 抓取

在 Prometheus 配置中添加：

```yaml
scrape_configs:
  - job_name: "cloud-dash"
    static_configs:
      - targets: ["localhost:9100"]
    scrape_interval: 30s
```

### 6. 导入 Grafana 仪表盘

1. 打开 Grafana → Dashboards → Import
2. 上传 [dashboards/cloud-ecs-monitoring.json](dashboards/cloud-ecs-monitoring.json)
3. 选择 Prometheus 数据源
4. 保存即可看到完整的 ECS 监控面板

## 配置指南

配置文件为 YAML 格式，支持通过命令行参数指定路径。完整配置示例：

```yaml
server:
  port: 9100                    # 服务监听端口

cache:
  ttl_seconds: 60               # 指标缓存 TTL（秒）

# 实例列表本地文件缓存
instance_cache:
  enabled: true                 # 是否启用缓存，关闭后每次都从云 API 获取
  ttl_seconds: 86400            # 缓存有效时长（秒），默认 86400（1 天）
  dir: "./cache/instances"      # 缓存文件存放目录，支持相对路径和绝对路径

# 采集间隔配置
collection:
  interval: 5                   # 采集周期数值
  unit: minutes                 # 时间单位，支持 seconds / minutes / hours
  # 未配置或配置无效时，默认采用 5 minutes
  # 最低间隔为 60 秒，低于此值会自动调整以避免云平台限流

# 线程池配置
thread_pool:
  max_workers: 5                # 最大工作线程数
  max_retries: 3                # 任务失败最大重试次数
  retry_delay: 1.0              # 重试基础延迟（秒），采用指数退避策略

# 云平台 Provider 配置（支持多个）
providers:
  - type: aliyun                # 云平台类型：aliyun / huawei
    name: aliyun-prod           # 自定义名称，用于标识和缓存
    region: cn-shenzhen         # 区域 ID
    include_name: "支付"         # 可选，只采集名称包含该字符串的实例
    credentials:
      access_key_id: "YOUR_AK"           # 必填
      access_key_secret: "YOUR_SK"       # 必填

  - type: huawei
    name: huawei-prod
    region: cn-north-4
    credentials:
      access_key_id: "YOUR_AK"           # 必填
      access_key_secret: "YOUR_SK"       # 必填
      project_id: "YOUR_PROJECT_ID"      # 华为云必填

# 启用的采集器
collectors:
  - ecs
```

### 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `server.port` | int | 9100 | HTTP 服务监听端口 |
| `cache.ttl_seconds` | int | 60 | 指标缓存 TTL |
| `instance_cache.enabled` | bool | true | 是否启用实例列表本地缓存 |
| `instance_cache.ttl_seconds` | int | 86400 | 实例缓存有效期（秒） |
| `instance_cache.dir` | string | "./cache/instances" | 缓存文件目录 |
| `collection.interval` | int | 5 | 采集周期数值 |
| `collection.unit` | string | "minutes" | 采集周期单位 |
| `thread_pool.max_workers` | int | 5 | 线程池最大线程数 |
| `thread_pool.max_retries` | int | 3 | 失败重试次数 |
| `thread_pool.retry_delay` | float | 1.0 | 重试基础延迟（秒） |
| `providers` | list | [] | 云平台配置列表 |
| `collectors` | list | ["ecs"] | 启用的采集器列表 |

### 云平台凭证获取

**阿里云**：
1. 登录 [RAM 访问控制](https://ram.console.aliyun.com/)
2. 创建用户并授予 `AliyunCloudMonitorReadOnlyAccess` 和 `AliyunECSReadOnlyAccess` 权限
3. 创建 AccessKey 并记录 ID 和 Secret

**华为云**：
1. 登录 [IAM](https://console.huaweicloud.com/iam/)
2. 创建用户并授予 `CES ReadOnlyAccess` 和 `ECS ReadOnlyAccess` 权限
3. 创建 AccessKey 并记录 AK、SK 和 Project ID

> **安全提示**：请勿将凭证硬编码在配置文件中提交到版本库，建议使用环境变量或密钥管理服务。

## Prometheus 指标

所有指标均带有以下标签：

| 标签 | 说明 |
|------|------|
| `cloud` | 云平台类型（aliyun / huawei） |
| `instance_id` | 实例 ID |
| `instance_name` | 实例名称 |
| `region` | 区域 |

磁盘指标额外包含 `disk` 标签，标识挂载路径（如 `/`、`/data`、`C:`）。

| 指标名称 | 类型 | 说明 |
|----------|------|------|
| `cloud_ecs_cpu_utilization_percent` | Gauge | CPU 利用率（%） |
| `cloud_ecs_memory_utilization_percent` | Gauge | 内存利用率（%） |
| `cloud_ecs_disk_utilization_percent` | Gauge | 磁盘利用率（%），含 `disk` 标签 |
| `cloud_ecs_network_in_rate_bytes_per_second` | Gauge | 网络入站速率（Bytes/s） |
| `cloud_ecs_network_out_rate_bytes_per_second` | Gauge | 网络出站速率（Bytes/s） |

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/metrics` | Prometheus 格式指标端点 |
| GET | `/health` | 健康检查，返回 `{"status": "ok"}` |
| GET | `/api/v1/status` | 运行状态，含调度统计和线程池信息 |

`/api/v1/status` 响应示例：

```json
{
  "status": "ok",
  "providers": [
    {
      "name": "aliyun-prod",
      "type": "aliyun",
      "region": "cn-shenzhen",
      "collection_interval_seconds": 300,
      "pool_stats": {
        "active_threads": 3,
        "queue_length": 0,
        "completed_tasks": 120,
        "failed_tasks": 0,
        "total_submitted": 120,
        "max_workers": 5,
        "current_cycle": 12
      }
    }
  ],
  "schedule": {
    "collection_interval_seconds": 300,
    "total_cycles": 12,
    "last_cycle_duration_seconds": 4.23,
    "last_collection_timestamp": 1746000000.0
  }
}
```

## 项目结构

```
cloud-dash/
├── main.py                     # 入口，初始化并启动服务
├── config.yaml                 # 默认配置文件
├── pyproject.toml              # 项目元数据和依赖
├── dashboards/
│   └── cloud-ecs-monitoring.json   # Grafana 仪表盘
├── cache/
│   └── instances/              # 实例列表本地缓存目录
└── src/
    ├── config.py               # 配置加载与校验
    ├── cache.py                # 指标缓存与定时调度
    ├── exporter.py             # FastAPI + Prometheus Exporter
    ├── pool.py                 # 优先级线程池
    ├── instance_cache.py       # 实例列表文件缓存
    ├── collectors/
    │   ├── base.py             # 采集器基类
    │   └── ecs.py              # ECS 指标采集器
    └── providers/
        ├── base.py             # 云平台 Provider 基类
        ├── aliyun.py           # 阿里云 Provider
        └── huawei.py           # 华为云 Provider
```

## 架构设计

```
┌─────────────────────────────────────────────────────┐
│                    FastAPI (Exporter)                │
│  /metrics  /health  /api/v1/status                  │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                  MetricsCache                        │
│  定时调度 → 调用各 Collector 采集 → 缓存指标          │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                EcsCollector                          │
│  将各 Provider 的采集结果转为 Prometheus Gauge        │
└──────────┬───────────────────────┬──────────────────┘
           │                       │
┌──────────▼──────────┐  ┌────────▼──────────────────┐
│  AliyunProvider     │  │  HuaweiProvider           │
│  ECS + CMS API      │  │  ECS + CES API            │
│  PriorityThreadPool │  │  PriorityThreadPool        │
└─────────────────────┘  └───────────────────────────┘
```

核心设计要点：

- **Provider 抽象**：通过 `CloudProvider` 基类统一接口，新增云平台只需实现 `list_instances()` 和 `get_metrics()`，并在 `PROVIDER_MAP` 注册
- **Collector 抽象**：通过 `MetricCollector` 基类统一采集接口，新增资源类型只需实现 `collect()`，并在 `COLLECTOR_MAP` 注册
- **优先级线程池**：CPU 指标优先级最高，网络指标最低；支持按采集间隔动态调整线程数
- **缓存策略**：实例列表使用文件缓存（TTL 可配），监控指标使用内存缓存（按采集间隔刷新）

## 常见问题

### 采集间隔为什么最低 60 秒？

云平台 API 有调用频率限制，过短的采集间隔可能导致限流。系统强制最低 60 秒间隔，低于此值的配置会自动调整。

### 实例列表缓存有什么作用？

实例列表变化频率远低于监控指标。启用本地文件缓存后，在 TTL 有效期内直接从本地读取实例列表，避免每次采集都调用云 API，降低限流风险。默认 TTL 为 1 天。

### 磁盘利用率为什么显示为 `total` 而非具体挂载点？

- **阿里云**：需要安装云监控插件才能获取 `device` 维度数据
- **华为云**：需要安装 CES Agent 并确保 `mount_point` 维度信息可被发现；未安装 Agent 时回退到实例级聚合数据，显示为 `total`

### 如何只监控部分实例？

在 Provider 配置中使用 `include_name` 字段，只采集名称包含该字符串的实例：

```yaml
providers:
  - type: aliyun
    name: aliyun-prod
    region: cn-shenzhen
    include_name: "生产"    # 只采集名称包含"生产"的实例
```

### 采集任务失败会怎样？

线程池内置重试机制（默认最多 3 次，指数退避）。如果重试后仍失败，该实例的对应指标将缺失，不影响其他实例的正常采集。可通过 `/api/v1/status` 查看失败任务数。

### 如何扩展支持新的云平台？

1. 在 `src/providers/` 下新建 Provider 类，继承 `CloudProvider`，实现 `list_instances()` 和 `get_metrics()`
2. 在 [main.py](main.py) 的 `PROVIDER_MAP` 中注册新类型
3. 在 `config.yaml` 的 `providers` 中添加对应配置

### 如何扩展支持新的资源类型？

1. 在 `src/collectors/` 下新建 Collector 类，继承 `MetricCollector`，实现 `collect()`
2. 在 [main.py](main.py) 的 `COLLECTOR_MAP` 中注册新类型
3. 在 `config.yaml` 的 `collectors` 中添加对应名称

## 贡献指南

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/your-feature`
3. 提交更改：`git commit -m "Add your feature"`
4. 推送分支：`git push origin feature/your-feature`
5. 创建 Pull Request

请确保：

- 代码遵循项目现有风格
- 新增功能有对应的测试
- 不提交凭证或敏感信息

## 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE) 文件。
