# 固化副本修复规划契约（`plan_preservation`）

`plan_preservation(data, assets, sites, replicas, request)` 在现有
[`process`](contract.md) 规范引用图之上，为一组重点文献及其沿引用方向可达的
全部被引证据，共同求解满足**故障域分散**要求的数字载体修复计划：给定每个
存储站点的写入窗口容量（`write_slots`）与单位写入成本（`write_cost`），在
有限写入窗口内生成可复核、可执行且字典序确定的复制动作序列。

```python
from citation_lineage import PreservationError, plan_preservation

try:
    plan = plan_preservation(data, assets, sites, replicas, request)
except PreservationError:  # ValueError 子类，整次调用失败
    ...
if plan["status"] == "planned":
    actions = plan["actions"]            # 完整修复动作
elif plan["status"] == "blocked":
    violations = plan["violations"]      # 结构化阻断证据，无任何部分动作
```

本功能只使用 Python 标准库，不访问网络、数据库或持久化服务，也不会修改调用方
传入的 `data` / `assets` / `sites` / `replicas` / `request` 任何对象。任一输入
非法时抛出 `PreservationError`（`ValueError` 子类），整次调用失败，不返回部分
结果。

## 输入

### `data`

必须完全符合 [`process`](contract.md) 的输入契约（1..1000 篇文献、0..5000 条
引用定位）。函数内部调用 `process(data)` 完成校验并取得规范引用图与
`cited_ancestors`。登记摘要只来自 `assets`，**不得**由 `date` / `title` 推断。

### `assets`

列表必须**恰好覆盖 `data` 中全部文献**，每项必须且只能包含：

| 字段 | 约束 |
| --- | --- |
| `id` | 文献 ID，必须存在于 `data` |
| `sha256` | 64 个小写十六进制字符的登记摘要 |
| `size_bytes` | `1..100_000_000` 的整数（布尔值不合法） |

缺失、重复、未知文献、非法摘要或越界大小都使整次调用抛 `ValueError`。

### `sites`

1..100 个唯一站点，每项必须且只能包含：

| 字段 | 约束 |
| --- | --- |
| `site_id` | 唯一 ID |
| `failure_domain` | 非空有界文本，故障域标识；同域可含多个站点 |
| `status` | `online` 或 `offline` |
| `write_slots` | `0..10_000` 整数，本窗口可原子写入的完整副本数 |
| `write_cost` | `0..1_000_000` 整数，一次写入的相对成本 |

离线站点既不能作为读取源，也不能作为写入目标，其 `write_slots` 不可用。

### `replicas`

0..10_000 条副本观测记录，每项必须且只能包含：

| 字段 | 约束 |
| --- | --- |
| `replica_id` | 唯一 ID |
| `document_id` | 必须存在于 `data` |
| `site_id` | 必须存在于 `sites` |
| `observed_sha256` | 64 位小写十六进制摘要，或 `null`（本次无法读取） |
| `checked_at` | 非空有界文本，本次校验时间，不解释其时间语义 |

`replica_id` 必须唯一；同一 `(document_id, site_id)` 至多一条记录。重复身份、
未知文献/站点、非法摘要都使整次调用抛 `ValueError`。

### `request`

必须且只能包含：

| 字段 | 约束 |
| --- | --- |
| `preservation_id` | 唯一、非空有界文本 |
| `seed_ids` | 1..100 个唯一且存在的文献 ID |
| `required_failure_domains` | `1..10` 整数 |
| `reason` | 非空有界文本 |

## 副本状态

副本状态**只由本次输入确定**，不进行多数表决，也不据此修改登记摘要：

| 状态 | 判定 |
| --- | --- |
| `healthy` | 站点 `online` 且 `observed_sha256` 等于该文献登记摘要 |
| `corrupt` | 站点 `online` 但观测摘要与登记摘要不同 |
| `unavailable` | 站点 `offline`，或 `observed_sha256` 为 `null` |

一篇文献只要存在至少一条 `healthy` 副本就可作为修复**读取源**；完全没有
healthy 副本的文献为 `unrecoverable`，即使多条 corrupt 副本摘要彼此相同也不能
“恢复”。corrupt / unavailable 副本**不能作为复制源**，但可作为 `replace` 目标。

## 保护闭包

保护集合为每个 seed 与其沿引用方向（`from → to`）可达的全部被引文献之并：

```
closure = {seed_ids} ∪ ⋃ cited_ancestors[seed]
```

实现直接复用 `process` 的 `cited_ancestors`，不为每个 seed 重扫整张图。闭包外
文献不参与修复分配，但其当前副本审计摘要仍在结果中完整保留。

## 达标与修复动作

一篇保护文献达标，当且仅当规划后它在至少 `required_failure_domains` 个**不同
故障域**各持有一个 healthy 副本；同一故障域内的多个 healthy 站点只贡献一个域。

- 已有的 healthy 域直接计入，**不消耗 slot**。
- 每个仍缺的故障域**恰好**规划一个动作，不为已达标文献或同一缺口生成冗余
  副本。
- 动作把文献从一个**现有 healthy 源**复制到一个 `online`、当前没有该文献
  healthy 副本、且属于未覆盖故障域的目标站点。
- 目标站点已存在该文献的 corrupt/unavailable 记录时动作为 `replace` 并关联
  原 `replica_id`；否则为 `create`（`replaced_replica_id` 为 `null`）。
- 每个动作的 `source_site_id` 固定为该文献**现有** healthy 源中 `site_id`
  字典序最小者；规划产生的新副本不能作为同次规划中其他动作的读取源。
- 一个站点规划后不能持有同一文献的多个 healthy 副本。

## 全局共同求解与阻断

规划对整个保护闭包**共同求解**，站点 `write_slots` 是所有文献动作共享的全局
容量。下列任一情形返回 `status="blocked"` 且 `actions` 为空，绝不返回看似可
执行的部分修复：

1. `no_healthy_source`：闭包内某文献完全没有 healthy 副本；
2. `insufficient_failure_domains`：某文献可达到的不同故障域数（含现有 healthy
   域）不足以满足 `required_failure_domains`；
3. `slot_capacity_infeasible`：单篇均可行，但多文献争用站点 slot，不存在能同时
   满足所有文献缺口与站点 slot 上限的分配。

每条 violation 都携带相关文献、当前 healthy 站点/故障域、仍缺域数、候选目标与
可复核的站点 slot 摘要。

## 优化目标与确定性

所有可行方案的动作数相同，等于各文献缺失故障域数之和。在此前提下：

1. **最小化总 `write_cost`**；
2. 总 write_cost 相同时，按动作序列字典序决胜，动作排序键为
   `(document_id, target_site_id, source_site_id, action,
   replaced_replica_id)`，其中空 `replaced_replica_id` 按空字符串比较。

实现用一个**向量费用最小费用流**（逐单位最短路增广 + Dijkstra + Johnson
势能）表达两级目标：第 0 分量是写入成本，其后每个保护文献一个有界字典序分量，
站点到汇点的容量即共享 slot。算法不枚举站点分配组合，也不枚举简单路径；费用
分量为有界整数，不产生随文献数指数膨胀的超大整数。逐文献贪心**不**能替代全局
求解——局部最低成本站点可能耗尽后续文献唯一可用站点的 slot。

## 输出

结果为标准 JSON 对象，可用 `json.dumps(..., allow_nan=False)` 序列化，且按身份
（而非输入顺序）稳定排序。至少包含：

| 字段 | 含义 |
| --- | --- |
| `status` | `planned` 或 `blocked` |
| `preservation_id` / `request` | 请求身份与规范化请求 |
| `assets` / `sites` / `replicas` | 按身份排序的规范化输入回显 |
| `process` | 完整 `process` 结果 |
| `closure` | 保护闭包文献、seed 及每个 seed 的贡献列表 |
| `replica_audit` | **全部文献**（含闭包外）的副本状态审计 |
| `planning` | 逐保护文献规划前/后的 healthy 站点、故障域、达标状态与缺口 |
| `actions` | 完整修复动作（blocked 时为空列表） |
| `action_count` / `total_write_cost` | 动作数与总成本 |
| `site_slot_usage` | 各站点 slot 使用/剩余/可用摘要 |
| `planned_replica_view` | 规划后副本视图；新动作 `checked_at=null`，不伪造校验时间 |
| `violations` | 结构化阻断证据（planned 时为空列表） |
| `summary` | 副本/文献/动作/slot 计数摘要 |

## 确定性与失败边界

- 打乱 `documents`、`citations`、`assets`、`sites`、`replicas` 与 `seed_ids`
  的无语义顺序，不改变按身份对齐后的任何结果。
- 非法覆盖、重复站点/副本身份、未知文献/站点、非法摘要/字段、数量越界均使整次
  调用抛 `ValueError`，不返回部分结果，也不修改任何输入。
- 规划后副本视图只描述“若执行本计划”的投影，绝不回写输入，也不为新副本伪造
  `checked_at`；真实校验时间须由执行后的新一轮观测提供。
