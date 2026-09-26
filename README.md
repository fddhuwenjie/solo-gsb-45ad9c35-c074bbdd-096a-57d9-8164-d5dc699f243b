# 分时阶梯计价 API（FastAPI + SQLite）

按**版本化费率规则**对带时区的用量区间做分时（TOU）+ 累计阶梯计价。
所有时间窗口均为**半开区间 `[start, end)`**，金额与用量全程使用 `Decimal`，
内置跨日、夏令时（DST）切换、工作日/节假日（含调休）与多段阶梯的精确切分。

## 一条命令启动

```bash
docker compose up --build
```

服务在 **http://localhost:8000** 提供：

- Swagger UI：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health

SQLite 数据文件持久化在 Docker 卷 `tariff-data`（容器内 `/data/tariff.db`），
首次启动自动建表并写入预置规则版本。

> 不用 Docker 也可以：
> `pip install -r requirements.txt && uvicorn app.main:app --port 8000`
> （默认使用本地 `./data/tariff.db`，可用环境变量 `DATABASE_URL` 覆盖）

## 预置规则版本

| 版本号 | 时区 | 说明 |
| --- | --- | --- |
| `us-ny-demo-v2024` | `America/New_York` | 2024 年纽约演示费率：夏令时/标准时工作日 + 周末节假日；含 2024 全年美国公共假日；三段累计阶梯（100 / 300 / ∞ kWh） |
| `us-ny-demo-v2025` | `America/New_York` | 2024-07-01 起生效的新版纽约演示费率：结构与 v2024 相同、单价上调，节假日日历略有差异（2024-10-14 不再列为节假日）；用于 `/quote-diff` 版本对比演示 |
| `cn-shanghai-demo-v2024` | `Asia/Shanghai` | 2024 年中国居民阶梯分时演示：工作日（含**调休上班**）/ 普通周末 / 法定节假日；三段累计阶梯（200 / 400 / ∞ kWh） |

> 价格均为演示值，不代表真实电价。

### 规则模型（SQLite 表）

- `tariff_versions`：版本号、币种、能量单位、描述。
- `rate_rules`：规则代码、**生效时间段（规则时区的本地朴素时间）**、**时区**、
  ISO 星期掩码（`1..7`，逗号分隔，空=每天）、`day_scope`
  （`all` / `workdays` / `holidays` / `non_workdays` / `weekends`）、
  日内时段 `time_on` / `time_off`（半开，支持 `24:00` 日末和跨午夜时段）。
- `tiers`：阶梯序号、**累计用量上限**（最后一档必须为空，表示开放）与单价。
- `holidays`：版本绑定的日历，`kind=holiday` 为法定假日，`kind=workday`
  为调休上班日（如中国周末调休）。
- `version_imports`：`POST /versions/import` 的导入登记（版本号、内容摘要、
  首次响应体），用于幂等重放与同 code 冲突检测。

规则在 `app/database.py` 的 `SEED_VERSIONS` 中声明，改完后清空数据库卷重启即可重新播种：

```bash
docker compose down -v && docker compose up --build
```

## 接口

### `POST /quote`

请求：

```json
{
  "version": "us-ny-demo-v2024",
  "intervals": [
    {
      "start": "2024-03-09T23:30:00-05:00",
      "end": "2024-03-10T05:30:00-04:00",
      "timezone": "America/New_York",
      "energy": 10
    }
  ]
}
```

- `intervals` 支持多段，按时间排序后统一计价；段与段不得重叠。
- 建议时间携带明确 UTC 偏移量（`-05:00` / `Z`）。若给**朴素时间**，
  则按 `timezone` 解释；落在夏令时**春季缺口**或**秋季歧义**的朴素时间会被拒绝（400），
  要求携带偏移量消歧。
- 累计阶梯以**整个报价的累计用量**为准（跨规则共享），跨档时按时间比例切出
  半开子段，舍入残差并入末段，保证分段能量之和严格等于输入能量。

响应（节选）：

```json
{
  "version": "us-ny-demo-v2024",
  "currency": "USD",
  "energy_unit": "kWh",
  "total_energy": "10.000000",
  "total_amount": "1.50",
  "matched_rules": ["leisure"],
  "segments": [
    {
      "interval_index": 0,
      "start": "2024-03-10T04:30:00Z",
      "end": "2024-03-10T05:00:00Z",
      "start_local": "2024-03-09T23:30:00-05:00",
      "end_local": "2024-03-10T00:00:00-05:00",
      "timezone": "America/New_York",
      "duration_seconds": 1800,
      "day_kind": "weekend",
      "energy": "1.000000",
      "rule_code": "leisure",
      "rule_name": "周末与节假日（全天）",
      "tier_ordinal": 1,
      "cumulative_from": "0.000000",
      "cumulative_to": "1.000000",
      "unit_price": "0.15",
      "amount": "0.150000"
    }
  ]
}
```

逐段字段含义：UTC 与本地两种表示的半开窗口、UTC 秒级时长、当日类型
（`weekday`/`weekend`/`holiday`/`workday`）、分摊能量、命中规则、所在阶梯、
该段在累计阶梯中的用量区间、单价与金额。`total_amount` 四舍五入到分。

### 夏令时示例

2024 年纽约春令时在 **3 月 10 日 02:00 → 03:00** 跳时。上面的请求从
3/9 23:30 到 3/10 05:30（本地），钟表走了 6 小时，但实际只覆盖 **5 个 UTC 小时**，
切分点为本地午夜 `00:00` 与跳时刻（不存在的 `02:00`）。

秋令时回拨（11 月 3 日 02:00 → 01:00，重复一小时）需带偏移量，例如
`2024-11-03T00:00:00-04:00` 到 `2024-11-03T08:00:00-05:00` 覆盖 9 个 UTC 小时。

### 跨规则 + 跨阶梯示例

美国独立日（2024-07-04）前夜到当天凌晨：

```bash
curl -s -X POST localhost:8000/quote -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "intervals": [{
    "start": "2024-07-03T22:00:00-04:00",
    "end":   "2024-07-04T06:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 150
  }]
}'
```

结果在本地午夜切为工作日段（`dst-workday`，2h，37.5 kWh @0.22）与
节假日段（`leisure`，6h，112.5 kWh）；节假日段又在累计用量 100 kWh 处
切成两档（62.5 @0.15、50 @0.18），总价 **26.63 USD**。

### `POST /quote-diff`

对**同一组用量区间**分别用两个版本的计价引擎独立报价，再逐段对比。
两版**币种或能量单位不同**直接拒绝（422 `incompatible_versions`）。

切分方式：取两版计费段的 **UTC 端点并集**切成原子段；每个原段的能量与
六位小数金额按**实际时长**比例摊到它的原子子段，舍入余量归入该原段的
**最后一个子段**——因此每版原子段的逐段之和与汇总都和 `POST /quote`
的独立报价**严格一致**。未发生变化的原子段也会保留（`reasons` 为空）。

请求（`intervals` 与 `/quote` 完全同格式；区间解析、结构校验与报错语义
也和 `/quote` 一致）：

```bash
curl -s -X POST localhost:8000/quote-diff -H 'Content-Type: application/json' -d '{
  "before_version": "us-ny-demo-v2024",
  "after_version":  "us-ny-demo-v2025",
  "intervals": [{
    "start": "2024-07-03T22:00:00-04:00",
    "end":   "2024-07-04T06:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 150
  }]
}'
```

响应（节选）：

```json
{
  "before_version": "us-ny-demo-v2024",
  "after_version": "us-ny-demo-v2025",
  "currency": "USD",
  "energy_unit": "kWh",
  "total_energy": "150.000000",
  "before_total_amount": "26.63",
  "after_total_amount": "28.13",
  "total_amount_diff": "1.50",
  "before_matched_rules": ["dst-workday", "leisure"],
  "after_matched_rules": ["dst-workday", "leisure"],
  "segments": [
    {
      "interval_index": 0,
      "start": "2024-07-04T02:00:00Z",
      "end": "2024-07-04T04:00:00Z",
      "duration_seconds": 7200,
      "before": {"rule_code": "dst-workday", "rule_name": "夏令时 工作日",
                 "tier_ordinal": 1, "unit_price": "0.22",
                 "energy": "37.500000", "amount": "8.250000"},
      "after":  {"rule_code": "dst-workday", "rule_name": "夏令时 工作日",
                 "tier_ordinal": 1, "unit_price": "0.23",
                 "energy": "37.500000", "amount": "8.625000"},
      "energy_diff": "0.000000",
      "amount_diff": "0.375000",
      "reasons": ["unit_price_changed"]
    }
  ]
}
```

每个原子段给出两版的规则、阶梯、单价、能量与金额，以及差额
（`amount_diff` / `energy_diff` = after − before）。`reasons` 列出
**同时成立**的变化维度：

| reason | 含义 |
| --- | --- |
| `rule_changed` | 两版命中的规则代码不同 |
| `tier_changed` | 两版所在阶梯序号不同 |
| `unit_price_changed` | 两版单价不同 |

两版节假日日历不同也会导致 `rule_changed`。例如 2024-10-14（哥伦布日）
在 v2024 中是节假日、在 v2025 中是普通工作日：

```bash
curl -s -X POST localhost:8000/quote-diff -H 'Content-Type: application/json' -d '{
  "before_version": "us-ny-demo-v2024",
  "after_version":  "us-ny-demo-v2025",
  "intervals": [{
    "start": "2024-10-14T09:00:00-04:00",
    "end":   "2024-10-14T17:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 80
  }]
}'
```

该窗口内每个原子段都是 `leisure`（@0.15）对 `dst-workday`（@0.23），
`reasons` 为 `["rule_changed", "unit_price_changed"]`，总价 12.00 →
18.40，差额 **6.40 USD**。

### `POST /shift-plan`

为**两项可移时用电任务**在各自可用窗口内寻找**总费用最低**的开机时刻组合。

请求给出规则版本、IANA 时区，以及恰好两项任务：

- `earliest_start` / `latest_end`：最早开机与最晚完工的 ISO-8601 时刻，
  **必须携带 UTC 偏移量**（如 `-04:00` / `Z`），任务须在 `latest_end`
  **之前**结束（半开区间）；两者跨度（可用窗口）**最多 8 小时**；
- `duration_minutes`：整数分钟的连续运行时长（必须能在窗口内放下）；
- `energy`：正用量（如 kWh）。

枚举与选优规则：

- 以**每项任务最早开机的 UTC 时刻为起点**，每隔 **30 分钟**枚举一个候选开机时刻
  （每项最多 17 个候选、组合数 ≤ 289）；
- 每组组合要求任务在最晚完工前结束、两任务**互不重叠**（半开区间，首尾相接允许）；
- 候选区间按开机时间排序后在**同一次** `/quote` 计价中统一报价，
  **累计阶梯跨两项任务共享**，绝不是分别报价再相加；
- 某组候选遇到**规则缺口**（`rule_gap`，某些时刻没有规则覆盖）只淘汰该组；
  版本存在**结构错误**（规则重叠、阶梯缺口）则直接返回 **409 `rule_conflict`**；
- 以**未舍入的六位小数金额**选最低费用；同价时取**输入顺序下 UTC 开机时间
  字典序最早**的组合（先比任务 0，再比任务 1）。

请求示例（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/shift-plan -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "timezone": "America/New_York",
  "tasks": [
    {
      "earliest_start": "2024-07-05T20:00:00-04:00",
      "latest_end":     "2024-07-06T04:00:00-04:00",
      "duration_minutes": 120,
      "energy": 5
    },
    {
      "earliest_start": "2024-07-05T20:00:00-04:00",
      "latest_end":     "2024-07-06T04:00:00-04:00",
      "duration_minutes": 120,
      "energy": 7
    }
  ]
}'
```

上面两项任务的窗口横跨周五（`dst-workday` @0.22）与周六（`leisure` @0.15），
引擎会把两任务都排进周六凌晨。响应（`segments` 有省略，实际字段与 `/quote` 一致）：

```json
{
  "version": "us-ny-demo-v2024",
  "currency": "USD",
  "energy_unit": "kWh",
  "timezone": "America/New_York",
  "feasible": true,
  "total_energy": "12",
  "total_amount": "1.80",
  "tasks": [
    {"task_index": 0,
     "start": "2024-07-06T04:00:00Z", "end": "2024-07-06T06:00:00Z",
     "start_local": "2024-07-06T00:00:00-04:00",
     "end_local":   "2024-07-06T02:00:00-04:00",
     "timezone": "America/New_York",
     "duration_minutes": 120, "energy": "5.000000"},
    {"task_index": 1,
     "start": "2024-07-06T06:00:00Z", "end": "2024-07-06T08:00:00Z",
     "start_local": "2024-07-06T02:00:00-04:00",
     "end_local":   "2024-07-06T04:00:00-04:00",
     "timezone": "America/New_York",
     "duration_minutes": 120, "energy": "7.000000"}
  ],
  "segments": [
    {"task_index": 0, "start": "2024-07-06T04:00:00Z",
     "end": "2024-07-06T06:00:00Z", "rule_code": "leisure",
     "tier_ordinal": 1, "energy": "5.000000", "amount": "0.750000",
     "...": "其余字段与 /quote 的逐段输出相同（含本地时间、阶梯区间等）"},
    {"task_index": 1, "start": "2024-07-06T06:00:00Z",
     "end": "2024-07-06T08:00:00Z", "rule_code": "leisure",
     "tier_ordinal": 1, "energy": "7.000000", "amount": "1.050000",
     "...": "..."}
  ],
  "candidates_evaluated": 169,
  "non_overlapping_pairs": 90,
  "rule_gap_pairs": 0,
  "reason_code": null,
  "reason": null,
  "gaps": null
}
```

- `tasks`：各任务最终的 UTC / 本地起止时间与时长、用量；
- `segments`：与 `/quote` 相同的计价段，用 **`task_index`（0/1，即输入顺序）**
  标明每段归属哪项任务；`total_amount` 为四舍五入到分的合计；
- `candidates_evaluated` / `non_overlapping_pairs` / `rule_gap_pairs`：
  枚举总数、互不重叠的组合数、被规则缺口淘汰的组合数。

**无可行组合**时 HTTP 仍为 200，但 `feasible=false`，并给出机器可读原因
（`reason_code` + 中文说明 `reason`，规则缺口场景还会在 `gaps` 里给出
最多 5 组被淘汰候选及缺口时刻）：

| reason_code | 含义 |
| --- | --- |
| `no_candidate_start` | 某任务在窗口内没有任何满足 30 分钟网格、且能按时完工的开机时刻 |
| `no_non_overlapping_pair` | 两任务的所有候选组合都互相重叠 |
| `all_pairs_rule_gap` | 所有互不重叠的组合都在计价区间内遇到规则缺口 |

### `POST /meter-readings/reconcile`

把**累计表计读数**整理成可直接提交给 `/quote` 的 UTC 半开用量区间
（不触碰数据库，纯计算）。计数器按 `modulus` 回卷；跨 reset 或采样间隔
超限的范围只记为 gap，**不估算用量**。

请求：

```json
{
  "modulus": "10000",
  "max_interval_seconds": 3600,
  "resets": ["2024-07-06T08:00:00Z"],
  "readings": [
    {"at": "2024-07-06T00:00:00Z", "value": "9950"},
    {"at": "2024-07-06T01:00:00Z", "value": "20"},
    {"at": "2024-07-06T02:00:00Z", "value": "55"},
    {"at": "2024-07-06T05:00:00Z", "value": "80"},
    {"at": "2024-07-06T08:00:00Z", "value": "5"},
    {"at": "2024-07-06T09:00:00Z", "value": "45"}
  ]
}
```

- `readings`：**2～500 条**累计读数，`at` 必须携带 UTC 偏移量
  （`Z` / `±HH:MM`），`value` 必须落在 **`[0, modulus)`** 内；顺序任意。
- `modulus`：正数计数器模数（读数达到后回卷为 0）。
- `max_interval_seconds`：正整数秒的最大采样间隔。
- `resets`：可选的 reset 时刻（同样必须带偏移量），**每个都必须命中
  某条读数**，否则 400 `reset_not_on_reading`。

处理规则：

1. 全部读数先按 **UTC 时刻**排序；同一时刻**同值去重**（保留输入下标
   最靠前的一条，计入 `duplicate_count`）、**异值报错**
   （400 `conflicting_readings`）。
2. 命中 **reset** 的读数是一段新序列的起点：通向它的相邻读数对只记为
   gap（`reason="reset"`）。
3. 相邻读数间隔**超过** `max_interval_seconds` 的对同样只记为 gap
   （`reason="max_interval_exceeded"`）；恰好等于不算超限。
4. 其余相邻读数对生成半开区间 `[start, end)`：差值为正直接取用；
   **出现下降按一次 modulus 回卷**计算（`delta = cur − prev + modulus`）
   并置 `wrapped=true`；差值为 0 的静止对不产生区间（`/quote` 不接受
   0 能量），也不算 gap。

响应（上面请求的节选）：

```json
{
  "modulus": "10000",
  "reading_count": 6,
  "duplicate_count": 0,
  "total_energy": "145",
  "intervals": [
    {"start": "2024-07-06T00:00:00Z", "end": "2024-07-06T01:00:00Z",
     "duration_seconds": 3600, "from_index": 0, "to_index": 1,
     "wrapped": true, "energy": "70"},
    {"start": "2024-07-06T01:00:00Z", "end": "2024-07-06T02:00:00Z",
     "duration_seconds": 3600, "from_index": 1, "to_index": 2,
     "wrapped": false, "energy": "35"},
    {"start": "2024-07-06T08:00:00Z", "end": "2024-07-06T09:00:00Z",
     "duration_seconds": 3600, "from_index": 4, "to_index": 5,
     "wrapped": false, "energy": "40"}
  ],
  "gaps": [
    {"start": "2024-07-06T02:00:00Z", "end": "2024-07-06T05:00:00Z",
     "duration_seconds": 10800, "from_index": 2, "to_index": 3,
     "reason": "max_interval_exceeded"},
    {"start": "2024-07-06T05:00:00Z", "end": "2024-07-06T08:00:00Z",
     "duration_seconds": 10800, "from_index": 3, "to_index": 4,
     "reason": "reset"}
  ]
}
```

- `from_index` / `to_index` 是区间端点读数在**请求 `readings` 中的原始
  下标**（0 起，排序/去重不改变下标含义）。
- 用量为**精确值（未舍入）**。**对账不变量**：任一连续段（不被
  reset / 采样间隔打断）内区间用量之和 == 该段末读数 − 首读数 +
  modulus × 回卷次数；因此 `total_energy` 严格等于各区间用量之和
  （上例：回卷段 (20−9950+10000)+35 = 105，reset 后 45−5 = 40，
  合计 145）。

`intervals` 的 `start` / `end` / `energy` 与 `/quote` 的区间格式一致
（时间已带 `Z` 偏移，`timezone` 字段可任填，如 `UTC`），可以管道直
接接力报价：

```bash
curl -s -X POST localhost:8000/meter-readings/reconcile \
  -H 'Content-Type: application/json' -d @readings.json \
  | jq '{version: "us-ny-demo-v2024",
         intervals: [.intervals[] | {start: .start, end: .end,
                                     timezone: "UTC", energy: .energy}]}' \
  | curl -s -X POST localhost:8000/quote \
      -H 'Content-Type: application/json' -d @-
```

上面的演示负载经 `/quote` 报价得 `total_energy=145`、
`total_amount=30.55`（周五 `dst-workday` 与周六 `leisure` 分段计价，
累计阶梯跨区间共享）。

### `POST /versions/import`

导入一个**完整费率版本**（版本 + 嵌套的 `holidays`、`rules`、`tiers`），
在**单个 SQLite 事务**内写入全部层级——任一校验失败整体回滚，
数据库不会留下半成品。导入成功后版本**立即**可用于
`/quote`、`/quote-diff` 与 `/shift-plan`。

请求示例（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/versions/import -H 'Content-Type: application/json' -d '{
  "code": "acme-2024-v1",
  "currency": "USD",
  "energy_unit": "kWh",
  "description": "ACME 2024 费率",
  "holidays": [
    {"day": "2024-07-04", "kind": "holiday", "name": "Independence Day"}
  ],
  "rules": [
    {"code": "night", "name": "夜间",
     "effective_from": "2024-01-01T00:00:00", "effective_to": "2025-01-01T00:00:00",
     "timezone": "America/New_York", "weekdays": "", "day_scope": "all",
     "time_on": "20:00", "time_off": "08:00",
     "tiers": [{"ordinal": 1, "cumulative_cap": null, "unit_price": "0.10"}]},
    {"code": "day", "name": "白天",
     "effective_from": "2024-01-01T00:00:00", "effective_to": "2025-01-01T00:00:00",
     "timezone": "America/New_York", "weekdays": "1,2,3,4,5", "day_scope": "workdays",
     "time_on": "08:00", "time_off": "20:00",
     "tiers": [{"ordinal": 1, "cumulative_cap": "100", "unit_price": "0.20"},
               {"ordinal": 2, "cumulative_cap": null, "unit_price": "0.30"}]}
  ]
}'
```

**规范化与内容摘要**：导入前先按业务键规范化——规则按 `code` 排序、
阶梯按 `ordinal` 排序、日历按日期排序（同日期同类型的重复条目去重）；
数值（`0.20` / `0.2` / `1e2`）、星期掩码（`"5,1,2"` / `[1,2,5]`）、
`HH:MM`（`8:00` / `08:00`）等写法统一为规范形式。对规范 JSON
（键排序、无空白）取 **SHA-256** 作为 `content_digest`，
因此**列表顺序与等价写法的变化都不会改变摘要**。

**幂等语义**（版本内容不可变）：

| 情形 | 结果 |
| --- | --- |
| 首次导入 | **201**，写入全部层级，返回版本内容与 `content_digest` |
| 同 `code`、同摘要重放 | **200**，原样返回首次的结果，不写库 |
| 同 `code`、不同摘要 | **409** `version_conflict`，`details` 含两个摘要 |

**校验**：版本号与规则代码唯一、IANA 时区有效且**全版本一致**、
生效区间（`effective_to > effective_from`，本地朴素时间）与日内窗口
（`HH:MM`，仅 `time_off` 允许 `24:00`）合法、holiday 日期与类型无冲突
（同一日期不得既是 `holiday` 又是 `workday`）、阶梯序号从 1 连续且
累计上限严格递增（中间档不得开放、最后一档必须开放），并复用与
`/quote` 相同的**规则重叠检查**。字段错误一次性全部返回
（**422 `invalid_import`**），每条都带 JSON 定位路径；结构冲突返回
**409 `rule_conflict`**，`details` 为带规则定位的 issues 列表：

```json
{
  "error": {
    "code": "invalid_import",
    "message": "导入内容存在 2 处字段错误，未写入任何数据",
    "details": {
      "errors": [
        {"path": "holidays[1].kind", "code": "holiday_conflict",
         "message": "日期 2024-05-01 的类型冲突：holidays[0] 为 'holiday'，此处为 'workday'"},
        {"path": "rules[0].tiers[1].ordinal", "code": "tier_gap",
         "message": "规则 day 的阶梯序号必须从 1 开始连续（第 2 档的序号为 3）"}
      ]
    }
  }
}
```

### 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/versions` | 列出全部版本及其规则/阶梯 |
| GET | `/versions/{code}` | 查看单个版本 |
| GET | `/versions/{code}/issues` | **显式校验**：返回规则时间重叠与阶梯缺口 |
| POST | `/versions/import` | 导入完整版本（单事务落库、SHA-256 内容摘要、幂等重放，见上） |
| POST | `/quote-diff` | 两版本对同一组用量区间独立报价并逐段对比（见上） |
| POST | `/shift-plan` | 两项可移时任务在 8 小时窗口内枚举 30 分钟候选，统一报价选最低费用组合（见上） |
| POST | `/meter-readings/reconcile` | 累计表计读数对账：排序去重、reset 断列、回卷与采样间隔 gap，产出可直提 `/quote` 的用量区间（见上） |

## 错误处理

统一响应体：`{"error": {"code": "...", "message": "...", "details": ...}}`。

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 400 | `nonexistent_local_time` | 朴素时间落在 DST 春季跳时缺口 |
| 400 | `ambiguous_local_time` | 朴素时间在 DST 回拨时含义不明确 |
| 400 | `invalid_timezone` / `invalid_datetime` / `invalid_interval` | 时区、时间格式或区间非法（end ≤ start、能量 ≤ 0） |
| 400 | `offset_required` | `/shift-plan` 的任务窗口或 `/meter-readings/reconcile` 的读数/reset 时刻未携带 UTC 偏移量 |
| 400 | `window_too_large` / `duration_exceeds_window` | `/shift-plan` 任务可用窗口超过 8 小时，或持续时长超出可用窗口 |
| 400 | `interval_overlap` | 输入的多个用量区间相互重叠 |
| 400 | `reading_out_of_range` | `/meter-readings/reconcile` 的读数值不在 `[0, modulus)` 内 |
| 400 | `conflicting_readings` | 同一时刻出现数值不一致的两条读数 |
| 400 | `reset_not_on_reading` | `resets` 中的时刻没有命中任何读数 |
| 404 | `version_not_found` | 规则版本不存在 |
| 409 | `rule_overlap` | 同一时刻有两条规则同时命中（考虑星期/节假日/日内时段后的**条件重叠**） |
| 409 | `tier_gap` | 阶梯上限不递增、中间档开放、最后一档封闭、无阶梯等 |
| 409 | `rule_gap` | 报价区间内存在任何规则都不覆盖的时刻 |
| 409 | `version_conflict` | `/versions/import` 的同 code 不同内容摘要，版本不可变，拒绝覆盖 |
| 422 | `invalid_import` | `/versions/import` 字段校验失败；`details.errors` 逐条给出 JSON 定位路径 |
| 422 | `invalid_rule` | 版本内规则字段（时区/时间/档位数值）无法解析 |
| 422 | `incompatible_versions` | `/quote-diff` 的两版币种或能量单位不同，拒绝比较 |

`POST /quote` 在计价前会强制对版本做结构校验；发现重叠或缺口直接返回
**409 并带完整 details**（可一次报出多处问题）。也可用
`GET /versions/{code}/issues` 单独查看。例如两条时间重叠的规则：

```json
{
  "version": "broken-demo",
  "ok": false,
  "issues": [
    {"type": "tier_gap", "rule": "b", "message": "规则 b 的最后一档必须开放……"},
    {
      "type": "rule_overlap",
      "rules": ["a", "b"],
      "at": "2024-06-01T00:00:00+00:00",
      "interval_a": ["2024-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"],
      "interval_b": ["2024-06-01T00:00:00+00:00", "2024-07-01T00:00:00+00:00"],
      "message": "规则 a 与 b 在 2024-06-01T00:00:00+00:00 起时间段重叠……"
    }
  ]
}
```

重叠检测是“条件感知”的：工作日规则与周末规则即便日期范围相同也**不**算重叠；
而 `holidays` 与覆盖节假日的 `non_workdays` 在同一节假日相遇时会被判为重叠。

## 切分算法要点

对全部输入区间取并集后，收集这些切分时刻（全部换算成 UTC）：

1. 每个相关时区（输入时区 + 各规则时区）的**本地午夜**；
2. 各时区的 **DST/UTC 偏移切换瞬间**（按日内偏移变化二分定位到秒）；
3. 规则的生效起止时刻、每日 `time_on`/`time_off`（含跨午夜）；
4. 阶梯按**累计用量**跨档时，再在段内按秒数比例二次切分。

随后逐段唯一定位命中规则（无规则即 `rule_gap`，多规则即 `rule_overlap`），
能量按 UTC 秒数比例分摊（DST 日自然得到 23/25 小时），残差并入每段末片。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
python -m pytest -q
```

测试使用内存 SQLite（`StaticPool`），共 82 个用例，覆盖跨日、春秋 DST、
节假日/调休、跨档切分、重叠与缺口报错、版本对比（原子段对账、
rule/tier/单价变化原因、币种拒绝），以及 `/shift-plan` 的候选枚举、
跨任务共享累计阶梯、同价字典序决胜、规则缺口只淘汰对应候选、
结构错误直接报错与全部输入校验路径，`/versions/import` 的
规范化与摘要稳定性（列表乱序/等价写法）、幂等重放与同 code 冲突、
字段错误定位、单事务原子性与导入后立即可用性，以及
`/meter-readings/reconcile` 的 UTC 排序与原始下标、同时刻去重/冲突、
一次回卷、reset 断列、采样间隔 gap、静止读数、对账不变量与
接力 `/quote` 的端到端报价。

## 目录结构

```
app/
  main.py        # FastAPI 路由与错误处理
  schemas.py    # Pydantic 请求/响应模型
  pricing.py    # 核心引擎：时区/DST/日历/切分/阶梯/校验
  diff.py        # 版本对比：原子段切分、差额与变化原因
  shiftplan.py  # 移时排程：30 分钟枚举 + 统一报价选优
  meter.py      # 表计对账：排序去重、reset 断列、回卷与采样间隔 gap
  importer.py   # 版本导入：规范化、SHA-256 摘要、定位校验、单事务落库
  database.py   # SQLAlchemy 模型与预置版本数据
tests/          # pytest 端到端测试
Dockerfile
docker-compose.yml
requirements.txt
```
